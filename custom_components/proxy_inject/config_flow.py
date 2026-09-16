"""UI setup and options for Proxy Inject."""

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import DEFAULT_NO_PROXY, DOMAIN, NAME
from .policy import SETTINGS_SCHEMA


def form_schema(values):
    bypass = values.get("no_proxy", DEFAULT_NO_PROXY)
    if not isinstance(bypass, str):
        bypass = "\n".join(bypass)
    return vol.Schema(
        {
            vol.Required(
                "proxy", default=values.get("proxy", "")
            ): selector.TextSelector(),
            vol.Required(
                "clients", default=values.get("clients", ["aiohttp"])
            ): selector.SelectSelector(
                {
                    "options": [
                        {
                            "value": "aiohttp",
                            "label": "aiohttp (HTTP / HTTPS / WebSocket)",
                        },
                        {"value": "httpx", "label": "httpx (AsyncClient / Client)"},
                    ],
                    "multiple": True,
                    "mode": "list",
                }
            ),
            vol.Optional("no_proxy", default=bypass): selector.TextSelector(
                {"multiline": True}
            ),
        }
    )


def validate(values):
    try:
        return SETTINGS_SCHEMA({**values, "no_proxy": values.get("no_proxy", "")}), {}
    except vol.Invalid as err:
        field = str(err.path[0]) if err.path else "base"
        return None, {field: "invalid_config"}


class ProxyInjectConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if DOMAIN in self.hass.data:
            return self.async_abort(reason="yaml_active")
        errors = {}
        if user_input is not None:
            settings, errors = validate(user_input)
            if not errors:
                return self.async_create_entry(title=NAME, data=settings)
        return self.async_show_form(
            step_id="user", data_schema=form_schema(user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ProxyInjectOptionsFlow()


class ProxyInjectOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            settings, errors = validate(user_input)
            if not errors:
                return self.async_create_entry(data=settings)
        return self.async_show_form(
            step_id="init",
            data_schema=form_schema(
                user_input
                if user_input is not None
                else self.config_entry.options or self.config_entry.data
            ),
            errors=errors,
        )
