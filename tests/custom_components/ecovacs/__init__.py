"""Fake Home Assistant Ecovacs integration: runtime_data.devices holds one fake GOAT mower.

The mower uses deebot_client's real EventBus and handles commands the way the cloud API answers.
"""
from types import SimpleNamespace

from deebot_client.event_bus import EventBus

DOMAIN = "ecovacs"


class FakeMower:
    def __init__(self):
        self.device_info = {"did": "E1", "name": "serial"}
        self.capabilities = SimpleNamespace(settings=SimpleNamespace())
        self.events = EventBus(self.execute_command, SimpleNamespace(get_refresh_commands=lambda event: []))
        self.stored = {"enable": 1, "start": "21:0", "end": "6:30"}
        self.calls = []
        self.accept = True

    async def execute_command(self, command):
        self.calls.append((command.NAME, command._args))
        if command.NAME == "getAnimProtect":
            response = {"ret": "ok", "resp": {"header": {}, "body": {"code": 0, "msg": "ok", "data": dict(self.stored)}}}
        else:
            if self.accept:
                self.stored = dict(command._args)
            response = {"ret": "ok", "resp": {"header": {}, "body": {"code": 0 if self.accept else 30000, "msg": "ok"}}}
        command._handle_response(self.events, response)
        return response


async def async_setup_entry(hass, entry):
    entry.runtime_data = SimpleNamespace(devices=[FakeMower()])
    return True


async def async_unload_entry(hass, entry):
    return True
