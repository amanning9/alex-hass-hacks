"""Hack: Ecovacs GOAT animal protection (on/off and its nightly hours) as entities."""

from __future__ import annotations

from datetime import time
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.switch import SwitchEntity
from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryState,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.selector import DeviceSelector, DeviceSelectorConfig

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

KEY = "ecovacs_animal_protection"
TITLE = "Ecovacs animal protection"
WHAT = (
    'Adds "Animal protection" (switch) and its "start" and "end" times to an Ecovacs GOAT '
    "mower, so they can be set from automations (e.g. following sunset and sunrise). Changes "
    "made in the Ecovacs app show up straight away; nothing is polled."
)
WHY = (
    "Home Assistant's Ecovacs integration (deebot-client 18.7.0) doesn't support the GOAT's "
    "animal protection setting, so it can only be changed in the Ecovacs app."
)
REMOVE_WHEN = (
    "deebot-client supports animal protection for the mower (DeebotUniverse/client.py PR "
    "#1778) and the Ecovacs integration exposes it (a repair says when the library has it)."
)
PLATFORMS = [Platform.SWITCH, Platform.TIME]

ECOVACS = "ecovacs"
CONF_DEVICE_ID = "ecovacs_device_id"


async def async_config_schema(hass: HomeAssistant) -> tuple[vol.Schema | None, str | None]:
    if not hass.config_entries.async_entries(ECOVACS):
        return None, "no_ecovacs"
    return (
        vol.Schema(
            {vol.Required(CONF_DEVICE_ID): DeviceSelector(DeviceSelectorConfig(integration=ECOVACS))}
        ),
        None,
    )


def async_entry_title(hass, data) -> str:
    device = dr.async_get(hass).async_get(data[CONF_DEVICE_ID])
    name = (device.name_by_user or device.name) if device else "Ecovacs"
    return f"{name}: {TITLE}"


def async_unique_id(data) -> str:
    return f"{KEY}_{data[CONF_DEVICE_ID]}"


def _find_mower(hass: HomeAssistant, entry: ConfigEntry):
    """The deebot_client Device behind the chosen Ecovacs device, and its config entry id."""
    device = dr.async_get(hass).async_get(entry.data[CONF_DEVICE_ID])
    if device is None:
        return None, None
    did = next((ident for domain, ident in device.identifiers if domain == ECOVACS), None)
    for ecovacs_entry in hass.config_entries.async_entries(ECOVACS):
        if ecovacs_entry.state is not ConfigEntryState.LOADED:
            continue
        for mower in getattr(ecovacs_entry.runtime_data, "devices", []):
            if mower.device_info.get("did") == did:
                return mower, ecovacs_entry.entry_id
    return None, None


def _issue_id(entry: ConfigEntry) -> str:
    return f"animal_protection_upstream_{entry.entry_id}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    mower, ecovacs_entry_id = _find_mower(hass, entry)
    if mower is None:
        raise ConfigEntryNotReady("The Ecovacs mower isn't loaded yet")
    from . import ecovacs_animal_protection_lib as lib  # deebot_client comes with Ecovacs

    # Let the library decode app changes while any entry of this hack is loaded.
    users = hass.data.setdefault(f"{DOMAIN}_{KEY}_p2p", {"entries": set(), "undo": None})
    if not users["entries"]:
        users["undo"] = lib.register_p2p()
    users["entries"].add(entry.entry_id)

    def _release() -> None:
        users["entries"].discard(entry.entry_id)
        if not users["entries"] and users["undo"]:
            users["undo"]()
            users["undo"] = None

    entry.async_on_unload(_release)
    entry.runtime_data = Controller(hass, entry, mower, lib)
    entry.async_on_unload(entry.runtime_data.async_start())

    # Reload with Ecovacs (it makes new Device objects).
    @callback
    def _entry_changed(change: ConfigEntryChange, changed: ConfigEntry) -> None:
        if changed.entry_id == ecovacs_entry_id and changed.state is ConfigEntryState.LOADED:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _entry_changed))

    # The library supports it itself now: say this hack can go.
    settings = getattr(mower.capabilities, "settings", None)
    if getattr(settings, "animal_protection", None) is not None:
        ir.async_create_issue(
            hass, DOMAIN, _issue_id(entry), is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key="animal_protection_upstream", translation_placeholders={"title": entry.title},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    return None


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))


async def async_setup_platform(hass, entry, platform, async_add_entities) -> None:
    controller: Controller = entry.runtime_data
    if platform == "switch":
        async_add_entities([AnimalProtectionSwitch(controller)])
    else:
        async_add_entities([AnimalProtectionTime(controller, "start"), AnimalProtectionTime(controller, "end")])


class Controller:
    """Holds the mower's current setting and changes it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, mower, lib) -> None:
        self.hass = hass
        self.entry = entry
        self.mower = mower
        self.lib = lib
        self.setting = None  # lib.AnimalProtectionEvent once read
        self.listeners: list = []
        ecovacs_device = dr.async_get(hass).async_get(entry.data[CONF_DEVICE_ID])
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            via_device_id=ecovacs_device.id if ecovacs_device else None,
        )
        self._was_available = True

    @callback
    def async_start(self):
        events = self.mower.events

        async def on_setting(event) -> None:
            self._update(event)

        async def on_availability(event) -> None:
            # Back online: it may have been changed meanwhile.
            if event.available and not self._was_available:
                self.refresh()
            self._was_available = event.available

        unsubs = [
            events.subscribe(self.lib.AnimalProtectionEvent, on_setting),
            events.subscribe(self.lib.AvailabilityEvent, on_availability),
        ]
        self.refresh()

        def stop() -> None:
            for unsub in unsubs:
                unsub()

        return stop

    @callback
    def _update(self, setting) -> None:
        self.setting = setting
        for listener in list(self.listeners):
            listener()

    @callback
    def refresh(self) -> None:
        self.entry.async_create_background_task(
            self.hass, self.mower.execute_command(self.lib.GetAnimalProtection()), f"{KEY} read"
        )

    async def async_set(self, **changes: Any) -> None:
        if self.setting is None:
            raise HomeAssistantError("Animal protection hasn't been read from the mower yet")
        new = {"enabled": self.setting.enabled, "start": self.setting.start, "end": self.setting.end, **changes}
        raw = await self.mower.execute_command(self.lib.SetAnimalProtection(**new))
        body = (raw.get("resp") or {}).get("body") or {}
        if raw.get("ret") != "ok" or body.get("code", -1) != 0:
            raise HomeAssistantError(f"The mower didn't accept the change: {raw}")
        setting = self.lib.AnimalProtectionEvent(**new)
        self._update(setting)
        self.mower.events.notify(setting)


class _Entity:
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: Controller, key: str) -> None:
        self.controller = controller
        self._attr_translation_key = key
        self._attr_unique_id = f"{controller.entry.data[CONF_DEVICE_ID]}_{key}"
        self._attr_device_info = controller.device_info

    @property
    def available(self) -> bool:
        return self.controller.setting is not None

    async def async_added_to_hass(self) -> None:
        listeners = self.controller.listeners
        listeners.append(self.async_write_ha_state)
        self.async_on_remove(lambda: listeners.remove(self.async_write_ha_state))


class AnimalProtectionSwitch(_Entity, SwitchEntity):
    _attr_icon = "mdi:paw"

    def __init__(self, controller: Controller) -> None:
        super().__init__(controller, "animal_protection")

    @property
    def is_on(self) -> bool | None:
        return self.controller.setting.enabled if self.controller.setting else None

    async def async_turn_on(self, **kwargs) -> None:
        await self.controller.async_set(enabled=True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.controller.async_set(enabled=False)


class AnimalProtectionTime(_Entity, TimeEntity):
    _attr_icon = "mdi:paw-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller: Controller, which: str) -> None:
        super().__init__(controller, f"animal_protection_{which}")
        self._which = which

    @property
    def native_value(self) -> time | None:
        setting = self.controller.setting
        return time.fromisoformat(getattr(setting, self._which)) if setting else None

    async def async_set_value(self, value: time) -> None:
        await self.controller.async_set(**{self._which: value.strftime("%H:%M")})
