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
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    from .api import GatewayDetails, GatewayInfo, MilesightDevice

from .api import APIAuthError, APIConnectionError, MilesightGatewayAPI
from .const import (
    CONF_ALL_SETTINGS,
    CONF_DATA_SOURCE,
    CONF_DEVICES,
    CONF_DEVICES_PANEL,
    CONF_FIRMWARE_CHECK,
    CONF_GATEWAY_MQTT_BASE_TOPIC,
    CONF_GATEWAY_ONLY,
    CONF_GATEWAY_URL,
    CONF_HOSTNAME,
    CONF_IV,
    CONF_MAC,
    CONF_MODEL,
    CONF_MQTT_ACK_TOPIC,
    CONF_MQTT_DOWNLINK_TOPIC,
    CONF_MQTT_ERROR_TOPIC,
    CONF_MQTT_JOIN_TOPIC,
    CONF_MQTT_REQUEST_TOPIC,
    CONF_MQTT_RESPONSE_TOPIC,
    CONF_MQTT_TOPIC,
    CONF_NETWORK_PANEL,
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
    SOURCE_LORAWAN,
    SOURCE_MQTT,
    WEB_ONLY_FIRMWARE,
    data_source,
    entry_title,
    gateway_only,
)
from .coordinator import status_interval
from .hostname import given_title, short_hostname, tracker_hostname
from .mqtt_discovery import (
    downlink_error,
    request_error,
    sibling_topic,
    topic_error,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required(CONF_GATEWAY_URL): str})


STEP_CREDENTIALS_DATA_SCHEMA = vol.Schema(
    {
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


# A gateway without a usable network server only needs its web login. The
# other fields keep their defaults: the web login uses the same AES constants.
STEP_WEB_LOGIN_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

# The texts about the topic write the placeholder literally; braces in a
# translation are placeholders themselves, so it comes in as a value.
TOPIC_PLACEHOLDER = {"dev_eui": "{dev_eui}"}

# At setup the web login also takes the MQTT topic the gateway's applications
# publish to, so its devices are found there.
STEP_WEB_SETUP_DATA_SCHEMA = STEP_WEB_LOGIN_DATA_SCHEMA.extend(
    {vol.Optional(CONF_MQTT_TOPIC): str}
)

STEP_WEB_RECONFIGURE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GATEWAY_URL): str,
        vol.Required(CONF_USERNAME): str,
        vol.Optional(CONF_PASSWORD): str,
    }
)

WEB_ONLY_DEFAULTS = {
    CONF_PORT: DEFAULT_PORT,
    CONF_SECRET_KEY: DEFAULT_SECRET_KEY,
    CONF_IV: DEFAULT_IV,
    CONF_GATEWAY_MQTT_BASE_TOPIC: DEFAULT_GATEWAY_MQTT_BASE_TOPIC,
}


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
    """
    Check the gateway takes the account, and whether its network server does.

    A Milesight Mini gateway (UG63) takes the account on its web interface but
    refuses the network server login that the device list and the LoRaWAN source
    need. Such an entry runs gateway-only: the gateway itself, without its
    devices. A login the web interface also refuses is simply wrong.
    """
    api = _api(data)
    session = async_get_clientsession(hass)
    try:
        await api.async_connect(session)
    except APIAuthError as err:
        if details := await _web_details(api, session):
            return {"api": api, "gateway_only": True, "hostname": details.hostname}
        raise InvalidAuthError from err
    except APIConnectionError as err:
        raise CannotConnectError from err

    details = await _web_details(api, session)
    return {
        "api": api,
        "gateway_only": False,
        "hostname": details.hostname if details else None,
    }


def _api(data: Mapping[str, Any]) -> MilesightGatewayAPI:
    """Build the API client from entry data, with defaults for what is missing."""
    data = {**WEB_ONLY_DEFAULTS, CONF_USERNAME: "", CONF_PASSWORD: "", **data}
    return MilesightGatewayAPI(
        gateway_url=data[CONF_GATEWAY_URL],
        port=data[CONF_PORT],
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        secret_key=data[CONF_SECRET_KEY],
        iv=data[CONF_IV],
    )


async def probe_gateway(hass: HomeAssistant, url: str) -> GatewayInfo:
    """
    Return the gateway's model and firmware, asked without a login.

    The form that follows depends on it, and no login is spent on a model whose
    network server refuses it.
    """
    try:
        info = await _api({CONF_GATEWAY_URL: url}).async_get_gateway_info(
            async_get_clientsession(hass)
        )
    except (APIAuthError, APIConnectionError) as err:
        raise CannotConnectError from err
    return info


async def validate_web_login(
    hass: HomeAssistant, data: dict[str, Any]
) -> dict[str, Any]:
    """Check the gateway's web interface takes the account; never the NS login."""
    api = _api(data)
    try:
        details = await api.async_get_gateway_details(async_get_clientsession(hass))
    except APIAuthError as err:
        raise InvalidAuthError from err
    except APIConnectionError as err:
        raise CannotConnectError from err
    return {"api": api, "gateway_only": True, "hostname": details.hostname}


def _validator(data: Mapping[str, Any]) -> Any:
    """Return the check that fits an existing entry, sparing a gateway-only one."""
    return validate_web_login if gateway_only(data) else validate_input


async def _web_details(api: MilesightGatewayAPI, session: Any) -> GatewayDetails | None:
    """Return what the web interface reports, or None when it refuses the account."""
    try:
        return await api.async_get_gateway_details(session)
    except APIAuthError, APIConnectionError:
        return None


def device_choices(devices: list[MilesightDevice]) -> list[SelectOptionDict]:
    """Label each device so the list can be searched by name, model and EUI."""
    return [
        SelectOptionDict(
            value=device.dev_eui,
            label=f"{device.name} · {device.model_id} · {device.dev_eui}",
        )
        for device in devices
    ]


SOURCE_NAMES = {SOURCE_LORAWAN: "LoRaWAN", SOURCE_MQTT: "MQTT"}


def source_selector() -> SelectSelector:
    """Return the choice between MQTT and the LoRaWAN packet log."""
    return SelectSelector(
        SelectSelectorConfig(
            options=[SOURCE_LORAWAN, SOURCE_MQTT],
            mode=SelectSelectorMode.LIST,
            translation_key=CONF_DATA_SOURCE,
        )
    )


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
    MINOR_VERSION = 3

    def __init__(self) -> None:
        """Hold the first step's input until the devices are chosen."""
        self._data: dict[str, Any] = {}
        self._choices: list[SelectOptionDict] = []
        # What /islogin named; it decides which login the next step asks for.
        self._model: str | None = None
        # Whether the firmware's network server refuses the login (UG63 on 63.x).
        self._web_only = False
        # Set by DHCP discovery; the entry's unique id when known.
        self._mac: str | None = None
        # The name the gateway sent with its DHCP request.
        self._hostname: str | None = None

    def _probe(self, info: GatewayInfo) -> None:
        """Keep what /islogin reported for the steps that follow."""
        self._model = info.model
        self._web_only = info.firmware.split(".")[0] in WEB_ONLY_FIRMWARE

    @staticmethod
    @callback
    def async_get_options_flow(
        _config_entry: ConfigEntry,
    ) -> MilesightGatewayOptionsFlow:
        """Return the options flow handler."""
        return MilesightGatewayOptionsFlow()

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Offer a discovered gateway, or move a known one to its new address."""
        mac = format_mac(discovery_info.macaddress)
        await self.async_set_unique_id(mac)
        # Matched by MAC, or by address for an entry added by hand before its
        # MAC was known; such an entry takes the MAC as its unique id now.
        for entry in self._async_current_entries(include_ignore=False):
            url = urlsplit(entry.data.get(CONF_GATEWAY_URL, ""))
            if entry.unique_id == mac or url.hostname == discovery_info.ip:
                updates = {
                    CONF_GATEWAY_URL: f"{url.scheme or 'http'}://{discovery_info.ip}",
                    CONF_MAC: mac,
                }
                # A hostname the gateway reports itself stays.
                if not entry.data.get(CONF_HOSTNAME) and (
                    name := short_hostname(discovery_info.hostname)
                ):
                    updates[CONF_HOSTNAME] = name
                # The title follows name and address, unless the user chose one.
                title = (
                    entry_title({**entry.data, **updates})
                    if given_title(entry.title, dict(entry.data))
                    else entry.title
                )
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=mac,
                    title=title,
                    data_updates=updates,
                    reason="already_configured",
                    reload_even_if_entry_is_unchanged=False,
                )
        # An ignored discovery stays ignored.
        self._abort_if_unique_id_configured()

        url = f"http://{discovery_info.ip}"
        try:
            self._probe(await probe_gateway(self.hass, url))
        except CannotConnectError:
            return self.async_abort(reason="cannot_connect")
        self._data = {CONF_GATEWAY_URL: url}
        self._mac = mac
        self._hostname = short_hostname(discovery_info.hostname)
        self.context["title_placeholders"] = {
            "model": self._model or "Gateway",
            "host": discovery_info.ip,
        }
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask whether to set up the discovered gateway."""
        if user_input is not None:
            return await self.async_step_credentials()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the address and find out which gateway answers there."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = _clean_url(user_input[CONF_GATEWAY_URL])
            if url_error := _url_error(url):
                errors[CONF_GATEWAY_URL] = url_error
            else:
                # By host, so http and https of one gateway are one gateway.
                host = urlsplit(url).hostname
                for entry in self._async_current_entries():
                    known = urlsplit(entry.data.get(CONF_GATEWAY_URL, "")).hostname
                    if known == host:
                        return self.async_abort(reason="already_configured")
                try:
                    self._probe(await probe_gateway(self.hass, url))
                except CannotConnectError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"
            if not errors:
                self._data = {CONF_GATEWAY_URL: url}
                return await self.async_step_credentials()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the login the detected model needs, and check it."""
        errors: dict[str, str] = {}
        # A model without a usable network server gets the web login only, and
        # no network server login is tried: failed logins lock the account.
        web_only = self._web_only
        schema = (
            STEP_WEB_SETUP_DATA_SCHEMA if web_only else STEP_CREDENTIALS_DATA_SCHEMA
        )

        if user_input is not None:
            # An option, not entry data: the options can change it later.
            topic = (user_input.get(CONF_MQTT_TOPIC) or "").strip()
            data = {
                **(WEB_ONLY_DEFAULTS if web_only else {}),
                **{k: v for k, v in user_input.items() if k != CONF_MQTT_TOPIC},
                **self._data,
            }
            info: dict[str, Any] = {}
            # A wrong topic costs no login: failed logins lock the account.
            if topic and (error := topic_error(topic)):
                errors[CONF_MQTT_TOPIC] = error
            else:
                try:
                    validate = validate_web_login if web_only else validate_input
                    info = await validate(self.hass, data)
                except CannotConnectError:
                    errors["base"] = "cannot_connect"
                except InvalidAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"
            if not errors:
                url = data[CONF_GATEWAY_URL]
                await self.async_set_unique_id(self._mac or f"{url}:{data[CONF_PORT]}")
                self._abort_if_unique_id_configured()
                if self._mac:
                    data[CONF_MAC] = self._mac
                if self._model:
                    data[CONF_MODEL] = self._model
                # The gateway's own name first, then its DHCP name.
                if hostname := (
                    info.get("hostname")
                    or self._hostname
                    or tracker_hostname(self.hass, self._mac, urlsplit(url).hostname)
                ):
                    data[CONF_HOSTNAME] = hostname
                self._data = data
                if info["gateway_only"]:
                    # No device list, so nothing to pick and no data source to
                    # choose: the entry covers the gateway itself. The flag is
                    # only set when true, so other entries keep their data as is.
                    self._data[CONF_GATEWAY_ONLY] = True
                    return self.async_create_entry(
                        title=entry_title(self._data),
                        data=self._data,
                        options={CONF_MQTT_TOPIC: topic} if topic else {},
                    )
                self._choices = await self._async_list_devices(info["api"])
                return await self.async_step_devices()

        suggested = {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD}
        return self.async_show_form(
            # Its own step so the form can say that devices are not shown.
            step_id="web_login" if web_only else "credentials",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={
                "model": self._model or "Milesight",
                **(TOPIC_PLACEHOLDER if web_only else {}),
            },
        )

    async def async_step_web_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take the web login of a model without a usable network server."""
        return await self.async_step_credentials(user_input)

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
                title=entry_title(self._data),
                data=self._data,
                options={
                    CONF_DATA_SOURCE: user_input.get(CONF_DATA_SOURCE, SOURCE_LORAWAN),
                    CONF_DEVICES: user_input.get(CONF_DEVICES, []),
                },
            )
        # New entries default to the LoRaWAN source: no broker needed, and
        # every model Milesight publishes a codec for is known.
        schema: dict[Any, Any] = {
            vol.Required(CONF_DATA_SOURCE, default=SOURCE_LORAWAN): source_selector()
        }
        # Without a device list there is nothing to pick; all are taken, and
        # the options can narrow them down later.
        if self._choices:
            schema[vol.Optional(CONF_DEVICES, default=[])] = device_selector(
                self._choices
            )
        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(schema),
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
                await _validator(entry.data)(self.hass, {**entry.data, **user_input})
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
                STEP_WEB_LOGIN_DATA_SCHEMA
                if gateway_only(entry.data)
                else STEP_REAUTH_DATA_SCHEMA,
                suggested,
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
                    info = await _validator(entry.data)(self.hass, merged)
                except CannotConnectError:
                    errors["base"] = "cannot_connect"
                except InvalidAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    _LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"
                else:
                    # The new address may be another model; keep the mode in step.
                    updates[CONF_GATEWAY_ONLY] = info["gateway_only"]
            if not errors:
                # Skips the entry being reconfigured, so moving this gateway to
                # a new address is allowed while colliding with another is not.
                self._async_abort_entries_match(
                    {
                        CONF_GATEWAY_URL: merged[CONF_GATEWAY_URL],
                        CONF_PORT: merged[CONF_PORT],
                    }
                )
                # Without a MAC the unique id is derived from the address, so it
                # has to move with it or the new address could be added twice.
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=entry.data.get(CONF_MAC)
                    or f"{merged[CONF_GATEWAY_URL]}:{merged[CONF_PORT]}",
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
                STEP_WEB_RECONFIGURE_DATA_SCHEMA
                if gateway_only(entry.data)
                else STEP_RECONFIGURE_DATA_SCHEMA,
                suggested,
            ),
            errors=errors,
        )


# A gateway-only entry's topics: the check, and the level a Milesight topic
# has in place of "uplink" (the request topic keeps the gateway's spelling).
_TOPICS = {
    CONF_MQTT_TOPIC: (topic_error, None),
    CONF_MQTT_DOWNLINK_TOPIC: (downlink_error, "downlink"),
    CONF_MQTT_ACK_TOPIC: (downlink_error, "ack"),
    CONF_MQTT_ERROR_TOPIC: (downlink_error, "error"),
    CONF_MQTT_JOIN_TOPIC: (downlink_error, "join"),
    CONF_MQTT_REQUEST_TOPIC: (request_error, "reqeust"),
    CONF_MQTT_RESPONSE_TOPIC: (request_error, "response"),
}


def _suggested_topic(uplink: str, kind: str | None) -> str | None:
    """Suggest a topic next to the uplink topic; requests name no device."""
    if kind is None:
        return uplink
    suggested = sibling_topic(uplink, kind)
    if suggested and kind in ("reqeust", "response"):
        return suggested.replace("{dev_eui}", "$deveui")
    return suggested


def _topic_errors(user_input: dict[str, Any]) -> dict[str, str]:
    """Trim the MQTT topics in place and say which ones are unusable."""
    errors: dict[str, str] = {}
    # Emptied, a field is missing: no devices, or no downlinks, any more.
    for key, (check, _kind) in _TOPICS.items():
        topic = user_input[key] = (user_input.get(key) or "").strip()
        if topic and (error := check(topic)):
            errors[key] = error
    return errors


class MilesightGatewayOptionsFlow(OptionsFlowWithReload):
    """Handle options for the Milesight Gateway integration."""

    def __init__(self) -> None:
        """Hold the options while a change of data source waits for its yes."""
        self._pending: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        only_gateway = gateway_only(self.config_entry.data)
        # A gateway-only entry has no device list and no network server: no
        # device choice, no data source, no device settings to send.
        choices = None if only_gateway else self._device_choices()
        errors: dict[str, str] = {}
        if user_input is not None:
            if choices is not None:
                # An emptied selection may arrive as a missing key; without
                # this the merge below would keep the old selection.
                user_input.setdefault(CONF_DEVICES, [])
            if only_gateway:
                errors = _topic_errors(user_input)
            if not errors:
                self._pending = self.config_entry.options | user_input
                if data_source(self._pending) != data_source(self.config_entry.options):
                    return await self.async_step_confirm_source()
                return self.async_create_entry(data=self._pending)

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
        schema[
            vol.Required(
                CONF_DEVICES_PANEL, default=options.get(CONF_DEVICES_PANEL, True)
            )
        ] = bool
        if not only_gateway:
            schema[vol.Required(CONF_DATA_SOURCE, default=data_source(options))] = (
                source_selector()
            )
            schema[
                vol.Required(
                    CONF_ALL_SETTINGS, default=options.get(CONF_ALL_SETTINGS, False)
                )
            ] = bool
            schema[
                vol.Required(
                    CONF_NETWORK_PANEL, default=options.get(CONF_NETWORK_PANEL, True)
                )
            ] = bool
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
        if only_gateway:
            uplink = options.get(CONF_MQTT_TOPIC) or ""
            for key, (_check, kind) in _TOPICS.items():
                suggested = options.get(key, _suggested_topic(uplink, kind))
                schema[
                    vol.Optional(key, description={"suggested_value": suggested})
                ] = str
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders=TOPIC_PLACEHOLDER,
        )

    async def async_step_confirm_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Say what a change of data source keeps and what it drops."""
        if user_input is not None:
            return self.async_create_entry(data=self._pending)
        return self.async_show_form(
            step_id="confirm_source",
            data_schema=vol.Schema({}),
            description_placeholders={
                "old": SOURCE_NAMES[data_source(self.config_entry.options)],
                "new": SOURCE_NAMES[data_source(self._pending)],
            },
        )

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
