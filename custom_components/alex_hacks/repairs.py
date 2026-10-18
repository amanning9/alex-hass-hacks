"""Repairs platform: fix flows are handed to the hack that raised the issue."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .const import CONF_HACK
from .hacks import HACKS


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None) -> RepairsFlow:
    entry = hass.config_entries.async_get_entry(data["entry_id"])
    return HACKS[entry.data[CONF_HACK]].async_create_fix_flow(hass, issue_id, entry)
