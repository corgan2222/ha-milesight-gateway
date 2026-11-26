"""
Pulse counters as water, gas or energy meters for the energy dashboard.

A device's pulse counter (EM300-DI, UC11xx, UC300, UC50x) gets three settings
on its device page: what it counts, the pulses per unit and the meter reading
to start from. The meter shows offset + pulses / pulses per unit. All four are
local: nothing is sent to the device.

A device starts its counter at zero again after a reboot or a new battery. The
meter then carries the count it had on, so the reading never falls: a falling
total would be booked as a new meter, and the offset as consumption.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfEnergy,
    UnitOfVolume,
)
from homeassistant.core import callback
from homeassistant.helpers.restore_state import RestoreEntity

from .lorawan_entity import MilesightLoRaWANEntity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .lorawan_coordinator import MilesightLoRaWANCoordinator

COUNTER = re.compile(r"^(gpio_counter(_\d+)?|pulse)$")
NONE = "none"
KINDS: dict[str, tuple[SensorDeviceClass | None, str | None]] = {
    NONE: (None, None),
    "water": (SensorDeviceClass.WATER, UnitOfVolume.CUBIC_METERS),
    "gas": (SensorDeviceClass.GAS, UnitOfVolume.CUBIC_METERS),
    "energy": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR),
}
DEFAULTS = {"kind": NONE, "pulses_per_unit": 1.0, "offset": 0.0}
ATTR_CARRIED = "pulses_carried"
ATTR_COUNTED = "pulses_counted"


def counters(coordinator: MilesightLoRaWANCoordinator) -> list[str]:
    """Return the pulse counter fields of a device's model."""
    return sorted(key for key in coordinator.data.fields if COUNTER.match(key))


def _config(coordinator: MilesightLoRaWANCoordinator, key: str) -> dict[str, Any]:
    """Return the meter settings of one counter, shared by its four entities."""
    return coordinator.meters.setdefault(key, dict(DEFAULTS))


def meter_value(raw: Any, config: dict[str, Any]) -> float | None:
    """Return the meter reading for a pulse count, or None when unusable."""
    if config["kind"] == NONE or isinstance(raw, bool):
        return None
    try:
        pulses = float(raw)
        per_unit = float(config["pulses_per_unit"])
    except TypeError, ValueError:
        return None
    if per_unit <= 0:
        return None
    return round(float(config["offset"]) + pulses / per_unit, 3)


class _MeterEntity(MilesightLoRaWANEntity):
    """One part of a counter's meter."""

    def __init__(
        self,
        coordinator: MilesightLoRaWANCoordinator,
        counter: str,
        part: str,
        description_type: type,
        **kwargs: Any,
    ) -> None:
        """Key it as <counter>_<part>; the name comes from the counter's."""
        field = coordinator.data.fields[counter]
        self.counter = counter
        super().__init__(
            coordinator,
            description_type(
                key=f"{counter}_{part}",
                name=f"{field.get('name') or counter} {part.replace('_', ' ')}",
                **kwargs,
            ),
        )

    @property
    def config(self) -> dict[str, Any]:
        """The counter's meter settings."""
        return _config(self.coordinator, self.counter)

    @property
    def field(self) -> dict[str, Any]:
        """No catalog field of its own."""
        return {}


class _MeterSetting(_MeterEntity, RestoreEntity):
    """A meter setting kept over restarts."""

    setting = ""

    async def async_added_to_hass(self) -> None:
        """Take the stored setting."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            try:
                self.config[self.setting] = self._parse(last.state)
            except ValueError:
                return
            self.coordinator.async_update_listeners()

    def _parse(self, state: str) -> Any:
        return float(state)

    @callback
    def _store(self, value: Any) -> None:
        self.config[self.setting] = value
        # The meter of the same counter reads it.
        self.coordinator.async_update_listeners()


class MeterKind(_MeterSetting, SelectEntity):
    """What the counter counts."""

    setting = "kind"

    def __init__(self, coordinator: MilesightLoRaWANCoordinator, counter: str) -> None:
        """Water, gas, energy or nothing."""
        super().__init__(
            coordinator,
            counter,
            "meter_type",
            SelectEntityDescription,
            options=list(KINDS),
            entity_category=EntityCategory.CONFIG,
        )

    def _parse(self, state: str) -> Any:
        if state not in KINDS:
            raise ValueError(state)
        return state

    @property
    def current_option(self) -> str:
        """The chosen kind."""
        return self.config["kind"]

    async def async_select_option(self, option: str) -> None:
        """Choose a kind."""
        self._store(option)


class MeterNumber(_MeterSetting, NumberEntity):
    """Pulses per unit, or the reading to start from."""

    def __init__(
        self, coordinator: MilesightLoRaWANCoordinator, counter: str, setting: str
    ) -> None:
        """Take a positive factor or any offset."""
        self.setting = setting
        per_unit = setting == "pulses_per_unit"
        super().__init__(
            coordinator,
            counter,
            setting if per_unit else "meter_offset",
            NumberEntityDescription,
            native_min_value=0.001 if per_unit else 0,
            native_max_value=1_000_000 if per_unit else 100_000_000,
            native_step=0.001,
            mode=NumberMode.BOX,
            entity_category=EntityCategory.CONFIG,
        )

    @property
    def native_value(self) -> float:
        """The setting."""
        return self.config[self.setting]

    async def async_set_native_value(self, value: float) -> None:
        """Change the setting."""
        self._store(value)


class Meter(_MeterEntity, RestoreEntity, SensorEntity):
    """The meter reading, for the energy dashboard."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: MilesightLoRaWANCoordinator, counter: str) -> None:
        """Kind and unit follow the meter type."""
        super().__init__(coordinator, counter, "meter", SensorEntityDescription)
        # Pulses counted before the device's counter last started afresh, and
        # the count it showed last.
        self._carried = 0.0
        self._last: float | None = None

    async def async_added_to_hass(self) -> None:
        """Take the carried pulses kept over the restart."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        try:
            self._carried = float(last.attributes.get(ATTR_CARRIED) or 0)
            counted = last.attributes.get(ATTR_COUNTED)
            self._last = float(counted) if counted is not None else None
        except TypeError, ValueError:
            self._carried, self._last = 0.0, None

    def _pulses(self) -> float | None:
        """Return all pulses so far, carrying the count on over a reset."""
        raw = self.device.values.get(self.counter)
        if isinstance(raw, bool):
            return None
        try:
            counted = float(raw)
        except TypeError, ValueError:
            return None
        if self._last is not None and counted < self._last:
            self._carried += self._last
        self._last = counted
        return self._carried + counted

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What the next start needs to carry on."""
        return {ATTR_CARRIED: self._carried, ATTR_COUNTED: self._last}

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """Water, gas or energy."""
        return KINDS[self.config["kind"]][0]

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Cubic meters or kilowatt hours."""
        return KINDS[self.config["kind"]][1]

    @property
    def native_value(self) -> float | None:
        """Offset plus pulses per unit; None until a type is chosen."""
        return meter_value(self._pulses(), self.config)


def _setup(
    entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
    make: Callable[[MilesightLoRaWANCoordinator, str], list[Any]],
) -> None:
    if (devices := entry.runtime_data.devices) is None:
        return

    @callback
    def added(coordinator: MilesightLoRaWANCoordinator) -> None:
        add_entities(
            entity for key in counters(coordinator) for entity in make(coordinator, key)
        )

    entry.async_on_unload(devices.subscribe_coordinator_added(added))


def setup_sensors(entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    """Add the meter readings."""
    _setup(entry, add_entities, lambda c, key: [Meter(c, key)])


def setup_selects(entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    """Add the meter types."""
    _setup(entry, add_entities, lambda c, key: [MeterKind(c, key)])


def setup_numbers(entry: ConfigEntry, add_entities: AddEntitiesCallback) -> None:
    """Pulses per unit and offsets."""
    _setup(
        entry,
        add_entities,
        lambda c, key: [
            MeterNumber(c, key, "pulses_per_unit"),
            MeterNumber(c, key, "offset"),
        ],
    )
