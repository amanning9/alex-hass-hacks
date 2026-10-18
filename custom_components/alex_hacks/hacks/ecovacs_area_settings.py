"""Hack: Ecovacs GOAT per-area mowing settings (direction, height, speed, obstacle avoidance)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryState,
)
from homeassistant.const import DEGREE, EntityCategory, Platform, UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
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
    "Adds, for each mowing area of an Ecovacs GOAT mower, its mowing direction, mowing height, "
    "mowing speed and obstacle avoidance mode, so they can be set from automations "
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
PLATFORMS = [Platform.NUMBER, Platform.SELECT]


@dataclass(frozen=True)
class NumberSetting:
    """A device field shown as a number, converted to and from what the app shows."""

    field: str
    key: str
    icon: str
    low: float
    high: float
    step: float
    unit: str | None
    category: EntityCategory | None
    mode: NumberMode
    show: Callable[[int], float]
    store: Callable[[float], int]


@dataclass(frozen=True)
class SelectSetting:
    """A device field shown as a choice: device value -> option."""

    field: str
    key: str
    icon: str
    options: dict[int, str]


NUMBERS = (
    # The device's angle is the app's, mirrored and turned a quarter (app = 270 - device).
    NumberSetting(
        "angle", "area_direction", "mdi:compass-outline", 0, 359, 1, DEGREE, None, NumberMode.BOX,
        show=lambda raw: (270 - raw) % 360, store=lambda value: (270 - round(value)) % 360,
    ),
    # Levels 1 (8 cm) to 11 (3 cm), half a centimetre apart.
    NumberSetting(
        "mowHeightLevel", "area_mowing_height", "mdi:content-cut", 3, 8, 0.5,
        UnitOfLength.CENTIMETERS, EntityCategory.CONFIG, NumberMode.SLIDER,
        show=lambda raw: 8.5 - raw / 2, store=lambda value: round((8.5 - value) * 2),
    ),
)
SELECTS = (
    SelectSetting("cutMode", "area_speed", "mdi:speedometer", {4: "quick", 7: "delicate"}),
    # The grass it mows among: taller grass means only taller things count as obstacles.
    SelectSetting(
        "obstacleHeight", "area_obstacle_avoidance", "mdi:grass", {1: "short_grass", 2: "normal", 3: "high_grass"}
    ),
)
SETTINGS = {"number": NUMBERS, "select": SELECTS}
# Entities from 1.2.0, when every setting was a raw number.
OLD_NUMBERS = ("_area_speed", "_area_obstacle_height", "_area_cutting_height")


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
    registry = er.async_get(hass)
    for old in er.async_entries_for_config_entry(registry, entry.entry_id):
        if old.domain == "number" and old.unique_id.endswith(OLD_NUMBERS):
            registry.async_remove(old.entity_id)
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
    entry.runtime_data.adders[platform] = async_add_entities
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
        self.adders: dict[str, Callable] = {}
        self._added: set[tuple[str, str]] = set()
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
        for platform, add in self.adders.items():
            new = [area for area in sorted(self.areas, key=_sort_key) if (platform, area) not in self._added]
            self._added.update((platform, area) for area in new)
            entity = AreaSettingNumber if platform == "number" else AreaSettingSelect
            if new:
                add([entity(self, area, setting) for area in new for setting in SETTINGS[platform]])

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


class _AreaSetting:
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, controller: Controller, area: str, setting) -> None:
        self.controller = controller
        self._area = area
        self._setting = setting
        self._attr_translation_key = setting.key
        self._attr_icon = setting.icon
        self._attr_unique_id = f"{controller.entry.data[CONF_DEVICE_ID]}_area_{area}_{setting.key}"
        self._attr_device_info = controller.device_info
        self._attr_translation_placeholders = {"area": controller.area_name(area)}

    @property
    def available(self) -> bool:
        return self._area in self.controller.areas

    @property
    def _raw(self) -> int | None:
        values = self.controller.areas.get(self._area)
        return values[self._setting.field] if values else None

    @property
    def extra_state_attributes(self) -> dict:
        return {"area_id": self._area, "device_value": self._raw}

    async def async_added_to_hass(self) -> None:
        listeners = self.controller.listeners
        listeners.append(self.async_write_ha_state)
        self.async_on_remove(lambda: listeners.remove(self.async_write_ha_state))


class AreaSettingNumber(_AreaSetting, NumberEntity):

    def __init__(self, controller: Controller, area: str, setting: NumberSetting) -> None:
        super().__init__(controller, area, setting)
        self._attr_native_min_value = setting.low
        self._attr_native_max_value = setting.high
        self._attr_native_step = setting.step
        self._attr_native_unit_of_measurement = setting.unit
        self._attr_entity_category = setting.category
        self._attr_mode = setting.mode

    @property
    def native_value(self) -> float | None:
        raw = self._raw
        return None if raw is None else self._setting.show(raw)

    async def async_set_native_value(self, value: float) -> None:
        await self.controller.async_set(self._area, self._setting.field, self._setting.store(value))


class AreaSettingSelect(_AreaSetting, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller: Controller, area: str, setting: SelectSetting) -> None:
        super().__init__(controller, area, setting)
        self._attr_options = list(setting.options.values())

    @property
    def current_option(self) -> str | None:
        # A value the app has but we don't know yet shows as unknown (see device_value).
        return self._setting.options.get(self._raw)

    async def async_select_option(self, option: str) -> None:
        raw = next(raw for raw, name in self._setting.options.items() if name == option)
        await self.controller.async_set(self._area, self._setting.field, raw)
