"""Config flow for the Milesight Gateway integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry

from .api import APIAuthError, APIConnectionError, MilesightGatewayAPI
from .const import (
    CONF_GATEWAY_MQTT_BASE_TOPIC,
    CONF_GATEWAY_URL,
    CONF_IV,
    CONF_SECRET_KEY,
    DEFAULT_GATEWAY_MQTT_BASE_TOPIC,
    DEFAULT_IV,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SECRET_KEY,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GATEWAY_URL): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_SECRET_KEY, default=DEFAULT_SECRET_KEY): str,
        vol.Required(CONF_IV, default=DEFAULT_IV): str,
        vol.Required(
            CONF_GATEWAY_MQTT_BASE_TOPIC, default=DEFAULT_GATEWAY_MQTT_BASE_TOPIC
        ): str,
    }
)


STEP_RECONFIGURE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GATEWAY_URL): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME): str,
        vol.Optional(CONF_PASSWORD): str,
        vol.Required(CONF_SECRET_KEY): str,
        vol.Required(CONF_IV): str,
        vol.Required(CONF_GATEWAY_MQTT_BASE_TOPIC): str,
    }
)


STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_SECRET_KEY): str,
        vol.Required(CONF_IV): str,
    }
)


def _clean_url(url: str) -> str:
    """Normalize a gateway URL so it is usable and comparable."""
    return url.strip().rstrip("/")


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    api = MilesightGatewayAPI(
        gateway_url=data[CONF_GATEWAY_URL],
        port=data[CONF_PORT],
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        secret_key=data[CONF_SECRET_KEY],
        iv=data[CONF_IV],
        mqtt_base_topic=data[CONF_GATEWAY_MQTT_BASE_TOPIC],
    )
    try:
        session = async_get_clientsession(hass)
        await api.async_connect(session)
    except APIAuthError as err:
        raise InvalidAuthError from err
    except APIConnectionError as err:
        raise CannotConnectError from err

    return {"title": data[CONF_GATEWAY_URL]}


class MilesightGatewayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Milesight Gateway."""

    VERSION = 1
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(
        _config_entry: ConfigEntry,
    ) -> MilesightGatewayOptionsFlow:
        """Return the options flow handler."""
        return MilesightGatewayOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_GATEWAY_URL] = _clean_url(user_input[CONF_GATEWAY_URL])
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                unique_id = f"{user_input[CONF_GATEWAY_URL]}:{user_input[CONF_PORT]}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, _entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start the credential-renewal flow after the gateway rejected us."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for working credentials and keep everything else untouched."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            try:
                await validate_input(self.hass, {**entry.data, **user_input})
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input
                )

        # The username and the two AES constants are prefilled because they
        # rarely change; the password is what the user is here to retype.
        suggested = {
            key: value
            for key, value in (user_input or entry.data).items()
            if key != CONF_PASSWORD
        }
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_REAUTH_DATA_SCHEMA, suggested
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the gateway address and credentials of an existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            user_input[CONF_GATEWAY_URL] = _clean_url(user_input[CONF_GATEWAY_URL])
            # An empty password means "keep the stored one", so drop it from
            # the updates instead of overwriting the stored value with "".
            updates = {
                key: value
                for key, value in user_input.items()
                if key != CONF_PASSWORD or value
            }
            merged = {**entry.data, **updates}
            try:
                await validate_input(self.hass, merged)
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Skips the entry being reconfigured, so moving this gateway to
                # a new address is allowed while colliding with another is not.
                self._async_abort_entries_match(
                    {
                        CONF_GATEWAY_URL: merged[CONF_GATEWAY_URL],
                        CONF_PORT: merged[CONF_PORT],
                    }
                )
                # The unique id is derived from the address, so it has to move
                # with it or the new address could be added a second time.
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=f"{merged[CONF_GATEWAY_URL]}:{merged[CONF_PORT]}",
                    data_updates=updates,
                )

        # Everything but the password is prefilled; secrets are not sent to the
        # frontend to be rendered back. After a failed submit this re-shows what
        # the user typed, so a retry does not silently revert their edit.
        suggested = {
            key: value
            for key, value in (user_input or entry.data).items()
            if key != CONF_PASSWORD
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_RECONFIGURE_DATA_SCHEMA, suggested
            ),
            errors=errors,
        )


class MilesightGatewayOptionsFlow(OptionsFlowWithReload):
    """Handle options for the Milesight Gateway integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=self.config_entry.options | user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Clamp(min=MIN_SCAN_INTERVAL)),
                }
            ),
        )


class CannotConnectError(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuthError(HomeAssistantError):
    """Error to indicate there is invalid auth."""
