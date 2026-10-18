"""Hack: Dreame cleaning mode that always offers "mopping after sweeping" when allowed."""

from __future__ import annotations

import sys
from datetime import timedelta

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from . import dreame_common as dreame

KEY = "dreame_cleaning_mode"
TITLE = "Dreame cleaning mode"
WHAT = (
    'Adds "Cleaning mode (fixed)" to the vacuum, offering "mopping after sweeping" whenever '
    "the robot would accept it. Raises a repair once a newer Dreame Vacuum version has run a "
    "week without the bug."
)
WHY = (
    "Dreame Vacuum v2.0.2 removes mopping_after_sweeping from its cleaning-mode options when a "
    "task starts and only re-adds it at the moment the task status changes, if the robot doesn't "
    "look busy then. Task status and status arrive separately, so it can miss that moment and "
    "the option stays missing until the next task."
)
REMOVE_WHEN = (
    "Dreame Vacuum re-checks the option on status changes too (the repair says when it looks "
    "fixed)."
)
PLATFORMS = [Platform.SELECT]

MODES = {"sweeping": 0, "mopping": 1, "sweeping_and_mopping": 2, "mopping_after_sweeping": 3}
MOPPING_AFTER_SWEEPING = "mopping_after_sweeping"
FIXED_AFTER = timedelta(days=7)

async_config_schema = dreame.async_config_schema


def async_entry_title(hass, data) -> str:
    return dreame.entry_title(hass, data, TITLE)


def async_unique_id(data) -> str:
    return f"{KEY}_{data[dreame.CONF_DREAME_ENTRY_ID]}"


async def dreame_version(hass: HomeAssistant) -> str:
    return str((await async_get_integration(hass, dreame.DREAME_DOMAIN)).version)


def _issue_id(entry: ConfigEntry) -> str:
    return f"probably_fixed_{entry.entry_id}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator = dreame.async_setup_coordinator(hass, entry)
    entry.runtime_data = {"coordinator": coordinator, "version": await dreame_version(hass)}

    async def _daily(_now=None) -> None:
        await async_check_fixed(hass, entry)

    entry.async_on_unload(async_track_time_interval(hass, _daily, timedelta(days=1)))
    await _daily()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    return None


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))


@callback
def async_record_bug(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The bug showed up on the running Dreame version (stored once per version)."""
    version = entry.runtime_data["version"]
    if entry.data.get("bug_seen_version") != version:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "bug_seen_version": version})
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))


async def async_check_fixed(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Repair once a newer Dreame version has run for a week without the bug."""
    version = await dreame_version(hass)
    entry.runtime_data["version"] = version
    data = dict(entry.data)
    if data.get("version") != version:
        data.update(version=version, version_since=dt_util.utcnow().isoformat())
        hass.config_entries.async_update_entry(entry, data=data)

    since = dt_util.parse_datetime(data["version_since"])
    bug_version = data.get("bug_seen_version")
    if bug_version is not None and bug_version != version and dt_util.utcnow() - since >= FIXED_AFTER:
        ir.async_create_issue(
            hass,
            DOMAIN,
            _issue_id(entry),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="probably_fixed",
            translation_placeholders={"bug_version": bug_version, "version": version, "title": entry.title},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))


async def async_setup_platform(hass, entry, platform, async_add_entities) -> None:
    async_add_entities([CleaningModeSelect(entry.runtime_data["coordinator"], entry)])


class CleaningModeSelect(CoordinatorEntity, SelectEntity):
    """Cleaning-mode select whose options follow the device's current state."""

    _attr_has_entity_name = True
    _attr_translation_key = "cleaning_mode_fixed"
    _attr_icon = "mdi:broom"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.data[dreame.CONF_DREAME_ENTRY_ID]}_cleaning_mode_sidecar"
        self._attr_device_info = dreame.device_info(coordinator)

    @property
    def _device(self):
        return self.coordinator.device

    @callback
    def _handle_coordinator_update(self) -> None:
        # The bug: we'd offer mopping after sweeping but the Dreame integration doesn't.
        device = self._device
        if (
            device is not None
            and self._mopping_after_sweeping_allowed(device)
            and MOPPING_AFTER_SWEEPING not in (device.status.cleaning_mode_list or {})
        ):
            async_record_bug(self.hass, self._entry)
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        device = self._device
        if device is None or not self.coordinator.last_update_success or not device.device_connected:
            return False
        if device.status.cleaning_mode is None:
            return False
        # Same rule as the Dreame select (e.g. not during fast mapping or a scheduled task).
        rules = getattr(sys.modules.get(type(device).__module__), "PROPERTY_AVAILABILITY", {})
        rule = rules.get("CLEANING_MODE")
        return rule(device) if rule else True

    @property
    def options(self) -> list[str]:
        device = self._device
        if device is None:
            return []
        # The Dreame integration's own list is right except for mopping after sweeping.
        offered = set(device.status.cleaning_mode_list or {})
        offered.discard(MOPPING_AFTER_SWEEPING)
        if self._mopping_after_sweeping_allowed(device):
            offered.add(MOPPING_AFTER_SWEEPING)
        return [name for name in MODES if name in offered]

    @staticmethod
    def _mopping_after_sweeping_allowed(device) -> bool:
        """What the Dreame integration means to offer, worked out from the current state."""
        status = device.status
        if not device.capability.mopping_after_sweeping:
            return False
        if status.mopping_after_sweeping:  # currently selected
            return True
        return not status.started and status.water_tank_or_mop_installed

    @property
    def current_option(self) -> str | None:
        device = self._device
        if device is None:
            return None
        name = device.status.cleaning_mode_name
        return name if name in MODES else None

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise HomeAssistantError(f"{option} isn't available right now (options: {', '.join(self.options)})")
        try:
            # The device's own checks still apply (e.g. no mopping after sweeping mid-task).
            await self.hass.async_add_executor_job(self._device.set_cleaning_mode, MODES[option])
        except Exception as ex:  # Dreame raises its own exception types
            raise HomeAssistantError(f"Couldn't set cleaning mode to {option}: {ex}") from ex
        self.async_write_ha_state()
