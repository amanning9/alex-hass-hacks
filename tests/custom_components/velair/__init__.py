"""Fake Velair: runtime_data has the scheduler and storage the return-mode hack uses, behaving
as Velair 1.9.0-beta.5 does (a new return mode cancels a current return)."""
from copy import deepcopy
from types import SimpleNamespace

DOMAIN = "velair"


class FakeScheduler:
    def __init__(self, storage):
        self.storage = storage
        self.temperature_migration_blocked = False
        self.calls = []

    def get_modes(self):
        return deepcopy([
            {"key": "mode_guests_purple", "name": "Guests in Purple", "profile_ids": []},
            {"key": "mode_away", "name": "Away", "profile_ids": []},
        ])

    async def async_update_planned_return(self, config):
        if config["destination_mode_id"] not in {"default", *(m["key"] for m in self.get_modes())}:
            raise ValueError("Planned return references an unknown mode")
        self.calls.append(("update", deepcopy(config)))
        previous = self.storage.data["planned_return"]
        self.storage.data["planned_return"] = deepcopy(config)
        episode = self.storage.data.get("planned_return_episode")
        if episode and episode["state"] in ("armed", "away") and previous["destination_mode_id"] != config["destination_mode_id"]:
            episode["state"] = "cancelled"

    async def async_start_planned_return(self, return_at, *, apply_away_now=False):
        self.calls.append(("start", return_at, apply_away_now))
        self.storage.data["planned_return_episode"] = {
            "state": "away" if apply_away_now else "armed", "return_at": return_at,
            "destination_mode_id": self.storage.data["planned_return"]["destination_mode_id"],
        }


async def async_setup_entry(hass, entry):
    storage = SimpleNamespace(data={
        "planned_return": {"enabled": False, "occupancy_entity_ids": [], "away_mode_id": "mode_away",
                           "destination_mode_id": "default", "absence_delay_minutes": 10, "weekly": {}},
        "planned_return_episode": None,
    })
    entry.runtime_data = SimpleNamespace(scheduler=FakeScheduler(storage), storage=storage)
    return True


async def async_unload_entry(hass, entry):
    return True
