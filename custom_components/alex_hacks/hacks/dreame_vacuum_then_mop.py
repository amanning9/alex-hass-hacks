"""Hack: the alex_hacks.dreame_vacuum_then_mop action.

Vacuums the given rooms, waits until the robot has completely finished, then mops them, each
room with its own suction, wetness and passes. Uses only Dreame Vacuum's public actions and
entities, and logs each phase to the logbook. Rooms come from the Dreame rooms hack.
"""

from __future__ import annotations

import asyncio
import itertools
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
    "room with its own suction, wetness, passes and (optionally) vacuum and mop routes. Rooms "
    "are picked from the Dreame rooms hack. "
    "Refuses to start if the robot is on a different map from the rooms. Turns CleanGenius off "
    "and customized cleaning on while it runs (and, for vacuum routes, sets the robot's cleaning "
    "mode and route), then puts them all back as they were."
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
# Routes. Custom cleaning only takes a route for rooms being mopped; for vacuuming, the robot's
# own route setting is used, which only offers standard and quick when sweeping.
VACUUM_ROUTES = ("standard", "quick")
MOP_ROUTES = ("standard", "intensive", "deep", "quick")
# Per-room mop route: robots with a route setting take cleaning_route; older ones take
# custom_mopping_route, which has no quick.
CLEANING_ROUTE = {"standard": 1, "intensive": 2, "deep": 3, "quick": 4}
CUSTOM_MOPPING_ROUTE = {"standard": 0, "intensive": 1, "deep": 2}
START_TIMEOUT = timedelta(minutes=5)
# "Completely done" must hold this long: task status alone blips to completed between the
# phases of mopping after sweeping and on mid-run returns, but task type stays set.
DONE_HOLD = timedelta(minutes=2)
# Between vacuum runs (one per stretch of rooms sharing a route) the next starts as soon as
# the last has finished, while the robot heads home; sweeping has no in-between blips.
RUN_HOLD = timedelta(seconds=15)
# Charging status on the base (it says charging completed once the battery is full).
CHARGING = ("charging", "charging_completed")

ROOM_SCHEMA = vol.Schema(
    {
        vol.Required("room"): cv.entity_id,
        vol.Optional("suction", default="standard"): vol.In(["none", *SUCTION]),
        vol.Optional("wetness", default=16): vol.All(vol.Coerce(int), vol.Range(min=0, max=32)),
        vol.Optional("passes", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
        vol.Optional("vacuum_route"): vol.In(VACUUM_ROUTES),
        vol.Optional("mop_route"): vol.In(MOP_ROUTES),
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
        "cleaning_mode": f"select.{name}_cleaning_mode",
        "cleaning_route": f"select.{name}_cleaning_route",
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
    has_route_setting = hass.states.get(entities["cleaning_route"]) is not None
    if any(r.get("vacuum_route") for r in vacuum_rooms):
        if missing := [e for e in (entities["cleaning_mode"], entities["cleaning_route"]) if hass.states.get(e) is None]:
            raise ServiceValidationError(f"vacuum_route needs {', '.join(missing)}; {vacuum} may not have routes")
    if not has_route_setting and any(r.get("mop_route") == "quick" for r in mop_rooms):
        raise ServiceValidationError(f"{vacuum} has no quick route for mopping")

    # Everything the action changes is put back afterwards (also after an error or being
    # stopped): CleanGenius, customized cleaning, and the robot's cleaning mode and route.
    # With customized cleaning on, Dreame's global cleaning mode is unavailable, which breaks
    # automations that set it. `current` is what the action has set, so it needn't wait for
    # entity states to catch up.
    keys = ("cleangenius", "customized_cleaning", "cleaning_mode", "cleaning_route")
    before = {key: _state(hass, entities[key]) for key in keys}
    current = dict(before)
    try:
        if before["cleangenius"] not in (None, "off"):
            await _ensure(hass, call, entities, current, "cleangenius", "off")
        await _async_phases(hass, call, vacuum, entities, current, vacuum_rooms, mop_rooms, has_route_setting)
    finally:
        await _async_restore(hass, call, vacuum, entities, before, current)


async def _ensure(hass: HomeAssistant, call: ServiceCall, entities: dict, current: dict, key: str, value: str) -> None:
    """Set a switch (on/off) or select (option) the action manages, if it isn't already."""
    if current.get(key) == value:
        return
    entity_id = entities[key]
    if entity_id.startswith("switch."):
        await hass.services.async_call(
            "switch", f"turn_{value}", {ATTR_ENTITY_ID: entity_id}, blocking=True, context=call.context
        )
    else:
        await hass.services.async_call(
            "select", "select_option", {ATTR_ENTITY_ID: entity_id, "option": value}, blocking=True, context=call.context
        )
    current[key] = value


async def _async_restore(
    hass: HomeAssistant, call: ServiceCall, vacuum: str, entities: dict, before: dict, current: dict
) -> None:
    def changed(key: str) -> bool:
        return before[key] not in (None, STATE_UNAVAILABLE, STATE_UNKNOWN) and current.get(key) != before[key]

    steps: list[tuple[str, str | None]] = []
    if changed("cleaning_mode") or changed("cleaning_route"):
        # The robot's own mode and route are set with customized cleaning off; mode first, as
        # changing it can reset the route.
        steps.append(("customized_cleaning", "off"))
    steps += [(key, None) for key in ("cleaning_mode", "cleaning_route", "customized_cleaning", "cleangenius")]
    restored = []
    for key, value in steps:
        if value is None:  # back to how it was
            if not changed(key):
                continue
            value = before[key]
        try:
            await _ensure(hass, call, entities, current, key, value)
        except Exception:  # e.g. the robot is still mid-clean after being stopped
            _LOGGER.warning("%s: couldn't restore %s", vacuum, entities[key], exc_info=True)
            continue
        if before[key] == value and entities[key] not in restored:
            restored.append(entities[key])
    if restored:
        _log(hass, call, vacuum, "restored " + ", ".join(restored))


async def _async_phases(
    hass: HomeAssistant,
    call: ServiceCall,
    vacuum: str,
    entities: dict,
    current: dict,
    vacuum_rooms: list,
    mop_rooms: list,
    has_route_setting: bool,
) -> None:
    # Each run: (phase, rooms, vacuum route). A vacuum route needs the robot's own route
    # setting, which customized cleaning overrides, so then the vacuum phase runs with
    # customized cleaning off, one run per stretch of rooms sharing a route (keeping their
    # order); the clean command still carries each room's suction and passes.
    runs: list[tuple[str, list, str | None]] = []
    if any(r.get("vacuum_route") for r in vacuum_rooms):
        for route, group in itertools.groupby(vacuum_rooms, key=lambda r: r.get("vacuum_route") or "standard"):
            runs.append(("vacuum", list(group), route))
    elif vacuum_rooms:
        runs.append(("vacuum", vacuum_rooms, None))
    if mop_rooms:
        runs.append(("mop", mop_rooms, None))

    for index, (phase, rooms, route) in enumerate(runs):
        ids = [r["id"] for r in rooms]
        suction = [SUCTION.get(r["suction"], 1) for r in rooms]
        repeats = [r["passes"] for r in rooms]
        if route is None:
            await _ensure(hass, call, entities, current, "customized_cleaning", "on")
            settings = {
                ATTR_ENTITY_ID: vacuum,
                "segment_id": ids,
                "cleaning_mode": [MOPPING if phase == "mop" else SWEEPING] * len(rooms),
                "suction_level": suction,
                "repeats": repeats,
            }
            if phase == "mop":
                settings["wetness_level"] = [r["wetness"] for r in rooms]
                if any(r.get("mop_route") for r in rooms):  # rooms without one get standard
                    routes = [r.get("mop_route") or "standard" for r in rooms]
                    if has_route_setting:
                        settings["cleaning_route"] = [CLEANING_ROUTE[x] for x in routes]
                    else:
                        settings["custom_mopping_route"] = [CUSTOM_MOPPING_ROUTE[x] for x in routes]
            await hass.services.async_call(
                "dreame_vacuum", "vacuum_set_custom_cleaning", settings, blocking=True, context=call.context
            )
            clean = {ATTR_ENTITY_ID: vacuum, "segments": ids}
            started = f"{phase} phase started (rooms {', '.join(map(str, ids))})"
        else:
            await _ensure(hass, call, entities, current, "customized_cleaning", "off")
            await _ensure(hass, call, entities, current, "cleaning_mode", "sweeping")
            await _ensure(hass, call, entities, current, "cleaning_route", route)
            clean = {ATTR_ENTITY_ID: vacuum, "segments": ids, "suction_level": suction, "repeats": repeats}
            started = f"{phase} phase started (rooms {', '.join(map(str, ids))}, {route} route)"
        await hass.services.async_call(
            "dreame_vacuum", "vacuum_clean_segment", clean, blocking=True, context=call.context
        )
        _log(hass, call, vacuum, started)

        def is_running() -> bool:
            return not _is(hass, entities["task_type"], STATE_UNAVAILABLE)

        def is_finished() -> bool:
            return _is(hass, entities["task_type"], STATE_UNAVAILABLE) and _is(hass, entities["task_status"], "completed")

        def is_done() -> bool:
            return is_finished() and (
                _is(hass, vacuum, "docked")
                # Some robots say idle, not docked, while charging on the base.
                or (_is(hass, vacuum, "idle") and _state(hass, entities["charging"]) in CHARGING)
            )

        def is_handed_over() -> bool:
            return is_finished() and _state(hass, vacuum) in ("returning", "docked", "idle")

        # Another vacuum run next: go as soon as this one has finished, without waiting for
        # the robot to get home. Otherwise wait until it's completely done and back.
        next_is_run = index + 1 < len(runs) and runs[index + 1][2] is not None
        done, hold = (is_handed_over, RUN_HOLD) if next_is_run else (is_done, DONE_HOLD)
        watched = [vacuum, entities["task_status"], entities["task_type"], entities["charging"]]
        try:
            await _wait_for(hass, watched, is_running, timedelta(0), START_TIMEOUT)
        except TimeoutError as err:
            raise HomeAssistantError(f"{vacuum} didn't start the {phase} phase within {START_TIMEOUT}") from err
        try:
            await _wait_for(hass, watched, done, hold, call.data["max_duration"])
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
