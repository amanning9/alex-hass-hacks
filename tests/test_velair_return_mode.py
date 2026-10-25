from datetime import timedelta

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

SELECT = "select.velair_return_mode"


async def setup(hass):
    velair = MockConfigEntry(domain="velair", title="Velair")
    velair.add_to_hass(hass)
    assert await hass.config_entries.async_setup(velair.entry_id)
    result = await hass.config_entries.flow.async_init("alex_hacks", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"hack": ["velair_return_mode"]})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    return hass.config_entries.async_get_entry(result["result"].entry_id), velair.runtime_data


async def select(hass, option):
    await hass.services.async_call("select", "select_option", {"entity_id": SELECT, "option": option}, blocking=True)


async def test_shows_and_sets_the_return_mode(hass):
    entry, velair = await setup(hass)
    state = hass.states.get(SELECT)
    assert state.state == "Default"
    assert state.attributes["options"] == ["Default", "Guests in Purple", "Away"]
    await select(hass, "Guests in Purple")
    assert velair.storage.data["planned_return"]["destination_mode_id"] == "mode_guests_purple"
    assert velair.storage.data["planned_return"]["away_mode_id"] == "mode_away"  # rest untouched
    assert hass.states.get(SELECT).state == "Guests in Purple"
    await select(hass, "Guests in Purple")  # already set: nothing sent
    assert [c[0] for c in velair.scheduler.calls] == ["update"]
    # Changed in Velair's panel: shows up at the next Velair event.
    velair.storage.data["planned_return"]["destination_mode_id"] = "default"
    hass.bus.async_fire("velair_event", {"event": "zone_control_changed"})
    await hass.async_block_till_done()
    assert hass.states.get(SELECT).state == "Default"


@pytest.mark.parametrize("state", ["armed", "away"])
async def test_plans_a_current_return_again(hass, state):
    entry, velair = await setup(hass)
    return_at = (dt_util.now() + timedelta(days=2)).isoformat()
    velair.storage.data["planned_return_episode"] = {"state": state, "return_at": return_at,
                                                     "destination_mode_id": "default"}
    await select(hass, "Guests in Purple")
    # Away stays away (no waiting for absence again); a pending one stays pending.
    assert velair.scheduler.calls[-1] == ("start", return_at, state == "away")
    episode = velair.storage.data["planned_return_episode"]
    assert episode["state"] == state and episode["destination_mode_id"] == "mode_guests_purple"


async def test_leaves_a_finished_return_alone(hass):
    entry, velair = await setup(hass)
    velair.storage.data["planned_return_episode"] = {
        "state": "fulfilled", "return_at": (dt_util.now() - timedelta(hours=1)).isoformat(),
        "destination_mode_id": "default"}
    await select(hass, "Guests in Purple")
    assert [c[0] for c in velair.scheduler.calls] == ["update"]


async def test_refuses_while_velair_is_migrating(hass):
    entry, velair = await setup(hass)
    velair.scheduler.temperature_migration_blocked = True
    hass.bus.async_fire("velair_event", {"event": "x"})
    await hass.async_block_till_done()
    assert hass.states.get(SELECT).state == "unavailable"


async def test_only_loads_on_checked_velair_versions(hass, monkeypatch):
    import custom_components.alex_hacks.hacks.velair_return_mode as hack

    async def newer(hass):
        return "1.9.0-beta.6"

    monkeypatch.setattr(hack, "velair_version", newer)
    velair = MockConfigEntry(domain="velair", title="Velair")
    velair.add_to_hass(hass)
    assert await hass.config_entries.async_setup(velair.entry_id)
    result = await hass.config_entries.flow.async_init("alex_hacks", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"hack": ["velair_return_mode"]})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(result["result"].entry_id)
    from homeassistant.config_entries import ConfigEntryState
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert hass.states.get(SELECT) is None
    issue = ir.async_get(hass).async_get_issue("alex_hacks", f"velair_version_{entry.entry_id}")
    assert issue and issue.translation_placeholders["version"] == "1.9.0-beta.6"
    assert velair.runtime_data.scheduler.calls == []
    # Back on a checked version: loads, and the repair goes.
    monkeypatch.undo()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(SELECT).state == "Default"
    assert ir.async_get(hass).async_get_issue("alex_hacks", f"velair_version_{entry.entry_id}") is None


async def test_unknown_mode_is_an_error(hass):
    entry, velair = await setup(hass)
    with pytest.raises(Exception):
        await select(hass, "Nope")
    assert velair.scheduler.calls == []
