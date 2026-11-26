"""
Climate entities for thermostatic radiator valves like the WT101.

State arrives via MQTT. Setting the target temperature queues a downlink on
the gateway, which the device picks up after its next uplink — the entity
shows the new target right away, and the device confirms it with a report.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.components.climate import (
    ATTR_TEMPERATURE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback

from . import lorawan_platforms
from .const import CONF_GATEWAY_URL, uses_lorawan
from .entity import MilesightDeviceEntity

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry
    from .api import MilesightDevice
    from .coordinator import MilesightGatewayCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# The encoder writes the target as a signed byte of whole degrees.
_MIN_TEMP = 5.0
_MAX_TEMP = 35.0
_STEP = 1.0
# Sent along when the device has not reported its own tolerance yet.
_DEFAULT_TOLERANCE = 1.0

# Home Assistant 2026.11 renamed the temperature attributes to native_*; the
# old names warn there and stop working in 2027.11, while the oldest supported
# release (2026.9) knows only the old ones. Drop this once that is >= 2026.11.
_NATIVE = hasattr(ClimateEntity, "native_current_temperature")
_CURRENT = (
    "_attr_native_current_temperature" if _NATIVE else "_attr_current_temperature"
)
_TARGET = "_attr_native_target_temperature" if _NATIVE else "_attr_target_temperature"
_UNIT = "_attr_native_temperature_unit" if _NATIVE else "_attr_temperature_unit"


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: MilesightGatewayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a climate entity for every device that reports a target."""
    if uses_lorawan(config_entry.data, config_entry.options):
        await lorawan_platforms.async_setup_climates(
            _hass, config_entry, async_add_entities
        )
        return
    coordinator = config_entry.runtime_data.coordinator
    gateway_url = config_entry.data[CONF_GATEWAY_URL]
    gateway_id = config_entry.runtime_data.gateway_device_id

    async_add_entities(
        MilesightThermostat(coordinator, device, gateway_url, gateway_id)
        for device in coordinator.data.devices
        if any(entity.platform == "climate" for entity in device.entities)
    )


class MilesightThermostat(MilesightDeviceEntity, ClimateEntity):
    """A LoRaWAN radiator thermostat behind the gateway."""

    # The thermostat is the device's main feature, so it carries its name.
    _attr_name = None
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    # off = temperature control disabled, auto = the device's own schedule,
    # heat = manual control through the target temperature.
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [
        HVACMode.OFF,
        HVACMode.AUTO,
        HVACMode.HEAT,
    ]
    _attr_min_temp = _MIN_TEMP
    _attr_max_temp = _MAX_TEMP
    _attr_target_temperature_step = _STEP

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Initialize the thermostat."""
        super().__init__(coordinator, device, configuration_url, gateway_id)
        self._attr_unique_id = f"{device.dev_eui}_thermostat"
        setattr(self, _UNIT, UnitOfTemperature.CELSIUS)
        setattr(self, _CURRENT, None)
        setattr(self, _TARGET, None)
        self._attr_hvac_mode = None
        self._tolerance = _DEFAULT_TOLERANCE
        self._valve_opening: int | None = None
        self._control_enabled: int | None = None
        self._control_mode: int | None = None

    @property
    def hvac_action(self) -> HVACAction | None:
        """Heating while the valve is open; unknown until it reported."""
        if self._control_enabled == 0:
            return HVACAction.OFF
        if self._valve_opening is None:
            return None
        return HVACAction.HEATING if self._valve_opening > 0 else HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the valve position for automations and dashboards."""
        return {"valve_opening": self._valve_opening}

    def _update_hvac_mode(self) -> None:
        """Derive the mode from what the device reported so far."""
        if self._control_enabled == 0:
            self._attr_hvac_mode = HVACMode.OFF
        elif self._control_mode is not None:
            self._attr_hvac_mode = (
                HVACMode.AUTO if self._control_mode == 0 else HVACMode.HEAT
            )

    @callback
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Take over whichever climate values the payload carries."""
        if (temperature := self._read_value(msg, "temperature")) is not None:
            setattr(self, _CURRENT, _as_float(temperature))
        if (target := self._read_value(msg, "target_temperature")) is not None:
            setattr(self, _TARGET, _as_float(target))
        tolerance = _as_float(self._read_value(msg, "temperature_tolerance"))
        if tolerance is not None:
            self._tolerance = tolerance
        if (valve := self._read_value(msg, "valve_opening")) is not None:
            valve_f = _as_float(valve)
            self._valve_opening = int(valve_f) if valve_f is not None else None
        enabled = self._read_path(msg, ("temperature_control", "enable"))
        if enabled is not None:
            self._control_enabled = _as_int(enabled)
        mode = self._read_path(msg, ("temperature_control", "mode"))
        if mode is not None:
            self._control_mode = _as_int(mode)
        self._update_hvac_mode()
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Queue the control state as a downlink and show it once queued."""
        if hvac_mode is HVACMode.OFF:
            await self._async_send_downlink({"temperature_control": {"enable": 0}})
            self._control_enabled = 0
        else:
            mode = 0 if hvac_mode is HVACMode.AUTO else 1
            await self._async_send_downlink(
                {"temperature_control": {"enable": 1, "mode": mode}}
            )
            self._control_enabled = 1
            self._control_mode = mode
        self._update_hvac_mode()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Queue the new target as a downlink and show it once queued."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        target = round(temperature)
        await self._async_send_downlink(
            {"target_temperature": target, "temperature_tolerance": self._tolerance}
        )
        setattr(self, _TARGET, float(target))
        self.async_write_ha_state()


def _as_float(value: Any) -> float | None:
    """Read a foreign JSON number that may arrive as a string."""
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _as_int(value: Any) -> int | None:
    """Read a foreign JSON integer that may arrive as a string."""
    parsed = _as_float(value)
    return int(parsed) if parsed is not None else None
