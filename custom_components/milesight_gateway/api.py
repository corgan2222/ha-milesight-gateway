"""
Milesight Gateway API wrapper.

Uses MilesightGatewayClient from the Milesight-Gateway-API package to fetch
devices from the gateway, then combines them with the devices_ha.json model
database to produce fully-described MilesightDevice objects ready for HA.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientResponseError, ContentTypeError
from milesight_gateway_api import (
    FirmwareRelease,
    MilesightGatewayClient,
    MilesightGatewayError,
    WebLoginRefusedError,
    get_latest_firmware,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)

_DEVICES_DB_PATH = pathlib.Path(__file__).parent / "devices_ha.json"

# Maps known entity keys to their HA metadata.
# Only keys present here will become entities. A list gives one key several
# entities; they need different platforms, since the unique id is the key.
ENTITY_MAPPING: dict[str, dict[str, Any] | list[dict[str, Any]]] = {
    "battery": {
        "platform": "sensor",
        "device_class": "battery",
        "enabled_by_default": True,
        "entity_category": "diagnostic",
        "state_class": "measurement",
        "unit": "%",
    },
    "temperature": {
        "platform": "sensor",
        "device_class": "temperature",
        "enabled_by_default": True,
        "state_class": "measurement",
        "unit": "°C",
    },
    "humidity": {
        "platform": "sensor",
        "device_class": "humidity",
        "enabled_by_default": True,
        "state_class": "measurement",
        "unit": "%",
    },
    "co2": {
        "platform": "sensor",
        "device_class": "carbon_dioxide",
        "enabled_by_default": True,
        "state_class": "measurement",
        "unit": "ppm",
    },
    "pressure": {
        "platform": "sensor",
        "device_class": "atmospheric_pressure",
        "enabled_by_default": True,
        "state_class": "measurement",
        "unit": "hPa",
    },
    "rssi": {
        "platform": "sensor",
        "device_class": "signal_strength",
        "enabled_by_default": True,
        "entity_category": "diagnostic",
        "state_class": "measurement",
        "unit": "dBm",
    },
    "loRaSNR": {
        "platform": "sensor",
        "icon": "mdi:signal",
        "enabled_by_default": True,
        "entity_category": "diagnostic",
        "state_class": "measurement",
        "unit": "dB",
        "name": "LoRa SNR",
    },
    # Devices on a default codec get the gateway template's key instead.
    "snr": {
        "platform": "sensor",
        "icon": "mdi:signal",
        "enabled_by_default": True,
        "entity_category": "diagnostic",
        "state_class": "measurement",
        "unit": "dB",
        "name": "LoRa SNR",
    },
    "occupancy": {
        "platform": "binary_sensor",
        "device_class": "occupancy",
        "enabled_by_default": True,
    },
    "people_total_counts": [
        {
            "platform": "binary_sensor",
            "device_class": "occupancy",
            "enabled_by_default": True,
            "name": "Occupancy",
        },
        {
            "platform": "sensor",
            "icon": "mdi:account-group",
            "state_class": "measurement",
            "enabled_by_default": True,
            "name": "People count",
        },
    ],
    "pir": {
        "platform": "binary_sensor",
        "device_class": "motion",
        "enabled_by_default": True,
    },
    "pir_trigger": {
        "platform": "binary_sensor",
        "device_class": "motion",
        "enabled_by_default": True,
    },
    "liquid": {
        "platform": "binary_sensor",
        "device_class": "moisture",
        "enabled_by_default": True,
    },
    "leakage_status": {
        "platform": "binary_sensor",
        "device_class": "moisture",
        "enabled_by_default": True,
    },
    "door": {
        "platform": "binary_sensor",
        "device_class": "door",
        "enabled_by_default": True,
    },
    "tamper": {
        "platform": "binary_sensor",
        "device_class": "tamper",
        "enabled_by_default": True,
        "entity_category": "diagnostic",
    },
    # WT101 reports 0 = installed, 1 = uninstalled; above zero reads as on.
    "tamper_status": {
        "platform": "binary_sensor",
        "device_class": "tamper",
        "enabled_by_default": True,
        "entity_category": "diagnostic",
        "name": "Tamper",
    },
    "window_detection": {
        "platform": "binary_sensor",
        "device_class": "window",
        "enabled_by_default": True,
        "name": "Window open",
    },
    "valve_opening": {
        "platform": "sensor",
        "icon": "mdi:pipe-valve",
        "state_class": "measurement",
        "enabled_by_default": True,
        "unit": "%",
        "name": "Valve opening",
    },
    # Stateless press events; the event platform maps the payload values.
    "button": {
        "platform": "event",
        "enabled_by_default": True,
        "name": "Button",
    },
    "button_event": {
        "platform": "event",
        "enabled_by_default": True,
        "name": "Button",
    },
    # Marks the device as a thermostat; the climate platform reads the
    # temperature keys itself and writes settings as downlinks.
    "target_temperature": {
        "platform": "climate",
        "enabled_by_default": True,
        "name": "Thermostat",
    },
    "sn": {
        "platform": "sensor",
        "enabled_by_default": False,
        "entity_category": "diagnostic",
    },
    "devEUI": {
        "platform": "sensor",
        "enabled_by_default": False,
        "entity_category": "diagnostic",
        "name": "Device EUI",
    },
    "applicationName": {
        "platform": "sensor",
        "enabled_by_default": False,
        "entity_category": "diagnostic",
        "name": "Application",
    },
    "deviceName": {
        "platform": "sensor",
        "enabled_by_default": False,
        "entity_category": "diagnostic",
        "name": "Device name",
    },
    "mac": {
        "platform": "sensor",
        "enabled_by_default": False,
        "entity_category": "diagnostic",
        "name": "MAC",
    },
    "gw": {
        "platform": "sensor",
        "enabled_by_default": False,
        "entity_category": "diagnostic",
        "name": "Gateway",
    },
}


def _format_name(key: str) -> str:
    """Turn a snake_case or camelCase key into a readable name."""
    return key.replace("_", " ").capitalize()


@dataclass
class EntityDefinition:
    """Describes one measurable value exposed by a device."""

    key: str
    name: str
    platform: str  # "sensor", "binary_sensor", "event" or "climate"
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    entity_category: str | None = None
    icon: str | None = None
    enabled_by_default: bool = True


@dataclass
class MilesightDevice:
    """A LoRaWAN device registered on the gateway."""

    dev_eui: str
    name: str
    model: str  # human-readable description, e.g. "Indoor Ambience Monitoring Sensor"
    model_id: str  # short device type ID from devices_ha.json, e.g. "am307l"
    data_topic: str
    entities: list[EntityDefinition] = field(default_factory=list)


def _load_devices_db() -> dict:
    """Load the devices_ha.json model database."""
    try:
        return json.loads(_DEVICES_DB_PATH.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError, UnicodeDecodeError:
        _LOGGER.exception("Failed to load devices_ha.json")
        return {"devices": []}


def _get_device_details(
    dev_eui: str, devices_db: dict, payload_name: str = ""
) -> dict | None:
    """
    Return the best-matching device model entry for this EUI.

    First, all entries whose ``deveui`` prefix (first 9 chars) matches are
    collected.  If exactly one is found it is returned immediately.  When
    multiple entries share the same prefix the ``payload_name`` from the
    gateway is compared case-insensitively against the ``name`` field to pick
    the correct model.  If no name match is found the first candidate is
    returned with a warning so we always produce *some* result.
    """
    prefix = dev_eui[:9].lower()
    candidates = [
        d
        for d in devices_db.get("devices", [])
        if d.get("deveui", "").lower() == prefix and "device_entities" in d
    ]

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # Strip known firmware-variant suffixes before comparing.
    _payload_name_suffixes = ("_new", "_esec")
    normalized = payload_name.lower()
    for suffix in _payload_name_suffixes:
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break

    # Multiple models share this EUI prefix — try to disambiguate by name.
    payload_name_lower = normalized
    for candidate in candidates:
        if candidate.get("name", "").lower() == payload_name_lower:
            return candidate

    _LOGGER.warning(
        "Multiple model definitions found for EUI prefix %s (%s), "
        "none matched payloadName %r (normalized: %r) — using first entry (%s)",
        prefix,
        [c.get("name") for c in candidates],
        payload_name,
        normalized,
        candidates[0].get("name"),
    )
    return candidates[0]


def _build_device(
    raw_device: dict, device_details: dict, mqtt_base_topic: str
) -> MilesightDevice:
    """Build a MilesightDevice from gateway data + model definition."""
    dev_eui = raw_device.get("devEUI", "")
    name = raw_device.get("name") or "Unknown Device"
    model = device_details.get("description") or device_details.get("name", "")
    model_id = device_details.get("id", "")
    data_topic = f"{mqtt_base_topic}/{dev_eui.lower()}"

    seen_keys: set[str] = set()
    entities: list[EntityDefinition] = []

    for key in device_details.get("device_entities", []):
        if key in seen_keys:
            continue
        seen_keys.add(key)

        mapping = ENTITY_MAPPING.get(key)
        if mapping is None:
            continue

        entities.extend(
            EntityDefinition(
                key=key,
                name=spec.get("name", _format_name(key)),
                platform=spec.get("platform", "sensor"),
                unit=spec.get("unit"),
                device_class=spec.get("device_class"),
                state_class=spec.get("state_class"),
                entity_category=spec.get("entity_category"),
                icon=spec.get("icon"),
                enabled_by_default=spec.get("enabled_by_default", True),
            )
            for spec in (mapping if isinstance(mapping, list) else [mapping])
        )

    return MilesightDevice(
        dev_eui=dev_eui,
        name=name,
        model=model,
        model_id=model_id,
        data_topic=data_topic,
        entities=entities,
    )


@dataclass(frozen=True)
class GatewayInfo:
    """What the gateway reports about itself without a login."""

    model: str | None
    firmware: str


@dataclass(frozen=True)
class GatewayDetails:
    """What the gateway's web interface reports after a login."""

    model_id: str | None
    serial_number: str | None
    hw_version: str | None
    # From firmware 60.0.0.50 on; older firmware and the UG63 report none.
    hostname: str | None = None


# Role of a gateway in a Gateway Fleet, from the web interface's summary.
FLEET_MASTER = "master"
FLEET_NODE = "node"
FLEET_STANDALONE = "standalone"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class SystemStatus:
    """Load and resources the web interface reports."""

    cpu_load: float | None
    memory_available: float | None
    storage_available: float | None
    uptime: int | None
    fleet_role: str | None = None


def fleet_role(system: dict[str, Any]) -> str | None:
    """
    Return whether the gateway leads a fleet, forwards to one, or stands alone.

    A master counts fleet gateways. A node forwards its packets to a network
    server elsewhere. Firmware without the packet forwarder block (UG63,
    60.0.0.48 and older) tells neither, so the role stays unknown.
    """
    if (_int(system.get("gateway_fleet_total")) or 0) > 0:
        return FLEET_MASTER
    forwarder = system.get("packet_forwarder")
    if not isinstance(forwarder, dict):
        return None
    for destination in _items(forwarder.get("destinations")):
        address = str(destination.get("serv_addr") or "").strip().lower()
        if destination.get("serv_enabled") is True and address not in _LOCAL_HOSTS:
            return FLEET_NODE
    return FLEET_STANDALONE


@dataclass(frozen=True)
class FleetGateway:
    """A gateway the network server receives packets from."""

    eui: str
    name: str
    connected: bool | None
    # The gateway reports its local time without a zone.
    last_seen: datetime | None


@dataclass(frozen=True)
class ApplicationLink:
    """The MQTT link of an application that has devices."""

    app_id: str
    name: str
    host: str | None
    connected: bool | None


@dataclass(frozen=True)
class Packet:
    """One packet from the network server's packet log."""

    key: tuple[str, ...]
    time: datetime
    dev_eui: str
    uplink: bool
    spread_factor: int | None
    frequency: int | None


@dataclass(frozen=True)
class NetworkStatus:
    """What the network server reports about itself and its gateways."""

    gateway_eui: str | None
    forwarder_connected: bool | None
    pending_packets: int | None
    gateways: list[FleetGateway]
    links: list[ApplicationLink]
    application_count: int | None
    codec_count: int | None
    profile_count: int | None
    codec_library_version: str | None
    packets: list[Packet]


# The packet log keeps the last 1000 packets; this many newest ones are read
# per refresh, which covers several minutes of a busy network.
PACKET_WINDOW = 300
# The whole device list in one request. Paging by offset is no snapshot: on a
# busy network the gateway reorders the list between requests, and per full
# read 11 to 24 of 646 devices fell between pages (UG65 fleet, 2026-11-24), so
# their entities flickered unavailable. One page of 646 took 15 to 20 s there.
DEVICE_PAGE_SIZE = 10000
# connectStatus of an MQTT link, as the web interface labels it.
_MQTT_CONNECTED = 3


def _items(value: Any) -> list[dict[str, Any]]:
    """Return the dicts of a list the API reports, ignoring anything else."""
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _int(value: Any) -> int | None:
    """Return a count the API reports as a number or a numeric string."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _percent(value: Any) -> float | None:
    """Return a percentage the web interface reports as text, such as "25.20%"."""
    if not isinstance(value, str):
        return None
    try:
        return float(value.strip().rstrip("%"))
    except ValueError:
        return None


def _share(part: Any, total: Any) -> float | None:
    """Return part of total in percent; numbers may arrive as text."""
    try:
        part, total = float(part), float(total)
    except TypeError, ValueError:
        return None
    if total <= 0 or not 0 <= part <= total:
        return None
    return round(part / total * 100, 2)


def _local_time(value: Any) -> datetime | None:
    """Parse a "YYYY-MM-DD HH:MM:SS" time, which the gateway gives without zone."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
    except ValueError:
        return None


def _packet(raw: dict[str, Any]) -> Packet | None:
    """Return a packet log entry, or None if it lacks a time or device."""
    try:
        time = datetime.fromisoformat(str(raw.get("time")))
    except ValueError:
        return None
    dev_eui = raw.get("devEUI")
    kind = str(raw.get("type", ""))
    if not isinstance(dev_eui, str) or not dev_eui or time.tzinfo is None:
        return None
    return Packet(
        key=(str(raw.get("time")), dev_eui, str(raw.get("fCnt")), kind),
        time=time,
        dev_eui=dev_eui.lower(),
        uplink=kind.startswith("Up"),
        spread_factor=_int(raw.get("spreadFactor")),
        frequency=_int(raw.get("frequency")),
    )


def _text(value: Any) -> str | None:
    """Return a reported text, or None for a missing or placeholder value."""
    return value if isinstance(value, str) and value not in ("", "-") else None


async def async_get_latest_firmware(
    session: ClientSession, family: str
) -> FirmwareRelease | None:
    """Return the newest release of ``family`` from Milesight's download page."""
    with _as_api_error():
        return await get_latest_firmware(session, family)


class APIAuthError(Exception):
    """Raised when authentication with the gateway fails."""


class APIConnectionError(Exception):
    """Raised when the gateway cannot be reached."""


@contextmanager
def _as_api_error() -> Iterator[None]:
    """Translate the client's exceptions into this integration's two types."""
    try:
        yield
    except WebLoginRefusedError as err:
        msg = "Gateway refused the web interface login"
        raise APIAuthError(msg) from err
    except MilesightGatewayError as err:
        msg = f"Unexpected answer from gateway: {err}"
        raise APIConnectionError(msg) from err
    except ContentTypeError as err:
        # Subclasses ClientResponseError, so it must be caught first; its
        # .status is the real status (usually 200), not an auth code.
        msg = "Gateway returned a non-JSON response"
        raise APIConnectionError(msg) from err
    except ClientResponseError as err:
        if err.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            msg = f"Gateway rejected the credentials (HTTP {err.status})"
            raise APIAuthError(msg) from err
        msg = f"Gateway returned HTTP {err.status}"
        raise APIConnectionError(msg) from err
    except ClientError as err:
        msg = f"Cannot reach gateway: {err}"
        raise APIConnectionError(msg) from err
    except TimeoutError as err:
        # Not an aiohttp.ClientError, so it needs its own clause.
        msg = "Timeout talking to gateway"
        raise APIConnectionError(msg) from err
    except json.JSONDecodeError as err:
        # Before ValueError below — JSONDecodeError is a ValueError.
        msg = "Malformed response from gateway"
        raise APIConnectionError(msg) from err
    except ValueError as err:
        # The client builds its AES cipher before any request, so a
        # wrong-length secret key or IV fails here, not over the network.
        msg = f"Invalid secret key or IV: {err}"
        raise APIAuthError(msg) from err
    except (AttributeError, TypeError) as err:
        # The client calls .get() on the decoded body, so a JSON response
        # that is not an object (null, a list, a bare string) fails there.
        msg = "Unexpected response from gateway"
        raise APIConnectionError(msg) from err


class MilesightGatewayAPI:
    """Wraps MilesightGatewayClient with HA-friendly async methods."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        gateway_url: str,
        port: str | int,
        username: str,
        password: str,
        secret_key: str,
        iv: str,
        mqtt_base_topic: str,
    ) -> None:
        """Initialize."""
        self.connected: bool = False
        self._mqtt_base_topic = mqtt_base_topic
        self._devices_db: dict = {}
        self._client = MilesightGatewayClient(
            username=username,
            password=password,
            secret_key=secret_key.encode("utf-8"),
            iv=iv.encode("utf-8"),
            base_url=gateway_url,
            port=port,
        )

    @property
    def web_login_refused(self) -> bool:
        """Whether the web interface refused the login; it is not tried again."""
        return self._client.web_login_refused

    @web_login_refused.setter
    def web_login_refused(self, refused: bool) -> None:
        self._client.web_login_refused = refused

    async def async_connect(self, session: ClientSession) -> bool:
        """Authenticate and obtain a JWT token from the gateway."""
        self._devices_db = await asyncio.to_thread(_load_devices_db)
        self.connected = False

        with _as_api_error():
            token = await self._client.get_jwt_token(session)

        if not token:
            # The client returns None instead of raising when the gateway
            # answers 200 without a "jwt" key, so this is the only signal.
            msg = "Gateway returned no JWT token"
            raise APIAuthError(msg)

        self.connected = True
        return True

    async def async_get_gateway_info(self, session: ClientSession) -> GatewayInfo:
        """
        Return the gateway's model and firmware version.

        The web interface's login check answers without a session and carries
        both, while the network server API has no call for either.
        """
        with _as_api_error():
            payload = await self._client.get_login_status(session)
        version = payload.get("rtver")
        if not isinstance(version, str) or not version:
            msg = "Gateway did not report a firmware version"
            raise APIConnectionError(msg)
        return GatewayInfo(model=_text(payload.get("model")), firmware=version)

    async def async_get_gateway_details(self, session: ClientSession) -> GatewayDetails:
        """Return the gateway's model number, serial number and hardware version."""
        system = await self._async_web_system(session)
        return GatewayDetails(
            model_id=_text(system.get("model")),
            serial_number=_text(system.get("sn")),
            hw_version=_text(system.get("hardware_ver")),
            hostname=_text(system.get("hostname")),
        )

    async def async_get_system_status(self, session: ClientSession) -> SystemStatus:
        """Return CPU load, free memory and storage, and uptime."""
        system = await self._async_web_system(session)
        uptime = system.get("uptime")
        memory = system.get("memory_avail_percent")
        return SystemStatus(
            cpu_load=_percent(system.get("cpu_load")),
            # The UG63 reports no percentage, only free and total megabytes.
            memory_available=_percent(memory)
            if memory is not None
            else _share(system.get("memory_free"), system.get("memory_total")),
            storage_available=_percent(system.get("emmc_avail_percent")),
            uptime=uptime if isinstance(uptime, int) and uptime >= 0 else None,
            fleet_role=fleet_role(system),
        )

    async def _async_web_system(self, session: ClientSession) -> dict[str, Any]:
        """
        Return the system block of the web interface's status summary.

        The client stops logging in after one refusal, because the gateway
        locks an account after a few failed web logins. The refusal is logged
        once, when it happens.
        """
        refused_before = self._client.web_login_refused
        try:
            with _as_api_error():
                return await self._client.get_system_info(session)
        except APIAuthError:
            if self._client.web_login_refused and not refused_before:
                _LOGGER.warning(
                    "The gateway's web interface refused the configured account, "
                    "so its system status and serial number are not shown. Not "
                    "retried until the integration is reloaded, so the account is "
                    "not locked"
                )
            raise

    async def async_get_network_status(self, session: ClientSession) -> NetworkStatus:
        """Return what the network server reports about itself and its gateways."""
        try:
            return await self._async_network_status(session)
        except APIAuthError:
            # An expired JWT looks like a bad password; log in once more.
            await self.async_connect(session)
            return await self._async_network_status(session)

    async def _async_network_status(self, session: ClientSession) -> NetworkStatus:
        """Read the network server's status calls."""
        client = self._client
        with _as_api_error():
            forwarder, _ = await client.get_packet_forwarder_info(session)
            gateways = await client.get_gateways(session)
            apps = await client.get_applications(session)
            _, codec_count = await client.get_payload_codecs_short(session)
            codec_setting = await client.get_payload_codec_settings(session)
            _, profile_count = await client.get_profiles(session, "0")
            packets = await client.get_packets(session, PACKET_WINDOW)

        links = []
        for app in _items(apps.get("result")):
            # Only applications with devices forward anything; the others are
            # configured but idle and their link status means nothing.
            if not app.get("using") or "URMQTT" not in (app.get("kinds") or []):
                continue
            with _as_api_error():
                mqtt = await client.get_data_transmission_integration(
                    session, app.get("id"), "mqtt"
                )
            if not isinstance(mqtt, dict):
                msg = f"Unexpected MQTT settings of application {app.get('id')}"
                raise APIConnectionError(msg)
            status = mqtt.get("connectStatus")
            links.append(
                ApplicationLink(
                    app_id=str(app.get("id")),
                    name=_text(app.get("name")) or f"Application {app.get('id')}",
                    host=_text(mqtt.get("host")),
                    connected=status == _MQTT_CONNECTED
                    if isinstance(status, int)
                    else None,
                )
            )

        return NetworkStatus(
            gateway_eui=_text(forwarder.get("gatewayEui")),
            forwarder_connected=forwarder.get("status") == 1
            if "status" in forwarder
            else None,
            pending_packets=_int(forwarder.get("pendingData")),
            gateways=[
                FleetGateway(
                    eui=str(gw.get("mac")),
                    name=_text(gw.get("name")) or str(gw.get("mac")),
                    connected=gw.get("connected")
                    if isinstance(gw.get("connected"), bool)
                    else None,
                    last_seen=_local_time(gw.get("lastSeenAt")),
                )
                for gw in _items(gateways.get("result"))
                if gw.get("mac")
            ],
            links=links,
            application_count=_int(apps.get("totalCount")),
            codec_count=_int(codec_count),
            profile_count=_int(profile_count),
            codec_library_version=_text(codec_setting.get("version")),
            packets=[
                p
                for p in (_packet(raw) for raw in _items(packets.get("packets")))
                if p is not None
            ],
        )

    async def async_enqueue_downlink(
        self,
        session: ClientSession,
        dev_eui: str,
        command: dict[str, Any],
        *,
        fport: int = 85,
        confirmed: bool = False,
    ) -> None:
        """Queue a downlink; the gateway delivers it after the next uplink."""
        try:
            await self._async_post_downlink(
                session, dev_eui, command, fport=fport, confirmed=confirmed
            )
        except APIAuthError:
            # An expired JWT looks like bad credentials; log in once more.
            await self.async_connect(session)
            await self._async_post_downlink(
                session, dev_eui, command, fport=fport, confirmed=confirmed
            )

    async def _async_post_downlink(
        self,
        session: ClientSession,
        dev_eui: str,
        command: dict[str, Any],
        *,
        fport: int,
        confirmed: bool,
    ) -> None:
        """Hand one queue item to the client, translating any failure."""
        with _as_api_error():
            await self._client.enqueue_downlink(
                session, dev_eui, command, fport=fport, confirmed=confirmed
            )

    async def _fetch_devices(self, session: ClientSession) -> list[dict]:
        """Fetch the raw device list, translating any failure."""
        with _as_api_error():
            all_devices, _ = await self._client.get_all_devices(
                session, limit=DEVICE_PAGE_SIZE
            )
        return all_devices

    async def async_get_devices(
        self, session: ClientSession
    ) -> tuple[list[MilesightDevice], int, int]:
        """Fetch devices; return (active_list, online_count, offline_count)."""
        try:
            all_devices = await self._fetch_devices(session)
        except APIAuthError:
            # A JWT that expired mid-session looks the same as a bad password,
            # so log in once more before reporting the credentials as wrong.
            await self.async_connect(session)
            all_devices = await self._fetch_devices(session)

        result: list[MilesightDevice] = []
        online_count = 0
        offline_count = 0

        for raw in all_devices:
            if not isinstance(raw, dict):
                continue
            if raw.get("active"):
                online_count += 1
            else:
                offline_count += 1
                continue

            dev_eui = raw.get("devEUI")
            if not isinstance(dev_eui, str) or not dev_eui:
                continue

            # The gateway sends null for a device without a payload codec.
            payload_name = raw.get("payloadName") or ""
            device_details = _get_device_details(
                dev_eui, self._devices_db, payload_name
            )
            if not device_details:
                _LOGGER.debug("No model definition found for device %s", dev_eui)
                continue

            result.append(_build_device(raw, device_details, self._mqtt_base_topic))

        return result, online_count, offline_count
