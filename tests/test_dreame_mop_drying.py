from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .helpers import add_hack, setup_dreame

SWITCH = "switch.robbie_allow_drying"
SENSOR = "sensor.robbie_mop_drying"
NUMBER = "number.robbie_mop_drying_time"
STOP = "self_wash_base 3,0"


async def setup(hass):
    dreame, coord = await setup_dreame(hass)
    entry = await add_hack(hass, "dreame_mop_drying", dreame)
    return entry, coord


async def push(hass, coord):
    coord.async_set_updated_data(coord.device)
    await hass.async_block_till_done()


async def tick(hass, freezer: FrozenDateTimeFactory, minutes):
    """Move time on, firing the hack's 30-second check along the way."""
    for _ in range(int(minutes * 2)):
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()


async def wash(hass, coord):
    dev = coord.device
    dev.status.washing, dev.status.drying_available = True, False
    await push(hass, coord)
    dev.status.washing, dev.status.drying_available = False, True
    await push(hass, coord)


async def settle(hass, freezer):
    """Long enough for the base to count as quiet (3 minutes), so drying can start."""
    await tick(hass, freezer, 3.5)


async def test_dries_for_full_time_then_stops(hass, freezer):
    entry, coord = await setup(hass)
    dev = coord.device
    assert hass.states.get(SENSOR).state == "dry"
    assert hass.states.get(SWITCH).state == "on"
    assert hass.states.get(NUMBER).state == "180"
    await wash(hass, coord)
    assert dev.calls == []  # not straight after the wash
    await settle(hass, freezer)
    assert dev.calls == ["start_drying"]
    assert hass.states.get(SENSOR).state == "drying"
    await tick(hass, freezer, 90)
    assert 89 <= hass.states.get(SENSOR).attributes["dried_minutes"] <= 90
    await tick(hass, freezer, 91)
    assert dev.calls == ["start_drying", STOP]
    assert hass.states.get(SENSOR).state == "dry"
    assert hass.states.get(SENSOR).attributes["minutes_left"] == 0


async def test_pause_and_resume(hass, freezer):
    entry, coord = await setup(hass)
    dev = coord.device
    await wash(hass, coord)
    await settle(hass, freezer)
    await tick(hass, freezer, 60)
    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()
    assert dev.calls == ["start_drying", STOP]
    assert hass.states.get(SENSOR).state == "paused"
    await tick(hass, freezer, 300)  # paused time doesn't count
    left = hass.states.get(SENSOR).attributes["minutes_left"]
    assert 119 <= left <= 121
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()
    assert dev.calls[-1] == "start_drying"
    await tick(hass, freezer, 121)
    assert dev.calls[-1] == STOP
    assert hass.states.get(SENSOR).state == "dry"


async def test_waits_for_robot_and_new_wash_resets(hass, freezer):
    entry, coord = await setup(hass)
    dev = coord.device
    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    # Drying starts after a mop (e.g. from the app): stopped, since drying isn't allowed.
    await wash(hass, coord)
    dev.status.drying = True
    await push(hass, coord)
    assert dev.calls == [STOP]
    dev.status.drying_available = False  # out cleaning
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    await tick(hass, freezer, 10)
    assert hass.states.get(SENSOR).state == "waiting"
    assert dev.calls == [STOP]
    dev.status.drying_available = True  # back on the dock
    await tick(hass, freezer, 1)
    assert dev.calls == [STOP]
    await settle(hass, freezer)
    assert dev.calls == [STOP, "start_drying"]
    await tick(hass, freezer, 100)
    await wash(hass, coord)  # mopped again: back to a full drying time
    assert hass.states.get(SENSOR).attributes["minutes_left"] == 180


async def test_progress_survives_reload(hass, freezer):
    entry, coord = await setup(hass)
    dev = coord.device
    await wash(hass, coord)
    await settle(hass, freezer)
    await tick(hass, freezer, 30)
    await hass.services.async_call("number", "set_value", {"entity_id": NUMBER, "value": 120}, blocking=True)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    # The robot kept drying while it was reloading.
    await tick(hass, freezer, 30)
    state = hass.states.get(SENSOR)
    assert hass.states.get(NUMBER).state == "120"
    assert state.state == "drying" and 59 <= state.attributes["dried_minutes"] <= 60


async def test_stops_even_when_dreame_thinks_it_cant_and_repairs_if_stuck(hass, freezer):
    from homeassistant.helpers import issue_registry as ir

    entry, coord = await setup(hass)
    dev = coord.device
    await wash(hass, coord)
    await settle(hass, freezer)
    # Dreame's own stop would do nothing here (e.g. it thinks a task is still running).
    dev.status.drying_available = False
    dev.ignore_stop = True
    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()
    assert dev.calls == ["start_drying", STOP]
    issue_id = f"drying_wont_stop_{entry.entry_id}"
    await tick(hass, freezer, 5)
    assert dev.calls.count(STOP) == 3
    assert ir.async_get(hass).async_get_issue("alex_hacks", issue_id) is None
    await tick(hass, freezer, 2)
    assert dev.calls.count(STOP) == 4
    assert ir.async_get(hass).async_get_issue("alex_hacks", issue_id) is not None
    dev.ignore_stop = False  # it listens at last
    await tick(hass, freezer, 2)
    assert hass.states.get(SENSOR).state == "paused"
    assert ir.async_get(hass).async_get_issue("alex_hacks", issue_id) is None


async def test_wet_as_soon_as_it_starts_mopping(hass, freezer):
    entry, coord = await setup(hass)
    dev = coord.device
    dev.status.cleaning_mode = 0  # sweeping only: stays dry
    dev.status.started, dev.status.drying_available = True, False
    await push(hass, coord)
    assert hass.states.get(SENSOR).state == "dry"
    dev.status.cleaning_mode = 2  # sweeping and mopping
    await push(hass, coord)
    assert hass.states.get(SENSOR).state == "waiting"
    assert dev.calls == []  # out cleaning: nothing started
    dev.status.started, dev.status.drying_available = False, True  # home, job finished
    # The gap before the base washes the mop: drying must not take it.
    await tick(hass, freezer, 3)
    assert dev.calls == []
    await wash(hass, coord)
    await settle(hass, freezer)
    assert dev.calls == ["start_drying"]


async def test_washes_an_unwashed_mop_first_waiting_for_water(hass, freezer):
    from types import SimpleNamespace

    entry, coord = await setup(hass)
    dev = coord.device
    # Mopped a floor without the base, then carried home: no wash comes.
    dev.status.cleaning_mode = 1
    dev.status.started, dev.status.drying_available = True, False
    await push(hass, coord)
    dev.status.started, dev.status.drying_available = False, True
    dev.status.low_water_warning = SimpleNamespace(name="NO_WATER_FOR_CLEAN")
    await tick(hass, freezer, 20)
    assert dev.calls == []
    assert hass.states.get(SENSOR).state == "needs_water"
    dev.status.low_water_warning = SimpleNamespace(name="NO_WARNING")  # refilled
    await tick(hass, freezer, 1)
    assert dev.calls == ["start_washing"]
    dev.status.washing, dev.status.drying_available = False, True  # wash done
    await push(hass, coord)
    await settle(hass, freezer)
    assert dev.calls == ["start_washing", "start_drying"]


async def test_waits_while_any_status_says_washing(hass, freezer):
    from types import SimpleNamespace

    entry, coord = await setup(hass)
    dev = coord.device
    await wash(hass, coord)
    dev.status.state = SimpleNamespace(name="RETURNING_TO_WASH")
    await tick(hass, freezer, 10)
    assert dev.calls == []
    dev.status.state = SimpleNamespace(name="CHARGING_COMPLETED")
    dev.status.self_wash_base_status = SimpleNamespace(name="ADDING_WATER")
    await tick(hass, freezer, 10)
    assert dev.calls == []
    dev.status.self_wash_base_status = SimpleNamespace(name="IDLE")
    await settle(hass, freezer)
    assert dev.calls == ["start_drying"]


async def test_doesnt_interrupt_the_base(hass, freezer):
    entry, coord = await setup(hass)
    dev = coord.device
    dev.status.auto_emptying = True
    await wash(hass, coord)
    assert dev.calls == []
    assert hass.states.get(SENSOR).state == "waiting"
    dev.status.auto_emptying = False
    await settle(hass, freezer)
    assert dev.calls == ["start_drying"]


async def test_hands_over_to_robots_auto_drying(hass, freezer):
    from homeassistant.helpers import issue_registry as ir

    entry, coord = await setup(hass)
    dev = coord.device
    issue_id = f"auto_drying_on_{entry.entry_id}"
    dev.status.auto_drying = True
    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    await wash(hass, coord)
    assert hass.states.get(SENSOR).state == "robot_auto_drying"
    assert ir.async_get(hass).async_get_issue("alex_hacks", issue_id) is not None
    dev.status.drying = True  # the robot dries by itself: left alone, but still counted
    await tick(hass, freezer, 60)
    assert dev.calls == []
    assert 59 <= hass.states.get(SENSOR).attributes["dried_minutes"] <= 60
    dev.status.auto_drying = False  # turned off in the app: take over (drying isn't allowed)
    await push(hass, coord)
    assert dev.calls == [STOP]
    assert ir.async_get(hass).async_get_issue("alex_hacks", issue_id) is None
    assert hass.states.get(SENSOR).state == "paused"


async def test_repair_turns_auto_drying_off(hass, freezer):
    from homeassistant.components.repairs import repairs_flow_manager
    from homeassistant.components.repairs.issue_handler import async_process_repairs_platforms
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "repairs", {})
    entry, coord = await setup(hass)
    dev = coord.device
    dev.status.auto_drying = True
    await wash(hass, coord)
    await settle(hass, freezer)
    issue_id = f"auto_drying_on_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue("alex_hacks", issue_id).is_fixable
    await async_process_repairs_platforms(hass)
    flows = repairs_flow_manager(hass)
    result = await flows.async_init("alex_hacks", data={"issue_id": issue_id})
    assert result["step_id"] == "confirm"
    result = await flows.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert dev.calls[:2] == ["set AUTO_DRYING 0", "start_drying"]  # took over at once
    assert ir.async_get(hass).async_get_issue("alex_hacks", issue_id) is None
