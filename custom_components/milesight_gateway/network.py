"""
The LoRaWAN network map: a sidebar panel, a dashboard card and their data.

Home Assistant's own LoRaWAN graph shows only the gateways that heard a
device's last uplink, and no signal values. The map shows every gateway of
each LoRaWAN entry, a fleet hub's silent ones included, and labels and colours
each reception by RSSI and SNR. Its data comes from the command below, which
any logged-in user may read, so the card also works on non-admin dashboards.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from lorawan_connection import ConnectionUnavailable
from milesight_gateway_api.exceptions import MilesightGatewayError
from milesight_gateway_api.lorawan import InventoryEntry

from .const import CONF_GATEWAY_URL, CONF_NETWORK_PANEL, DOMAIN, uses_lorawan

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

try:  # Home Assistant 2026.11 moved websocket schemas to probatio.
    import probatio as schema
except ImportError:  # pragma: no cover - older Home Assistant
    import voluptuous as schema

_LOGGER = logging.getLogger(__name__)

URL_BASE = f"/{DOMAIN}_static"
SCRIPT = "milesight-lorawan-map.js"
PANEL_ELEMENT = "milesight-lorawan-network-panel"
PANEL_PATH = "milesight-lorawan"
_SET_UP = f"{DOMAIN}_network_view"
_STORE = f"{DOMAIN}_receptions"
_URL = f"{DOMAIN}_network_script"
_SIDEBAR = f"{DOMAIN}_network_sidebar"
_LOCK = f"{DOMAIN}_network_lock"


async def async_setup_network_view(hass: HomeAssistant) -> None:
    """Serve the script, add the panel and the card, register the command."""
    # Entries set up side by side: the second waits until the first is done,
    # or it would register the panel before the script has its address.
    lock = hass.data.setdefault(_LOCK, asyncio.Lock())
    async with lock:
        await _async_setup_network_view(hass)


async def _async_setup_network_view(hass: HomeAssistant) -> None:
    if not hass.data.get(_SET_UP):
        hass.data[_SET_UP] = True
        store = hass.data[_STORE] = _ReceptionStore(hass)
        await store.async_load()
        websocket_api.async_register_command(hass, websocket_network)
    if "frontend" not in hass.config.components:
        return  # no frontend (tests, headless): the data command is enough
    if _URL not in hass.data:
        folder = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_BASE, str(folder), cache_headers=False)]
        )
        # A new script gets a new URL; browsers kept the old one after an update.
        digest = await hass.async_add_executor_job(
            lambda: hashlib.sha256((folder / SCRIPT).read_bytes()).hexdigest()[:12]
        )
        hass.data[_URL] = f"{URL_BASE}/{SCRIPT}?v={digest}"
        # The card: loaded on every dashboard, no resource entry needed.
        frontend.add_extra_js_url(hass, hass.data[_URL])
        await async_setup_component(hass, "panel_custom", {})
    await _async_register_panel(hass)


def show_in_sidebar(hass: HomeAssistant) -> bool:
    """
    Whether the map belongs in the sidebar: unless a LoRaWAN entry turned it off.

    There is one menu entry for all gateways; switching it off at one gateway
    is meant to remove it, without visiting every other entry.
    """
    return all(
        entry.options.get(CONF_NETWORK_PANEL, True)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if uses_lorawan(entry.data, entry.options)
    )


async def _async_register_panel(hass: HomeAssistant) -> None:
    """
    Add the panel, in the sidebar or only under its address.

    An options change reloads the entry, which lands here again; the panel is
    registered anew only when the sidebar choice changed.
    """
    sidebar = show_in_sidebar(hass)
    _LOGGER.debug(
        "Network map in the sidebar: %s (was %s)", sidebar, hass.data.get(_SIDEBAR)
    )
    if hass.data.get(_SIDEBAR) == sidebar:
        return
    if _SIDEBAR in hass.data:
        frontend.async_remove_panel(hass, PANEL_PATH)
    hass.data[_SIDEBAR] = sidebar
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name=PANEL_ELEMENT,
        sidebar_title="LoRaWAN network" if sidebar else None,
        sidebar_icon="mdi:access-point-network" if sidebar else None,
        module_url=hass.data[_URL],
    )


def _device_id(
    registry: dr.DeviceRegistry, entry_id: str, identifier: str
) -> str | None:
    """Return the registry ID of this entry's device with that identifier."""
    device = registry.async_get_device_by_identifier((DOMAIN, identifier), entry_id)
    return device.id if device else None


def _receptions(uplink: Any) -> dict[str, Any]:
    return {
        "last_uplink": uplink.received_at.isoformat(),
        "receptions": [
            {"gateway_id": rx.gateway_id, "rssi": rx.rssi, "snr": rx.snr}
            for rx in uplink.receptions
        ],
    }


@websocket_api.websocket_command({schema.Required("type"): f"{DOMAIN}/network"})
@websocket_api.async_response
async def websocket_network(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """
    Gateways and devices with their newest receptions, per LoRaWAN entry.

    Every device the network server lists is included: "selected" tells the
    ones the entry has entities for, "active" the server's online verdict. A
    device without an uplink since the start shows the last one stored, so the
    map does not start empty after a restart ("stored" marks those).
    """
    registry = dr.async_get(hass)
    store = hass.data[_STORE]
    changed = False
    result = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        if entry.state is not ConfigEntryState.LOADED or runtime is None:
            continue
        if (lorawan := runtime.lorawan) is None:
            continue
        item: dict[str, Any] = {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "configuration_url": entry.data[CONF_GATEWAY_URL],
            "device_id": runtime.gateway_device_id,
            "gateways": [],
            "devices": [],
            "error": None,
        }
        try:
            gateways = await lorawan.async_gateways()
        except (
            ClientError,
            TimeoutError,
            MilesightGatewayError,
            ConnectionUnavailable,
        ) as err:
            gateways = []
            item["error"] = str(err) or type(err).__name__
        diagnostics = runtime.diagnostics.data
        local = diagnostics.network.gateway_eui if diagnostics else None
        item["gateways"] = [
            {
                **gateway,
                "device_id": runtime.gateway_device_id
                if local and str(gateway.get("id", "")).upper() == local.upper()
                else _device_id(
                    registry, entry.entry_id, str(gateway.get("id", "")).upper()
                ),
            }
            for gateway in gateways
        ]
        link = lorawan.connection
        stored = store.data.setdefault(entry.entry_id, {})
        # A connection without an inventory lists the selected devices only.
        inventory = getattr(link, "inventory", None) or {
            eui: InventoryEntry(descriptor, active=True, wanted=True)
            for eui, descriptor in link.devices.items()
        }
        for eui, known in inventory.items():
            descriptor = known.descriptor
            device: dict[str, Any] = {
                "dev_eui": eui,
                "name": descriptor.name,
                "model": descriptor.model,
                "selected": known.wanted,
                "active": known.active,
                "device_id": _device_id(registry, entry.entry_id, eui.upper())
                if known.wanted
                else None,
                "last_uplink": None,
                "receptions": [],
                "stored": False,
            }
            if (uplink := link.last_uplinks.get(eui)) is not None:
                fresh = _receptions(uplink)
                device |= fresh
                if stored.get(eui) != fresh:
                    stored[eui] = fresh
                    changed = True
            elif eui in stored:
                device |= stored[eui] | {"stored": True}
            item["devices"].append(device)
        result.append(item)
    if changed:
        store.save()
    connection.send_result(msg["id"], {"connections": result})


class _ReceptionStore:
    """The newest receptions per entry and device, kept over a restart."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.receptions")
        self.data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        loaded = await self._store.async_load()
        self.data = loaded if isinstance(loaded, dict) else {}

    def save(self) -> None:
        # Every poll may bring news; writing once a minute is enough.
        self._store.async_delay_save(lambda: self.data, 60)
