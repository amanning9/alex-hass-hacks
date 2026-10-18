"""Fake Home Assistant Ecovacs integration: runtime_data.devices holds one fake GOAT mower.

The mower uses deebot_client's real EventBus and handles commands the way the cloud API answers.
"""
from types import SimpleNamespace

from deebot_client.event_bus import EventBus

DOMAIN = "ecovacs"

# getAreaSet's subsets from DeebotUniverse/client.py PR #1774: areas 4 "Østkanten", 1 "Sentrum",
# 2 "Vestkanten".
AREA_SET = "XQAABACRAAAAAC2WwEIAXhHX9FS5sGj0QZjR5C9LwgskaRv7NNnlwYLfW5cqizd8DELGgHRrytM6XLeHVxHTtCZRrB5hCVldTC+NInBErb3e8FmImM2Df07MYA=="


class FakeMower:
    def __init__(self):
        self.device_info = {"did": "E1", "name": "serial"}
        self.capabilities = SimpleNamespace(settings=SimpleNamespace())
        self.events = EventBus(self.execute_command, SimpleNamespace(get_refresh_commands=lambda event: []))
        self.stored = {"enable": 1, "start": "21:0", "end": "6:30"}
        self.areas = {
            "1": {"mowHeightLevel": 10, "cutMode": 7, "obstacleHeight": 1, "angle": 180},
            "2": {"mowHeightLevel": 9, "cutMode": 4, "obstacleHeight": 2, "angle": 0},
        }
        self.area_set = AREA_SET
        self.calls = []
        self.accept = True

    async def execute_command(self, command):
        self.calls.append((command.NAME, command._args))
        data = None
        if command.NAME == "getAnimProtect":
            data = dict(self.stored)
        elif command.NAME == "getAreaParameter":
            data = {"areaParameters": [{"areaID": area, **values} for area, values in self.areas.items()]}
        elif command.NAME == "getAreaSet":
            data = {"type": "ar", "subsets": self.area_set}
        elif self.accept and command.NAME == "setAreaParameter":
            args = dict(command._args)
            self.areas[args.pop("areaID")] = args
        elif self.accept:
            self.stored = dict(command._args)
        if data is not None:
            response = {"ret": "ok", "resp": {"header": {}, "body": {"code": 0, "msg": "ok", "data": data}}}
        else:
            response = {"ret": "ok", "resp": {"header": {}, "body": {"code": 0 if self.accept else 30000, "msg": "ok"}}}
        command._handle_response(self.events, response)
        return response


async def async_setup_entry(hass, entry):
    entry.runtime_data = SimpleNamespace(devices=[FakeMower()])
    return True


async def async_unload_entry(hass, entry):
    return True
