"""Hack: Ecovacs GOAT per-area mowing settings (direction, height, speed, obstacle grass height)."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryState,
)
from homeassistant.const import DEGREE, EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from ..const import DOMAIN
from .ecovacs_animal_protection import (  # same mower picker
    CONF_DEVICE_ID,
    _find_mower,
    async_config_schema,  # noqa: F401
)

_LOGGER = logging.getLogger(__name__)

KEY = "ecovacs_area_settings"
TITLE = "Ecovacs area settings"
WHAT = (
    "Adds, for each mowing area of an Ecovacs GOAT mower, its mowing direction, cutting height, "
    "speed and the grass height it treats as an obstacle, so they can be set from automations "
    "(e.g. turning the stripes every week). Areas are named as in the Ecovacs app. Changes made "
    "in the app show up straight away; nothing is polled."
)
WHY = (
    "Home Assistant's Ecovacs integration (deebot-client 18.7.0) doesn't support the GOAT's "
    "per-area settings, so they can only be changed in the Ecovacs app."
)
REMOVE_WHEN = (
    "deebot-client supports area settings for the mower (DeebotUniverse/client.py PR #1767) "
    "and the Ecovacs integration exposes them (a repair says when the library has it)."
)
PLATFORMS = [Platform.NUMBER]

# Device field: (translation key, icon, min, max, unit, entity category). The ranges other than
# the angle aren't known, so they're wide; the mower refuses what it can't take.
SETTINGS: dict[str, tuple[str, str, int, int, str | None, EntityCategory | None]] = {
    "angle": ("area_direction", "mdi:compass-outline", 0, 359, DEGREE, None),
    "mowHeightLevel": ("area_cutting_height", "mdi:content-cut", 0, 99, None, EntityCategory.CONFIG),
    "cutMode": ("area_speed", "mdi:speedometer", 0, 99, None, EntityCategory.CONFIG),
    "obstacleHeight": ("area_obstacle_height", "mdi:grass", 0, 99, None, EntityCategory.CONFIG),
}


def async_entry_title(hass, data) -> str:
    device = dr.async_get(hass).async_get(data[CONF_DEVICE_ID])
    name = (device.name_by_user or device.name) if device else "Ecovacs"
    return f"{name}: {TITLE}"


def async_unique_id(data) -> str:
    return f"{KEY}_{data[CONF_DEVICE_ID]}"


def _issue_id(entry: ConfigEntry) -> str:
    return f"area_settings_upstream_{entry.entry_id}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    mower, ecovacs_entry_id = _find_mower(hass, entry)
    if mower is None:
        raise ConfigEntryNotReady("The Ecovacs mower isn't loaded yet")
    from . import ecovacs_area_settings_lib as lib  # deebot_client comes with Ecovacs

    # Let the library decode the mower's pushes and app changes while any entry is loaded.
    users = hass.data.setdefault(f"{DOMAIN}_{KEY}_p2p", {"entries": set(), "undo": None})
    if not users["entries"]:
        users["undo"] = lib.register()
    users["entries"].add(entry.entry_id)

    def _release() -> None:
        users["entries"].discard(entry.entry_id)
        if not users["entries"] and users["undo"]:
            users["undo"]()
            users["undo"] = None

    entry.async_on_unload(_release)
    controller = Controller(hass, entry, mower, lib)
    entry.async_on_unload(controller.async_start())
    await controller.async_read()
    entry.runtime_data = controller

    # Reload with Ecovacs (it makes new Device objects).
    @callback
    def _entry_changed(change: ConfigEntryChange, changed: ConfigEntry) -> None:
        if changed.entry_id == ecovacs_entry_id and changed.state is ConfigEntryState.LOADED:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _entry_changed))

    # The library supports it itself now: say this hack can go.
    settings = getattr(mower.capabilities, "settings", None)
    if getattr(settings, "area_parameter", None) is not None:
        ir.async_create_issue(
            hass, DOMAIN, _issue_id(entry), is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key="area_settings_upstream", translation_placeholders={"title": entry.title},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    return None


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))


async def async_setup_platform(hass, entry, platform, async_add_entities) -> None:
    entry.runtime_data.async_add_entities = async_add_entities
    entry.runtime_data.add_new_areas()


def _ok(raw: dict) -> dict | None:
    """The body data of a successful reply, else None."""
    body = (raw.get("resp") or {}).get("body") or {}
    if raw.get("ret") != "ok" or body.get("code", -1) != 0:
        return None
    return body.get("data") or {}


class Controller:
    """Holds the mower's area settings and changes them."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, mower, lib) -> None:
        self.hass = hass
        self.entry = entry
        self.mower = mower
        self.lib = lib
        self.areas: dict[str, dict[str, int]] = {}
        self.names: dict[str, str] = {}
        self.listeners: list = []
        self.async_add_entities = None
        self._added: set[str] = set()
        ecovacs_device = dr.async_get(hass).async_get(entry.data[CONF_DEVICE_ID])
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            via_device_id=ecovacs_device.id if ecovacs_device else None,
        )
        self._was_available = True

    async def async_read(self) -> None:
        """Read the areas at set-up (from the replies, so they're there before the entities)."""
        try:
            names = _ok(await self.mower.execute_command(self.lib.GetAreaNames()))
            self.names = self.lib.parse_area_names(names) if names else {}
        except Exception as ex:  # names are nice to have: fall back to "Area <id>"
            _LOGGER.warning("Couldn't read the mower's area names: %s", ex)
        raw = await self.mower.execute_command(self.lib.GetAreaParameters())
        data = _ok(raw)
        if data is None or "areaParameters" not in data:
            raise ConfigEntryNotReady(f"Couldn't read the mower's area settings: {raw}")
        self.areas = self.lib.parse_area_parameters(data).areas

    @callback
    def async_start(self):
        events = self.mower.events

        async def on_settings(event) -> None:
            self._update(event)

        async def on_names(event) -> None:
            # Renamed in the app: reload for the new names (Home Assistant keeps entity names).
            if event.names != self.names:
                self.names = dict(event.names)
                if self._added:
                    self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

        async def on_availability(event) -> None:
            # Back online: they may have been changed meanwhile.
            if event.available and not self._was_available:
                self.refresh()
            self._was_available = event.available

        unsubs = [
            events.subscribe(self.lib.AreaParametersEvent, on_settings),
            events.subscribe(self.lib.AreaNamesEvent, on_names),
            events.subscribe(self.lib.AvailabilityEvent, on_availability),
        ]

        def stop() -> None:
            for unsub in unsubs:
                unsub()

        return stop

    @callback
    def _update(self, event) -> None:
        if event.complete:
            self.areas = {area: dict(values) for area, values in event.areas.items()}
        else:
            self.areas = {**self.areas, **{area: dict(values) for area, values in event.areas.items()}}
        self.add_new_areas()
        self._changed()

    @callback
    def _changed(self) -> None:
        for listener in list(self.listeners):
            listener()

    @callback
    def add_new_areas(self) -> None:
        if self.async_add_entities is None:
            return
        new = [area for area in self.areas if area not in self._added]
        self._added.update(new)
        if new:
            self.async_add_entities(
                [AreaSettingNumber(self, area, field) for area in sorted(new, key=_sort_key) for field in SETTINGS]
            )

    def area_name(self, area: str) -> str:
        return self.names.get(area) or f"Area {area}"

    @callback
    def refresh(self) -> None:
        for command in (self.lib.GetAreaNames(), self.lib.GetAreaParameters()):
            self.entry.async_create_background_task(
                self.hass, self.mower.execute_command(command), f"{KEY} read {command.NAME}"
            )

    async def async_set(self, area: str, field: str, value: int) -> None:
        if area not in self.areas:
            raise HomeAssistantError(f"The mower has no area {area} any more")
        new = {**self.areas[area], field: int(value)}
        raw = await self.mower.execute_command(
            self.lib.SetAreaParameters(
                area_id=area, angle=new["angle"], mow_height_level=new["mowHeightLevel"],
                cut_mode=new["cutMode"], obstacle_height=new["obstacleHeight"],
            )
        )
        if _ok(raw) is None:
            raise HomeAssistantError(f"The mower didn't accept the change: {raw}")
        event = self.lib.AreaParametersEvent({area: new}, complete=False)
        self._update(event)
        self.mower.events.notify(event)


def _sort_key(area: str):
    return (0, int(area)) if area.isdigit() else (1, area)


class AreaSettingNumber(NumberEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(self, controller: Controller, area: str, field: str) -> None:
        self.controller = controller
        self._area = area
        self._field = field
        key, icon, low, high, unit, category = SETTINGS[field]
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_unit_of_measurement = unit
        self._attr_entity_category = category
        self._attr_unique_id = f"{controller.entry.data[CONF_DEVICE_ID]}_area_{area}_{key}"
        self._attr_device_info = controller.device_info
        self._attr_extra_state_attributes = {"area_id": area}
        self._attr_translation_placeholders = {"area": controller.area_name(area)}

    @property
    def available(self) -> bool:
        return self._area in self.controller.areas

    @property
    def native_value(self) -> int | None:
        values = self.controller.areas.get(self._area)
        return values[self._field] if values else None

    async def async_set_native_value(self, value: float) -> None:
        await self.controller.async_set(self._area, self._field, round(value))

    async def async_added_to_hass(self) -> None:
        listeners = self.controller.listeners
        listeners.append(self.async_write_ha_state)
        self.async_on_remove(lambda: listeners.remove(self.async_write_ha_state))
