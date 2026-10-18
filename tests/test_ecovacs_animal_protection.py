import asyncio

import orjson
import pytest
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

SWITCH = "switch.goaty_ecovacs_animal_protection_animal_protection"
START = "time.goaty_ecovacs_animal_protection_animal_protection_start"
END = "time.goaty_ecovacs_animal_protection_animal_protection_end"


async def settle(hass):
    """Let deebot_client's event bus deliver (its tasks aren't Home Assistant's)."""
    for _ in range(5):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


async def setup(hass):
    ecovacs = MockConfigEntry(domain="ecovacs", title="Ecovacs")
    ecovacs.add_to_hass(hass)
    assert await hass.config_entries.async_setup(ecovacs.entry_id)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=ecovacs.entry_id, identifiers={("ecovacs", "E1")}, name="Goaty")
    result = await hass.config_entries.flow.async_init("alex_hacks", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"hack": ["ecovacs_animal_protection"]})
    assert result["step_id"] == "configure" and "**Remove when:**" in result["description_placeholders"]["hacks"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"ecovacs_device_id": device.id})
    assert result["type"] == "create_entry", result
    await settle(hass)
    entry = hass.config_entries.async_get_entry(result["result"].entry_id)
    return entry, ecovacs.runtime_data.devices[0]


async def test_reads_and_sets(hass):
    entry, mower = await setup(hass)
    assert entry.title == "Goaty: Ecovacs animal protection"
    assert hass.states.get(SWITCH).state == "on"
    assert hass.states.get(START).state == "21:00:00" and hass.states.get(END).state == "06:30:00"
    await hass.services.async_call("time", "set_value", {"entity_id": START, "time": "19:45"}, blocking=True)
    assert mower.calls[-1] == ("setAnimProtect", {"enable": 1, "start": "19:45", "end": "06:30"})
    assert hass.states.get(START).state == "19:45:00"
    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    assert mower.calls[-1] == ("setAnimProtect", {"enable": 0, "start": "19:45", "end": "06:30"})
    assert hass.states.get(SWITCH).state == "off"
    # Only one read, at start-up: no polling.
    assert [c for c in mower.calls if c[0] == "getAnimProtect"] == [("getAnimProtect", {})]


async def test_rejected_change_errors_and_keeps_the_old_value(hass):
    from homeassistant.exceptions import HomeAssistantError
    entry, mower = await setup(hass)
    mower.accept = False
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("time", "set_value", {"entity_id": END, "time": "08:00"}, blocking=True)
    assert hass.states.get(END).state == "06:30:00"


async def test_app_changes_arrive_without_polling(hass):
    from deebot_client.commands import COMMANDS_WITH_MQTT_P2P_HANDLING
    from deebot_client.const import DataType
    entry, mower = await setup(hass)
    # What deebot_client's MQTT client does with the app's set request and the mower's reply.
    command_type = COMMANDS_WITH_MQTT_P2P_HANDLING[DataType.JSON]["setAnimProtect"]
    request = {"header": {}, "body": {"data": {"enable": 1, "start": "18:15", "end": "7:5"}}}
    command = command_type.create_from_mqtt(orjson.dumps(request))
    command.handle_mqtt_p2p(mower.events, orjson.dumps({"header": {}, "body": {"code": 0, "msg": "ok"}}))
    await settle(hass)
    assert hass.states.get(START).state == "18:15:00" and hass.states.get(END).state == "07:05:00"
    # Unloading takes the registration away again.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert "setAnimProtect" not in COMMANDS_WITH_MQTT_P2P_HANDLING[DataType.JSON]


async def test_rereads_when_back_online_and_repairs_when_upstream_has_it(hass):
    from deebot_client.events import AvailabilityEvent
    entry, mower = await setup(hass)
    mower.events.notify(AvailabilityEvent(available=False))
    await settle(hass)
    mower.stored = {"enable": 0, "start": "22:0", "end": "5:0"}  # changed while offline
    mower.events.notify(AvailabilityEvent(available=True))
    await settle(hass)
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(START).state == "22:00:00"

    mower.capabilities.settings.animal_protection = object()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue("alex_hacks", f"animal_protection_upstream_{entry.entry_id}")
