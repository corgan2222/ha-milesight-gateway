"""Drop the device entities the other data source left behind after a switch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .lorawan_entity import LEGACY_KEYS, is_binary, is_event, wanted_control

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import MilesightGatewayCoordinator

# Device entities of both sources that are not catalog fields: the gateway's
# radio values of a device's newest uplink, the last-seen time, the thermostat.
_KEPT = {
    "spreading_factor",
    "frequency",
    "rssi",
    "loRaSNR",
    "last_seen",
    "thermostat",
}


def _platform(key: str, field: dict[str, Any]) -> str:
    """Return the platform the LoRaWAN source puts a catalog field on."""
    access = field.get("access")
    if access == "W":
        return "button"
    if access == "RW":
        kind = field.get("type")
        return {"NUMBER": "number", "ENUM": "select", "BOOL": "switch"}.get(kind, "")
    if is_event(key, field):
        return "event"
    return "binary_sensor" if is_binary(key, field) else "sensor"


@callback
def remove_mqtt_only_entities(
    hass: HomeAssistant, entry: ConfigEntry, _coordinator: MilesightGatewayCoordinator
) -> None:
    """
    Remove device entities the LoRaWAN source will never provide.

    A field both sources share keeps its entity: same unique ID, same entity
    ID, same history. A field only MQTT knows, or one LoRaWAN puts on another
    platform (a sensor that becomes a binary sensor), would otherwise linger
    as "no longer provided".
    """
    collection = entry.runtime_data.lorawan.collection
    fields_by_eui = {
        device.descriptor.dev_eui.upper(): device.fields
        for device in collection.devices.values()
    }
    unlegacy = {legacy: key for key, legacy in LEGACY_KEYS.items()}
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        eui, _, key = item.unique_id.partition("_")
        fields = fields_by_eui.get(eui)
        if fields is None or key in _KEPT:
            continue  # not a device entity, or one both sources keep
        field = fields.get(unlegacy.get(key, key))
        platform = _platform(key, field) if field is not None else ""
        control = platform in ("button", "number", "select", "switch")
        # An enabled control stays: the device reported it, or someone wants it.
        unwanted = control and item.disabled_by and not wanted_control(entry, key)
        if field is None or platform != item.domain or unwanted:
            registry.async_remove(item.entity_id)


@callback
def remove_lorawan_only_entities(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: MilesightGatewayCoordinator
) -> None:
    """
    After an MQTT setup, remove device entities no platform provided.

    The MQTT source adds every entity of an active device at setup, so what is
    left in the registry for such a device came from the LoRaWAN source.
    Offline devices are left alone, and so are disabled entities, which no
    platform adds.
    """
    provided = {
        entity_id
        for platform in entity_platform.async_get_platforms(hass, DOMAIN)
        if platform.config_entry is not None
        and platform.config_entry.entry_id == entry.entry_id
        for entity_id in platform.entities
    }
    active = {device.dev_eui for device in coordinator.data.devices}
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        eui = item.unique_id.partition("_")[0]
        if eui in active and not item.disabled_by and item.entity_id not in provided:
            registry.async_remove(item.entity_id)
