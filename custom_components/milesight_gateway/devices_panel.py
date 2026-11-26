"""
The device page: all devices of all entries, their settings and downlinks.

Pick devices, set what their models have in common, look at the bytes, send,
and follow each command until the device confirms it. The page talks to the
commands below; they send downlinks, so only administrators may use them.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from lorawan_connection import DownlinkError
from milesight_gateway_api import decoder

from . import link
from .const import CONF_DEVICES_PANEL, DEVICE_TIMEOUT, DOMAIN
from .importer import async_choices, async_import
from .lorawan_coordinator import device_eui
from .sync import can_sync

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import MilesightDevice
    from .lorawan_coordinator import MilesightLoRaWANCoordinator

try:  # Home Assistant 2026.11 moved websocket schemas to probatio.
    import probatio as schema
except ImportError:  # pragma: no cover - older Home Assistant
    import voluptuous as schema

_LOGGER = logging.getLogger(__name__)

URL_BASE = f"/{DOMAIN}_devices_static"
SCRIPT = "milesight-devices.js"
PANEL_PATH = "milesight-devices"
PANEL_ELEMENT = "milesight-devices-panel"
_SET_UP = f"{DOMAIN}_devices_page"
_URL = f"{DOMAIN}_devices_url"
_SIDEBAR = f"{DOMAIN}_devices_sidebar"
_LOCK = f"{DOMAIN}_devices_lock"
# Between two downlinks of one send; the gateway's API throttles bursts.
SEND_PAUSE = 0.2
_FIELD_KEYS = (
    "name",
    "type",
    "unit",
    "min",
    "max",
    "values",
    "value_type",
    "default",
)


async def async_setup_devices_page(hass: HomeAssistant) -> None:
    """Register the commands and the page, once."""
    lock = hass.data.setdefault(_LOCK, asyncio.Lock())
    async with lock:
        if hass.data.get(_SET_UP):
            # An options change reloads the entry and lands here again.
            await _async_register_panel(hass)
            return
        hass.data[_SET_UP] = True
        for command in (
            ws_devices,
            ws_preview,
            ws_send,
            ws_log,
            ws_import_choices,
            ws_import,
        ):
            websocket_api.async_register_command(hass, command)
        if "frontend" not in hass.config.components:
            return  # tests, headless: the commands are enough
        folder = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_BASE, str(folder), cache_headers=False)]
        )
        digest = await hass.async_add_executor_job(
            lambda: hashlib.sha256((folder / SCRIPT).read_bytes()).hexdigest()[:12]
        )
        await async_setup_component(hass, "panel_custom", {})
        hass.data[_URL] = f"{URL_BASE}/{SCRIPT}?v={digest}"
        await _async_register_panel(hass)


def show_in_sidebar(hass: HomeAssistant) -> bool:
    """
    Whether the page belongs in the sidebar: unless an entry turned it off.

    There is one menu entry for all gateways; switching it off at one of
    them removes it, without visiting every other entry.
    """
    return all(
        entry.options.get(CONF_DEVICES_PANEL, True)
        for entry in hass.config_entries.async_entries(DOMAIN)
    )


async def _async_register_panel(hass: HomeAssistant) -> None:
    """Add the page, in the sidebar or only under its address."""
    if _URL not in hass.data:
        return  # no frontend
    sidebar = show_in_sidebar(hass)
    if hass.data.get(_SIDEBAR) == sidebar:
        return
    if _SIDEBAR in hass.data:
        frontend.async_remove_panel(hass, PANEL_PATH)
    hass.data[_SIDEBAR] = sidebar
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title="LoRaWAN devices" if sidebar else None,
        sidebar_icon="mdi:access-point" if sidebar else None,
        module_url=hass.data[_URL],
        require_admin=True,
    )


def _entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and entry.runtime_data.devices is not None
    ]


def _fields(fields: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: {k: field[k] for k in _FIELD_KEYS if k in field}
        for key, field in fields.items()
    }


def _number(value: Any) -> float | None:
    """Return a reading as a number; MQTT payloads are foreign JSON."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _known_gateways(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """
    Name the gateways the entries know, by their EUI.

    That is each entry's own gateway and the fleet a hub reports. A gateway
    no entry knows stays an EUI.
    """
    registry = dr.async_get(hass)
    known: dict[str, dict[str, Any]] = {}
    for entry in _entries(hass):
        runtime = entry.runtime_data
        data = runtime.diagnostics.data if runtime.diagnostics else None
        network = getattr(data, "network", None)
        for gateway in getattr(network, "gateways", None) or ():
            eui = str(gateway.eui).upper()
            device = registry.async_get_device_by_identifier(
                (DOMAIN, eui), entry.entry_id
            )
            known[eui] = {
                "name": gateway.name,
                "device_id": device.id if device else None,
            }
        if network is not None and network.gateway_eui:
            known[network.gateway_eui.upper()] = {
                "name": entry.title,
                "device_id": runtime.gateway_device_id,
            }
    return known


def _heard_by(
    coordinator: MilesightLoRaWANCoordinator, known: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """List the gateways that heard the device in the last 24 hours, best first."""
    heard = [
        {
            "eui": eui.upper(),
            "rssi": reception.rssi,
            "snr": reception.snr,
            "at": reception.at.isoformat(),
            **known.get(eui.upper(), {"name": None, "device_id": None}),
        }
        for eui, reception in coordinator.link.in_range().items()
        if eui and eui != "?"
    ]
    return sorted(heard, key=lambda h: -(h["rssi"] if h["rssi"] is not None else -999))


def _spreading_factor(
    entry: ConfigEntry, coordinator: MilesightLoRaWANCoordinator
) -> int | None:
    if coordinator.link.spreading_factor is not None:
        return coordinator.link.spreading_factor
    status = entry.runtime_data.diagnostics.data
    packet = (
        status.last_uplinks.get(coordinator.data.descriptor.dev_eui.lower())
        if status
        else None
    )
    return packet.spread_factor if packet else None


def _device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: MilesightLoRaWANCoordinator,
    listed: dict[str, MilesightDevice],
    gateways: dict[str, dict[str, Any]],
) -> dict:
    device = coordinator.data
    registered = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, device_eui(device)), entry.entry_id
    )
    runtime = entry.runtime_data.devices
    uplink = runtime.connection.last_uplinks.get(device.descriptor.dev_eui)
    stats = coordinator.link
    margin = link.link_margin(stats.snr, _spreading_factor(entry, coordinator))
    tracker = coordinator.commands
    kept = (
        entry.runtime_data.settings.get(device_eui(device))
        if (entry.runtime_data.settings)
        else {}
    )
    settings = {
        key: kept.get(key, {"v": device.values[key], "src": "current"})
        for key in device.settings
        if key in device.values or key in kept
    }
    values = device.values
    known = listed.get(device_eui(device))
    # An uplink within the device's timeout counts like on the gateway. It
    # also covers a device another gateway publishes on the same topics.
    heard = uplink is not None and dt_util.utcnow() - uplink.received_at < (
        timedelta(minutes=known.timeout if known else DEVICE_TIMEOUT)
    )
    return {
        "entry_id": entry.entry_id,
        "gateway": entry.title,
        "gateway_device_id": entry.runtime_data.gateway_device_id,
        "heard_by": _heard_by(coordinator, gateways),
        "sends": runtime.sends_downlinks,
        "path": runtime.path,
        "confirms": bool(getattr(device, "confirms", False)),
        "dev_eui": device_eui(device),
        "name": device.descriptor.name,
        "model_id": device.model_id,
        "device_id": registered.id if registered else None,
        # The gateway's verdict where it lists the device, as its own page shows.
        # Without its list only what Home Assistant heard counts.
        "active": not device.closed
        and (
            heard
            or (getattr(device, "active", True) and known is not None and known.online)
        ),
        "interval": stats.interval,
        "last_seen": uplink.received_at.isoformat()
        if uplink
        else (known.last_seen.isoformat() if known and known.last_seen else None),
        "battery": _number(values.get("battery")),
        "firmware": values.get("firmware_version"),
        "hardware": values.get("hardware_version"),
        "rssi": stats.rssi,
        "snr": stats.snr,
        "margin": margin,
        "quality": link.quality(margin),
        "packet_loss": stats.packet_loss,
        "command": {
            "state": tracker.state,
            "command": tracker.changes,
            "attempts": tracker.attempts,
            "reported": tracker.reported,
            "changed": tracker.changed_at.isoformat() if tracker.changed_at else None,
        },
        "settings": settings,
    }


@websocket_api.require_admin
@websocket_api.websocket_command({schema.Required("type"): f"{DOMAIN}/devices"})
@websocket_api.async_response
async def ws_devices(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return every device with its settings, and each model's settings."""
    models: dict[str, Any] = {}
    devices = []
    gateways = _known_gateways(hass)
    for entry in _entries(hass):
        listed = entry.runtime_data.coordinator.data
        known = {d.dev_eui.upper(): d for d in (listed.matched if listed else ())}
        for coordinator in entry.runtime_data.devices.coordinators.values():
            device = coordinator.data
            if device.model_id not in models:
                model = decoder.models()[device.model_id]
                models[device.model_id] = {
                    "name": model["name"],
                    "description": model.get("description"),
                    "settings": _fields(device.settings),
                    "commands": _fields(device.commands),
                }
            devices.append(_device(hass, entry, coordinator, known, gateways))
    gateways = [
        {"entry_id": entry.entry_id, "title": entry.title}
        for entry in _entries(hass)
        if can_sync(entry)
    ]
    connection.send_result(
        msg["id"], {"devices": devices, "models": models, "gateways": gateways}
    )


def _coordinator(
    hass: HomeAssistant, entry_id: str, dev_eui: str
) -> MilesightLoRaWANCoordinator:
    for entry in _entries(hass):
        if entry.entry_id != entry_id:
            continue
        for coordinator in entry.runtime_data.devices.coordinators.values():
            if device_eui(coordinator.data) == dev_eui.upper():
                return coordinator
    msg = f"Unknown device {dev_eui}"
    raise HomeAssistantError(msg)


def _check_range(device: Any, changes: dict[str, Any]) -> None:
    """
    Refuse a value outside the model's range before it goes out.

    Some encoders accept more than the device takes: the VS321 is encoded a
    report interval of 1 min and acknowledges it, but keeps its old value.
    """
    fields = {**device.settings, **device.commands}
    for key, value in changes.items():
        field = fields.get(key) or {}
        low, high = field.get("min"), field.get("max")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if (low is not None and value < low) or (high is not None and value > high):
            unit = f" {field['unit']}" if field.get("unit") else ""
            msg = f"{field.get('name', key)} must be between {low} and {high}{unit}"
            raise HomeAssistantError(msg)


_ITEMS = schema.Required("items")


@websocket_api.require_admin
@websocket_api.websocket_command(
    {schema.Required("type"): f"{DOMAIN}/preview", _ITEMS: list}
)
@websocket_api.async_response
async def ws_preview(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the bytes each item's changes encode to, or why they do not."""
    results = []
    for item in msg["items"]:
        result = {"dev_eui": item.get("dev_eui")}
        try:
            coordinator = _coordinator(hass, item["entry_id"], item["dev_eui"])
            device = coordinator.data
            _check_range(device, item.get("changes") or {})
            await hass.async_add_executor_job(decoder.load, [device.model_id])
            result["hex"] = device.encode(item.get("changes") or {}).hex()
        except (HomeAssistantError, decoder.EncodeError, KeyError) as err:
            result["error"] = str(err)
        results.append(result)
    connection.send_result(msg["id"], {"results": results})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {schema.Required("type"): f"{DOMAIN}/send", _ITEMS: list}
)
@websocket_api.async_response
async def ws_send(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send each item's changes; one failure does not stop the others."""
    results = []
    for index, item in enumerate(msg["items"]):
        if index:
            await asyncio.sleep(SEND_PAUSE)
        result = {"dev_eui": item.get("dev_eui")}
        try:
            coordinator = _coordinator(hass, item["entry_id"], item["dev_eui"])
            _check_range(coordinator.data, item.get("changes") or {})
            await hass.async_add_executor_job(decoder.load, [coordinator.data.model_id])
            await coordinator.commands.async_send(item.get("changes") or {})
            result["ok"] = True
        except (
            HomeAssistantError,
            decoder.EncodeError,
            DownlinkError,
            KeyError,
        ) as err:
            result["error"] = str(err)
        except (TypeError, ValueError) as err:
            # A value the encoder chokes on: this device fails, the ones
            # before it are out already and the ones after still go.
            _LOGGER.warning("Sending to %s failed: %r", item.get("dev_eui"), err)
            result["error"] = str(err) or type(err).__name__
        results.append(result)
    connection.send_result(msg["id"], {"results": results})


@websocket_api.require_admin
@websocket_api.websocket_command({schema.Required("type"): f"{DOMAIN}/log"})
@websocket_api.async_response
async def ws_log(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the newest records of every entry's journal, newest first."""
    records = []
    for entry in _entries(hass):
        journal = entry.runtime_data.journal
        if journal is None:
            continue
        names = {
            device_eui(c.data): c.data.descriptor.name
            for c in entry.runtime_data.devices.coordinators.values()
        }
        records.extend(
            {**record, "name": names.get(record["dev_eui"]), "gateway": entry.title}
            for record in journal.records
        )
    records.sort(key=lambda r: r["t"], reverse=True)
    connection.send_result(msg["id"], {"records": records[:300]})


def _entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry:
    entry = next((e for e in _entries(hass) if e.entry_id == entry_id), None)
    if entry is None:
        msg = f"Unknown gateway {entry_id}"
        raise HomeAssistantError(msg)
    return entry


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        schema.Required("type"): f"{DOMAIN}/import_choices",
        schema.Required("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_import_choices(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the gateway's applications, device profiles and payload codecs."""
    try:
        choices = await async_choices(hass, _entry(hass, msg["entry_id"]))
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "import_failed", str(err))
        return
    connection.send_result(msg["id"], choices)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        schema.Required("type"): f"{DOMAIN}/import",
        schema.Required("entry_id"): str,
        schema.Required("application_id"): str,
        schema.Required("profile_id"): str,
        schema.Required("codec_id"): str,
        schema.Required("rows"): list,
    }
)
@websocket_api.async_response
async def ws_import(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Create the listed devices on the gateway."""
    try:
        results = await async_import(
            hass,
            _entry(hass, msg["entry_id"]),
            application_id=msg["application_id"],
            profile_id=msg["profile_id"],
            codec_id=msg["codec_id"],
            rows=msg["rows"],
        )
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "import_failed", str(err))
        return
    connection.send_result(msg["id"], {"results": results})
