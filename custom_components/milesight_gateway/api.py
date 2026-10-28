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
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientResponseError, ContentTypeError
from milesight_gateway_api.milesight_gateway_client import MilesightGatewayClient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)

_DEVICES_DB_PATH = pathlib.Path(__file__).parent / "devices_ha.json"

# Maps known entity keys to their HA metadata.
# Only keys present here will become entities.
ENTITY_MAPPING: dict[str, dict[str, Any]] = {
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
    "occupancy": {
        "platform": "binary_sensor",
        "device_class": "occupancy",
        "enabled_by_default": True,
    },
    "people_total_counts": {
        "platform": "binary_sensor",
        "unit_of_measurement": "people",
        "icon": "mdi:account-group",
        "device_class": "occupancy",
        "enabled_by_default": True,
    },
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
    platform: str  # "sensor" or "binary_sensor"
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
    name = raw_device.get("name", "Unknown Device")
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

        entities.append(
            EntityDefinition(
                key=key,
                name=mapping.get("name", _format_name(key)),
                platform=mapping.get("platform", "sensor"),
                unit=mapping.get("unit"),
                device_class=mapping.get("device_class"),
                state_class=mapping.get("state_class"),
                entity_category=mapping.get("entity_category"),
                icon=mapping.get("icon"),
                enabled_by_default=mapping.get("enabled_by_default", True),
            )
        )

    return MilesightDevice(
        dev_eui=dev_eui,
        name=name,
        model=model,
        model_id=model_id,
        data_topic=data_topic,
        entities=entities,
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

    async def _fetch_devices(self, session: ClientSession) -> list[dict]:
        """Fetch the raw device list, translating any failure."""
        with _as_api_error():
            all_devices, _ = await self._client.get_all_devices(session)
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
            if raw.get("active"):
                online_count += 1
            else:
                offline_count += 1
                continue

            dev_eui = raw.get("devEUI")
            if not dev_eui:
                continue

            payload_name = raw.get("payloadName", "")
            device_details = _get_device_details(
                dev_eui, self._devices_db, payload_name
            )
            if not device_details:
                _LOGGER.debug("No model definition found for device %s", dev_eui)
                continue

            result.append(_build_device(raw, device_details, self._mqtt_base_topic))

        return result, online_count, offline_count
