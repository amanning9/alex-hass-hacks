import asyncio

import orjson
import pytest
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

PREFIX = "number.goaty_ecovacs_area_settings"
SELECT = "select.goaty_ecovacs_area_settings"
SENTRUM = f"{PREFIX}_sentrum_direction"
VEST_SPEED = f"{SELECT}_vestkanten_mowing_speed"


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
    # As the app shows them: the device's 180 is the app's 90.
    assert state.state == "90" and state.attributes["friendly_name"] == "Goaty: Ecovacs area settings Sentrum direction"
    assert state.attributes["device_value"] == 180
    assert hass.states.get(f"{PREFIX}_sentrum_mowing_height").state == "3.5"
    assert hass.states.get(f"{SELECT}_sentrum_obstacle_avoidance_mode").state == "short_grass"
    assert hass.states.get(f"{SELECT}_sentrum_mowing_speed").state == "delicate"
    assert hass.states.get(VEST_SPEED).state == "quick"
    await hass.services.async_call("number", "set_value", {"entity_id": SENTRUM, "value": 130}, blocking=True)
    # Every field is sent, the others unchanged.
    assert mower.calls[-1] == (
        "setAreaParameter", {"areaID": "1", "mowHeightLevel": 10, "cutMode": 7, "obstacleHeight": 1, "angle": 140})
    assert hass.states.get(SENTRUM).state == "130"
    await hass.services.async_call("number", "set_value", {"entity_id": SENTRUM, "value": 53}, blocking=True)
    assert mower.calls[-1][1]["angle"] == 217  # as seen on the real mower
    await hass.services.async_call(
        "number", "set_value", {"entity_id": f"{PREFIX}_sentrum_mowing_height", "value": 8}, blocking=True)
    assert mower.calls[-1][1]["mowHeightLevel"] == 1
    await hass.services.async_call(
        "select", "select_option", {"entity_id": f"{SELECT}_vestkanten_obstacle_avoidance_mode", "option": "high_grass"}, blocking=True)
    assert mower.calls[-1][1]["obstacleHeight"] == 3
    await hass.services.async_call("select", "select_option", {"entity_id": VEST_SPEED, "option": "delicate"}, blocking=True)
    assert mower.calls[-1] == (
        "setAreaParameter", {"areaID": "2", "mowHeightLevel": 9, "cutMode": 7, "obstacleHeight": 3, "angle": 0})
    assert hass.states.get(SENTRUM).state == "53"
    # One read of each, at start-up: no polling.
    assert [c[0] for c in mower.calls if c[0].startswith("get")] == ["getAreaSet", "getAreaParameter"]


async def test_rejected_change_errors_and_keeps_the_old_value(hass):
    from homeassistant.exceptions import HomeAssistantError
    entry, mower = await setup(hass)
    mower.accept = False
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("number", "set_value", {"entity_id": SENTRUM, "value": 90}, blocking=True)
    assert hass.states.get(SENTRUM).state == "90"


async def test_unknown_value_shows_unknown(hass):
    def odd_speed(mower):
        mower.areas["1"]["cutMode"] = 5
    await setup(hass, odd_speed)
    state = hass.states.get(f"{SELECT}_sentrum_mowing_speed")
    assert state.state == "unknown" and state.attributes["device_value"] == 5


async def test_unnamed_areas_fall_back(hass):
    def no_names(mower):
        mower.area_set = None
    await setup(hass, no_names)
    assert hass.states.get(f"{PREFIX}_area_1_direction").state == "90"


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
    assert hass.states.get(SENTRUM).state == "225"
    assert hass.states.get(f"{PREFIX}_sentrum_mowing_height").state == "4.5"
    assert hass.states.get(f"{PREFIX}_ostkanten_direction").state == "180"
    assert hass.states.get(f"{SELECT}_ostkanten_obstacle_avoidance_mode").state == "high_grass"
    assert hass.states.get(VEST_SPEED).state == "unavailable"  # area 2 has gone
    # The app sets one area over MQTT, as deebot_client's MQTT client hands it over.
    command_type = COMMANDS_WITH_MQTT_P2P_HANDLING[DataType.JSON]["setAreaParameter"]
    request = {"header": {}, "body": {"data": {
        "areaID": "4", "mowHeightLevel": 9, "cutMode": 4, "obstacleHeight": 3, "angle": 10}}}
    command = command_type.create_from_mqtt(orjson.dumps(request))
    command.handle_mqtt_p2p(mower.events, orjson.dumps({"header": {}, "body": {"code": 0, "msg": "ok"}}))
    await settle(hass)
    assert hass.states.get(f"{PREFIX}_ostkanten_direction").state == "260"
    assert hass.states.get(f"{SELECT}_ostkanten_mowing_speed").state == "quick"
    assert hass.states.get(SENTRUM).state == "225"  # other areas untouched
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
    assert hass.states.get(SENTRUM).state == "0"

    mower.capabilities.settings.area_parameter = object()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue("alex_hacks", f"area_settings_upstream_{entry.entry_id}")


async def test_removes_the_old_raw_number_entities(hass):
    from homeassistant.helpers import entity_registry as er
    entry, mower = await setup(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "number", "alex_hacks", f"{entry.data['ecovacs_device_id']}_area_1_area_speed", config_entry=entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    assert hass.states.get(f"{SELECT}_sentrum_mowing_speed").state == "delicate"
