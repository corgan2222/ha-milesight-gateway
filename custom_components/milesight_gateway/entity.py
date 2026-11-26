"""Helpers shared by the gateway's entities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .api import FleetGateway
    from .coordinator import DiagnosticsData, MilesightDiagnosticsCoordinator


@callback
def remove_switched_off_entity(
    hass: HomeAssistant, platform: str, unique_id: str
) -> None:
    """Drop an entity an option switched off, instead of leaving it orphaned."""
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(platform, DOMAIN, unique_id):
        registry.async_remove(entity_id)


def fleet_device_info(
    gateway: FleetGateway, entry_id: str, gateway_id: str, *, local: bool
) -> DeviceInfo:
    """Return the device of a fleet gateway; the local one is the gateway device."""
    if local:
        return DeviceInfo(identifiers={(DOMAIN, entry_id)})
    return DeviceInfo(
        identifiers={(DOMAIN, gateway.eui)},
        name=gateway.name,
        model="LoRaWAN Gateway",
        serial_number=gateway.eui,
        via_device_id=gateway_id,
        # The member's own web interface; they answer on HTTPS.
        configuration_url=f"https://{gateway.ip}" if gateway.ip else None,
    )


@callback
def on_first_status(
    entry: ConfigEntry,
    status: MilesightDiagnosticsCoordinator,
    make: Callable[[DiagnosticsData], list[Entity]],
    add_entities: AddEntitiesCallback,
) -> None:
    """
    Add the entities the gateway status names, now or once it was read.

    The status is read in the background at setup: during Home Assistant's
    start it can take a minute, which the entry need not wait for.
    """
    if status.data is not None:
        add_entities(make(status.data))
        return
    # Holds the unsubscribe until it ran once: after the first status, or at
    # unload, whichever comes first. A second call raises.
    listening: list[Callable[[], None]] = []

    @callback
    def stop() -> None:
        if listening:
            listening.pop()()

    @callback
    def first() -> None:
        if status.data is None:
            return
        stop()
        add_entities(make(status.data))

    listening.append(status.async_add_listener(first))
    entry.async_on_unload(stop)
