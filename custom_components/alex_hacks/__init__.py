"""Alex's Hacks: small, separate workarounds for other integrations' gaps.

Each hack lives in hacks/<key>.py and is added on its own (one config entry per hack and
target). Each records what it does, why, and what upstream change would make it unnecessary;
see HACKS.md.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import CONF_HACK, DOMAIN
from .hacks import HACKS

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hack = HACKS[entry.data[CONF_HACK]]
    await hack.async_setup_entry(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, hack.PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hack = HACKS[entry.data[CONF_HACK]]
    if not await hass.config_entries.async_unload_platforms(entry, hack.PLATFORMS):
        return False
    await hack.async_unload_entry(hass, entry)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hack = HACKS.get(entry.data.get(CONF_HACK))
    if hack is not None and hasattr(hack, "async_remove_entry"):
        await hack.async_remove_entry(hass, entry)
