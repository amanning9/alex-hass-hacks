"""The hacks. Each module is one self-contained hack (see HACKS.md).

A hack module defines:
  KEY, TITLE         identifier and display name
  WHAT, WHY          what it does and why it's needed
  REMOVE_WHEN        the upstream change that would make it unnecessary
  PLATFORMS          entity platforms it provides
  async_config_schema(hass) -> (vol.Schema, errors-or-abort-reason)   the add form
  async_entry_title(hass, data) -> str, async_unique_id(data) -> str
  async_setup_entry(hass, entry) / async_unload_entry(hass, entry)
  async_setup_platform(hass, entry, platform, async_add_entities)
and optionally async_remove_entry(hass, entry). A hack providing an action registers it in
async_setup_entry (once) and removes it when its last entry unloads.
"""

from __future__ import annotations

from types import ModuleType

from . import dreame_cleaning_mode, dreame_mop_drying, dreame_rooms, dreame_vacuum_then_mop, person_notify

HACKS: dict[str, ModuleType] = {
    hack.KEY: hack for hack in (dreame_cleaning_mode, dreame_rooms, dreame_vacuum_then_mop, dreame_mop_drying, person_notify)
}
