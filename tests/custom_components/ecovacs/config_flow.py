from homeassistant.config_entries import ConfigFlow


class FakeFlow(ConfigFlow, domain="ecovacs"):
    VERSION = 1
