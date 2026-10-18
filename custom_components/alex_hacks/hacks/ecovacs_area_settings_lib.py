"""deebot_client pieces for the Ecovacs area settings hack.

Imported only when the hack is set up, because deebot_client is only installed alongside Home
Assistant's Ecovacs integration. The command shapes (getAreaParameter / setAreaParameter /
onAreaParameter with areaID, mowHeightLevel, cutMode, obstacleHeight, angle) come from
DeebotUniverse/client.py PR #1767, and getAreaSet from PR #1774 (GPL-3.0).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import orjson

from deebot_client.command import InitParam
from deebot_client.commands import COMMANDS_WITH_MQTT_P2P_HANDLING
from deebot_client.commands.json.common import JsonCommandWithMessageHandling, JsonGetCommand, JsonSetCommand
from deebot_client.const import DataType
from deebot_client.event_bus import EventBus
from deebot_client.events import AvailabilityEvent
from deebot_client.events.base import Event
from deebot_client.message import HandlingResult, MessageBodyDataDict
from deebot_client.messages import MESSAGES
from deebot_client.rs.util import decompress_base64_data

__all__ = [
    "FIELDS",
    "AreaNamesEvent",
    "AreaParametersEvent",
    "AvailabilityEvent",
    "GetAreaNames",
    "GetAreaParameters",
    "SetAreaParameters",
    "parse_area_names",
    "parse_area_parameters",
    "register",
]

# The device's field names, besides areaID.
FIELDS = ("angle", "mowHeightLevel", "cutMode", "obstacleHeight")


@dataclass(frozen=True)
class AreaParametersEvent(Event):
    """Settings per area id. complete: every area (a read), else only the areas changed."""

    areas: dict[str, dict[str, int]]
    complete: bool


@dataclass(frozen=True)
class AreaNamesEvent(Event):
    """The app's name for each area id."""

    names: dict[str, str]


def _one(data: dict[str, Any]) -> tuple[str, dict[str, int]]:
    return str(data["areaID"]), {field: int(data[field]) for field in FIELDS}


def parse_area_parameters(data: dict[str, Any]) -> AreaParametersEvent:
    """getAreaParameter / onAreaParameter data, or a setAreaParameter's arguments."""
    if "areaParameters" in data:
        return AreaParametersEvent(dict(map(_one, data["areaParameters"])), complete=True)
    return AreaParametersEvent(dict([_one(data)]), complete=False)


def parse_area_names(data: dict[str, Any]) -> dict[str, str]:
    """getAreaSet data: subsets is compressed JSON, [[map id, area id, name, ...], ...]."""
    if data.get("type") != "ar" or not data.get("subsets"):
        return {}
    subsets = orjson.loads(decompress_base64_data(data["subsets"]).decode())
    return {str(subset[1]): str(subset[2]) for subset in subsets if len(subset) > 2 and subset[2]}


class GetAreaParameters(JsonGetCommand):
    NAME = "getAreaParameter"

    @classmethod
    def _handle_body_data_dict(cls, event_bus: EventBus, data: dict[str, Any]) -> HandlingResult:
        event_bus.notify(parse_area_parameters(data))
        return HandlingResult.success()


class OnAreaParameters(MessageBodyDataDict):
    """Pushed by the mower when its area settings change."""

    NAME = "onAreaParameter"

    @classmethod
    def _handle_body_data_dict(cls, event_bus: EventBus, data: dict[str, Any]) -> HandlingResult:
        event_bus.notify(parse_area_parameters(data))
        return HandlingResult.success()


class SetAreaParameters(JsonSetCommand):
    """Every field must be sent, even the ones not changing."""

    NAME = "setAreaParameter"
    get_command = GetAreaParameters
    _mqtt_params = MappingProxyType(
        {
            "areaID": InitParam(str, "area_id"),
            "angle": InitParam(int),
            "mowHeightLevel": InitParam(int, "mow_height_level"),
            "cutMode": InitParam(int, "cut_mode"),
            "obstacleHeight": InitParam(int, "obstacle_height"),
        }
    )

    def __init__(
        self, area_id: str, angle: int, mow_height_level: int, cut_mode: int, obstacle_height: int
    ) -> None:
        super().__init__(
            {
                "areaID": str(area_id),
                "mowHeightLevel": int(mow_height_level),
                "cutMode": int(cut_mode),
                "obstacleHeight": int(obstacle_height),
                "angle": int(angle),
            }
        )


class GetAreaNames(JsonCommandWithMessageHandling, MessageBodyDataDict):
    NAME = "getAreaSet"

    def __init__(self) -> None:
        super().__init__({"mid": "1", "aid": "0", "type": "ar"})

    @classmethod
    def _handle_body_data_dict(cls, event_bus: EventBus, data: dict[str, Any]) -> HandlingResult:
        if names := parse_area_names(data):
            event_bus.notify(AreaNamesEvent(names))
            return HandlingResult.success()
        return HandlingResult.analyse()


def register() -> callable:
    """Let deebot_client decode the mower's onAreaParameter pushes and the app's
    setAreaParameter requests (so app changes arrive without polling).

    Returns a function that undoes it (leaving deebot_client's own, if it has them).
    """
    undo = []
    for table, name, handler in (
        (MESSAGES[DataType.JSON], OnAreaParameters.NAME, OnAreaParameters),
        (COMMANDS_WITH_MQTT_P2P_HANDLING.setdefault(DataType.JSON, {}), SetAreaParameters.NAME, SetAreaParameters),
    ):
        if name not in table:
            table[name] = handler
            undo.append((table, name, handler))

    def unregister() -> None:
        for table, name, handler in undo:
            if table.get(name) is handler:
                del table[name]

    return unregister
