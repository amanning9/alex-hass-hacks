from homeassistant.config_entries import ConfigFlow


class FakeFlow(ConfigFlow, domain="dreame_vacuum"):
    VERSION = 1
