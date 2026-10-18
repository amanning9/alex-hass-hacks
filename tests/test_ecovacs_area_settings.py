import asyncio

import orjson
import pytest
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

PREFIX = "number.goaty_ecovacs_area_settings"
SENTRUM = f"{PREFIX}_sentrum_direction"
VEST_SPEED = f"{PREFIX}_vestkanten_speed"


async def settle(hass):
    """Let deebot_client's event bus deliver (its tasks aren't Home Assistant's)."""
    for _ in range(5):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


async def setup(hass, prepare=None):
    ecovacs = MockConfigEntry(domain="ecovacs", title="Ecovacs")
    ecovacs.add_to_hass(hass)
    assert await hass.config_entries.async_setup(ecovacs.entry_id)
    mower = ecovacs.runtime_data.devices[0]
    if prepare:
        prepare(mower)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=ecovacs.entry_id, identifiers={("ecovacs", "E1")}, name="Goaty")
    result = await hass.config_entries.flow.async_init("alex_hacks", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"hack": ["ecovacs_area_settings"]})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"ecovacs_device_id": device.id})
    assert result["type"] == "create_entry", result
    await settle(hass)
    return hass.config_entries.async_get_entry(result["result"].entry_id), mower


async def test_reads_named_areas_and_sets(hass):
    entry, mower = await setup(hass)
    assert entry.title == "Goaty: Ecovacs area settings"
    state = hass.states.get(SENTRUM)
    assert state.state == "180" and state.attributes["friendly_name"] == "Goaty: Ecovacs area settings Sentrum direction"
    assert hass.states.get(f"{PREFIX}_sentrum_cutting_height").state == "10"
    assert hass.states.get(f"{PREFIX}_sentrum_obstacle_grass_height").state == "1"
    assert hass.states.get(VEST_SPEED).state == "4"
    await hass.services.async_call("number", "set_value", {"entity_id": SENTRUM, "value": 135}, blocking=True)
    # Every field is sent, the others unchanged.
    assert mower.calls[-1] == (
        "setAreaParameter", {"areaID": "1", "mowHeightLevel": 10, "cutMode": 7, "obstacleHeight": 1, "angle": 135})
    assert hass.states.get(SENTRUM).state == "135"
    await hass.services.async_call("number", "set_value", {"entity_id": VEST_SPEED, "value": 5}, blocking=True)
    assert mower.calls[-1] == (
        "setAreaParameter", {"areaID": "2", "mowHeightLevel": 9, "cutMode": 5, "obstacleHeight": 2, "angle": 0})
    assert hass.states.get(SENTRUM).state == "135"
    # One read of each, at start-up: no polling.
    assert [c[0] for c in mower.calls if c[0].startswith("get")] == ["getAreaSet", "getAreaParameter"]


async def test_rejected_change_errors_and_keeps_the_old_value(hass):
    from homeassistant.exceptions import HomeAssistantError
    entry, mower = await setup(hass)
    mower.accept = False
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("number", "set_value", {"entity_id": SENTRUM, "value": 90}, blocking=True)
    assert hass.states.get(SENTRUM).state == "180"


async def test_unnamed_areas_fall_back(hass):
    def no_names(mower):
        mower.area_set = None
    await setup(hass, no_names)
    assert hass.states.get(f"{PREFIX}_area_1_direction").state == "180"


async def test_mower_pushes_and_app_changes_arrive_without_polling(hass):
    from deebot_client.commands import COMMANDS_WITH_MQTT_P2P_HANDLING
    from deebot_client.const import DataType
    from deebot_client.messages import MESSAGES
    entry, mower = await setup(hass)
    # The mower pushes onAreaParameter, now with a new area too.
    message = MESSAGES[DataType.JSON]["onAreaParameter"]
    message.handle(mower.events, {"header": {}, "body": {"data": {"areaParameters": [
        {"areaID": "1", "mowHeightLevel": 8, "cutMode": 7, "obstacleHeight": 1, "angle": 45},
        {"areaID": "4", "mowHeightLevel": 9, "cutMode": 7, "obstacleHeight": 3, "angle": 90},
    ]}}})
    await settle(hass)
    assert hass.states.get(SENTRUM).state == "45"
    assert hass.states.get(f"{PREFIX}_sentrum_cutting_height").state == "8"
    assert hass.states.get(f"{PREFIX}_ostkanten_direction").state == "90"
    assert hass.states.get(VEST_SPEED).state == "unavailable"  # area 2 has gone
    # The app sets one area over MQTT, as deebot_client's MQTT client hands it over.
    command_type = COMMANDS_WITH_MQTT_P2P_HANDLING[DataType.JSON]["setAreaParameter"]
    request = {"header": {}, "body": {"data": {
        "areaID": "4", "mowHeightLevel": 9, "cutMode": 6, "obstacleHeight": 3, "angle": 10}}}
    command = command_type.create_from_mqtt(orjson.dumps(request))
    command.handle_mqtt_p2p(mower.events, orjson.dumps({"header": {}, "body": {"code": 0, "msg": "ok"}}))
    await settle(hass)
    assert hass.states.get(f"{PREFIX}_ostkanten_direction").state == "10"
    assert hass.states.get(f"{PREFIX}_ostkanten_speed").state == "6"
    assert hass.states.get(SENTRUM).state == "45"  # other areas untouched
    # Unloading takes the registrations away again.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert "setAreaParameter" not in COMMANDS_WITH_MQTT_P2P_HANDLING[DataType.JSON]
    assert "onAreaParameter" not in MESSAGES[DataType.JSON]


async def test_rereads_when_back_online_and_repairs_when_upstream_has_it(hass):
    from deebot_client.events import AvailabilityEvent
    entry, mower = await setup(hass)
    mower.events.notify(AvailabilityEvent(available=False))
    await settle(hass)
    mower.areas["1"]["angle"] = 270  # changed while offline
    mower.events.notify(AvailabilityEvent(available=True))
    await settle(hass)
    assert hass.states.get(SENTRUM).state == "270"

    mower.capabilities.settings.area_parameter = object()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue("alex_hacks", f"area_settings_upstream_{entry.entry_id}")
