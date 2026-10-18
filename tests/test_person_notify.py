import pytest
from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alex_hacks.hacks import person_notify

ADA = {"friendly_name": "Ada", "device_trackers": ["device_tracker.phone", "device_tracker.tablet", "device_tracker.gps"]}


async def setup(hass, monkeypatch):
    hass.config.components.add("mobile_app")
    phones = MockConfigEntry(domain="mobile_app")
    phones.add_to_hass(hass)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    services = {}
    for name in ("phone", "tablet"):
        device = devices.async_get_or_create(config_entry_id=phones.entry_id, identifiers={("mobile_app", name)})
        entities.async_get_or_create("device_tracker", "mobile_app", name, suggested_object_id=name,
                                     device_id=device.id, config_entry=phones)
        services[device.id] = f"mobile_app_{name}"
    # A tracker from another integration (not a phone the app can notify).
    entities.async_get_or_create("device_tracker", "gpslogger", "gps", suggested_object_id="gps")
    monkeypatch.setattr(person_notify, "_device_notify_service", lambda hass, device_id: services.get(device_id))

    sent = []
    async def handler(call: ServiceCall):
        if call.service == "mobile_app_tablet" and call.data.get("message") == "fail tablet":
            raise HomeAssistantError("tablet offline")
        sent.append((call.service, dict(call.data)))
    for name in ("phone", "tablet"):
        hass.services.async_register("notify", f"mobile_app_{name}", handler)

    hass.states.async_set("person.ada", "home", ADA)
    result = await hass.config_entries.flow.async_init("alex_hacks", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"hack": ["person_notify"]})
    assert "**Remove when:**" in result["description_placeholders"]["hacks"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    return sent


async def test_entity_per_person_sends_to_every_phone(hass, monkeypatch):
    sent = await setup(hass, monkeypatch)
    state = hass.states.get("notify.person_ada")
    assert state.attributes["friendly_name"] == "Ada (all phones)"
    assert state.attributes["phones"] == ["notify.mobile_app_phone", "notify.mobile_app_tablet"]
    await hass.services.async_call("notify", "send_message",
                                   {"entity_id": "notify.person_ada", "title": "Hi", "message": "Dinner"}, blocking=True)
    assert sent == [("mobile_app_phone", {"message": "Dinner", "title": "Hi"}),
                    ("mobile_app_tablet", {"message": "Dinner", "title": "Hi"})]


async def test_action_passes_data_and_one_failure_doesnt_stop_the_rest(hass, monkeypatch):
    sent = await setup(hass, monkeypatch)
    data = {"actions": [{"action": "YES", "title": "Yes"}]}
    await hass.services.async_call("alex_hacks", "notify_person",
                                   {"person": "person.ada", "message": "fail tablet", "data": data}, blocking=True)
    assert sent == [("mobile_app_phone", {"message": "fail tablet", "data": data})]
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("alex_hacks", "notify_person",
                                       {"person": "light.kitchen", "message": "x"}, blocking=True)


async def test_new_people_get_entities_and_phoneless_ones_error(hass, monkeypatch):
    await setup(hass, monkeypatch)
    hass.states.async_set("person.ben", "away", {"friendly_name": "Ben", "device_trackers": []})
    await hass.async_block_till_done()
    assert hass.states.get("notify.person_ben") is not None
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("alex_hacks", "notify_person",
                                       {"person": ["person.ben"], "message": "x"}, blocking=True)
    hass.states.async_remove("person.ben")
    await hass.async_block_till_done()
    assert hass.states.get("notify.person_ben").state == "unavailable"
