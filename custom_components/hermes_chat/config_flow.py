"""Config flow for Hermes Chat."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector

from .const import (
    CONF_API_HOST,
    CONF_API_KEY,
    CONF_API_PORT,
    CONF_DEFAULT_PROFILE,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_PROFILES,
    CONF_USERNAME,
    DEFAULT_API_PORT,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_PROFILE,
    DEFAULT_PROFILES,
    DEFAULT_USERNAME,
    DOMAIN,
)
from .hermes import test_hermes_login


def _schema(defaults: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_HOST, default=defaults.get(CONF_HOST, DEFAULT_HOST)
            ): TextSelector(),
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): int,
            vol.Required(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, DEFAULT_USERNAME)
            ): TextSelector(),
            vol.Required(CONF_PASSWORD, default=""): TextSelector(),
            vol.Required(
                CONF_PROFILES, default=defaults.get(CONF_PROFILES, DEFAULT_PROFILES)
            ): TextSelector(),
            vol.Required(
                CONF_DEFAULT_PROFILE,
                default=defaults.get(CONF_DEFAULT_PROFILE, DEFAULT_PROFILE),
            ): TextSelector(),
            # OpenAI-compatible API relay (other agents). Optional: leave
            # the key empty to disable the relay. The API host defaults
            # to the same host as the dashboard.
            vol.Optional(
                CONF_API_HOST, default=defaults.get(CONF_API_HOST, "")
            ): TextSelector(),
            vol.Optional(
                CONF_API_PORT, default=defaults.get(CONF_API_PORT, DEFAULT_API_PORT)
            ): int,
            vol.Optional(CONF_API_KEY, default=""): TextSelector(),
        }
    )


class HermesChatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Hermes Chat."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input.get(CONF_HOST, "")).strip()
            try:
                port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            except (TypeError, ValueError):
                port = 0
            username = str(user_input.get(CONF_USERNAME, "")).strip()
            password = str(user_input.get(CONF_PASSWORD, ""))
            if not host or port < 1 or port > 65535 or not username or not password:
                errors["base"] = "invalid_input"
            else:
                ok, message = await test_hermes_login(host, port, username, password)
                if ok:
                    return self.async_create_entry(title="Hermes Chat", data=user_input)
                errors["base"] = (
                    "invalid_auth" if "password" in message or "login" in message.lower() else "cannot_connect"
                )
        return self.async_show_form(
            step_id="user", data_schema=_schema({}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HermesChatOptionsFlow(config_entry)


class HermesChatOptionsFlow(OptionsFlow):
    """Change Hermes Chat options."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        defaults = {**self._config_entry.data, **self._config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(defaults))
