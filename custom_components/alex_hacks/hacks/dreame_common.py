"""Shared helpers for hacks that sit alongside Tasshack/dreame-vacuum.

They use the Dreame integration's own coordinator (hass.data["dreame_vacuum"][entry_id]) and
change nothing in it. When the Dreame entry is reloaded it gets a new coordinator, so the hack's
entry is reloaded too.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.selector import ConfigEntrySelector, ConfigEntrySelectorConfig

DREAME_DOMAIN = "dreame_vacuum"
CONF_DREAME_ENTRY_ID = "dreame_entry_id"


def get_coordinator(hass: HomeAssistant, dreame_entry_id: str):
    """The Dreame integration's coordinator for that entry, if it's running."""
    dreame_entry = hass.config_entries.async_get_entry(dreame_entry_id)
    if dreame_entry is None or dreame_entry.state is not ConfigEntryState.LOADED:
        return None
    coordinator = hass.data.get(DREAME_DOMAIN, {}).get(dreame_entry_id)
    if coordinator is None or getattr(coordinator, "device", None) is None:
        return None
    return coordinator


def device_info(coordinator) -> DeviceInfo:
    """Put the hack's entities on the vacuum's own device."""
    return DeviceInfo(identifiers={(DREAME_DOMAIN, coordinator.device.mac)})


async def async_config_schema(hass: HomeAssistant) -> tuple[vol.Schema | None, str | None]:
    entries = hass.config_entries.async_entries(DREAME_DOMAIN)
    if not entries:
        return None, "no_dreame"
    return (
        vol.Schema(
            {
                vol.Required(CONF_DREAME_ENTRY_ID, default=entries[0].entry_id): ConfigEntrySelector(
                    ConfigEntrySelectorConfig(integration=DREAME_DOMAIN)
                )
            }
        ),
        None,
    )


def entry_title(hass: HomeAssistant, data: dict[str, Any], suffix: str) -> str:
    dreame_entry = hass.config_entries.async_get_entry(data[CONF_DREAME_ENTRY_ID])
    return f"{dreame_entry.title if dreame_entry else 'Dreame'}: {suffix}"


def async_setup_coordinator(hass: HomeAssistant, entry: ConfigEntry):
    """Return the coordinator (or raise not-ready) and follow Dreame reloads."""
    dreame_entry_id = entry.data[CONF_DREAME_ENTRY_ID]
    coordinator = get_coordinator(hass, dreame_entry_id)
    if coordinator is None:
        raise ConfigEntryNotReady("Dreame Vacuum isn't loaded yet")

    @callback
    def _entry_changed(change: ConfigEntryChange, changed: ConfigEntry) -> None:
        if (
            changed.entry_id == dreame_entry_id
            and changed.state is ConfigEntryState.LOADED
            and get_coordinator(hass, dreame_entry_id) not in (None, coordinator)
        ):
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _entry_changed))
    return coordinator
