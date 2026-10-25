from homeassistant.config_entries import ConfigFlow


class FakeFlow(ConfigFlow, domain="velair"):
    VERSION = 1
