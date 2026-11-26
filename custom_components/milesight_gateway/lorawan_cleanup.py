"""Drop the device entities an older version or the other data source left behind."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .lorawan_entity import (
    LEGACY_KEYS,
    PEOPLE_COUNTS,
    is_binary,
    is_event,
    wanted_control,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .lorawan import LoRaWANRuntime
    from .mqtt_discovery import MqttDiscoveryRuntime

# Device entities that are not catalog fields: the gateway's radio values of a
# device's newest uplink, the last-seen time, the thermostat, what the
# integration derives (command status, link, battery forecast).
_KEPT = {
    "spreading_factor",
    "frequency",
    "rssi",
    "loRaSNR",
    "last_seen",
    "thermostat",
    "command_status",
    "signal_quality",
    "packet_loss",
    "gateways_in_range",
    "battery_days_left",
}
# The meters of pulse counters, "<counter>_<part>"; meters.py.
_METER_PARTS = ("_meter", "_meter_type", "_pulses_per_unit", "_meter_offset")
_CONTROLS = ("button", "number", "select", "switch", "climate")


def _drop_foreign(
    hass: HomeAssistant, entry: ConfigEntry, item: er.RegistryEntry, eui: str
) -> None:
    """
    Remove what this entry made for a device another entry owns.

    The gateway lists a device that another entry already has; only the
    radio values of the packet log were added for it, under a device of
    this entry's own. They go, and so does that device once it is empty.
    """
    devices = dr.async_get(hass)
    owned_elsewhere = any(
        devices.async_get_device_by_identifier((DOMAIN, eui), other.entry_id)
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    )
    device = devices.async_get_device_by_identifier((DOMAIN, eui), entry.entry_id)
    if not owned_elsewhere or device is None:
        return
    registry = er.async_get(hass)
    registry.async_remove(item.entity_id)
    if not er.async_entries_for_device(
        registry, device.id, include_disabled_entities=True
    ):
        devices.async_remove_device(device.id)


def _platform(key: str, field: dict[str, Any]) -> str:
    """Return the platform a catalog field gets its entity on."""
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
def remove_stale_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    devices: LoRaWANRuntime | MqttDiscoveryRuntime,
) -> None:
    """
    Remove device entities the catalog will never provide.

    A field keeps its entity across a change of source or version: same unique
    ID, same entity ID, same history. A field the catalog does not know, or
    one it puts on another platform (a sensor that becomes a binary sensor),
    would otherwise linger as "no longer provided". So would the controls of
    a gateway that takes no downlinks.
    """
    fields_by_eui = {
        coordinator.data.descriptor.dev_eui.upper(): coordinator.data.fields
        for coordinator in devices.coordinators.values()
    }
    unlegacy = {legacy: key for key, legacy in LEGACY_KEYS.items()}
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        eui, _, key = item.unique_id.partition("_")
        fields = fields_by_eui.get(eui)
        if fields is None:
            _drop_foreign(hass, entry, item, eui)
            continue  # not a device entity, or a device not loaded
        if (
            item.domain in _CONTROLS
            and not devices.sends_downlinks
            and not key.endswith(_METER_PARTS)
        ):
            registry.async_remove(item.entity_id)
            continue
        if (
            key in _KEPT
            or key.endswith(_METER_PARTS)
            or (key in PEOPLE_COUNTS and item.domain == "binary_sensor")
        ):
            continue
        field = fields.get(unlegacy.get(key, key))
        platform = _platform(key, field) if field is not None else ""
        control = platform in _CONTROLS
        # An enabled control stays: the device reported it, or someone wants it.
        unwanted = control and item.disabled_by and not wanted_control(entry, key)
        if field is None or platform != item.domain or unwanted:
            registry.async_remove(item.entity_id)
