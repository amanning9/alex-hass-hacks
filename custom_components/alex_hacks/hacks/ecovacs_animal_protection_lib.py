"""deebot_client pieces for the Ecovacs animal protection hack.

Imported only when the hack is set up, because deebot_client is only installed alongside Home
Assistant's Ecovacs integration. The command shapes (getAnimProtect / setAnimProtect with
enable, start, end) come from DeebotUniverse/client.py PR #1778 (GPL-3.0).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from deebot_client.command import InitParam
from deebot_client.commands import COMMANDS_WITH_MQTT_P2P_HANDLING
from deebot_client.commands.json.common import JsonGetCommand, JsonSetCommand
from deebot_client.const import DataType
from deebot_client.event_bus import EventBus
from deebot_client.events import AvailabilityEvent
from deebot_client.events.base import Event
from deebot_client.message import HandlingResult

__all__ = [
    "AnimalProtectionEvent",
    "AvailabilityEvent",
    "GetAnimalProtection",
    "SetAnimalProtection",
    "register_p2p",
]


def normalize_time(value: str) -> str:
    """The device sends e.g. "7:5"; use zero-padded HH:MM."""
    hour, minute = value.split(":", maxsplit=1)
    return f"{int(hour):02d}:{int(minute):02d}"


@dataclass(frozen=True)
class AnimalProtectionEvent(Event):
    """The animal protection schedule: on/off and the nightly window."""

    enabled: bool
    start: str
    end: str


class GetAnimalProtection(JsonGetCommand):
    NAME = "getAnimProtect"

    @classmethod
    def _handle_body_data_dict(cls, event_bus: EventBus, data: dict[str, Any]) -> HandlingResult:
        event_bus.notify(
            AnimalProtectionEvent(
                enabled=bool(int(data["enable"])),
                start=normalize_time(str(data["start"])),
                end=normalize_time(str(data["end"])),
            )
        )
        return HandlingResult.success()


class SetAnimalProtection(JsonSetCommand):
    NAME = "setAnimProtect"
    get_command = GetAnimalProtection
    _mqtt_params = MappingProxyType(
        {"enable": InitParam(bool, "enabled"), "start": InitParam(str), "end": InitParam(str)}
    )

    def __init__(self, enabled: bool, start: str, end: str) -> None:
        super().__init__(
            {"enable": 1 if enabled else 0, "start": normalize_time(start), "end": normalize_time(end)}
        )


def register_p2p() -> callable:
    """Let deebot_client decode setAnimProtect sent by the Ecovacs app (no polling needed).

    deebot_client handles a set command another client sends over MQTT ("p2p") only if it's in
    its table; it then calls GetAnimalProtection.handle_set_args, which notifies our event.
    Returns a function that undoes the registration (leaving deebot_client's own, if it has one).
    """
    table = COMMANDS_WITH_MQTT_P2P_HANDLING.setdefault(DataType.JSON, {})
    if SetAnimalProtection.NAME in table:
        return lambda: None
    table[SetAnimalProtection.NAME] = SetAnimalProtection

    def unregister() -> None:
        if table.get(SetAnimalProtection.NAME) is SetAnimalProtection:
            del table[SetAnimalProtection.NAME]

    return unregister
