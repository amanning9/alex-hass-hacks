"""Fake dreame_vacuum: a coordinator holding a stub device that reproduces the bug."""
from enum import Enum
from types import SimpleNamespace
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

DOMAIN = "dreame_vacuum"
NAMES = {0: "sweeping", 1: "mopping", 2: "sweeping_and_mopping", 3: "mopping_after_sweeping"}

# Same shape as dreame.types.PROPERTY_AVAILABILITY (looked up via the device's module).
PROPERTY_AVAILABILITY = {"CLEANING_MODE": lambda device: not device.status.fast_mapping}


class DreameVacuumAutoSwitchProperty(Enum):
    AUTO_DRYING = "AutoDry"


class FakeStatus:
    def __init__(self):
        self.cleaning_mode = 3
        self.started = False
        self.fast_mapping = False
        self.water_tank_or_mop_installed = True
        # Self-wash base: washing / drying, and whether drying could start now.
        self.washing = False
        self.drying = False
        self.drying_available = True
        # The bug: the integration's list has lost mopping_after_sweeping.
        self.cleaning_mode_list = {"sweeping": 0, "mopping": 1, "sweeping_and_mopping": 2}
        # Two saved maps; room numbers are only unique within a map.
        self.current_segments = {
            1: SimpleNamespace(name="Kitchen", custom_name=None, icon="mdi:stove"),
            3: SimpleNamespace(name="Living Room", custom_name="Lounge", icon=None),
            5: SimpleNamespace(name="Room 5", custom_name="Lounge rug", icon=None),
        }
        self.map_data_list = {
            10: SimpleNamespace(map_id=10, map_name="Downstairs", segments=self.current_segments),
            20: SimpleNamespace(map_id=20, map_name="Upstairs", segments={
                1: SimpleNamespace(name="Bedroom", custom_name=None, icon=None),
                2: SimpleNamespace(name="Bathroom", custom_name=None, icon=None),
            }),
        }
        self.selected_map = self.map_data_list[10]

    @property
    def cleaning_mode_name(self):
        return NAMES[self.cleaning_mode]

    @property
    def mopping_after_sweeping(self):
        return self.cleaning_mode == 3


class FakeDevice:
    def __init__(self):
        self.mac = "AA:BB"
        self.device_connected = True
        self.capability = SimpleNamespace(mopping_after_sweeping=True, self_wash_base=True)
        self.status = FakeStatus()
        self.calls = []
        self.info = SimpleNamespace(version=2000)
        self.ignore_stop = False  # robot that doesn't stop drying when told

    def set_cleaning_mode(self, mode):
        if mode == 3 and self.status.started and self.status.cleaning_mode != 3:
            raise ValueError("unavailable")
        self.calls.append(mode)
        self.status.cleaning_mode = mode

    def start_drying(self):
        self.calls.append("start_drying")
        self.status.drying = True

    def stop_drying(self):
        self.calls.append("stop_drying")
        self.status.drying = False

    def start_washing(self):
        self.calls.append("start_washing")
        self.status.washing, self.status.drying_available = True, False

    def set_property(self, prop, value):
        self.calls.append(f"set {prop.name} {value}")
        if prop is DreameVacuumAutoSwitchProperty.AUTO_DRYING:
            self.status.auto_drying = bool(value)

    def start_self_wash_base(self, parameters):
        self.calls.append(f"self_wash_base {parameters}")
        if parameters == "3,0" and not self.ignore_stop:
            self.status.drying = False
        return {"code": 0}


class FakeCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(hass, __import__("logging").getLogger(__name__), name="fake", config_entry=entry)
        self.device = FakeDevice()


async def async_setup_entry(hass, entry):
    coordinator = FakeCoordinator(hass, entry)
    coordinator.async_set_updated_data(coordinator.device)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return True


async def async_unload_entry(hass, entry):
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
