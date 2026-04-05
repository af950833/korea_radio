from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlowWithReload
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import DOMAIN, STATIONS

BITRATE_OPTIONS = ["128", "192", "256", "320"]
DEFAULT_NAME = "Korea Radio"


def _channel_options() -> list[selector.SelectOptionDict]:
    """Return channel options for selectors."""
    return [
        selector.SelectOptionDict(value=key, label=name)
        for key, name in STATIONS.items()
    ]


def _schema() -> vol.Schema:
    """Return the shared schema for config and options flows."""
    channel_options = _channel_options()

    return vol.Schema(
        {
            vol.Required(CONF_NAME): str,
            vol.Required("target_media_player"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="media_player")
            ),
            vol.Required("bitrate"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=BITRATE_OPTIONS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required("channels"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=channel_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required("default_channel"): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=channel_options,
                    multiple=False,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _normalize_input(data: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize flow input before storing it."""
    normalized = dict(data)
    normalized[CONF_NAME] = normalized.get(CONF_NAME, DEFAULT_NAME)
    normalized["bitrate"] = int(normalized.get("bitrate", "192"))

    channels = normalized.get("channels")
    if not channels:
        channels = list(STATIONS.keys())
    normalized["channels"] = list(channels)

    default_channel = normalized.get("default_channel")
    if not default_channel or default_channel not in normalized["channels"]:
        default_channel = normalized["channels"][0]

    normalized["default_channel"] = default_channel
    return normalized


class KoreaRadioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 4

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KoreaRadioOptionsFlow:
        return KoreaRadioOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            data = _normalize_input(user_input)
            return self.async_create_entry(title=data[CONF_NAME], data=data)

        all_channels = list(STATIONS.keys())
        defaults = {
            CONF_NAME: DEFAULT_NAME,
            "bitrate": "192",
            "channels": all_channels,
            "default_channel": all_channels[0],
        }

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_schema(), defaults),
        )


class KoreaRadioOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_normalize_input(user_input))

        current = {
            **self.config_entry.data,
            **self.config_entry.options,
        }
        current.setdefault(CONF_NAME, DEFAULT_NAME)
        current["bitrate"] = str(current.get("bitrate", "192"))
        current.setdefault("channels", list(STATIONS.keys()))

        if current.get("default_channel") not in current["channels"]:
            current["default_channel"] = current["channels"][0]

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(_schema(), current),
        )
