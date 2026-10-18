"""Config flow: tick one or more hacks, read what they do, then set them up.

Each ticked hack still gets its own config entry (so each can be removed on its own); the
first is created by this flow and the rest by import flows started from it.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig

from .const import CONF_HACK, DOMAIN
from .hacks import HACKS


class AlexHacksConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._hack_keys: list[str] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            self._hack_keys = [k for k in HACKS if k in user_input[CONF_HACK]]
            if self._hack_keys:
                return await self.async_step_configure()
            errors[CONF_HACK] = "none_selected"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HACK): SelectSelector(
                        SelectSelectorConfig(
                            options=[SelectOptionDict(value=k, label=h.TITLE) for k, h in HACKS.items()],
                            multiple=True,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_configure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hacks = [HACKS[k] for k in self._hack_keys]
        # One form for all of them: their questions combined (shared questions asked once).
        schema = vol.Schema({})
        for hack in hacks:
            hack_schema, abort_reason = await hack.async_config_schema(self.hass)
            if abort_reason:
                return self.async_abort(reason=abort_reason)
            schema = schema.extend(hack_schema.schema)

        if user_input is not None:
            datas = [{CONF_HACK: hack.KEY, **user_input} for hack in hacks]
            configured = {e.unique_id for e in self._async_current_entries(include_ignore=False)}
            datas = [d for d in datas if HACKS[d[CONF_HACK]].async_unique_id(d) not in configured]
            if not datas:
                return self.async_abort(reason="already_configured")
            first, *rest = datas
            for data in rest:
                self.hass.async_create_task(
                    self.hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data=data)
                )
            return await self._async_create(first)

        return self.async_show_form(
            step_id="configure",
            data_schema=schema,
            description_placeholders={
                "hacks": "\n\n".join(
                    f"### {h.TITLE}\n**What:** {h.WHAT}\n\n**Why:** {h.WHY}\n\n**Remove when:** {h.REMOVE_WHEN}"
                    for h in hacks
                )
            },
        )

    async def async_step_import(self, data: dict[str, Any]) -> ConfigFlowResult:
        """The other hacks ticked in the same setup."""
        return await self._async_create(data)

    async def _async_create(self, data: dict[str, Any]) -> ConfigFlowResult:
        hack = HACKS[data[CONF_HACK]]
        await self.async_set_unique_id(hack.async_unique_id(data))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=hack.async_entry_title(self.hass, data), data=data)
