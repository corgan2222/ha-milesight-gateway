"""
Device entities, one setup function per platform.

The platform modules of the integration call these for every entry that has
devices, whichever data source feeds them: both build the same device models.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.event import EventEntity, EventEntityDescription
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
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util
from milesight_gateway_api import decoder

from . import commands, link
from .lorawan_coordinator import MilesightLoRaWANCoordinator, device_eui
from .lorawan_entity import (
    OPTIONAL,
    PEOPLE_COUNTS,
    RETIRE_EMPTY_OPTIONAL,
    MilesightLoRaWANEntity,
    binary_kind,
    event_type,
    ha_unit,
    is_binary,
    is_event,
    is_sensor,
    is_set,
    registered_keys,
    sensor_kind,
    setting_kind,
    track_fields,
    value_name,
    wanted_control,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from milesight_gateway_api.lorawan_devices import MilesightDevice

Make = Callable[[MilesightLoRaWANCoordinator, str, dict[str, Any]], Any]


class _Batch:
    """
    Collect the entities a platform setup adds, and add them in one call.

    Each add_entities call is a task of its own; a fleet of 100 devices made
    880 of them at setup. Entities of devices added later go straight on.
    """

    def __init__(self, add_entities: AddEntitiesCallback) -> None:
        self._add = add_entities
        self._held: list[Any] | None = []

    def __call__(self, entities: Iterable[Any]) -> None:
        if self._held is None:
            self._add(entities)
        else:
            self._held.extend(entities)

    def flush(self) -> None:
        held, self._held = self._held, None
        if held:
            self._add(held)


def _batched(setup: Callable[..., Any]) -> Callable[..., Any]:
    """Run a platform setup with its entities added in one call."""

    @functools.wraps(setup)
    async def run(
        hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
    ) -> None:
        batch = _Batch(add_entities)
        await setup(hass, entry, batch)
        batch.flush()

    return run


def _for_each_device(
    entry: ConfigEntry, setup: Callable[[MilesightLoRaWANCoordinator], None]
) -> None:
    entry.async_on_unload(entry.runtime_data.devices.subscribe_coordinator_added(setup))


def _sends_downlinks(entry: ConfigEntry) -> bool:
    """Whether the entry has devices that take settings: it needs a way to send."""
    devices = entry.runtime_data.devices
    return devices is not None and devices.sends_downlinks


def _readings(
    entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
    wanted: Callable[[str, dict[str, Any]], bool],
    make: Make,
) -> None:
    """Add an entity per reported field, as the device reports new ones."""

    @callback
    def added(coordinator: MilesightLoRaWANCoordinator) -> None:
        def add(keys: Iterable[str]) -> None:
            fields = coordinator.data.fields
            add_entities(make(coordinator, key, fields[key]) for key in keys)

        entry.async_on_unload(track_fields(coordinator, wanted, add))

    _for_each_device(entry, added)


def _controls(
    entry: ConfigEntry,
    add_entities: AddEntitiesCallback,
    pick: Callable[[MilesightDevice], dict[str, dict[str, Any]]],
    make: Make,
) -> None:
    """
    Add a control per picked setting or command.

    The wanted ones exist from the start. A setting the device reports in its
    uplinks (a WT101 sends its valve opening every minute) gets its entity
    when it first does, enabled, whatever the "all settings" option says.
    """
    if not _sends_downlinks(entry):
        return

    @callback
    def added(coordinator: MilesightLoRaWANCoordinator) -> None:
        device = coordinator.data
        fields = pick(device)
        created: set[str] = set()

        def add(keys: Iterable[str], *, reported: bool) -> None:
            new = [key for key in keys if key not in created]
            created.update(new)
            if new:
                add_entities(
                    make(coordinator, key, {**fields[key], "reported": reported})
                    for key in new
                )

        add([k for k in fields if wanted_control(entry, k)], reported=False)
        # Had an entity before: its registry entry keeps it enabled or not.
        add(registered_keys(coordinator) & fields.keys(), reported=True)

        @callback
        def check() -> None:
            add([k for k in device.values if k in fields], reported=True)

        check()
        entry.async_on_unload(coordinator.async_add_listener(check))

    _for_each_device(entry, added)


class _Remembered(MilesightLoRaWANEntity, RestoreEntity):
    """
    A field that keeps its last value over a restart.

    Settings arrive only on request, and alarms like tamper or an open window
    only on change, so without this a restart empties them until then.
    """

    def _raw_from(self, state: str) -> Any:
        """Turn a stored state back into the device's raw value."""
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        """Seed the device's value from the stored state, unless it reported one."""
        await super().async_added_to_hass()
        key = self.entity_description.key
        if key in self.device.values:
            return
        last = await self.async_get_last_state()
        if last is None or last.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self._retire_if_never_used(key)
            return
        try:
            raw = self._raw_from(last.state)
        except StopIteration, ValueError:
            return
        self.device.values.setdefault(key, raw)
        # The thermostat reads the same value. Only then: every entity of the
        # device would write its state again, for each one restored.
        if TARGET in self.device.settings:
            self.coordinator.async_update_listeners()

    def _retire_if_never_used(self, key: str) -> None:
        """
        Disable a region or line reading an older version made enabled.

        Only in the run that follows the update, and only without a value: one
        that carries a value is in use, and what is enabled later stays.
        """
        entry = self.coordinator.config_entry
        if (
            OPTIONAL.fullmatch(key)
            and entry.entry_id in self.hass.data.get(RETIRE_EMPTY_OPTIONAL, ())
            and self.registry_entry is not None
            and not self.registry_entry.disabled
        ):
            er.async_get(self.hass).async_update_entity(
                self.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )


# --- readings ----------------------------------------------------------------


class MilesightLoRaWANSensor(_Remembered, SensorEntity):
    """The last reported value of one field."""

    def _raw_from(self, state: str) -> Any:
        if self.entity_description.device_class == SensorDeviceClass.ENUM:
            return int(
                next(v for v, name in self.field["values"].items() if name == state)
            )
        if not self.entity_description.state_class:
            return state
        # A battery of 99 stays 99, not 99.0.
        return int(state) if state.lstrip("-").isdigit() else float(state)

    @property
    def native_value(self) -> Any:
        """Name an enumeration, keep a number, show anything else as text."""
        raw = self.raw
        if raw is None:
            return None
        if self.entity_description.device_class == SensorDeviceClass.ENUM:
            return value_name(self.field, raw)
        if self.entity_description.state_class is not None:
            number = isinstance(raw, (int, float)) and not isinstance(raw, bool)
            return raw if number else None
        return str(raw)


class MilesightLoRaWANLastSeen(MilesightLoRaWANEntity, SensorEntity):
    """When the newest uplink of the device arrived."""

    def __init__(self, coordinator: MilesightLoRaWANCoordinator) -> None:
        """Share the MQTT source's ID, so switching keeps the history."""
        super().__init__(
            coordinator,
            SensorEntityDescription(
                key="last_seen",
                name="Last seen",
                device_class=SensorDeviceClass.TIMESTAMP,
                entity_category=EntityCategory.DIAGNOSTIC,
            ),
        )

    async def async_added_to_hass(self) -> None:
        """Follow the device list too, for the network server's time."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.config_entry.runtime_data.coordinator.async_add_listener(
                self._handle_coordinator_update
            )
        )

    @property
    def native_value(self) -> Any:
        """
        The receive time of the newest uplink.

        Before Home Assistant heard one, the time the network server last
        heard the device: a device that reports twice a day shows it after a
        restart too.
        """
        uplink = self.coordinator.connection.last_uplinks.get(
            self.device.descriptor.dev_eui
        )
        if uplink:
            return uplink.received_at
        listed = self.coordinator.config_entry.runtime_data.coordinator.data
        known = listed.by_eui.get(device_eui(self.device)) if listed else None
        return known.last_seen if known else None


class MilesightCommandStatus(MilesightLoRaWANEntity, SensorEntity):
    """What became of the last settings or command sent to the device."""

    _unrecorded_attributes = frozenset({"command", "reported", "attempts"})

    def __init__(self, coordinator: MilesightLoRaWANCoordinator) -> None:
        """One per device that takes downlinks."""
        super().__init__(
            coordinator,
            SensorEntityDescription(
                key="command_status",
                translation_key="command_status",
                device_class=SensorDeviceClass.ENUM,
                options=commands.STATES,
                entity_category=EntityCategory.DIAGNOSTIC,
            ),
        )

    @property
    def native_value(self) -> str | None:
        """The state of the newest command, or None before the first."""
        return self.coordinator.commands.state

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """What was sent, how often, and what the device reported back."""
        tracker = self.coordinator.commands
        if tracker.state is None:
            return None
        return {
            "command": tracker.changes,
            "attempts": tracker.attempts,
            "reported": tracker.reported,
            "changed_at": tracker.changed_at,
        }


class _LinkSensor(MilesightLoRaWANEntity, SensorEntity):
    """A value derived from the device's uplinks since Home Assistant started."""

    def __init__(
        self, coordinator: MilesightLoRaWANCoordinator, key: str, **kwargs: Any
    ) -> None:
        """Diagnostic, named by translation."""
        super().__init__(
            coordinator,
            SensorEntityDescription(
                key=key,
                translation_key=key,
                entity_category=EntityCategory.DIAGNOSTIC,
                **kwargs,
            ),
        )


class MilesightSignalQuality(_LinkSensor):
    """Excellent to poor, by how far the SNR clears the spreading factor's floor."""

    def __init__(self, coordinator: MilesightLoRaWANCoordinator) -> None:
        """Offer the link qualities as an enumeration."""
        super().__init__(
            coordinator,
            "signal_quality",
            device_class=SensorDeviceClass.ENUM,
            options=link.QUALITIES,
        )

    def _spreading_factor(self) -> int | None:
        if (sf := self.coordinator.link.spreading_factor) is not None:
            return sf
        # The LoRaWAN source reads it from the packet log instead.
        status = self.coordinator.entry.runtime_data.diagnostics.data
        packet = (
            status.last_uplinks.get(self.device.descriptor.dev_eui.lower())
            if status
            else None
        )
        return packet.spread_factor if packet else None

    @property
    def native_value(self) -> str | None:
        """The quality of the newest uplink."""
        margin = link.link_margin(self.coordinator.link.snr, self._spreading_factor())
        return link.quality(margin)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The margin and what it was computed from."""
        sf = self._spreading_factor()
        return {
            "link_margin": link.link_margin(self.coordinator.link.snr, sf),
            "snr": self.coordinator.link.snr,
            "spreading_factor": sf,
        }


class MilesightPacketLoss(_LinkSensor):
    """Uplinks lost today, from the gaps in the frame counter."""

    def __init__(self, coordinator: MilesightLoRaWANCoordinator) -> None:
        """Report a percentage."""
        super().__init__(
            coordinator,
            "packet_loss",
            native_unit_of_measurement="%",
            state_class=SensorStateClass.MEASUREMENT,
        )

    @property
    def native_value(self) -> float | None:
        """Lost of all uplinks since local midnight, in percent."""
        return self.coordinator.link.packet_loss

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The counts behind the percentage."""
        stats = self.coordinator.link
        return {"received": stats.received_today, "lost": stats.lost_today}


class MilesightGatewaysInRange(_LinkSensor):
    """How many gateways heard the device within the last 24 hours."""

    def __init__(self, coordinator: MilesightLoRaWANCoordinator) -> None:
        """Count them; the gateways go in an attribute."""
        super().__init__(
            coordinator, "gateways_in_range", state_class=SensorStateClass.MEASUREMENT
        )

    @property
    def native_value(self) -> int | None:
        """The count, or None before an uplink names a gateway."""
        heard = self.coordinator.link.in_range()
        return len(heard) if self.coordinator.link.gateways else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Each gateway with its newest RSSI and SNR."""
        return {
            "gateways": {
                gateway: {"rssi": h.rssi, "snr": h.snr, "last_heard": h.at.isoformat()}
                for gateway, h in self.coordinator.link.in_range().items()
            }
        }


class MilesightBatteryDaysLeft(_LinkSensor):
    """Days until the battery is empty, from the trend of its level."""

    def __init__(self, coordinator: MilesightLoRaWANCoordinator) -> None:
        """Report a duration in days."""
        super().__init__(
            coordinator,
            "battery_days_left",
            device_class=SensorDeviceClass.DURATION,
            native_unit_of_measurement="d",
            suggested_display_precision=0,
        )

    @property
    def native_value(self) -> float | None:
        """None until the level has fallen over a few days."""
        log = self.coordinator.entry.runtime_data.battery
        return log.days_left(device_eui(self.device)) if log else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """When the battery is due, as a date."""
        if (days := self.native_value) is None:
            return None
        return {"replacement_due": (dt_util.now() + timedelta(days=days)).date()}


# Keys as the MQTT source names them, so switching keeps the history.
SIGNAL_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="rssi",
        name="RSSI",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="loRaSNR",
        name="LoRa SNR",
        icon="mdi:signal",
        native_unit_of_measurement="dB",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class MilesightLoRaWANSignal(MilesightLoRaWANEntity, SensorEntity):
    """RSSI or SNR of the newest uplink, at the gateway that heard it best."""

    @property
    def native_value(self) -> Any:
        """The value of the strongest reception, or None before an uplink."""
        uplink = self.coordinator.connection.last_uplinks.get(
            self.device.descriptor.dev_eui
        )
        if uplink is None or not uplink.receptions:
            return None
        best = max(
            uplink.receptions, key=lambda rx: rx.rssi if rx.rssi is not None else -999
        )
        return best.rssi if self.entity_description.key == "rssi" else best.snr


@_batched
async def async_setup_sensors(
    _hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Add readings as sensors, plus each device's last-seen time and signal."""
    _readings(
        entry,
        add_entities,
        is_sensor,
        lambda c, key, field: MilesightLoRaWANSensor(
            c, SensorEntityDescription(key=key, **sensor_kind(key, field))
        ),
    )
    _for_each_device(entry, lambda c: add_entities([MilesightLoRaWANLastSeen(c)]))
    if _sends_downlinks(entry):
        _for_each_device(entry, lambda c: add_entities([MilesightCommandStatus(c)]))
    mqtt = entry.runtime_data.mqtt is not None
    _for_each_device(
        entry,
        lambda c: add_entities(
            [MilesightBatteryDaysLeft(c)] if "battery" in c.data.fields else []
        ),
    )
    _for_each_device(
        entry,
        lambda c: add_entities(
            [MilesightSignalQuality(c), MilesightGatewaysInRange(c)]
            # The packet log has no frame counter for the LoRaWAN source.
            + ([MilesightPacketLoss(c)] if mqtt else [])
        ),
    )
    # Only the packet log knows the receptions; on MQTT a payload codec may
    # add the same values as fields, which the readings above cover.
    if entry.runtime_data.lorawan is not None:
        _for_each_device(
            entry,
            lambda c: add_entities(
                MilesightLoRaWANSignal(c, description) for description in SIGNAL_SENSORS
            ),
        )


class MilesightLoRaWANBinarySensor(_Remembered, BinarySensorEntity):
    """A two-state field; the catalog's value 0 is off."""

    def _raw_from(self, state: str) -> Any:
        return int(state == "on")

    @property
    def is_on(self) -> bool | None:
        """On for any value but 0, or for a word that means so."""
        return is_set(self.field, self.raw)


class MilesightLoRaWANOccupancy(MilesightLoRaWANEntity, BinarySensorEntity):
    """Occupied while a people counter counts anyone."""

    def __init__(self, coordinator: MilesightLoRaWANCoordinator, key: str) -> None:
        """Share the count's key; the platform tells the two apart."""
        super().__init__(
            coordinator,
            BinarySensorEntityDescription(
                key=key,
                name="Occupancy",
                device_class=BinarySensorDeviceClass.OCCUPANCY,
            ),
        )

    @property
    def is_on(self) -> bool | None:
        """On from one person."""
        raw = self.raw
        number = isinstance(raw, (int, float)) and not isinstance(raw, bool)
        return raw > 0 if number else None


@_batched
async def async_setup_binary_sensors(
    _hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Two-state readings as binary sensors."""
    _readings(
        entry,
        add_entities,
        is_binary,
        lambda c, key, field: MilesightLoRaWANBinarySensor(
            c, BinarySensorEntityDescription(key=key, **binary_kind(key, field))
        ),
    )
    _readings(
        entry,
        add_entities,
        lambda key, _field: key in PEOPLE_COUNTS,
        lambda c, key, _field: MilesightLoRaWANOccupancy(c, key),
    )


class MilesightLoRaWANEvent(MilesightLoRaWANEntity, EventEntity):
    """Fires when an uplink carries this field; the value names the press."""

    _seen = -1

    async def async_added_to_hass(self) -> None:
        """Start counting from now; earlier presses are history."""
        await super().async_added_to_hass()
        self._seen = self.device.uplinks

    @callback
    def _handle_coordinator_update(self) -> None:
        device = self.device
        if device.uplinks != self._seen:
            self._seen = device.uplinks
            raw = decoder.flatten(device.last_decoded).get(self.entity_description.key)
            name = value_name(self.field, raw)
            if name is not None:
                self._trigger_event(event_type(name))
        super()._handle_coordinator_update()


@_batched
async def async_setup_events(
    _hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Button presses as event entities."""
    _readings(
        entry,
        add_entities,
        is_event,
        lambda c, key, field: MilesightLoRaWANEvent(
            c,
            EventEntityDescription(
                key=key,
                name=field.get("name") or key,
                event_types=list(
                    dict.fromkeys(map(event_type, field["values"].values()))
                ),
            ),
        ),
    )


# --- controls ----------------------------------------------------------------

# Limits a value type allows, where the encoder documents no range.
_TYPE_LIMITS = {
    "UINT8": (0, 255),
    "INT8": (-128, 127),
    "UINT16": (0, 65535),
    "INT16": (-32768, 32767),
    "UINT32": (0, 4294967295),
    "INT32": (-2147483648, 2147483647),
}


class MilesightLoRaWANNumber(_Remembered, NumberEntity):
    """A numeric setting; unknown until reported, set or restored."""

    def _raw_from(self, state: str) -> Any:
        value = float(state)
        if self.entity_description.key == TARGET and value == INITIAL_TARGET:
            # Possibly the stand-in, not a value the device has; it shows anyway.
            raise ValueError(state)
        return value if self.field.get("value_type") == "FLOAT" else int(value)

    @property
    def native_value(self) -> float | None:
        """The reported or last set value; a target temperature is never empty."""
        raw = self.raw
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return raw
        return INITIAL_TARGET if self.entity_description.key == TARGET else None

    async def async_set_native_value(self, value: float) -> None:
        """Send the new value; integers unless the field is a float."""
        if self.field.get("value_type") != "FLOAT":
            value = int(value)
        await self.async_send({self.entity_description.key: value})


def _number(
    coordinator: MilesightLoRaWANCoordinator, key: str, field: dict[str, Any]
) -> Any:
    low, high = (
        (field["min"], field["max"])
        if "min" in field
        else _TYPE_LIMITS.get(field.get("value_type", ""), (-1000, 1000))
    )
    return MilesightLoRaWANNumber(
        coordinator,
        NumberEntityDescription(
            key=key,
            native_min_value=low,
            native_max_value=high,
            native_step=0.1 if field.get("value_type") == "FLOAT" else 1,
            native_unit_of_measurement=ha_unit(field),
            mode=NumberMode.BOX,
            **setting_kind(key, field),
        ),
    )


@_batched
async def async_setup_numbers(
    _hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Numeric settings."""
    _controls(
        entry,
        add_entities,
        lambda d: {k: f for k, f in d.settings.items() if f.get("type") == "NUMBER"},
        _number,
    )


class MilesightLoRaWANSelect(_Remembered, SelectEntity):
    """An enumerated setting; the catalog names its raw values."""

    def _raw_from(self, state: str) -> Any:
        return int(next(v for v, name in self.field["values"].items() if name == state))

    @property
    def current_option(self) -> str | None:
        """The name of the reported or last set value."""
        return value_name(self.field, self.raw)

    async def async_select_option(self, option: str) -> None:
        """Send the raw value behind the chosen name."""
        raw = next(v for v, name in self.field["values"].items() if name == option)
        await self.async_send({self.entity_description.key: int(raw)})


@_batched
async def async_setup_selects(
    _hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Add enumerated settings."""
    _controls(
        entry,
        add_entities,
        lambda d: {
            k: f
            for k, f in d.settings.items()
            if f.get("type") == "ENUM" and f.get("values")
        },
        lambda c, key, field: MilesightLoRaWANSelect(
            c,
            SelectEntityDescription(
                key=key,
                options=list(dict.fromkeys(field["values"].values())),
                **setting_kind(key, field),
            ),
        ),
    )


class MilesightLoRaWANSwitch(_Remembered, SwitchEntity):
    """A two-state setting; the catalog's value 0 is off."""

    def _raw_from(self, state: str) -> Any:
        return int(state == "on")

    @property
    def is_on(self) -> bool | None:
        """On for any value but 0."""
        return is_set(self.field, self.raw)

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Send 1."""
        await self.async_send({self.entity_description.key: 1})

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Send 0."""
        await self.async_send({self.entity_description.key: 0})


@_batched
async def async_setup_switches(
    _hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Two-state settings."""
    _controls(
        entry,
        add_entities,
        lambda d: {k: f for k, f in d.settings.items() if f.get("type") == "BOOL"},
        lambda c, key, field: MilesightLoRaWANSwitch(
            c, SwitchEntityDescription(key=key, **setting_kind(key, field))
        ),
    )


class MilesightLoRaWANButton(MilesightLoRaWANEntity, ButtonEntity):
    """Sends the command's "yes" (1)."""

    async def async_press(self) -> None:
        """Send the command."""
        await self.async_send({self.entity_description.key: 1})


@_batched
async def async_setup_buttons(
    _hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Yes/no commands: reboot, report status."""
    # Commands that take a number or an option get no entity. On the models in
    # use (VS321, WS203, AM103, AM307, EM300-TH; checked 2026-11-24) the only
    # one is fetch_history with a start and end time: a history download, not
    # a value to set. A service fits that better, if someone needs it.
    _controls(
        entry,
        add_entities,
        lambda d: {k: f for k, f in d.commands.items() if f.get("type") == "BOOL"},
        lambda c, key, field: MilesightLoRaWANButton(
            c, ButtonEntityDescription(key=key, **setting_kind(key, field))
        ),
    )


# --- thermostat ----------------------------------------------------------------

TARGET = "target_temperature"
ENABLE = "temperature_control.enable"
MODE = "temperature_control.mode"
VALVE = "valve_opening"
TOLERANCE = "temperature_tolerance"
# Milesight's example tolerance is 0; the WT101 accepted 1 °C.
DEFAULT_TOLERANCE = 1.0
# Shown until the device reports a target or one is set or restored; without a
# target Home Assistant draws no dial to set one. MQTT climate uses 21 °C too.
INITIAL_TARGET = 21.0
# Home Assistant 2026.11 renamed the temperature attributes.
_NATIVE = hasattr(ClimateEntity, "native_current_temperature")
_CURRENT = (
    "_attr_native_current_temperature" if _NATIVE else "_attr_current_temperature"
)
_TARGET = "_attr_native_target_temperature" if _NATIVE else "_attr_target_temperature"
_UNIT = "_attr_native_temperature_unit" if _NATIVE else "_attr_temperature_unit"


def _number_value(device: MilesightDevice, key: str) -> float | None:
    raw = device.values.get(key)
    return raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


def _valve(device: MilesightDevice) -> float | None:
    """
    Return the valve opening in percent.

    A WT101 reports the opening only in manual mode, but its motor position
    with every uplink: at the full stroke the valve is closed (measured with
    10 °C target in a 15 °C room: 489 of 489; manual 50 %: 247 of 495).
    """
    position = _number_value(device, "motor_position")
    stroke = _number_value(device, "motor_stroke")
    if position is not None and stroke:
        return round(max(0.0, stroke - position) * 100 / stroke)
    return _number_value(device, VALVE)


class MilesightLoRaWANClimate(MilesightLoRaWANEntity, ClimateEntity):
    """Heating by target temperature; temperature control switches it off."""

    _attr_name = None
    _attr_target_temperature_step = 1  # the encoders write whole degrees

    def __init__(self, coordinator: MilesightLoRaWANCoordinator) -> None:
        """Share the MQTT source's thermostat ID."""
        super().__init__(coordinator, ClimateEntityDescription(key="thermostat"))
        device = coordinator.data
        target = device.settings[TARGET]
        self._attr_min_temp = target.get("min", 5)
        self._attr_max_temp = target.get("max", 35)
        self._switchable = ENABLE in device.settings
        self._attr_hvac_modes = (
            [HVACMode.OFF, HVACMode.HEAT] if self._switchable else [HVACMode.HEAT]
        )
        features = ClimateEntityFeature.TARGET_TEMPERATURE
        if self._switchable:
            features |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        self._attr_supported_features = features
        setattr(self, _UNIT, UnitOfTemperature.CELSIUS)
        self._read()

    def _read(self) -> None:
        device = self.device
        setattr(self, _CURRENT, _number_value(device, "temperature"))
        target = _number_value(device, TARGET)
        setattr(
            self,
            _TARGET,
            target
            if target is not None
            else min(max(INITIAL_TARGET, self._attr_min_temp), self._attr_max_temp),
        )
        enabled = _number_value(device, ENABLE) if self._switchable else 1
        if enabled is None:
            # Settings arrive only on request; a reported target temperature
            # shows that the device regulates.
            mode = HVACMode.HEAT if _number_value(device, TARGET) is not None else None
        else:
            mode = HVACMode.HEAT if enabled else HVACMode.OFF
        self._attr_hvac_mode = mode
        valve = _valve(device)
        if mode == HVACMode.OFF:
            self._attr_hvac_action = HVACAction.OFF
        elif valve is None:
            self._attr_hvac_action = None
        else:
            self._attr_hvac_action = (
                HVACAction.HEATING if valve > 0 else HVACAction.IDLE
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._read()
        super()._handle_coordinator_update()

    def _heating(self, *, on: bool) -> dict[str, Any]:
        """
        Return the settings that switch regulation on or off.

        On also sets the control mode to auto: a WT101 in manual mode holds a
        fixed valve opening and ignores the target temperature.
        """
        if not self._switchable:
            return {}
        changes: dict[str, Any] = {ENABLE: int(on)}
        mode = self.device.settings.get(MODE)
        if on and mode is not None:
            auto = next(
                (raw for raw, name in mode.get("values", {}).items() if name == "auto"),
                None,
            )
            if auto is not None:
                changes[MODE] = int(auto)
        return changes

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Send the target and regulate to it; a tolerance if none is known."""
        changes: dict[str, Any] = {}
        mode = kwargs.get(ATTR_HVAC_MODE)
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            changes[TARGET] = round(temperature)
            if TOLERANCE in self.device.fields and TOLERANCE not in self.device.values:
                changes[TOLERANCE] = DEFAULT_TOLERANCE
            if mode is None:
                # A target only means something while the device regulates.
                changes |= self._heating(on=True)
        if mode is not None:
            changes |= self._heating(on=mode != HVACMode.OFF)
        if changes:
            await self.async_send(changes)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Switch temperature control on (auto mode) or off."""
        if changes := self._heating(on=hvac_mode != HVACMode.OFF):
            await self.async_send(changes)

    async def async_turn_on(self) -> None:
        """Heat."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Stop regulating."""
        await self.async_set_hvac_mode(HVACMode.OFF)


@_batched
async def async_setup_climates(
    _hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback
) -> None:
    """Add a thermostat for every model with a settable target temperature."""
    if not _sends_downlinks(entry):
        return

    @callback
    def added(coordinator: MilesightLoRaWANCoordinator) -> None:
        device = coordinator.data
        if TARGET in device.settings and "temperature" in device.readings:
            add_entities([MilesightLoRaWANClimate(coordinator)])

    _for_each_device(entry, added)
