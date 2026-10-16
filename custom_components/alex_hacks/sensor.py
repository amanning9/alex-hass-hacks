"""Sensor platform: handed to the entry's hack."""

from .const import CONF_HACK
from .hacks import HACKS


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    await HACKS[entry.data[CONF_HACK]].async_setup_platform(hass, entry, "sensor", async_add_entities)
