import asyncio
from datetime import timedelta

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component

from .helpers import add_hack, setup_dreame

ACTION = "alex_hacks.dreame_vacuum_then_mop"


@pytest.fixture
def fast(monkeypatch):
    import custom_components.alex_hacks.hacks.dreame_vacuum_then_mop as m
    monkeypatch.setattr(m, "DONE_HOLD", timedelta(seconds=1))
    monkeypatch.setattr(m, "START_TIMEOUT", timedelta(seconds=1))


async def setup(hass):
    calls = []
    for domain, service in [("select", "select_option"), ("switch", "turn_on"), ("switch", "turn_off"),
                            ("dreame_vacuum", "vacuum_clean_segment"), ("dreame_vacuum", "vacuum_set_custom_cleaning"),
                            ("notify", "done")]:
        hass.services.async_register(domain, service,
            lambda call, d=domain, s=service: calls.append((f"{d}.{s}", dict(call.data))))
    dreame, coord = await setup_dreame(hass)
    await add_hack(hass, "dreame_rooms", dreame)
    await add_hack(hass, "dreame_vacuum_then_mop", dreame)
    st = hass.states.async_set
    st("vacuum.robbie", "docked", {"selected_map": "Downstairs"})
    st("sensor.robbie_task_status", "completed")
    st("sensor.robbie_task_type", "unavailable")
    st("select.robbie_cleangenius", "routine_cleaning")
    st("switch.robbie_customized_cleaning", "off")
    return coord, calls


async def test_end_to_end(hass, fast, monkeypatch):
    import homeassistant.components.logbook as logbook
    entries = []
    monkeypatch.setattr(logbook, "async_log_entry", lambda hass, name, message, domain, entity_id, context: entries.append(message))
    hass.config.components.add("logbook")
    coord, calls = await setup(hass)
    st = hass.states.async_set
    del coord.device.status.current_segments[3]          # Lounge gone from the map
    coord.async_set_updated_data(coord.device)
    await hass.async_block_till_done()
    assert await async_setup_component(hass, "automation", {"automation": [{
        "id": "wrap", "alias": "wrap",
        "triggers": [{"trigger": "event", "event_type": "go_clean"}],
        "actions": [
            {"action": ACTION, "target": {"entity_id": "vacuum.robbie"}, "data": {"rooms": [
                {"room": "sensor.robbie_kitchen", "suction": "turbo", "wetness": 20, "passes": 2},
                {"room": "sensor.robbie_lounge", "suction": "strong"},
                {"room": "sensor.robbie_lounge_rug", "suction": "none"},
            ]}},
            {"action": "notify.done"},
        ],
    }]})
    hass.bus.async_fire("go_clean")
    await asyncio.sleep(0.2)
    assert [c[0] for c in calls] == ["select.select_option", "switch.turn_on",
        "dreame_vacuum.vacuum_set_custom_cleaning", "dreame_vacuum.vacuum_clean_segment"], calls
    assert calls[0][1] == {"entity_id": "select.robbie_cleangenius", "option": "off"}
    assert calls[2][1] == {"entity_id": "vacuum.robbie", "segment_id": [1], "cleaning_mode": [0],
                           "suction_level": [3], "repeats": [2]}
    assert calls[3][1] == {"entity_id": "vacuum.robbie", "segments": [1]}

    # Starts; mid-run return (completed + docked, task type still set): keeps waiting.
    st("sensor.robbie_task_type", "standard"); st("vacuum.robbie", "cleaning"); st("sensor.robbie_task_status", "cleaning")
    await asyncio.sleep(0.2)
    st("sensor.robbie_task_status", "completed"); st("vacuum.robbie", "docked", {"selected_map": "Downstairs"})
    await asyncio.sleep(1.5)
    assert len(calls) == 4
    st("sensor.robbie_task_status", "cleaning"); st("vacuum.robbie", "cleaning")
    await asyncio.sleep(0.2)
    # Done, but only briefly (blip back to running), then really done.
    st("sensor.robbie_task_status", "completed"); st("vacuum.robbie", "docked", {"selected_map": "Downstairs"})
    st("sensor.robbie_task_type", "unavailable")
    await asyncio.sleep(0.5)
    st("sensor.robbie_task_type", "standard")
    await asyncio.sleep(1.0)
    assert len(calls) == 4
    st("sensor.robbie_task_type", "unavailable")
    await asyncio.sleep(1.5)
    assert [c[0] for c in calls[4:]] == ["dreame_vacuum.vacuum_set_custom_cleaning",
        "dreame_vacuum.vacuum_clean_segment"], calls
    assert calls[4][1] == {"entity_id": "vacuum.robbie", "segment_id": [1, 5], "cleaning_mode": [1, 1],
                           "suction_level": [3, 1], "wetness_level": [20, 16], "repeats": [2, 1]}
    # Mop phase runs and finishes; only then does the automation's next step run.
    st("sensor.robbie_task_type", "standard"); st("vacuum.robbie", "cleaning"); st("sensor.robbie_task_status", "cleaning")
    await asyncio.sleep(0.2)
    assert calls[-1][0] != "notify.done"
    st("sensor.robbie_task_status", "completed"); st("vacuum.robbie", "docked", {"selected_map": "Downstairs"}); st("sensor.robbie_task_type", "unavailable")
    await asyncio.sleep(1.5)
    assert [c[0] for c in calls[6:]] == ["switch.turn_off", "select.select_option", "notify.done"], calls
    assert calls[6][1] == {"entity_id": "switch.robbie_customized_cleaning"}
    assert calls[7][1] == {"entity_id": "select.robbie_cleangenius", "option": "routine_cleaning"}

    # Logbook has the skipped room and each phase.
    assert entries == [
        "skipping rooms no longer on the map: sensor.robbie_lounge",
        "vacuum phase started (rooms 1)", "vacuum phase finished",
        "mop phase started (rooms 1, 5)", "mop phase finished",
        "restored switch.robbie_customized_cleaning, select.robbie_cleangenius",
    ], entries


async def test_errors(hass, fast):
    coord, calls = await setup(hass)
    with pytest.raises(ServiceValidationError, match="exactly one vacuum"):
        await hass.services.async_call("alex_hacks", "dreame_vacuum_then_mop",
            {"entity_id": [], "rooms": [{"room": "sensor.robbie_kitchen"}]}, blocking=True)
    hass.states.async_remove("sensor.robbie_task_type")
    with pytest.raises(ServiceValidationError, match="sensor.robbie_task_type"):
        await hass.services.async_call("alex_hacks", "dreame_vacuum_then_mop",
            {"entity_id": "vacuum.robbie", "rooms": [{"room": "sensor.robbie_kitchen"}]}, blocking=True)
    hass.states.async_set("sensor.robbie_task_type", "unavailable")
    with pytest.raises(ServiceValidationError, match="None of the rooms"):
        await hass.services.async_call("alex_hacks", "dreame_vacuum_then_mop",
            {"entity_id": "vacuum.robbie", "rooms": [{"room": "sensor.nope"}]}, blocking=True)
    # Robot never starts: clear error after the start timeout.
    with pytest.raises(HomeAssistantError, match="didn't start the vacuum phase"):
        await hass.services.async_call("alex_hacks", "dreame_vacuum_then_mop",
            {"entity_id": "vacuum.robbie", "rooms": [{"room": "sensor.robbie_kitchen"}]}, blocking=True)
    # Settings put back after the error too.
    assert [c[0] for c in calls[-2:]] == ["switch.turn_off", "select.select_option"], calls
    assert calls[-1][1]["option"] == "routine_cleaning"


async def test_restored_when_stopped_and_left_if_already_on(hass, fast):
    coord, calls = await setup(hass)
    # Already on before: not turned on, not turned off.
    hass.states.async_set("switch.robbie_customized_cleaning", "on")
    hass.states.async_set("select.robbie_cleangenius", "off")
    task = hass.async_create_task(hass.services.async_call("alex_hacks", "dreame_vacuum_then_mop",
        {"entity_id": "vacuum.robbie", "rooms": [{"room": "sensor.robbie_kitchen"}]}, blocking=True))
    await asyncio.sleep(0.2)
    hass.states.async_set("sensor.robbie_task_type", "standard")
    await asyncio.sleep(0.2)
    task.cancel()                                   # the calling automation is stopped
    with pytest.raises(asyncio.CancelledError):
        await task
    names = [c[0] for c in calls]
    assert "switch.turn_on" not in names and "switch.turn_off" not in names, calls
    assert not [c for c in calls if c[1].get("entity_id") == "select.robbie_cleangenius"], calls

    # Was off: stopped mid-clean still turns it back off.
    calls.clear()
    hass.states.async_set("switch.robbie_customized_cleaning", "off")
    hass.states.async_set("sensor.robbie_task_type", "unavailable")
    task = hass.async_create_task(hass.services.async_call("alex_hacks", "dreame_vacuum_then_mop",
        {"entity_id": "vacuum.robbie", "rooms": [{"room": "sensor.robbie_kitchen"}]}, blocking=True))
    await asyncio.sleep(0.2)
    hass.states.async_set("sensor.robbie_task_type", "standard")
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls[-1] == ("switch.turn_off", {"entity_id": "switch.robbie_customized_cleaning"}), calls


async def test_bad_fields_rejected(hass):
    await setup(hass)
    import voluptuous as vol
    with pytest.raises(vol.Invalid):
        await hass.services.async_call("alex_hacks", "dreame_vacuum_then_mop",
            {"entity_id": "vacuum.robbie", "rooms": [{"room": "sensor.robbie_kitchen", "wetness": 40}]}, blocking=True)


async def test_action_form_loads(hass):
    import json, pathlib
    from homeassistant.helpers.selector import selector
    from homeassistant.util.yaml import load_yaml
    base = pathlib.Path(__file__).parent.parent / "custom_components/alex_hacks"
    desc = load_yaml(str(base / "services.yaml"))["dreame_vacuum_then_mop"]
    assert set(desc["fields"]) == {"rooms", "max_duration"}
    for field in desc["fields"].values():
        selector(field["selector"])  # raises if the selector config is invalid
    rooms = desc["fields"]["rooms"]["selector"]["object"]
    for sub in rooms["fields"].values():
        selector(sub["selector"])
    strings = json.loads((base / "strings.json").read_text())["services"]["dreame_vacuum_then_mop"]
    assert set(strings["fields"]) == set(desc["fields"])


async def test_refuses_on_another_map(hass, fast):
    coord, calls = await setup(hass)
    dev = coord.device
    dev.status.selected_map = dev.status.map_data_list[20]       # carried upstairs
    coord.async_set_updated_data(dev)
    hass.states.async_set("vacuum.robbie", "docked", {"selected_map": "Upstairs"})
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError, match="vacuum.robbie is on map 'Upstairs', not 'Downstairs'"):
        await hass.services.async_call("alex_hacks", "dreame_vacuum_then_mop",
            {"entity_id": "vacuum.robbie", "rooms": [{"room": "sensor.robbie_kitchen"}]}, blocking=True)
    assert calls == []      # nothing changed on the robot


async def test_action_only_while_hack_added(hass):
    dreame, coord = await setup_dreame(hass)
    await add_hack(hass, "dreame_rooms", dreame)
    assert not hass.services.has_service("alex_hacks", "dreame_vacuum_then_mop")
    entry = await add_hack(hass, "dreame_vacuum_then_mop", dreame)
    assert entry.title == "Robbie: Dreame vacuum then mop"
    assert hass.services.has_service("alex_hacks", "dreame_vacuum_then_mop")
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service("alex_hacks", "dreame_vacuum_then_mop")
