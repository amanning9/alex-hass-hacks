"""Hack: pausable, resumable mop drying for Dreame self-wash bases."""

from __future__ import annotations

import logging
import sys
import math
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from . import dreame_common as dreame

_LOGGER = logging.getLogger(__name__)

KEY = "dreame_mop_drying"
TITLE = "Dreame mop drying"
WHAT = (
    'Adds "Allow drying" (switch), "Mop drying time" (number, default 3 hours) and "Mop drying" '
    "(sensor: dry / drying / paused / waiting / needs water). A mop that hasn't been washed "
    "(e.g. carried back from a floor without the base) is washed first, once the base has water. Needs the robot's own auto-drying off (it "
    "raises a repair and leaves drying to the robot while it's on). The pads count as wet from the start of each mop (or mop wash) until "
    "they've dried for the drying time in total. While they're wet and drying is allowed, it "
    "starts drying whenever the robot is docked; turning the switch off stops it, and turning it "
    "back on carries on where it left off. It keeps checking the robot really has stopped, "
    "and raises a repair if it won't. Progress is saved, so restarts don't lose it."
)
WHY = (
    "Dreame drying can only be started or stopped: every start is a fresh run of the robot's "
    "drying time (whole hours), and nothing says whether the pads are still wet. So drying "
    "can't be paused (e.g. while it's noisy or electricity is dear) and resumed. Dreame's stop "
    "also quietly does nothing whenever it thinks drying couldn't be started (e.g. it thinks a "
    "task is still running)."
)
REMOVE_WHEN = (
    "Dreame (or the integration) offers pausing and resuming drying, or reports how dry the "
    "pads are. Remove the hack's entities from your automations, then turn the robot's own "
    "auto-drying back on if you'd turned it off."
)
PLATFORMS = [Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]

DEFAULT_MINUTES = 180
RETRY_AFTER = timedelta(minutes=2)  # after a start/stop, give the robot time to report it
TICK = timedelta(seconds=30)
SETTLE = timedelta(minutes=3)  # the base must be quiet this long before drying starts
WASH_WAIT = timedelta(minutes=5)  # after a mop, give the robot this long to wash it, then wash it ourselves
NO_WATER = ("NO_WATER_LEFT", "NO_WATER_LEFT_AFTER_CLEAN", "NO_WATER_FOR_CLEAN", "TANK_NOT_INSTALLED")
MOPPING_MODES = (1, 2, 3)  # mopping, sweeping and mopping, mopping after sweeping
STOP_ATTEMPTS = 3  # then raise a repair (it keeps trying)

async_config_schema = dreame.async_config_schema


def async_entry_title(hass, data) -> str:
    return dreame.entry_title(hass, data, TITLE)


def async_unique_id(data) -> str:
    return f"{KEY}_{data[dreame.CONF_DREAME_ENTRY_ID]}"


def _store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(hass, 1, f"{DOMAIN}.{KEY}.{entry.entry_id}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator = dreame.async_setup_coordinator(hass, entry)
    if not getattr(coordinator.device.capability, "self_wash_base", False):
        raise ConfigEntryError(f"{entry.title}: this robot has no self-wash base to dry the mop")
    controller = DryingController(hass, entry, coordinator, _store(hass, entry))
    await controller.async_load()
    entry.runtime_data = controller
    entry.async_on_unload(coordinator.async_add_listener(controller.async_update))
    entry.async_on_unload(async_track_time_interval(hass, controller.async_update, TICK))
    entry.async_on_unload(controller.async_save_now)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    return None


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await _store(hass, entry).async_remove()
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))
    ir.async_delete_issue(hass, DOMAIN, _auto_issue_id(entry))


def _auto_issue_id(entry: ConfigEntry) -> str:
    return f"auto_drying_on_{entry.entry_id}"


def _issue_id(entry: ConfigEntry) -> str:
    return f"drying_wont_stop_{entry.entry_id}"


def _base_idle(status) -> bool:
    """Nothing in any of the robot's three status fields says it's washing or heading to wash."""
    base = getattr(status, "self_wash_base_status", None)
    if base is not None and base.name not in ("IDLE", "UNKNOWN"):
        return False
    for field in ("state", "status"):
        value = getattr(status, field, None)
        name = getattr(value, "name", "")
        if "WASH" in name or "RETURNING" in name:
            return False
    return True


def _has_water(status) -> bool:
    """The base has water to wash the mop with (as far as its low-water warning says)."""
    warning = getattr(status, "low_water_warning", None)
    return getattr(warning, "name", None) not in NO_WATER


def _can_start(status) -> bool:
    """Starting drying won't interrupt anything.

    Dreame's own check (drying_available) covers: mop fitted, docked, no job running or paused
    (including the base cleaning itself or a mid-job recharge), not washing, draining or
    self-testing. On top of that, don't start during auto-empty or base-tray cleaning, nor during
    dust-bag drying, which Dreame's start_drying would cancel.
    """
    return bool(
        status.drying_available
        and _base_idle(status)
        and not getattr(status, "auto_emptying", False)
        and not getattr(status, "station_cleaning", False)
        and not getattr(status, "dust_bag_drying", False)
    )


def _mopping(status) -> bool:
    """Out on a job that mops (sweeping and mopping, mopping, or mopping after sweeping).

    With customized cleaning each room has its own mode, which isn't reported, so those jobs
    only count from the base's mop wash.
    """
    return bool(
        status.started
        and status.water_tank_or_mop_installed
        and not getattr(status, "customized_cleaning", False)
        and status.cleaning_mode in MOPPING_MODES
    )


def _force_stop_drying(device) -> None:
    """Send the base's drying-off command, whatever the integration thinks of the robot's state.

    Dreame's stop_drying (and its button) only sends it when it thinks drying could be started
    (mop fitted, docked, no task running...), and otherwise quietly does nothing; it also marks
    the base idle before the robot confirms. This sends the same command ("3,0") without either,
    so the caller sees what the robot really reports and can try again.
    """
    if device.info is None or device.info.version <= 1037:
        # Old firmware drops the parameters (the bare action would start a wash): leave it to Dreame.
        device.stop_drying()
        return
    response = device.start_self_wash_base("3,0")
    if not response or response.get("code") != 0:
        raise RuntimeError(f"robot answered {response!r}")


async def async_setup_platform(hass, entry, platform, async_add_entities) -> None:
    controller: DryingController = entry.runtime_data
    entity = {"switch": AllowDryingSwitch, "number": DryingTimeNumber, "sensor": MopDryingSensor}[platform]
    async_add_entities([entity(controller)])


class DryingController:
    """Tracks how long the pads have dried since they last got wet, and starts/stops drying.

    Saved state: wet, dried (seconds), last_tick (last time we saw it drying), allowed, minutes.
    Drying time is counted from the robot's own status, so it doesn't matter who started it
    (us, the robot's auto-drying, or the Dreame button).
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator, store: Store) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._store = store
        self.wet = False
        self.dried = 0.0
        self.last_tick: datetime | None = None
        self.allowed = True
        self.minutes = DEFAULT_MINUTES
        self._next_command: datetime | None = None
        self._busy = False
        self._finished = False  # just reached the drying time: stop the run once
        self._stop_attempts = 0  # stops sent while the robot still reports drying
        self._auto_drying = False  # robot's own auto-drying is on, so we're not in control
        self._quiet_since: datetime | None = None  # since when drying could start
        self.awaiting_wash = False  # mopped, and the wash after it hasn't happened yet
        self._listeners: list[Callable[[], None]] = []

    @property
    def device(self):
        return self.coordinator.device

    @property
    def connected(self) -> bool:
        device = self.device
        return device is not None and self.coordinator.last_update_success and device.device_connected

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.wet = data.get("wet", False)
        self.dried = data.get("dried", 0.0)
        self.last_tick = dt_util.parse_datetime(data["last_tick"]) if data.get("last_tick") else None
        self.allowed = data.get("allowed", True)
        self.minutes = data.get("minutes", DEFAULT_MINUTES)
        self.awaiting_wash = data.get("awaiting_wash", False)

    def _data(self) -> dict:
        return {
            "wet": self.wet,
            "dried": self.dried,
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
            "allowed": self.allowed,
            "minutes": self.minutes,
            "awaiting_wash": self.awaiting_wash,
        }

    async def async_save_now(self) -> None:
        await self._store.async_save(self._data())

    def _save(self) -> None:
        self._store.async_delay_save(self._data, 60)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @property
    def target(self) -> float:
        return self.minutes * 60

    @property
    def minutes_left(self) -> int:
        return math.ceil(max(0.0, self.target - self.dried) / 60) if self.wet else 0

    @property
    def state(self) -> str:
        if not self.wet:
            return "dry"
        if self.connected and self.device.status.drying:
            return "drying"
        if self._auto_drying:
            return "robot_auto_drying"
        if not self.allowed:
            return "paused"
        if self.awaiting_wash and self.connected and not _has_water(self.device.status):
            return "needs_water"
        return "waiting"

    @callback
    def async_update(self, _now=None) -> None:
        if not self.connected:
            return  # nothing reliable to go on; keep last_tick so a drying spell isn't lost
        status = self.device.status
        now = dt_util.utcnow()
        changed = False

        if status.washing or _mopping(status):
            if self.wet is not True or self.dried:
                self.wet, self.dried, changed = True, 0.0, True
        # The wash comes after the job has finished and the robot has docked: don't take that gap.
        if _mopping(status) and not self.awaiting_wash:
            self.awaiting_wash, changed = True, True
        elif status.washing and self.awaiting_wash:
            self.awaiting_wash, changed = False, True
        if status.drying:
            if self.last_tick is not None:
                # Includes any time HA was down while the robot kept drying.
                self.dried += max(0.0, (now - self.last_tick).total_seconds())
            self.last_tick, changed = now, True
        elif self.last_tick is not None:
            self.last_tick, changed = None, True

        if self.wet and self.dried >= self.target:
            self.wet, self._finished, changed = False, True, True
            _LOGGER.info("%s: mop dry after %d minutes", self.entry.title, self.dried // 60)

        if not _can_start(status):
            self._quiet_since = None
        elif self._quiet_since is None:
            self._quiet_since = now

        if changed:
            self._save()
        self._act(status, now)
        for listener in list(self._listeners):
            listener()

    @callback
    def _act(self, status, now: datetime) -> None:
        if not status.drying:
            self._finished = False
            if self._stop_attempts:
                self._stop_attempts = 0
                ir.async_delete_issue(self.hass, DOMAIN, _issue_id(self.entry))
        if getattr(status, "auto_drying", False):
            # The robot dries by itself after every wash: two controllers would fight, so leave
            # it to the robot (still counting) until auto-drying is turned off in the app.
            if not self._auto_drying:
                self._auto_drying = True
                _LOGGER.warning("%s: robot's auto-drying is on; not controlling drying", self.entry.title)
                ir.async_create_issue(
                    self.hass, DOMAIN, _auto_issue_id(self.entry), is_fixable=True,
                    severity=ir.IssueSeverity.WARNING, translation_key="auto_drying_on",
                    translation_placeholders={"title": self.entry.title},
                    data={"entry_id": self.entry.entry_id},
                )
            return
        if self._auto_drying:
            self._auto_drying = False
            self._next_command = None
            ir.async_delete_issue(self.hass, DOMAIN, _auto_issue_id(self.entry))
        if self._busy or (self._next_command and now < self._next_command):
            return
        if status.drying:
            # Stop when paused, or once the mop has had its full time. Drying a dry mop by hand
            # is left alone while drying is allowed.
            if not self.allowed or self._finished:
                if self._stop_attempts == STOP_ATTEMPTS:
                    _LOGGER.warning("%s: drying still hasn't stopped after %d tries", self.entry.title, STOP_ATTEMPTS)
                    ir.async_create_issue(
                        self.hass, DOMAIN, _issue_id(self.entry), is_fixable=False,
                        severity=ir.IssueSeverity.WARNING, translation_key="drying_wont_stop",
                        translation_placeholders={"title": self.entry.title},
                    )
                self._stop_attempts += 1
                self._command("stop_drying", now)
        elif self.wet and self.allowed and self._settled(status, now):
            if not self.awaiting_wash:
                self._command("start_drying", now)
            elif _has_water(status):
                # Mopped but never washed (e.g. carried back from a floor without the base, or
                # the base ran dry): wash first, then dry.
                self._command("start_washing", now)

    def _settled(self, status, now: datetime) -> bool:
        """Drying could have started for a while (and, after a mop, its wash is done)."""
        if self._quiet_since is None:
            return False
        quiet = now - self._quiet_since
        return quiet >= (WASH_WAIT if self.awaiting_wash else SETTLE)

    @callback
    def _command(self, name: str, now: datetime) -> None:
        self._busy = True
        self._next_command = now + RETRY_AFTER

        async def _run() -> None:
            try:
                func = _force_stop_drying if name == "stop_drying" else getattr(type(self.device), name)
                await self.hass.async_add_executor_job(func, self.device)
                _LOGGER.info("%s: %s (%d of %d minutes dried)", self.entry.title, name.replace("_", " "),
                             self.dried // 60, self.minutes)
            except Exception as ex:  # Dreame raises its own exception types
                _LOGGER.warning("%s: couldn't %s: %s", self.entry.title, name.replace("_", " "), ex)
            finally:
                self._busy = False
            self.async_update()  # show the robot's new state straight away

        self.entry.async_create_task(self.hass, _run(), f"{KEY} {name}")

    async def async_turn_off_auto_drying(self) -> None:
        """Turn the robot's own auto-drying off, as its Auto drying switch does."""
        device = self.device
        props = getattr(sys.modules.get(type(device).__module__), "DreameVacuumAutoSwitchProperty")
        await self.hass.async_add_executor_job(device.set_property, props.AUTO_DRYING, 0)
        self.async_update()

    @callback
    def async_set(self, *, allowed: bool | None = None, minutes: int | None = None) -> None:
        if allowed is not None:
            self.allowed = allowed
        if minutes is not None:
            self.minutes = minutes
        self._next_command = None  # act on it straight away
        self._save()
        self.async_update()


def async_create_fix_flow(hass: HomeAssistant, issue_id: str, entry: ConfigEntry) -> RepairsFlow:
    return TurnOffAutoDryingFlow(entry)


class TurnOffAutoDryingFlow(ConfirmRepairFlow):
    """The repair's Submit turns the robot's auto-drying off."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    async def async_step_confirm(self, user_input: dict[str, str] | None = None):
        if user_input is not None:
            controller = getattr(self._entry, "runtime_data", None)
            if not isinstance(controller, DryingController) or not controller.connected:
                return self.async_abort(reason="not_connected")
            try:
                await controller.async_turn_off_auto_drying()
            except Exception as ex:  # Dreame raises its own exception types
                _LOGGER.warning("%s: couldn't turn auto-drying off: %s", self._entry.title, ex)
                return self.async_abort(reason="failed")
            return self.async_create_entry(data={})
        return await super().async_step_confirm()


class _Entity:
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: DryingController) -> None:
        self.controller = controller
        dreame_entry_id = controller.entry.data[dreame.CONF_DREAME_ENTRY_ID]
        self._attr_unique_id = f"{dreame_entry_id}_{self._attr_translation_key}"
        self._attr_device_info = dreame.device_info(controller.coordinator)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.controller.async_add_listener(self.async_write_ha_state))


class AllowDryingSwitch(_Entity, SwitchEntity):
    _attr_translation_key = "allow_mop_drying"
    _attr_icon = "mdi:weather-sunny"

    @property
    def is_on(self) -> bool:
        return self.controller.allowed

    async def async_turn_on(self, **kwargs) -> None:
        self.controller.async_set(allowed=True)

    async def async_turn_off(self, **kwargs) -> None:
        self.controller.async_set(allowed=False)


class DryingTimeNumber(_Entity, NumberEntity):
    _attr_translation_key = "mop_drying_time"
    _attr_icon = "mdi:sun-clock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 30
    _attr_native_max_value = 720
    _attr_native_step = 15
    _attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> int:
        return self.controller.minutes

    async def async_set_native_value(self, value: float) -> None:
        self.controller.async_set(minutes=int(value))


class MopDryingSensor(_Entity, SensorEntity):
    _attr_translation_key = "mop_drying"
    _attr_icon = "mdi:water-percent"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["dry", "drying", "paused", "waiting", "needs_water", "robot_auto_drying"]

    @property
    def native_value(self) -> str:
        return self.controller.state

    @property
    def extra_state_attributes(self) -> dict:
        return {"minutes_left": self.controller.minutes_left, "dried_minutes": int(self.controller.dried // 60)}
