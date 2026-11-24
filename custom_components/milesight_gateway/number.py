"""
Number entities for device settings, sent to the device as downlinks.

Each setting key doubles as the uplink report key and the downlink command
key, because the Milesight codecs use the same vocabulary in both directions.
A device reports its settings in a status report, so the value stays unknown
until the first one arrives; a value set here shows right away and the device
confirms it after its next uplink.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory

from .const import CONF_GATEWAY_URL
from .entity import MilesightDeviceEntity, nested_command

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry
    from .api import MilesightDevice
    from .coordinator import MilesightGatewayCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MilesightNumberDescription(NumberEntityDescription):
    """
    A numeric setting with its place in the payload.

    Without a ``path`` the key itself is the payload key. Some encoders take
    a whole object at once; ``defaults`` fills the sibling fields until the
    device has reported them.
    """

    path: tuple[str, ...] = ()
    defaults: dict[str, Any] = field(default_factory=dict)


# Most Milesight encoders take the reporting interval in minutes, 1 to 1440;
# the AM300 family takes seconds. Ranges come from the encoder scripts.
_REPORT_MINUTES = NumberEntityDescription(
    key="report_interval",
    name="Reporting interval",
    icon="mdi:timer-cog-outline",
    native_min_value=1,
    native_max_value=1440,
    native_step=1,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)
_REPORT_SECONDS = NumberEntityDescription(
    key="report_interval",
    name="Reporting interval",
    icon="mdi:timer-cog-outline",
    native_min_value=60,
    native_max_value=64800,
    native_step=1,
    native_unit_of_measurement=UnitOfTime.SECONDS,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)
# How long a seat or room stays "occupied" after the last detection.
_VACANCY_VS340 = NumberEntityDescription(
    key="vacancy_reporting_interval",
    name="Vacancy reporting interval",
    icon="mdi:seat-outline",
    native_min_value=0,
    native_max_value=100,
    native_step=1,
    native_unit_of_measurement=UnitOfTime.SECONDS,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)
_VACANT_WS203 = NumberEntityDescription(
    key="vacant_reporting_interval",
    name="Vacancy reporting interval",
    icon="mdi:motion-sensor-off",
    native_min_value=60,
    native_max_value=3600,
    native_step=1,
    native_unit_of_measurement=UnitOfTime.SECONDS,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)
# How often the sensor measures, apart from how often it reports.
_COLLECTION_MINUTES = NumberEntityDescription(
    key="collection_interval",
    name="Collection interval",
    icon="mdi:timer-sand",
    native_min_value=1,
    native_max_value=1440,
    native_step=1,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)

# --- WT101 thermostat ------------------------------------------------------
_OWD_THRESHOLD = MilesightNumberDescription(
    key="owd_temperature_threshold",
    name="Window detection threshold",
    icon="mdi:window-open-variant",
    path=("open_window_detection", "temperature_threshold"),
    defaults={"enable": 1, "time": 10},
    native_min_value=2,
    native_max_value=10,
    native_step=1,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)
_OWD_TIME = MilesightNumberDescription(
    key="owd_time",
    name="Window detection time",
    icon="mdi:window-open-variant",
    path=("open_window_detection", "time"),
    defaults={"enable": 1, "temperature_threshold": 5},
    native_min_value=1,
    native_max_value=1440,
    native_step=1,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)
_FREEZE_TEMPERATURE = MilesightNumberDescription(
    key="freeze_protection_temperature",
    name="Freeze protection temperature",
    icon="mdi:snowflake-thermometer",
    path=("freeze_protection_config", "temperature"),
    defaults={"enable": 1},
    native_min_value=0,
    native_max_value=15,
    native_step=0.5,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)
_OTC_TIMEOUT = MilesightNumberDescription(
    key="outside_temperature_timeout",
    name="Outside temperature timeout",
    icon="mdi:timer-alert-outline",
    path=("outside_temperature_control", "timeout"),
    defaults={"enable": 1},
    native_min_value=3,
    native_max_value=60,
    native_step=1,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)
_CALIBRATION_VALUE = MilesightNumberDescription(
    key="temperature_calibration_value",
    name="Temperature calibration value",
    icon="mdi:thermometer-plus",
    path=("temperature_calibration_settings", "calibration_value"),
    defaults={"enable": 1},
    native_min_value=-10,
    native_max_value=10,
    native_step=0.1,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)
_RANGE_MIN = MilesightNumberDescription(
    key="target_temperature_min",
    name="Target temperature min",
    icon="mdi:thermometer-chevron-down",
    path=("target_temperature_range", "min"),
    defaults={"max": 35},
    native_min_value=5,
    native_max_value=15,
    native_step=1,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)
_RANGE_MAX = MilesightNumberDescription(
    key="target_temperature_max",
    name="Target temperature max",
    icon="mdi:thermometer-chevron-up",
    path=("target_temperature_range", "max"),
    defaults={"min": 5},
    native_min_value=16,
    native_max_value=35,
    native_step=1,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    mode=NumberMode.BOX,
    entity_category=EntityCategory.CONFIG,
)
# Drives the valve directly; the thermostat only follows it while its own
# temperature control is off.
_VALVE_OPENING = MilesightNumberDescription(
    key="set_valve_opening",
    name="Manual valve opening",
    icon="mdi:pipe-valve",
    path=("valve_opening",),
    native_min_value=0,
    native_max_value=100,
    native_step=1,
    native_unit_of_measurement=PERCENTAGE,
    mode=NumberMode.SLIDER,
)
# Feeds an external room sensor to the device, for example from an automation.
_OUTSIDE_TEMPERATURE = MilesightNumberDescription(
    key="outside_temperature",
    name="Outside temperature",
    icon="mdi:thermometer",
    path=("outside_temperature",),
    native_min_value=-20,
    native_max_value=60,
    native_step=0.1,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    mode=NumberMode.BOX,
)

# Settings per model, keyed by the model id from devices_ha.json. The
# description key is the codec's key, in uplink reports and downlinks alike.
# Models with an empty encoder (ws101, vs351) can take no settings.
DEVICE_SETTINGS: dict[str, tuple[NumberEntityDescription, ...]] = {
    "am307": (_REPORT_SECONDS,),
    "am307l": (_REPORT_SECONDS,),
    "am308": (_REPORT_SECONDS,),
    "am308l": (_REPORT_SECONDS,),
    "em300_mld": (_REPORT_MINUTES, _COLLECTION_MINUTES),
    "em300_sld": (_REPORT_MINUTES, _COLLECTION_MINUTES),
    "em300_th": (_REPORT_MINUTES, _COLLECTION_MINUTES),
    "vs321": (_REPORT_MINUTES,),
    "vs340": (_REPORT_MINUTES, _VACANCY_VS340),
    "vs341": (_REPORT_MINUTES, _VACANCY_VS340),
    "vs350": (_REPORT_MINUTES,),
    "ws203": (_REPORT_MINUTES, _VACANT_WS203, _COLLECTION_MINUTES),
    "wt101": (
        _REPORT_MINUTES,
        _OWD_THRESHOLD,
        _OWD_TIME,
        _FREEZE_TEMPERATURE,
        _OTC_TIMEOUT,
        _CALIBRATION_VALUE,
        _RANGE_MIN,
        _RANGE_MAX,
        _VALVE_OPENING,
        _OUTSIDE_TEMPERATURE,
    ),
}


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: MilesightGatewayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create number entities for every known setting of each device."""
    coordinator = config_entry.runtime_data.coordinator
    gateway_url = config_entry.data[CONF_GATEWAY_URL]
    gateway_id = config_entry.runtime_data.gateway_device_id

    async_add_entities(
        MilesightSettingNumber(
            coordinator, device, description, gateway_url, gateway_id
        )
        for device in coordinator.data.devices
        for description in DEVICE_SETTINGS.get(device.model_id, ())
    )


class MilesightSettingNumber(MilesightDeviceEntity, NumberEntity):
    """One numeric device setting, written as a downlink."""

    entity_description: NumberEntityDescription

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        description: NumberEntityDescription,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Initialize the setting."""
        super().__init__(coordinator, device, configuration_url, gateway_id)
        self.entity_description = description
        self._attr_unique_id = f"{device.dev_eui}_{description.key}"
        self._path: tuple[str, ...] = getattr(description, "path", ()) or (
            description.key,
        )
        self._defaults: dict[str, Any] = getattr(description, "defaults", {})
        # Sibling fields of the setting's object, as last reported.
        self._known: dict[str, Any] = {}

    def _as_wire_value(self, value: float) -> float | int:
        """Whole-number settings travel as integers, finer ones as floats."""
        step = self.entity_description.native_step or 1
        return round(value, 1) if step < 1 else int(value)

    @callback
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Take the reported value over, when a status report carries it."""
        if len(self._path) > 1:
            parent = self._read_path(msg, self._path[:-1])
            if isinstance(parent, dict):
                self._known.update(parent)
        value = self._read_path(msg, self._path)
        if value is None:
            return
        try:
            self._attr_native_value = float(value)
        except TypeError, ValueError:
            _LOGGER.debug(
                "Unreadable setting %s on topic %s: %r",
                self.entity_description.key,
                self._data_topic,
                value,
            )
            return
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Queue the new value as a downlink and show it right away."""
        wire = self._as_wire_value(value)
        if len(self._path) == 1:
            command = nested_command(self._path, wire)
        else:
            # The encoder wants the whole object; fill what the device has
            # not reported yet from the defaults.
            fields = {**self._defaults, **self._known}
            fields[self._path[-1]] = wire
            command = nested_command(self._path[:-1], fields)
        await self._async_send_downlink(command)
        self._known[self._path[-1]] = wire
        self._attr_native_value = value
        self.async_write_ha_state()
