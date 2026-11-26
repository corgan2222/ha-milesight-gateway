"""
Entities built from Milesight's codec catalog instead of per-model code.

Every field of a model has a type, a unit, an access mode and, for
enumerations, names for its raw values. That describes an entity. Readings
get an entity once the device has reported them, or when Milesight's example
uplink for the model carries them, so a device does not start with dozens of
alarm and history fields it never sends. Settings and commands exist from the
start, the essential ones only unless the "all settings" option is on.

Unique IDs follow the MQTT source, ``<devEUI>_<field>``, so switching the data
source keeps entity IDs, history and automations of every shared field.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify
from lorawan_connection import DownlinkError
from milesight_gateway_api import decoder
from milesight_gateway_api.lorawan_devices import INFO_FIELDS, MilesightDevice

from .const import CONF_ALL_SETTINGS, DOMAIN
from .lorawan_coordinator import MilesightLoRaWANCoordinator, device_eui

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from homeassistant.config_entries import ConfigEntry

# Field IDs whose entity the MQTT source names differently. The controls are
# the MQTT descriptions whose payload path, joined by dots, is the field ID.
LEGACY_KEYS = {
    "button_event.status": "button_event",
    "child_lock_config.enable": "child_lock",
    "freeze_protection_config.enable": "freeze_protection",
    "freeze_protection_config.temperature": "freeze_protection_temperature",
    "open_window_detection.enable": "open_window_detection",
    "open_window_detection.temperature_threshold": "owd_temperature_threshold",
    "open_window_detection.time": "owd_time",
    "outside_temperature_control.enable": "outside_temperature_control",
    "outside_temperature_control.timeout": "outside_temperature_timeout",
    "target_temperature_range.max": "target_temperature_max",
    "target_temperature_range.min": "target_temperature_min",
    "temperature_calibration_settings.calibration_value": (
        "temperature_calibration_value"
    ),
    "temperature_calibration_settings.enable": "temperature_calibration",
    "valve_opening": "set_valve_opening",
}
# Event types the MQTT source names differently; automations keep working.
LEGACY_EVENT_TYPES = {"short_press": "single_press"}


def event_type(name: str) -> str:
    """Return the event type for a catalog value name, as the MQTT source names it."""
    slug = slugify(name)
    return LEGACY_EVENT_TYPES.get(slug, slug)


# Catalog unit -> (device class, Home Assistant unit, state class)
_UNITS: dict[str, tuple[SensorDeviceClass | None, str, SensorStateClass]] = {
    "°C": (SensorDeviceClass.TEMPERATURE, "°C", SensorStateClass.MEASUREMENT),
    "°F": (SensorDeviceClass.TEMPERATURE, "°F", SensorStateClass.MEASUREMENT),
    "℉": (SensorDeviceClass.TEMPERATURE, "°F", SensorStateClass.MEASUREMENT),
    "%r.h.": (SensorDeviceClass.HUMIDITY, "%", SensorStateClass.MEASUREMENT),
    "%RH": (SensorDeviceClass.HUMIDITY, "%", SensorStateClass.MEASUREMENT),
    "A": (SensorDeviceClass.CURRENT, "A", SensorStateClass.MEASUREMENT),
    "mA": (SensorDeviceClass.CURRENT, "mA", SensorStateClass.MEASUREMENT),
    "V": (SensorDeviceClass.VOLTAGE, "V", SensorStateClass.MEASUREMENT),
    "mV": (SensorDeviceClass.VOLTAGE, "mV", SensorStateClass.MEASUREMENT),
    "W": (SensorDeviceClass.POWER, "W", SensorStateClass.MEASUREMENT),
    "kW": (SensorDeviceClass.POWER, "kW", SensorStateClass.MEASUREMENT),
    "Wh": (SensorDeviceClass.ENERGY, "Wh", SensorStateClass.TOTAL_INCREASING),
    "kWh": (SensorDeviceClass.ENERGY, "kWh", SensorStateClass.TOTAL_INCREASING),
    "kPa": (SensorDeviceClass.PRESSURE, "kPa", SensorStateClass.MEASUREMENT),
    "hPa": (
        SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        "hPa",
        SensorStateClass.MEASUREMENT,
    ),
    "dBA": (SensorDeviceClass.SOUND_PRESSURE, "dBA", SensorStateClass.MEASUREMENT),
    "lx": (SensorDeviceClass.ILLUMINANCE, "lx", SensorStateClass.MEASUREMENT),
    "m/s": (SensorDeviceClass.WIND_SPEED, "m/s", SensorStateClass.MEASUREMENT),
    "mm": (SensorDeviceClass.DISTANCE, "mm", SensorStateClass.MEASUREMENT),
    "cm": (SensorDeviceClass.DISTANCE, "cm", SensorStateClass.MEASUREMENT),
    "s": (SensorDeviceClass.DURATION, "s", SensorStateClass.MEASUREMENT),
    "min": (SensorDeviceClass.DURATION, "min", SensorStateClass.MEASUREMENT),
    "µg/m³": (None, "μg/m³", SensorStateClass.MEASUREMENT),
}
# Field ID -> device class, where the unit alone does not tell.
_KEYS: dict[str, SensorDeviceClass] = {
    "battery": SensorDeviceClass.BATTERY,
    "co2": SensorDeviceClass.CO2,
    "pm2_5": SensorDeviceClass.PM25,
    "pm10": SensorDeviceClass.PM10,
    "rssi": SensorDeviceClass.SIGNAL_STRENGTH,
}
# Words in a field ID -> binary sensor class, first match wins.
_BINARY: tuple[tuple[str, BinarySensorDeviceClass], ...] = (
    ("tamper", BinarySensorDeviceClass.TAMPER),
    ("leak", BinarySensorDeviceClass.MOISTURE),
    ("magnet", BinarySensorDeviceClass.DOOR),
    ("door", BinarySensorDeviceClass.DOOR),
    ("window", BinarySensorDeviceClass.WINDOW),
    ("pir", BinarySensorDeviceClass.MOTION),
    ("motion", BinarySensorDeviceClass.MOTION),
    ("occup", BinarySensorDeviceClass.OCCUPANCY),
    ("alarm", BinarySensorDeviceClass.PROBLEM),
)
# The controls every device gets. The rest (about 25 per device: alarms,
# schedules, calibration) only with the option "all settings", and disabled:
# on a fleet they would fill the registry by the thousand.
ESSENTIAL_CONTROLS = frozenset(
    {
        "report_interval",
        "target_temperature",
        "reboot",
        "report_status",
        "query_device_status",
    }
)


# A people count also gets an occupancy binary sensor under the same key.
PEOPLE_COUNTS = frozenset({"people_total_counts", "people_count_all"})
# Readings a counter only has for what was drawn in its image: up to sixteen
# regions and the crossings between four lines. Their entities start disabled,
# or a counter without regions shows forty rows that never get a value.
# hass.data key: the entries whose optional readings without a value are
# disabled as they load, once, after the update that made them start disabled.
RETIRE_EMPTY_OPTIONAL = f"{DOMAIN}_retire_empty_optional"
OPTIONAL = re.compile(r"region_(\d+(_count)?|count)|[a-d]_to_[a-d]")
# Words a gateway codec writes for an active two-state value.
_ACTIVE_WORDS = frozenset({"true", "on", "open", "motion", "occupied", "trigger"})


def value_name(field: dict[str, Any], raw: Any) -> str | None:
    """
    Name an enumerated raw value.

    The decoders here report the number; a payload codec on the gateway may
    already have put the name in its place.
    """
    if raw is None:
        return None
    names = field.get("values") or {}
    if (name := names.get(str(raw))) is not None:
        return name
    return raw if isinstance(raw, str) and raw in names.values() else None


def is_set(field: dict[str, Any], raw: Any) -> bool | None:
    """Tell whether a two-state value is on: not 0, by number or by name."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    for value, name in (field.get("values") or {}).items():
        if str(name).lower() == text:
            text = value
            break
    try:
        return float(text) != 0
    except ValueError:
        return text in _ACTIVE_WORDS


def is_binary(_key: str, field: dict[str, Any]) -> bool:
    """Tell whether a field has two states, for a binary sensor."""
    return field.get("type") == "BOOL"


def is_event(key: str, field: dict[str, Any]) -> bool:
    """Tell whether a field is a button press: an event, not a state that stays."""
    return (
        field.get("type") == "ENUM"
        and bool(field.get("values"))
        and key not in INFO_FIELDS
        and ("button" in key or key.endswith("_event"))
    )


def is_sensor(key: str, field: dict[str, Any]) -> bool:
    """Everything else that a device reports."""
    return not is_binary(key, field) and not is_event(key, field)


def wanted_control(entry: ConfigEntry, key: str) -> bool:
    """Whether a setting or command of this key gets an entity."""
    return key in ESSENTIAL_CONTROLS or bool(entry.options.get(CONF_ALL_SETTINGS))


def ha_unit(field: dict[str, Any]) -> str | None:
    """Return the Home Assistant unit for a catalog unit."""
    unit = field.get("unit", "")
    return _UNITS[unit][1] if unit in _UNITS else unit or None


def setting_kind(key: str, field: dict[str, Any]) -> dict[str, Any]:
    """
    Entity description arguments shared by every setting and command.

    Enabled when essential, or when the device reports the setting by itself
    (``reported``, set by the platform setup).
    """
    return {
        "name": field.get("name") or key,
        "entity_category": EntityCategory.CONFIG,
        "entity_registry_enabled_default": key in ESSENTIAL_CONTROLS
        or bool(field.get("reported")),
    }


def sensor_kind(key: str, field: dict[str, Any]) -> dict[str, Any]:
    """Entity description arguments for a non-binary field."""
    kind: dict[str, Any] = {"name": field.get("name") or key}
    if OPTIONAL.fullmatch(key):
        kind["entity_registry_enabled_default"] = False
    # "diagnostic" marks fields outside the catalog, such as the radio values
    # a payload codec adds on MQTT.
    if key in INFO_FIELDS or field.get("diagnostic"):
        kind["entity_category"] = EntityCategory.DIAGNOSTIC
    if field.get("type") == "ENUM" and field.get("values"):
        kind["device_class"] = SensorDeviceClass.ENUM
        kind["options"] = sorted(set(field["values"].values()))
    elif field.get("type") == "NUMBER":
        unit = field.get("unit", "")
        device_class, unit_ha, state_class = _UNITS.get(
            unit, (None, unit or None, SensorStateClass.MEASUREMENT)
        )
        if unit in ("%", "ppm") or device_class is None:
            device_class = _KEYS.get(key.rsplit(".", 1)[-1], device_class)
        kind |= {
            "device_class": device_class,
            "native_unit_of_measurement": unit_ha,
            "state_class": state_class,
        }
    return kind


def binary_kind(key: str, field: dict[str, Any]) -> dict[str, Any]:
    """Entity description arguments for a two-state field."""
    kind: dict[str, Any] = {"name": field.get("name") or key}
    if key in INFO_FIELDS:
        kind["entity_category"] = EntityCategory.DIAGNOSTIC
    for word, device_class in _BINARY:
        if word in key:
            kind["device_class"] = device_class
            break
    return kind


def _example_keys(model_id: str) -> set[str]:
    example = decoder.models()[model_id].get("example")
    if not example:
        return set()
    try:
        return set(decoder.flatten(decoder.decode(model_id, bytes.fromhex(example))))
    except decoder.DecodeError, ValueError:
        return set()


def registered_keys(coordinator: MilesightLoRaWANCoordinator) -> set[str]:
    """
    Field IDs that had an entity before the restart.

    Alarms like tamper or an open window arrive only on change, settings only
    on request; without this their entities would stay unavailable until then.
    Read once per device: every platform asks, and a fleet has thousands of
    entities to look through.
    """
    if (cached := getattr(coordinator, "registered", None)) is not None:
        return cached
    prefix = f"{device_eui(coordinator.data)}_"
    original = {legacy: key for key, legacy in LEGACY_KEYS.items()}
    registry = er.async_get(coordinator.hass)
    keys = set()
    for item in er.async_entries_for_config_entry(
        registry, coordinator.config_entry.entry_id
    ):
        if item.unique_id.startswith(prefix):
            key = item.unique_id.removeprefix(prefix)
            keys.add(original.get(key, key))
    coordinator.registered = keys
    return keys


def track_fields(
    coordinator: MilesightLoRaWANCoordinator,
    wanted: Callable[[str, dict[str, Any]], bool],
    add: Callable[[Iterable[str]], None],
) -> Callable[[], None]:
    """Call add() with the field IDs that deserve an entity, now and later."""
    device = coordinator.data
    readings = device.readings | {
        key: field for key, field in device.fields.items() if key in INFO_FIELDS
    }
    known: set[str] = set()
    pending = registered_keys(coordinator)
    examples = False

    @callback
    def check() -> None:
        nonlocal examples, pending
        keys = set(device.values) | pending
        pending = set()
        # Still packed: codec_loaded() notifies again once it is unpacked.
        if not examples and decoder.is_loaded(device.model_id):
            examples = True
            keys |= _example_keys(device.model_id)
        new = [
            key
            for key in sorted(keys - known)
            if key in readings and wanted(key, readings[key])
        ]
        known.update(keys)
        if new:
            add(new)

    check()
    return coordinator.async_add_listener(check)


class MilesightLoRaWANEntity(CoordinatorEntity[MilesightLoRaWANCoordinator]):
    """One catalog field of one device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: MilesightLoRaWANCoordinator, description: Any
    ) -> None:
        """Name the entity like the MQTT source does."""
        super().__init__(coordinator)
        self.entity_description = description
        key = LEGACY_KEYS.get(description.key, description.key)
        self._attr_unique_id = f"{device_eui(coordinator.data)}_{key}"
        self._attr_device_info = coordinator.device_info

    @property
    def device(self) -> MilesightDevice:
        """The decoded device shared by this device's entities."""
        return self.coordinator.data

    @property
    def available(self) -> bool:
        """Unavailable once the gateway drops the device or lists it inactive."""
        device = self.device
        return (
            super().available
            and not device.closed
            # Only MQTT devices know it; the LoRaWAN source closes them instead.
            and getattr(device, "active", True)
        )

    @property
    def field(self) -> dict[str, Any]:
        """The catalog entry of this entity's field."""
        return self.device.fields[self.entity_description.key]

    @property
    def raw(self) -> Any:
        """The last decoded value, or None before the first report."""
        return self.device.values.get(self.entity_description.key)

    async def async_send(self, changes: dict[str, Any]) -> None:
        """Send settings or commands; the device applies them after its next uplink."""
        try:
            await self.coordinator.commands.async_send(changes)
        except (decoder.EncodeError, DownlinkError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="downlink_failed",
                translation_placeholders={"error": str(err)},
            ) from err
