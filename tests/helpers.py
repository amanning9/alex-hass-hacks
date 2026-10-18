from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def setup_dreame(hass):
    dreame = MockConfigEntry(domain="dreame_vacuum", title="Robbie")
    dreame.add_to_hass(hass)
    assert await hass.config_entries.async_setup(dreame.entry_id)
    # The real integration registers the device; do the same so entities attach to it.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=dreame.entry_id, identifiers={("dreame_vacuum", "AA:BB")}, name="Robbie")
    return dreame, hass.data["dreame_vacuum"][dreame.entry_id]


async def add_hack(hass, hack, dreame):
    """Add one hack for the vacuum through the UI flow."""
    result = await hass.config_entries.flow.async_init("alex_hacks", context={"source": "user"})
    assert result["type"] == "form" and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"hack": [hack]})
    assert result["type"] == "form" and result["step_id"] == "configure", result
    assert "**Remove when:**" in result["description_placeholders"]["hacks"]  # what/why/remove-when shown
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"dreame_entry_id": dreame.entry_id})
    assert result["type"] == "create_entry", result
    await hass.async_block_till_done()
    return hass.config_entries.async_get_entry(result["result"].entry_id)
