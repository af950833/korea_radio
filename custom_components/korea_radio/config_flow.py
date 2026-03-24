import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector

from .const import DOMAIN


class KoreaRadioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            data = dict(user_input)
            data["bitrate"] = int(data["bitrate"])
            return self.async_create_entry(
                title=data[CONF_NAME],
                data=data,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Korea Radio"): str,
                vol.Required("target_media_player"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="media_player")
                ),
                vol.Required("bitrate", default="128"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["128", "192", "256", "320"],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
