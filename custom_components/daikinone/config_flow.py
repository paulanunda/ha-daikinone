"""Config, re-authentication, reconfigure, and options flows.

Credentials are checked against Daikin before anything is saved. Only the
email, API key, and integrator token are ever asked for; the Daikin account
password is not used by the official API.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_EMAIL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    AuthenticationError,
    ConnectionFailedError,
    DaikinOneClient,
    DaikinOneError,
    RateLimitError,
    ServerError,
    create_ssl_context,
    mask_email,
)
from .const import (
    CONF_INTEGRATOR_TOKEN,
    CONF_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_SCAN_INTERVAL_MINUTES,
)
from .coordinator import DaikinOneConfigEntry, scan_interval_from_options

_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_SECRETS_SCHEMA = {
    probatio.Required(CONF_API_KEY): _SECRET,
    probatio.Required(CONF_INTEGRATOR_TOKEN): _SECRET,
}
USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL)
        ),
        **_SECRETS_SCHEMA,
    }
)
REAUTH_SCHEMA = probatio.Schema(_SECRETS_SCHEMA)


def account_unique_id(email: str) -> str:
    """Return a stable id for the account that does not reveal the email."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def entry_title(email: str) -> str:
    """Return the entry title. Titles appear in logs, so the email is masked."""
    return f"Daikin One ({mask_email(email)})"


def _cleaned(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Strip whitespace that comes along when values are pasted. Case is kept."""
    return {key: str(value).strip() for key, value in user_input.items()}


class DaikinOneConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Daikin account."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: DaikinOneConfigEntry) -> DaikinOneOptionsFlow:
        """Return the options flow."""
        return DaikinOneOptionsFlow()

    async def _async_validate(self, credentials: Mapping[str, str]) -> str | None:
        """Try the credentials against Daikin. Return an error key, or None if they work."""
        ssl_context = await self.hass.async_add_executor_job(create_ssl_context)
        client = DaikinOneClient(
            async_get_clientsession(self.hass),
            email=credentials[CONF_EMAIL],
            api_key=credentials[CONF_API_KEY],
            integrator_token=credentials[CONF_INTEGRATOR_TOKEN],
            ssl_context=ssl_context,
        )
        try:
            # The token call checks the email and integrator token; the device
            # list call checks that the token and API key work together.
            await client.authenticate()
            await client.list_devices()
        except AuthenticationError:
            return "invalid_auth"
        except RateLimitError:
            return "rate_limited"
        except ConnectionFailedError, ServerError:
            return "cannot_connect"
        except DaikinOneError:
            return "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the email, API key, and integrator token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = _cleaned(user_input)
            await self.async_set_unique_id(account_unique_id(credentials[CONF_EMAIL]))
            self._abort_if_unique_id_configured()
            error = await self._async_validate(credentials)
            if error is None:
                return self.async_create_entry(
                    title=entry_title(credentials[CONF_EMAIL]), data=credentials
                )
            errors["base"] = error
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start re-authentication after Daikin rejected the saved credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key and integrator token for the same account."""
        entry = self._get_reauth_entry()
        email: str = entry.data[CONF_EMAIL]
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = {CONF_EMAIL: email, **_cleaned(user_input)}
            error = await self._async_validate(credentials)
            if error is None:
                return self.async_update_reload_and_abort(entry, data_updates=credentials)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"account": mask_email(email)},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the saved credentials, keeping the entry on the same account."""
        entry = self._get_reconfigure_entry()
        email: str = entry.data[CONF_EMAIL]
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = _cleaned(user_input)
            await self.async_set_unique_id(account_unique_id(credentials[CONF_EMAIL]))
            self._abort_if_unique_id_mismatch(reason="account_mismatch")
            error = await self._async_validate(credentials)
            if error is None:
                return self.async_update_reload_and_abort(
                    entry,
                    title=entry_title(credentials[CONF_EMAIL]),
                    data_updates=credentials,
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure",
            # Pre-fill the email only; secrets are never sent back to the browser.
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, {CONF_EMAIL: email}),
            errors=errors,
            description_placeholders={"account": mask_email(email)},
        )


class DaikinOneOptionsFlow(OptionsFlow):
    """Change the polling interval, within Daikin's limit."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the polling interval. The form refuses anything under 3 minutes."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = scan_interval_from_options(self.config_entry.options)
        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_SCAN_INTERVAL_MINUTES, default=int(current.total_seconds() // 60)
                ): probatio.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL_MINUTES,
                            max=MAX_SCAN_INTERVAL_MINUTES,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                    probatio.Coerce(int),
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
