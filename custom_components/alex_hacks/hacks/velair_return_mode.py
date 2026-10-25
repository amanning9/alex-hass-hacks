"""Hack: choose the Velair Mode a planned return comes back to, from Home Assistant."""

from __future__ import annotations

from copy import deepcopy
import logging

import voluptuous as vol

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryState,
)
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.util import dt as dt_util

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

KEY = "velair_return_mode"
TITLE = "Velair return mode"
WHAT = (
    'Adds "Velair return mode" (select): the Velair Mode a planned return comes back to, so an '
    "automation can match it to who'll be home (e.g. a guest mode). Changing it while a return "
    "is planned or under way plans that return again, so it isn't lost."
)
WHY = (
    "Velair sets the return mode only in its panel (Planned return settings): there's no action "
    "for it, so every return comes back to the same mode, e.g. Default even when guests are "
    "staying."
)
REMOVE_WHEN = (
    "Velair lets automations choose the return mode (e.g. a field on velair.start_planned_return). "
    "This hack uses Velair's internals, so it only loads on the Velair versions it was checked "
    "against (a repair says when Velair is a different version)."
)
PLATFORMS = [Platform.SELECT]

VELAIR = "velair"
# Checked against exactly these: entry.runtime_data.scheduler / .storage, storage.data's
# planned_return and planned_return_episode, scheduler.get_modes(),
# async_update_planned_return(config) (a new return mode cancels a current return) and
# async_start_planned_return(return_at, apply_away_now=...).
VELAIR_VERSIONS = ("1.9.0-beta.3", "1.9.0-beta.4", "1.9.0-beta.5")
DEFAULT_OPTION = "Default"  # Velair's built-in mode (key "default")


async def async_config_schema(hass: HomeAssistant) -> tuple[vol.Schema | None, str | None]:
    if not hass.config_entries.async_entries(VELAIR):
        return None, "no_velair"
    return vol.Schema({}), None


def async_entry_title(hass, data) -> str:
    return TITLE


def async_unique_id(data) -> str:
    return KEY


def _issue_id(entry: ConfigEntry) -> str:
    return f"velair_version_{entry.entry_id}"


async def velair_version(hass: HomeAssistant) -> str | None:
    try:
        return str((await async_get_integration(hass, VELAIR)).version)
    except IntegrationNotFound:
        return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    version = await velair_version(hass)
    if version not in VELAIR_VERSIONS:
        ir.async_create_issue(
            hass, DOMAIN, _issue_id(entry), is_fixable=False, severity=ir.IssueSeverity.ERROR,
            translation_key="velair_version",
            translation_placeholders={"title": entry.title, "version": str(version),
                                      "checked": ", ".join(VELAIR_VERSIONS)},
        )
        raise ConfigEntryError(f"Not checked with Velair {version} (checked: {', '.join(VELAIR_VERSIONS)})")
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))

    velair_entry = next(
        (e for e in hass.config_entries.async_entries(VELAIR) if e.state is ConfigEntryState.LOADED), None
    )
    if velair_entry is None:
        raise ConfigEntryNotReady("Velair isn't loaded yet")
    entry.runtime_data = Controller(hass, entry, velair_entry.runtime_data)

    # Reload with Velair (it makes a new scheduler).
    @callback
    def _entry_changed(change: ConfigEntryChange, changed: ConfigEntry) -> None:
        if changed.entry_id == velair_entry.entry_id and changed.state is ConfigEntryState.LOADED:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _entry_changed))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    return None


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))


async def async_setup_platform(hass, entry, platform, async_add_entities) -> None:
    async_add_entities([ReturnModeSelect(entry.runtime_data)])


class Controller:
    """Reads and sets Velair's planned return mode."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, velair) -> None:
        self.hass = hass
        self.entry = entry
        self.scheduler = velair.scheduler
        self.storage = velair.storage

    def _config(self) -> dict:
        return deepcopy(self.storage.data.get("planned_return") or {})

    def options(self) -> list[str]:
        return [DEFAULT_OPTION, *(mode["name"] for mode in self.scheduler.get_modes())]

    def current(self) -> str | None:
        key = self._config().get("destination_mode_id", "default")
        if key == "default":
            return DEFAULT_OPTION
        return next((mode["name"] for mode in self.scheduler.get_modes() if mode["key"] == key), None)

    def blocked(self) -> bool:
        return bool(getattr(self.scheduler, "temperature_migration_blocked", False))

    async def async_set(self, option: str) -> None:
        if option == DEFAULT_OPTION:
            key = "default"
        else:
            key = next((mode["key"] for mode in self.scheduler.get_modes() if mode["name"] == option), None)
            if key is None:
                raise HomeAssistantError(f"Velair has no mode called {option}")
        config = self._config()
        if config.get("destination_mode_id", "default") == key:
            return
        if self.blocked():
            raise HomeAssistantError("Velair is busy (data migration); try again later")
        if missing := [e for e in config.get("occupancy_entity_ids", []) if self.hass.states.get(e) is None]:
            raise HomeAssistantError(f"Velair's occupancy sources aren't available: {', '.join(missing)}")
        # A new return mode cancels a return that's planned or under way, so plan it again
        # (straight back to Away if it was away, rather than waiting for absence again).
        episode = deepcopy(self.storage.data.get("planned_return_episode") or {})
        return_at = episode.get("return_at")
        replan = (
            episode.get("state") in ("armed", "away")
            and return_at is not None
            and dt_util.parse_datetime(return_at) is not None
            and dt_util.parse_datetime(return_at) > dt_util.now()
        )
        config["destination_mode_id"] = key
        try:
            await self.scheduler.async_update_planned_return(config)
            if replan:
                await self.scheduler.async_start_planned_return(
                    return_at, apply_away_now=episode["state"] == "away"
                )
        except ValueError as err:
            raise HomeAssistantError(f"Velair didn't accept that: {err}") from err
        _LOGGER.info("Velair return mode set to %s%s", option, " (return planned again)" if replan else "")


class ReturnModeSelect(SelectEntity):
    """Velair's return mode. Velair doesn't announce changes made in its panel, so it's
    re-read on every Velair event and whenever it's set from here."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_icon = "mdi:home-import-outline"
    _attr_unique_id = KEY

    def __init__(self, controller: Controller) -> None:
        self.controller = controller
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, controller.entry.entry_id)})

    @property
    def available(self) -> bool:
        return not self.controller.blocked()

    @property
    def options(self) -> list[str]:
        return self.controller.options()

    @property
    def current_option(self) -> str | None:
        return self.controller.current()

    async def async_select_option(self, option: str) -> None:
        await self.controller.async_set(option)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        @callback
        def _velair_event(event: Event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(self.hass.bus.async_listen("velair_event", _velair_event))
