"""
Milesight Gateway API wrapper.

Uses MilesightGatewayClient from the Milesight-Gateway-API package to fetch
the gateway's device list, status and firmware. Each device's model comes from
Milesight's codec catalog bundled with that package.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientResponseError, ContentTypeError
from milesight_gateway_api import (
    FirmwareRelease,
    MilesightGatewayClient,
    MilesightGatewayError,
    WebLoginRefusedError,
    decoder,
    download_firmware,
    get_firmware_releases,
    get_latest_firmware,
)
from milesight_gateway_api.lorawan import identify, is_online

from .const import DEVICE_TIMEOUT

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)


@dataclass
class MilesightDevice:
    """A device the gateway's network server lists, with its catalog model."""

    dev_eui: str
    name: str
    model: str  # catalog description, e.g. "Indoor Ambience Monitoring Sensor"
    model_id: str  # catalog model ID, e.g. "am307l"
    # Whether the network server still hears it. A device that stopped
    # reporting stays listed, so its entities stay, unavailable.
    online: bool = True
    # Minutes without an uplink after which the network server calls the
    # device offline; its own setting per device.
    timeout: int = DEVICE_TIMEOUT


def _timeout(value: Any) -> int:
    """Read the device timeout in minutes; foreign JSON, so anything may come."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEVICE_TIMEOUT
    return int(value) if value > 0 else DEVICE_TIMEOUT


def _catalog_device(raw: dict[str, Any]) -> MilesightDevice | None:
    """
    Describe a network server record by Milesight's codec catalog, or None.

    The model comes from the assigned codec's name (``WT101``, ``VS340_esec``)
    and falls back to the devEUI prefix, as the LoRaWAN source does it.
    """
    dev_eui = raw.get("devEUI")
    if not isinstance(dev_eui, str) or not dev_eui:
        return None
    _brand, model_id = identify(raw)
    model = decoder.models().get(model_id)
    if model is None:
        return None
    return MilesightDevice(
        dev_eui=dev_eui,
        name=raw.get("name") or dev_eui,
        model=model.get("description") or model["name"],
        model_id=model_id,
        online=is_online(raw),
        timeout=_timeout(raw.get("timeout")),
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
    # What the network server knows of a fleet member besides its radio; all
    # None for the gateway it runs on, and on firmware without these fields.
    wan_connected: bool | None = None
    ip: str | None = None
    uptime: int | None = None  # seconds
    system_ok: bool | None = None
    cellular_connected: bool | None = None
    cellular_signal: int | None = None  # dBm


@dataclass(frozen=True)
class ApplicationLink:
    """The MQTT link of an application that has devices."""

    app_id: str
    name: str
    host: str | None
    connected: bool | None


@dataclass(frozen=True)
class MqttTopics:
    """The topics one application's MQTT link uses, as the gateway names them."""

    uplink: str
    downlink: str | None = None
    ack: str | None = None
    error: str | None = None
    join: str | None = None
    request: str | None = None
    response: str | None = None


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


_SIGNAL = re.compile(r"(-?\d+)\s*dBm")
_NO_ADDRESS = ("", "0.0.0.0")  # noqa: S104 - what the gateway says for "none"


def _connected(value: Any) -> bool | None:
    """Read a "Connected" or "Disconnected" text; empty means not known."""
    text = _text(value)
    return None if text is None else text.lower() == "connected"


def _fleet_detail(raw: Any) -> dict[str, Any]:
    """Read a fleet gateway's detail; foreign JSON, so every field may be off."""
    if not isinstance(raw, dict):
        return {}
    address = _text(raw.get("networkIP"))
    try:
        uptime = int(raw.get("systemRuntime"))
    except TypeError, ValueError:
        uptime = None
    codes = raw.get("systemRunningStatus")
    signal = _SIGNAL.search(str(raw.get("cellularSignal") or ""))
    wan = _connected(raw.get("wanStatus"))
    return {
        "wan_connected": wan,
        "ip": address if address not in _NO_ADDRESS else None,
        "uptime": uptime if uptime and uptime > 0 else None,
        # A list of codes; 0 alone is "Normal" on the gateway's own page.
        "system_ok": codes == [0] if isinstance(codes, list) and codes else None,
        # Without WAN data the gateway reports nothing about itself at all.
        "cellular_connected": _connected(raw.get("cellularNetworkStatus"))
        if wan is not None
        else None,
        "cellular_signal": int(signal.group(1)) if signal and wan is not None else None,
    }


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


async def async_get_firmware_releases(
    session: ClientSession, family: str
) -> list[FirmwareRelease]:
    """Return the releases of ``family`` on Milesight's download page, oldest first."""
    with _as_api_error():
        return await get_firmware_releases(session, family)


async def async_download_firmware(
    session: ClientSession, release: FirmwareRelease
) -> bytes:
    """
    Download a release's image from Milesight.

    Not through _as_api_error: this talks to Milesight, not to the gateway,
    and an incomplete download is a ValueError, which there means a bad key.
    """
    try:
        return await download_firmware(session, release)
    except ValueError as err:
        raise APIConnectionError(str(err)) from err
    except (ClientError, TimeoutError) as err:
        msg = f"Cannot download from Milesight: {err or type(err).__name__}"
        raise APIConnectionError(msg) from err


def system_status(system: dict[str, Any]) -> SystemStatus:
    """Read the system block of a web interface's status summary."""
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


# For the modules that talk to a gateway with a client of their own.
as_api_error = _as_api_error


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
    ) -> None:
        """Initialize."""
        self.connected: bool = False
        self._account = {
            "username": username,
            "password": password,
            "secret_key": secret_key.encode("utf-8"),
            "iv": iv.encode("utf-8"),
            "port": port,
        }
        self._client = MilesightGatewayClient(
            username=username,
            password=password,
            secret_key=secret_key.encode("utf-8"),
            iv=iv.encode("utf-8"),
            base_url=gateway_url,
            port=port,
        )

    @property
    def client(self) -> MilesightGatewayClient:
        """The client of the gateway itself, with its web session."""
        return self._client

    def member_client(self, address: str) -> MilesightGatewayClient:
        """Return a client for a fleet member at that address, with this account."""
        # Fleet members answer on HTTPS, whatever the hub's own URL says.
        return MilesightGatewayClient(base_url=f"https://{address}", **self._account)

    def around(self, client: MilesightGatewayClient) -> MilesightGatewayAPI:
        """Return these calls for another gateway's client, such as a member's."""
        other = copy.copy(self)
        other._client = client  # noqa: SLF001 - a copy of this very class
        return other

    @property
    def web_login_refused(self) -> bool:
        """Whether the web interface refused the login; it is not tried again."""
        return self._client.web_login_refused

    @web_login_refused.setter
    def web_login_refused(self, refused: bool) -> None:
        self._client.web_login_refused = refused

    async def async_connect(self, session: ClientSession) -> bool:
        """Authenticate and obtain a JWT token from the gateway."""
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

    async def async_download_backup(self, session: ClientSession) -> bytes:
        """Return the gateway's full configuration backup."""
        with _as_api_error():
            return await self._client.download_backup(session)

    async def async_upgrade_firmware(
        self, session: ClientSession, image: bytes
    ) -> None:
        """Upload a firmware image; the gateway installs it and reboots."""
        with _as_api_error():
            await self._client.upgrade_firmware(session, image)

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
        return system_status(await self._async_web_system(session))

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

        local = _text(forwarder.get("gatewayEui"))
        details: dict[str, dict[str, Any]] = {}
        for gw in _items(gateways.get("result")):
            mac = gw.get("mac")
            if not mac or (local and str(mac).upper() == local.upper()):
                continue  # about itself the network server knows nothing here
            try:
                with _as_api_error():
                    details[str(mac)] = _fleet_detail(
                        await client.get_gateway(session, str(mac))
                    )
            except APIConnectionError as err:
                # One member's detail is no reason to lose the whole status.
                _LOGGER.debug("No detail of fleet gateway %s: %s", mac, err)

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
                    **details.get(str(gw.get("mac")), {}),
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

    async def async_get_mqtt_topics(self, session: ClientSession) -> list[MqttTopics]:
        """Return the MQTT topics of every application that publishes by MQTT."""
        client = self._client
        with _as_api_error():
            apps = await client.get_applications(session)
        found = []
        for app in _items(apps.get("result")):
            if "URMQTT" not in (app.get("kinds") or []):
                continue
            with _as_api_error():
                mqtt = await client.get_data_transmission_integration(
                    session, app.get("id"), "mqtt"
                )
            if isinstance(mqtt, dict) and (uplink := _text(mqtt.get("uplinkTopic"))):
                found.append(
                    MqttTopics(
                        uplink=uplink,
                        downlink=_text(mqtt.get("downlinkTopic")),
                        ack=_text(mqtt.get("ackTopic")),
                        error=_text(mqtt.get("errorTopic")),
                        join=_text(mqtt.get("joinTopic")),
                        request=_text(mqtt.get("requestTopic")),
                        response=_text(mqtt.get("responseTopic")),
                    )
                )
        return found

    async def async_enqueue_downlink(
        self,
        session: ClientSession,
        dev_eui: str,
        data: bytes,
        *,
        fport: int = 85,
        reference: str | None = None,
    ) -> None:
        """
        Queue encoded bytes; the gateway delivers them after the next uplink.

        The bytes pass the device's payload codec, so a gateway codec without
        an encoder does not matter. With a reference the downlink is
        confirmed: the gateway reports its ACK on the application's MQTT ACK
        topic and repeats the reference there (checked 2026-11-26, firmware
        60.0.0.51).
        """
        body: dict[str, Any] = {
            "confirmed": reference is not None,
            "devEUI": dev_eui,
            "fPort": fport,
            "data": base64.b64encode(data).decode("ascii"),
        }
        if reference is not None:
            body["reference"] = reference
        await self.async_ns_call(session, "POST", f"/api/devices/{dev_eui}/queue", body)

    async def _fetch_devices(self, session: ClientSession) -> list[dict]:
        """Fetch the raw device list, translating any failure."""
        with _as_api_error():
            all_devices, _ = await self._client.get_all_devices(
                session, limit=DEVICE_PAGE_SIZE
            )
        return all_devices

    async def async_ns_call(
        self,
        session: ClientSession,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call the network server's API; an expired login is renewed once."""
        try:
            return await self._async_ns_call(session, method, path, body)
        except APIAuthError:
            await self.async_connect(session)
            return await self._async_ns_call(session, method, path, body)

    async def _async_ns_call(
        self,
        session: ClientSession,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        client = self._client
        with _as_api_error():
            async with session.request(
                method,
                f"{client.api_url}{path}",
                json=body,
                headers=client.headers,
                ssl=False,
            ) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
        data = data if isinstance(data, dict) else {}
        if data.get("error"):
            msg = f"{method} {path}: {data['error']}"
            raise APIConnectionError(msg)
        return data

    async def async_get_device_euis(self, session: ClientSession) -> set[str]:
        """Return every devEUI the network server knows, active or not."""
        try:
            all_devices = await self._fetch_devices(session)
        except APIAuthError:
            await self.async_connect(session)
            all_devices = await self._fetch_devices(session)
        return {
            raw["devEUI"].upper()
            for raw in all_devices
            if isinstance(raw, dict) and isinstance(raw.get("devEUI"), str)
        }

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
            # "active" stays true for a device that stopped reporting; the
            # network server's status tells. One it deactivated is left out.
            if is_online(raw):
                online_count += 1
            else:
                offline_count += 1
            if not raw.get("active"):
                continue

            if (device := _catalog_device(raw)) is None:
                _LOGGER.debug("No catalog model for device %s", raw.get("devEUI"))
                continue
            result.append(device)

        return result, online_count, offline_count
