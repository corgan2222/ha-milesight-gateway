"""Config flow for the Milesight Gateway integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntryState,
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
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry

    from .api import MilesightDevice

from .api import APIAuthError, APIConnectionError, MilesightGatewayAPI
from .const import (
    CONF_DEVICES,
    CONF_FIRMWARE_CHECK,
    CONF_GATEWAY_MQTT_BASE_TOPIC,
    CONF_GATEWAY_URL,
    CONF_IV,
    CONF_SECRET_KEY,
    CONF_STATUS_INTERVAL,
    CONF_WEB_DATA,
    DEFAULT_GATEWAY_MQTT_BASE_TOPIC,
    DEFAULT_IV,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SECRET_KEY,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import status_interval

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


def _url_error(url: str) -> str | None:
    """
    Return why a cleaned gateway URL cannot be used, or None if it can.

    The client appends ":{port}/api/..." to the URL, so anything but a bare
    scheme and host builds an address that never reaches the gateway, and
    would otherwise only surface as a generic "cannot connect".
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "missing_scheme"
    try:
        port = parts.port
    except ValueError:
        return "invalid_url"
    if not parts.hostname or parts.username or parts.password:
        return "invalid_url"
    if port is not None:
        return "url_with_port"
    if parts.path or parts.query or parts.fragment:
        return "invalid_url"
    return None


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

    return {"title": data[CONF_GATEWAY_URL], "api": api}


def device_choices(devices: list[MilesightDevice]) -> list[SelectOptionDict]:
    """Label each device so the list can be searched by name, model and EUI."""
    return [
        SelectOptionDict(
            value=device.dev_eui,
            label=f"{device.name} · {device.model_id} · {device.dev_eui}",
        )
        for device in devices
    ]


def device_selector(choices: list[SelectOptionDict]) -> SelectSelector:
    """Return the searchable multi-select for the device choice."""
    return SelectSelector(
        SelectSelectorConfig(
            options=choices,
            multiple=True,
            mode=SelectSelectorMode.DROPDOWN,
            sort=True,
        )
    )


class MilesightGatewayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Milesight Gateway."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Hold the first step's input until the devices are chosen."""
        self._data: dict[str, Any] = {}
        self._choices: list[SelectOptionDict] = []

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
            info: dict[str, Any] = {}
            if url_error := _url_error(user_input[CONF_GATEWAY_URL]):
                errors[CONF_GATEWAY_URL] = url_error
            else:
                try:
                    info = await validate_input(self.hass, user_input)
                except CannotConnectError:
                    errors["base"] = "cannot_connect"
                except InvalidAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"
            if not errors:
                unique_id = f"{user_input[CONF_GATEWAY_URL]}:{user_input[CONF_PORT]}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                self._data = user_input
                self._choices = await self._async_list_devices(info["api"])
                if not self._choices:
                    return self.async_create_entry(title=info["title"], data=user_input)
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Let the user pick devices before any entity is created.

        On a network with hundreds of sensors this is the only point where
        picking avoids creating them all first.
        """
        if user_input is not None:
            return self.async_create_entry(
                title=self._data[CONF_GATEWAY_URL],
                data=self._data,
                options={CONF_DEVICES: user_input.get(CONF_DEVICES, [])},
            )
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(
                {vol.Optional(CONF_DEVICES, default=[]): device_selector(self._choices)}
            ),
            description_placeholders={"count": str(len(self._choices))},
        )

    async def _async_list_devices(
        self, api: MilesightGatewayAPI
    ) -> list[SelectOptionDict]:
        """List the gateway's devices; none when the list cannot be read."""
        try:
            devices, _, _ = await api.async_get_devices(
                async_get_clientsession(self.hass)
            )
        except (APIAuthError, APIConnectionError) as err:
            # Not worth failing the setup over: without a list, all devices
            # are taken, and the options can narrow them down later.
            _LOGGER.debug("Cannot list the devices for the choice: %s", err)
            return []
        return device_choices(devices)

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
            if url_error := _url_error(merged[CONF_GATEWAY_URL]):
                errors[CONF_GATEWAY_URL] = url_error
            else:
                try:
                    await validate_input(self.hass, merged)
                except CannotConnectError:
                    errors["base"] = "cannot_connect"
                except InvalidAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"
            if not errors:
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
        choices = self._device_choices()
        if user_input is not None:
            if choices is not None:
                # An emptied selection may arrive as a missing key; without
                # this the merge below would keep the old selection.
                user_input.setdefault(CONF_DEVICES, [])
            return self.async_create_entry(data=self.config_entry.options | user_input)

        options = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Clamp(min=MIN_SCAN_INTERVAL)),
            vol.Required(
                CONF_STATUS_INTERVAL, default=status_interval(options)
            ): vol.All(vol.Coerce(int), vol.Clamp(min=MIN_SCAN_INTERVAL)),
            vol.Required(CONF_WEB_DATA, default=options.get(CONF_WEB_DATA, True)): bool,
            vol.Required(
                CONF_FIRMWARE_CHECK,
                default=options.get(CONF_FIRMWARE_CHECK, True),
            ): bool,
        }
        # The choice needs the gateway's device list, so it is only offered
        # while the entry is loaded; a stored selection stays as it is.
        if choices is not None:
            # A suggestion, not a default: a default would be filled back in
            # when the emptied field is left out, so "all" could not be chosen.
            schema[
                vol.Optional(
                    CONF_DEVICES,
                    description={
                        "suggested_value": list(options.get(CONF_DEVICES, []))
                    },
                )
            ] = device_selector(choices)
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))

    def _device_choices(self) -> list[SelectOptionDict] | None:
        """List the gateway's devices to pick from, or None when unknown."""
        entry = self.config_entry
        runtime = getattr(entry, "runtime_data", None)
        if entry.state is not ConfigEntryState.LOADED or runtime is None:
            return None
        data = runtime.coordinator.data
        if data is None:
            return None
        choices = device_choices(data.matched)
        # A picked device the gateway no longer lists stays choosable, so
        # saving the form does not fail on it and it can be removed.
        listed = {device.dev_eui for device in data.matched}
        choices.extend(
            SelectOptionDict(value=eui, label=eui)
            for eui in entry.options.get(CONF_DEVICES, [])
            if eui not in listed
        )
        return choices


class CannotConnectError(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuthError(HomeAssistantError):
    """Error to indicate there is invalid auth."""
