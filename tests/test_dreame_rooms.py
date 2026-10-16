from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from .helpers import add_hack, setup_dreame


async def test_room_sensors(hass):
    dreame, coord = await setup_dreame(hass)
    entry = await add_hack(hass, "dreame_rooms", dreame)
    assert entry.title == "Robbie: Dreame rooms"
    reg = er.async_get(hass)
    rooms = {e.entity_id: e for e in er.async_entries_for_config_entry(reg, entry.entry_id)}
    assert sorted(rooms) == ["sensor.robbie_kitchen", "sensor.robbie_lounge", "sensor.robbie_lounge_rug"]
    lounge = hass.states.get("sensor.robbie_lounge")
    assert lounge.state == "3" and lounge.attributes["room_id"] == 3
    assert lounge.attributes["friendly_name"] == "Robbie Lounge"
    assert hass.states.get("sensor.robbie_lounge_rug").state == "5"

    # A new room appears; a room disappears (goes unavailable, keeps its entity).
    dev = coord.device
    dev.status.current_segments[7] = SimpleNamespace(name="Hall", custom_name=None, icon=None)
    del dev.status.current_segments[1]
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.robbie_hall").state == "7"
    assert hass.states.get("sensor.robbie_kitchen").state == "unavailable"

    # Renamed in the app: friendly name follows, entity ID stays.
    dev.status.current_segments[3].custom_name = "Sitting room"
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.robbie_lounge").attributes["friendly_name"] == "Robbie Sitting room"


async def test_follows_dreame_reload_and_duplicate_abort(hass):
    dreame, coord = await setup_dreame(hass)
    entry = await add_hack(hass, "dreame_rooms", dreame)
    assert await hass.config_entries.async_reload(dreame.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.robbie_kitchen").state == "1"
    # Same hack for the same vacuum again: refused.
    result = await hass.config_entries.flow.async_init("alex_hacks", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"hack": ["dreame_rooms"]})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"dreame_entry_id": dreame.entry_id})
    assert result["type"] == "abort" and result["reason"] == "already_configured"


async def test_tick_several_hacks(hass):
    dreame, coord = await setup_dreame(hass)
    flow = hass.config_entries.flow
    result = await flow.async_init("alex_hacks", context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"hack": []})
    assert result["type"] == "form" and result["errors"] == {"hack": "none_selected"}
    result = await flow.async_configure(result["flow_id"], {"hack": ["dreame_rooms", "dreame_cleaning_mode"]})
    assert result["step_id"] == "configure"
    assert set(result["data_schema"].schema) == {"dreame_entry_id"}       # asked once
    text = result["description_placeholders"]["hacks"]
    assert "### Dreame cleaning mode" in text and "### Dreame rooms" in text
    result = await flow.async_configure(result["flow_id"], {"dreame_entry_id": dreame.entry_id})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    entries = hass.config_entries.async_entries("alex_hacks")
    assert sorted(e.data["hack"] for e in entries) == ["dreame_cleaning_mode", "dreame_rooms"]
    assert all(e.state is ConfigEntryState.LOADED for e in entries)
    assert hass.states.get("select.robbie_cleaning_mode_fixed") is not None
    assert hass.states.get("sensor.robbie_kitchen").state == "1"

    # Removing one leaves the other.
    rooms = next(e for e in entries if e.data["hack"] == "dreame_rooms")
    assert await hass.config_entries.async_remove(rooms.entry_id)
    await hass.async_block_till_done()
    assert [e.data["hack"] for e in hass.config_entries.async_entries("alex_hacks")] == ["dreame_cleaning_mode"]

    # Tick both again: only the missing one is added.
    result = await flow.async_init("alex_hacks", context={"source": "user"})
    result = await flow.async_configure(result["flow_id"], {"hack": ["dreame_rooms", "dreame_cleaning_mode"]})
    result = await flow.async_configure(result["flow_id"], {"dreame_entry_id": dreame.entry_id})
    assert result["type"] == "create_entry" and result["result"].data["hack"] == "dreame_rooms"
    await hass.async_block_till_done()
    assert len(hass.config_entries.async_entries("alex_hacks")) == 2


async def test_no_dreame(hass):
    result = await hass.config_entries.flow.async_init("alex_hacks", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"hack": ["dreame_rooms"]})
    assert result["type"] == "abort" and result["reason"] == "no_dreame"


async def test_missing_room_repair(hass):
    from homeassistant.helpers import issue_registry as ir
    dreame, coord = await setup_dreame(hass)
    entry = await add_hack(hass, "dreame_rooms", dreame)
    issue = lambda: ir.async_get(hass).async_get_issue("alex_hacks", f"room_missing_{entry.entry_id}_5")
    dev = coord.device
    rug = dev.status.current_segments.pop(5)
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.robbie_lounge_rug").state == "unavailable"
    assert issue() is not None
    assert issue().translation_placeholders == {
        "room": "Lounge rug", "room_id": "5", "entity_id": "sensor.robbie_lounge_rug", "vacuum": "Robbie"}
    # Comes back: repair cleared.
    dev.status.current_segments[5] = rug
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert issue() is None and hass.states.get("sensor.robbie_lounge_rug").state == "5"
    # Map empty (e.g. still loading): no repairs.
    saved = dict(dev.status.current_segments)
    dev.status.current_segments.clear()
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert issue() is None
    dev.status.current_segments.update(saved)

    # Gone while Home Assistant was off: back as unavailable with a repair after a reload.
    del dev.status.current_segments[5]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.robbie_lounge_rug")
    assert state.state == "unavailable"
    assert issue() is not None and issue().translation_placeholders["room"] == "Lounge rug"

    # Removing the hack clears its repairs.
    assert await hass.config_entries.async_remove(entry.entry_id)
    assert issue() is None


async def test_other_map(hass):
    from homeassistant.helpers import issue_registry as ir
    dreame, coord = await setup_dreame(hass)
    entry = await add_hack(hass, "dreame_rooms", dreame)
    assert entry.data["map_id"] == 10 and entry.title == "Robbie: Dreame rooms"
    kitchen = hass.states.get("sensor.robbie_kitchen")
    assert kitchen.attributes["map_name"] == "Downstairs" and kitchen.attributes["on_current_map"] is True
    # Carried upstairs: same entities, still available, no repairs, no upstairs rooms added.
    dev = coord.device
    dev.status.selected_map = dev.status.map_data_list[20]
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    kitchen = hass.states.get("sensor.robbie_kitchen")
    assert kitchen.state == "1" and kitchen.attributes["friendly_name"] == "Robbie Kitchen"
    assert kitchen.attributes["on_current_map"] is False
    assert hass.states.get("sensor.robbie_bedroom") is None
    assert not [i for i in ir.async_get(hass).issues if i[0] == "alex_hacks"]
    # Restart while upstairs: map remembered, rooms unchanged.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data["map_id"] == 10
    assert hass.states.get("sensor.robbie_kitchen").state == "1"
    assert hass.states.get("sensor.robbie_bedroom") is None
