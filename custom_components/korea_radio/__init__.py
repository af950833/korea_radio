from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Korea Radio from a config entry."""
    if not hass.data.get(f"{DOMAIN}_icons_registered"):
        icons_path = Path(__file__).parent / "icons"
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    f"/api/{DOMAIN}/icons",
                    str(icons_path),
                    True,
                )
            ]
        )
        hass.data[f"{DOMAIN}_icons_registered"] = True

    await hass.config_entries.async_forward_entry_setups(entry, ["media_player"])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_forward_entry_unload(entry, "media_player")
