"""Hack: one entity per room on a Dreame robot's map, for picking rooms in actions."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN
from . import dreame_common as dreame

KEY = "dreame_rooms"
TITLE = "Dreame rooms"
WHAT = (
    "Adds a sensor per room on one of the robot's maps (the one it's on when the hack is added), "
    "named as in the Dreame app, whose state is the room number. Pickers can filter to these "
    "(integration: alex_hacks, domain: sensor), so they list only rooms, and a room the robot "
    "splits in two shows as two. If a room disappears from that map, its entity goes unavailable "
    "(not deleted, so automations keep their references) and a repair says which room. While the "
    "robot is on another map, nothing changes."
)
WHY = (
    "Dreame Vacuum's per-room entities can't be told apart from its other entities in an entity "
    "picker (no device class to filter on), and Home Assistant's area mapping for vacuums can't "
    "be read from templates, so automations can't turn a picked area into room numbers, and an "
    "area covering two robot rooms can't be split."
)
REMOVE_WHEN = (
    "Dreame Vacuum gives each room a filterable entity or device, or Home Assistant exposes the "
    "vacuum area mapping to templates (and part of an area can be picked)."
)
PLATFORMS = [Platform.SENSOR]
CONF_MAP_ID = "map_id"

async_config_schema = dreame.async_config_schema


def async_entry_title(hass, data) -> str:
    return dreame.entry_title(hass, data, TITLE)


def async_unique_id(data) -> str:
    return f"{KEY}_{data[dreame.CONF_DREAME_ENTRY_ID]}"


def _selected_map(coordinator):
    device = coordinator.device
    return getattr(device.status, "selected_map", None) if device is not None and device.status else None


def _map(coordinator, map_id):
    device = coordinator.device
    if device is None or device.status is None:
        return None
    return (device.status.map_data_list or {}).get(map_id)


def _rooms(coordinator, map_id) -> dict[int, object] | None:
    """Rooms on the hack's map, or None if that map's data isn't loaded."""
    map_data = _map(coordinator, map_id)
    if map_data is None or not getattr(map_data, "segments", None):
        return None
    return {
        room_id: segment
        for room_id, segment in map_data.segments.items()
        if getattr(segment, "visibility", None) is not False and not getattr(segment, "unmapped", False)
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator = dreame.async_setup_coordinator(hass, entry)
    if CONF_MAP_ID not in entry.data:
        # The map the robot is on now (new entries, and entries from before maps were tracked).
        selected = _selected_map(coordinator)
        if selected is not None and selected.map_id is not None:
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MAP_ID: selected.map_id})
    entry.runtime_data = {"coordinator": coordinator}


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    return None


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(f"room_missing_{entry.entry_id}_"):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def _room_name(room_id: int, segment) -> str:
    return getattr(segment, "custom_name", None) or getattr(segment, "name", None) or f"Room {room_id}"


async def async_setup_platform(hass, entry, platform, async_add_entities) -> None:
    coordinator = entry.runtime_data["coordinator"]
    # Rooms seen before (still in the entity registry) come back even if they're no longer on
    # the map, so they show as unavailable with a repair rather than vanishing.
    prefix = f"{entry.data[dreame.CONF_DREAME_ENTRY_ID]}_room_"
    known: set[int] = set()
    restored = {
        int(e.unique_id[len(prefix):]): e.original_name
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.startswith(prefix) and e.unique_id[len(prefix):].isdigit()
    }
    if restored:
        known.update(restored)
        async_add_entities(
            RoomSensor(coordinator, entry, room_id, last_name=restored[room_id]) for room_id in sorted(restored)
        )

    @callback
    def _add_new_rooms() -> None:
        new = [room_id for room_id in (_rooms(coordinator, entry.data.get(CONF_MAP_ID)) or {}) if room_id not in known]
        known.update(new)
        if new:
            async_add_entities(RoomSensor(coordinator, entry, room_id) for room_id in sorted(new))

    _add_new_rooms()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_rooms))


class RoomSensor(CoordinatorEntity, SensorEntity):
    """A room on the hack's map. State: its room number."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, room_id: int, last_name: str | None = None) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._room_id = room_id
        self._last_name = last_name
        self._issue_id = f"room_missing_{entry.entry_id}_{room_id}"
        self._attr_unique_id = f"{entry.data[dreame.CONF_DREAME_ENTRY_ID]}_room_{room_id}"
        self._attr_device_info = dreame.device_info(coordinator)

    @property
    def _map_id(self):
        return self._entry.data.get(CONF_MAP_ID)

    @property
    def _segment(self):
        return (_rooms(self.coordinator, self._map_id) or {}).get(self._room_id)

    @property
    def name(self) -> str:
        segment = self._segment
        if segment is None:
            # Gone from the map (or its data not loaded): keep the last name it had.
            return self._last_name or f"Room {self._room_id}"
        self._last_name = _room_name(self._room_id, segment)
        return self._last_name

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._check_missing()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._check_missing()
        super()._handle_coordinator_update()

    @callback
    def _check_missing(self) -> None:
        """Repair while the map's data is loaded and lacks this room."""
        rooms = _rooms(self.coordinator, self._map_id)
        if rooms is None:
            return
        if self._room_id in rooms:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        else:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="room_missing",
                translation_placeholders={
                    "room": self.name,
                    "room_id": str(self._room_id),
                    "entity_id": self.entity_id,
                    "vacuum": self._entry.title.split(":")[0],
                },
            )

    @property
    def icon(self) -> str:
        return getattr(self._segment, "icon", None) or "mdi:floor-plan"

    @property
    def available(self) -> bool:
        return super().available and self._segment is not None

    @property
    def native_value(self) -> int:
        return self._room_id

    @property
    def extra_state_attributes(self) -> dict:
        map_data = _map(self.coordinator, self._map_id)
        selected = _selected_map(self.coordinator)
        return {
            "room_id": self._room_id,
            "map_id": self._map_id,
            "map_name": getattr(map_data, "map_name", None),
            "on_current_map": selected is not None and selected.map_id == self._map_id,
        }
