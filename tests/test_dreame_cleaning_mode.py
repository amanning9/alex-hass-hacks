import pytest
from homeassistant.config_entries import ConfigEntryState

from .helpers import add_hack, setup_dreame

ENTITY = "select.robbie_dreame_cleaning_mode_cleaning_mode_fixed"


async def setup(hass):
    dreame, coord = await setup_dreame(hass)
    await add_hack(hass, "dreame_cleaning_mode", dreame)
    return dreame, coord

async def test_offers_option_when_idle_even_if_dreame_list_lost_it(hass):
    dreame, coord = await setup(hass)
    dev = coord.device
    dev.status.cleaning_mode = 0  # left on sweeping after the sweep
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == "sweeping"
    assert state.attributes["options"] == ["sweeping", "mopping", "sweeping_and_mopping", "mopping_after_sweeping"]
    await hass.services.async_call("select", "select_option", {"entity_id": ENTITY, "option": "mopping_after_sweeping"}, blocking=True)
    assert dev.calls == [3]
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "mopping_after_sweeping"


async def test_mid_task_option_withheld(hass):
    dreame, coord = await setup(hass)
    dev = coord.device
    dev.status.cleaning_mode = 0
    dev.status.started = True
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert "mopping_after_sweeping" not in hass.states.get(ENTITY).attributes["options"]
    with pytest.raises(Exception):
        await hass.services.async_call("select", "select_option", {"entity_id": ENTITY, "option": "mopping_after_sweeping"}, blocking=True)
    assert dev.calls == []
    # Task ends: option comes straight back on the next update (no task-status change needed).
    dev.status.started = False
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert "mopping_after_sweeping" in hass.states.get(ENTITY).attributes["options"]


async def test_unavailable_rules_and_no_mop(hass):
    dreame, coord = await setup(hass)
    dev = coord.device
    dev.status.cleaning_mode = 0
    dev.status.water_tank_or_mop_installed = False
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert "mopping_after_sweeping" not in hass.states.get(ENTITY).attributes["options"]
    dev.status.fast_mapping = True
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unavailable"


async def test_follows_dreame_reload(hass):
    dreame, coord = await setup(hass)
    sidecar = hass.config_entries.async_entries("alex_hacks")[0]
    assert await hass.config_entries.async_reload(dreame.entry_id)
    await hass.async_block_till_done()
    assert sidecar.state is ConfigEntryState.LOADED
    new = hass.data["dreame_vacuum"][dreame.entry_id]
    assert new is not coord
    new.device.status.cleaning_mode = 1
    new.async_set_updated_data(new.device)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "mopping"
    # And again (watch re-registered).
    assert await hass.config_entries.async_reload(dreame.entry_id)
    await hass.async_block_till_done()
    assert sidecar.state is ConfigEntryState.LOADED
    assert hass.states.get(ENTITY).state == "mopping_after_sweeping"


async def test_removing_sidecar_then_reloading_dreame(hass):
    dreame, coord = await setup(hass)
    sidecar = hass.config_entries.async_entries("alex_hacks")[0]
    assert await hass.config_entries.async_remove(sidecar.entry_id)
    assert await hass.config_entries.async_reload(dreame.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY) is None



async def test_repair_when_newer_version_runs_a_week_without_bug(hass, freezer, monkeypatch):
    from datetime import timedelta
    from homeassistant.helpers import issue_registry as ir
    import custom_components.alex_hacks.hacks.dreame_cleaning_mode as sidecar_mod
    version = {"v": "v2.0.2"}

    async def fake_version(hass):
        return version["v"]

    monkeypatch.setattr(sidecar_mod, "dreame_version", fake_version)
    dreame, coord = await setup(hass)
    entry = hass.config_entries.async_entries("alex_hacks")[0]
    issue = lambda: ir.async_get(hass).async_get_issue("alex_hacks", f"probably_fixed_{entry.entry_id}")

    # Bug seen on v2.0.2 (Dreame's list lacks the option, ours offers it).
    dev = coord.device
    dev.status.cleaning_mode = 0
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert entry.data["bug_seen_version"] == "v2.0.2"

    # Daily checks on the same version: no repair.
    freezer.tick(timedelta(days=8))
    await sidecar_mod.async_check_fixed(hass, entry)
    assert issue() is None

    # Update to v2.0.3 (the fake now keeps the option): no repair until it's run a week.
    version["v"] = "v2.0.3"
    dev.status.cleaning_mode_list["mopping_after_sweeping"] = 3
    await sidecar_mod.async_check_fixed(hass, entry)
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    freezer.tick(timedelta(days=6))
    await sidecar_mod.async_check_fixed(hass, entry)
    assert issue() is None
    freezer.tick(timedelta(days=1, minutes=1))
    await sidecar_mod.async_check_fixed(hass, entry)
    assert issue() is not None
    assert issue().translation_placeholders["version"] == "v2.0.3"

    # Bug shows up again on v2.0.3: repair withdrawn.
    del dev.status.cleaning_mode_list["mopping_after_sweeping"]
    coord.async_set_updated_data(dev)
    await hass.async_block_till_done()
    assert issue() is None
    assert entry.data["bug_seen_version"] == "v2.0.3"
    await sidecar_mod.async_check_fixed(hass, entry)
    assert issue() is None


async def test_no_repair_if_bug_never_seen(hass, freezer, monkeypatch):
    from datetime import timedelta
    from homeassistant.helpers import issue_registry as ir
    import custom_components.alex_hacks.hacks.dreame_cleaning_mode as sidecar_mod
    version = {"v": "v2.0.2"}

    async def fake_version(hass):
        return version["v"]

    monkeypatch.setattr(sidecar_mod, "dreame_version", fake_version)
    dreame, coord = await setup(hass)
    coord.device.status.cleaning_mode_list["mopping_after_sweeping"] = 3
    entry = hass.config_entries.async_entries("alex_hacks")[0]
    version["v"] = "v2.0.3"
    freezer.tick(timedelta(days=30))
    await sidecar_mod.async_check_fixed(hass, entry)
    freezer.tick(timedelta(days=30))
    await sidecar_mod.async_check_fixed(hass, entry)
    assert ir.async_get(hass).async_get_issue("alex_hacks", f"probably_fixed_{entry.entry_id}") is None
