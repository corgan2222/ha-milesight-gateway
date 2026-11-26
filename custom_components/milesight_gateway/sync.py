"""
Remove the devices the gateway no longer knows.

A device that stops reporting stays, unavailable, with its history: it may
come back. One deleted on the gateway does not; the sync button removes it
on request. The gateway's full device list decides, read by REST or, for a
gateway Home Assistant cannot reach, by MQTT. Gateways of the fleet are
devices of this entry too, and are kept.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import APIAuthError, APIConnectionError
from .const import DOMAIN, gateway_only
from .nsapi import NsApiError

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_EUI = re.compile(r"^[0-9A-F]{16}$")
GATEWAY_MODEL = "LoRaWAN Gateway"


def can_sync(entry: ConfigEntry) -> bool:
    """Whether the entry can read the gateway's full device list."""
    if not gateway_only(entry.data):
        return True
    mqtt = entry.runtime_data.mqtt
    return mqtt is not None and mqtt.ns_api is not None


async def _async_known(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    runtime = entry.runtime_data
    try:
        if not gateway_only(entry.data):
            return await runtime.coordinator.api.async_get_device_euis(
                async_get_clientsession(hass)
            )
        devices = await runtime.mqtt.ns_api.async_devices()
    except (APIAuthError, APIConnectionError, NsApiError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="sync_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    return {str(d.get("devEUI", "")).upper() for d in devices}


async def async_sync_devices(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Remove this entry's devices the gateway does not list; return their names."""
    known = await _async_known(hass, entry)
    if not known:
        # An empty answer is more likely a fault than a gateway without devices.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="sync_failed",
            translation_placeholders={"error": "the gateway lists no devices"},
        )
    registry = dr.async_get(hass)
    removed = []
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            device.model == GATEWAY_MODEL
            or device.id == entry.runtime_data.gateway_device_id
        ):
            continue
        euis = {
            identifier.upper()
            for domain, identifier in device.identifiers
            if domain == DOMAIN and _EUI.match(identifier.upper())
        }
        if euis and not euis & known:
            registry.async_remove_device(device.id)
            # The LoRaWAN source retires its own; the MQTT one is told.
            if forget := getattr(entry.runtime_data.devices, "forget", None):
                for eui in euis:
                    forget(eui)
            removed.append(device.name_by_user or device.name or next(iter(euis)))
    _LOGGER.info(
        "Removed %d devices the gateway no longer lists: %s", len(removed), removed
    )
    return removed
