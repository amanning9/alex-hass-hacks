"""Hack: notify a person on all their phones (companion app devices)."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, State, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_added_domain, async_track_state_change_event

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

KEY = "person_notify"
TITLE = "Person notify"
WHAT = (
    'Adds a notify entity per person (e.g. notify.person_alex) that sends to every phone '
    "they're signed in to with the companion app, and the action "
    '"alex_hacks.notify_person" which does the same and also passes "data" through '
    "(buttons, links, images, channels). Phones are found from the person's device trackers, "
    "so adding or removing one needs no change."
)
WHY = (
    "The companion app only offers one notify action per phone (notify.mobile_app_<phone>), so "
    "every automation has to list each person's phones, and a new phone means editing them "
    "all. Notify groups have to be kept up to date by hand, and Home Assistant's notify "
    "entities can't carry data, so they can't send actionable notifications."
)
REMOVE_WHEN = (
    "Home Assistant (or the companion app) can notify a person, or a person's devices, "
    "directly, with notification data. Switch automations over, then delete the entry."
)
PLATFORMS = [Platform.NOTIFY]

SERVICE = "notify_person"
SCHEMA = vol.Schema(
    {
        vol.Required("person"): cv.entity_ids,
        vol.Required("message"): cv.string,
        vol.Optional("title"): cv.string,
        vol.Optional("data"): dict,
    }
)


async def async_config_schema(hass: HomeAssistant) -> tuple[vol.Schema | None, str | None]:
    return vol.Schema({}), None


def async_entry_title(hass, data) -> str:
    return TITLE


def async_unique_id(data) -> str:
    return KEY


def _device_notify_service(hass: HomeAssistant, device_id: str) -> str | None:
    """The companion app's notify action for this phone, if it can take notifications."""
    from homeassistant.components.mobile_app.util import (  # loaded with mobile_app
        get_notify_service,
        supports_push,
        webhook_id_from_device_id,
    )

    try:
        webhook_id = webhook_id_from_device_id(hass, device_id)
        if webhook_id is None or not supports_push(hass, webhook_id):
            return None
        return get_notify_service(hass, webhook_id)
    except KeyError:  # companion app still starting up
        return None


def phone_services(hass: HomeAssistant, person: State | None) -> list[str]:
    """notify.<service> names for every companion-app phone behind the person's trackers."""
    if person is None or "mobile_app" not in hass.config.components:
        return []
    registry = er.async_get(hass)
    services = []
    for tracker in person.attributes.get("device_trackers", []):
        entry = registry.async_get(tracker)
        if entry is None or entry.platform != "mobile_app" or entry.device_id is None:
            continue
        service = _device_notify_service(hass, entry.device_id)
        if service and service not in services:
            services.append(service)
    return services


async def async_notify(
    hass: HomeAssistant, person_id: str, message: str, title: str | None = None, data: dict | None = None
) -> int:
    """Send to each of the person's phones; returns how many it was sent to."""
    services = phone_services(hass, hass.states.get(person_id))
    if not services:
        _LOGGER.warning("%s has no companion-app phones to notify", person_id)
        return 0
    payload: dict[str, Any] = {"message": message}
    if title:
        payload["title"] = title
    if data:
        payload["data"] = data
    sent = 0
    for service in services:
        try:
            await hass.services.async_call("notify", service, payload, blocking=True)
            sent += 1
        except Exception as ex:  # one phone failing mustn't stop the others
            _LOGGER.warning("Couldn't notify %s via notify.%s: %s", person_id, service, ex)
    return sent


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    async def handle(call: ServiceCall) -> None:
        people = call.data["person"]
        if bad := [p for p in people if not p.startswith("person.") or hass.states.get(p) is None]:
            raise ServiceValidationError(f"Not a person: {', '.join(bad)}")
        sent = 0
        for person_id in people:
            sent += await async_notify(
                hass, person_id, call.data["message"], call.data.get("title"), call.data.get("data")
            )
        if not sent:
            raise HomeAssistantError(f"Couldn't notify anyone: no phones reachable for {', '.join(people)}")

    hass.services.async_register(DOMAIN, SERVICE, handle, schema=SCHEMA)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hass.services.async_remove(DOMAIN, SERVICE)


async def async_setup_platform(hass, entry, platform, async_add_entities) -> None:
    added: set[str] = set()

    @callback
    def _add(person_ids) -> None:
        new = [p for p in person_ids if p not in added]
        added.update(new)
        if new:
            async_add_entities([PersonNotifyEntity(person_id) for person_id in new])

    _add(hass.states.async_entity_ids("person"))

    @callback
    def _person_added(event: Event) -> None:
        _add([event.data["entity_id"]])

    entry.async_on_unload(async_track_state_added_domain(hass, "person", _person_added))


class PersonNotifyEntity(NotifyEntity):
    """Sends to all of one person's phones (message and title only; use the action for data)."""

    _attr_should_poll = False
    _attr_icon = "mdi:cellphone-message"

    def __init__(self, person_id: str) -> None:
        self._person_id = person_id
        object_id = person_id.split(".", 1)[1]
        self.entity_id = f"notify.person_{object_id}"
        self._attr_unique_id = f"{KEY}_{person_id}"

    @property
    def _person(self) -> State | None:
        return self.hass.states.get(self._person_id)

    @property
    def name(self) -> str:
        person = self._person
        return f"{person.name if person else self._person_id} (all phones)"

    @property
    def available(self) -> bool:
        return self._person is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"person": self._person_id, "phones": [f"notify.{s}" for s in phone_services(self.hass, self._person)]}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _person_changed(event: Event) -> None:
            self.async_write_ha_state()  # name, availability and phones follow the person

        self.async_on_remove(async_track_state_change_event(self.hass, [self._person_id], _person_changed))

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        if not await async_notify(self.hass, self._person_id, message, title):
            raise HomeAssistantError(f"No phones reachable for {self._person_id}")
