"""Hack: the alex_hacks.dreame_vacuum_then_mop action.

Vacuums the given rooms, waits until the robot has completely finished, then mops them, each
room with its own suction, wetness and passes. Uses only Dreame Vacuum's public actions and
entities, and logs each phase to the logbook. Rooms come from the Dreame rooms hack.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from ..const import CONF_HACK, DOMAIN
from . import dreame_common as dreame

_LOGGER = logging.getLogger(__name__)

KEY = "dreame_vacuum_then_mop"
TITLE = "Dreame vacuum then mop"
WHAT = (
    "Adds the alex_hacks.dreame_vacuum_then_mop action: vacuum the rooms, wait until the robot "
    "has completely finished (not just a recharge, mop wash or auto-empty), then mop them, each "
    "room with its own suction, wetness and passes. Rooms are picked from the Dreame rooms hack. "
    "Refuses to start if the robot is on a different map from the rooms. Turns CleanGenius off "
    "and customized cleaning on while it runs, then puts both back as they were."
)
WHY = (
    "Per-room suction and wetness need customized cleaning, which can't be combined with the "
    "robot's own mopping after sweeping; and Dreame Vacuum has no single \"completely finished\" "
    "signal (task status briefly reads completed between phases and on mid-run returns), so "
    "running the two phases needs careful waiting."
)
REMOVE_WHEN = (
    "The robot (or Dreame Vacuum) supports mopping after sweeping with per-room settings, or "
    "exposes a reliable \"whole job finished\" signal so a plain script can do this."
)
PLATFORMS: list = []

async_config_schema = dreame.async_config_schema


def async_entry_title(hass, data) -> str:
    return dreame.entry_title(hass, data, TITLE)


def async_unique_id(data) -> str:
    return f"{KEY}_{data[dreame.CONF_DREAME_ENTRY_ID]}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the action (once, for all vacuums with this hack)."""
    if not hass.services.has_service(DOMAIN, SERVICE):

        async def handle(call: ServiceCall) -> None:
            await _async_run(hass, call)

        hass.services.async_register(DOMAIN, SERVICE, handle, schema=SCHEMA)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the action when the last entry of this hack goes."""
    others = [
        e for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.data.get(CONF_HACK) == KEY and e.state is ConfigEntryState.LOADED
    ]
    if not others:
        hass.services.async_remove(DOMAIN, SERVICE)

SERVICE = "dreame_vacuum_then_mop"
SUCTION = {"quiet": 0, "standard": 1, "strong": 2, "turbo": 3}
SWEEPING, MOPPING = 0, 1
START_TIMEOUT = timedelta(minutes=5)
# "Completely done" must hold this long: task status alone blips to completed between the
# phases of mopping after sweeping and on mid-run returns, but task type stays set.
DONE_HOLD = timedelta(minutes=2)
# Charging status on the base (it says charging completed once the battery is full).
CHARGING = ("charging", "charging_completed")

ROOM_SCHEMA = vol.Schema(
    {
        vol.Required("room"): cv.entity_id,
        vol.Optional("suction", default="standard"): vol.In(["none", *SUCTION]),
        vol.Optional("wetness", default=16): vol.All(vol.Coerce(int), vol.Range(min=0, max=32)),
        vol.Optional("passes", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
    }
)
SCHEMA = cv.make_entity_service_schema(
    {
        vol.Required("rooms"): vol.All(cv.ensure_list, [ROOM_SCHEMA], vol.Length(min=1)),
        vol.Optional("max_duration", default=timedelta(hours=3)): cv.positive_time_period,
    }
)


def _log(hass: HomeAssistant, call: ServiceCall, vacuum: str, message: str) -> None:
    _LOGGER.info("%s: %s", vacuum, message)
    if "logbook" in hass.config.components:
        from homeassistant.components.logbook import async_log_entry

        async_log_entry(hass, "Vacuum then mop", message, DOMAIN, vacuum, call.context)


async def _async_run(hass: HomeAssistant, call: ServiceCall) -> None:
    vacuums = [e for e in call.data.get(ATTR_ENTITY_ID, []) if e.startswith("vacuum.")]
    if len(vacuums) != 1:
        raise ServiceValidationError("Pick exactly one vacuum")
    vacuum = vacuums[0]
    name = vacuum.split(".", 1)[1]
    entities = {
        "cleangenius": f"select.{name}_cleangenius",
        "customized_cleaning": f"switch.{name}_customized_cleaning",
        "task_status": f"sensor.{name}_task_status",
        "task_type": f"sensor.{name}_task_type",
        "charging": f"sensor.{name}_charging_status",
    }
    required = [vacuum, entities["customized_cleaning"], entities["task_status"], entities["task_type"]]
    if missing := [e for e in required if hass.states.get(e) is None]:
        raise ServiceValidationError(f"Not found (expected from Dreame Vacuum): {', '.join(missing)}")
    if hass.states.get(entities["charging"]) is None:
        _log(hass, call, vacuum, f"no {entities['charging']}: only 'docked' will count as back on the base")

    plan, skipped = [], []
    for room in call.data["rooms"]:
        state = hass.states.get(room["room"])
        room_id = state.attributes.get("room_id") if state is not None else None
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) or room_id is None:
            skipped.append(room["room"])
            continue
        plan.append({**room, "id": int(room_id)})
    if skipped:
        _log(hass, call, vacuum, f"skipping rooms no longer on the map: {', '.join(skipped)}")
    if not plan:
        raise ServiceValidationError("None of the rooms are on the robot's map")
    # Room numbers only mean something on their own map: never start on a different one.
    elsewhere = [r for r in plan if hass.states.get(r["room"]).attributes.get("on_current_map") is False]
    if elsewhere:
        room_map = hass.states.get(elsewhere[0]["room"]).attributes.get("map_name") or "the rooms' map"
        robot_map = hass.states.get(vacuum).attributes.get("selected_map") or "another map"
        raise ServiceValidationError(f"{vacuum} is on map '{robot_map}', not '{room_map}'")

    vacuum_rooms = [r for r in plan if r["suction"] != "none"]
    mop_rooms = [r for r in plan if r["wetness"] > 0]
    if not vacuum_rooms and not mop_rooms:
        _log(hass, call, vacuum, "nothing to do: every room has suction none and wetness 0")
        return

    # Put CleanGenius and customized cleaning back as they were afterwards (also after an error
    # or being stopped): with customized cleaning on, Dreame's global cleaning mode is
    # unavailable, which breaks automations that set it.
    cleangenius = hass.states.get(entities["cleangenius"])
    before = {
        "cleangenius": cleangenius.state if cleangenius is not None else None,
        "customized_cleaning": hass.states.get(entities["customized_cleaning"]).state,
    }
    try:
        if cleangenius is not None and cleangenius.state != "off":
            await hass.services.async_call(
                "select", "select_option", {ATTR_ENTITY_ID: entities["cleangenius"], "option": "off"},
                blocking=True, context=call.context,
            )
        if before["customized_cleaning"] != "on":
            await hass.services.async_call(
                "switch", "turn_on", {ATTR_ENTITY_ID: entities["customized_cleaning"]},
                blocking=True, context=call.context,
            )
        await _async_phases(hass, call, vacuum, entities, vacuum_rooms, mop_rooms)
    finally:
        await _async_restore(hass, call, vacuum, entities, before)


async def _async_restore(hass: HomeAssistant, call: ServiceCall, vacuum: str, entities: dict, before: dict) -> None:
    restores = []
    if before["customized_cleaning"] == "off":
        restores.append(("switch", "turn_off", {ATTR_ENTITY_ID: entities["customized_cleaning"]}))
    if before["cleangenius"] not in (None, "off", STATE_UNAVAILABLE, STATE_UNKNOWN):
        restores.append(
            ("select", "select_option", {ATTR_ENTITY_ID: entities["cleangenius"], "option": before["cleangenius"]})
        )
    for domain, service, data in restores:
        try:
            await hass.services.async_call(domain, service, data, blocking=True, context=call.context)
        except Exception:  # e.g. the robot is still mid-clean after being stopped
            _LOGGER.warning("%s: couldn't restore %s", vacuum, data[ATTR_ENTITY_ID], exc_info=True)
    if restores:
        _log(hass, call, vacuum, "restored " + ", ".join(d[ATTR_ENTITY_ID] for _, _, d in restores))


async def _async_phases(
    hass: HomeAssistant, call: ServiceCall, vacuum: str, entities: dict, vacuum_rooms: list, mop_rooms: list
) -> None:
    for phase, rooms, mode in (("vacuum", vacuum_rooms, SWEEPING), ("mop", mop_rooms, MOPPING)):
        if not rooms:
            continue
        ids = [r["id"] for r in rooms]
        settings = {
            ATTR_ENTITY_ID: vacuum,
            "segment_id": ids,
            "cleaning_mode": [mode] * len(rooms),
            "suction_level": [SUCTION.get(r["suction"], 1) for r in rooms],
            "repeats": [r["passes"] for r in rooms],
        }
        if mode == MOPPING:
            settings["wetness_level"] = [r["wetness"] for r in rooms]
        await hass.services.async_call(
            "dreame_vacuum", "vacuum_set_custom_cleaning", settings, blocking=True, context=call.context
        )
        await hass.services.async_call(
            "dreame_vacuum", "vacuum_clean_segment", {ATTR_ENTITY_ID: vacuum, "segments": ids},
            blocking=True, context=call.context,
        )
        _log(hass, call, vacuum, f"{phase} phase started (rooms {', '.join(map(str, ids))})")

        def is_running() -> bool:
            return not _is(hass, entities["task_type"], STATE_UNAVAILABLE)

        def is_done() -> bool:
            return (
                _is(hass, entities["task_type"], STATE_UNAVAILABLE)
                and _is(hass, entities["task_status"], "completed")
                # Some robots say idle, not docked, while charging on the base.
                and (
                    _is(hass, vacuum, "docked")
                    or (_is(hass, vacuum, "idle") and _state(hass, entities["charging"]) in CHARGING)
                )
            )

        watched = [vacuum, entities["task_status"], entities["task_type"], entities["charging"]]
        try:
            await _wait_for(hass, watched, is_running, timedelta(0), START_TIMEOUT)
        except TimeoutError as err:
            raise HomeAssistantError(f"{vacuum} didn't start the {phase} phase within {START_TIMEOUT}") from err
        try:
            await _wait_for(hass, watched, is_done, DONE_HOLD, call.data["max_duration"])
        except TimeoutError as err:
            raise HomeAssistantError(
                f"{vacuum} didn't finish the {phase} phase within {call.data['max_duration']}"
            ) from err
        _log(hass, call, vacuum, f"{phase} phase finished")


def _state(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    return state.state if state is not None else None


def _is(hass: HomeAssistant, entity_id: str, value: str) -> bool:
    return _state(hass, entity_id) == value


async def _wait_for(
    hass: HomeAssistant, entity_ids: list[str], predicate: Callable[[], bool], hold: timedelta, timeout: timedelta
) -> None:
    """Wait until predicate() has been true for `hold` (checked on changes to entity_ids)."""
    done = asyncio.Event()
    cancel_hold: Callable[[], None] | None = None

    @callback
    def _held(_now) -> None:
        nonlocal cancel_hold
        cancel_hold = None
        if predicate():
            done.set()

    @callback
    def _check(_event=None) -> None:
        nonlocal cancel_hold
        if predicate():
            if hold <= timedelta(0):
                done.set()
            elif cancel_hold is None:
                cancel_hold = async_call_later(hass, hold, _held)
        elif cancel_hold is not None:
            cancel_hold()
            cancel_hold = None

    unsub = async_track_state_change_event(hass, entity_ids, _check)
    try:
        _check()
        async with asyncio.timeout(timeout.total_seconds()):
            await done.wait()
    finally:
        unsub()
        if cancel_hold is not None:
            cancel_hold()
