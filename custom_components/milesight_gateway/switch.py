"""
Switch entities for on/off device settings, sent as downlinks.

The payload path names where the value sits in uplink reports; the downlink
command nests the same path, because the codecs use one vocabulary for both.
A device reports its settings in a status report, so the state stays unknown
until the first one arrives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory

from . import lorawan_platforms
from .const import CONF_GATEWAY_URL, SOURCE_LORAWAN, data_source
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
class MilesightSwitchDescription(SwitchEntityDescription):
    """
    An on/off setting with its place in the payload.

    Some encoders take a whole object at once, like open_window_detection
    with enable, threshold and time. ``defaults`` fills the sibling fields
    until the device has reported them.
    """

    path: tuple[str, ...]
    defaults: dict[str, Any] = field(default_factory=dict)


_CHILD_LOCK = MilesightSwitchDescription(
    key="child_lock",
    name="Child lock",
    icon="mdi:lock-outline",
    path=("child_lock_config", "enable"),
    entity_category=EntityCategory.CONFIG,
)
_BUZZER = MilesightSwitchDescription(
    key="buzzer_enable",
    name="Buzzer",
    icon="mdi:volume-high",
    path=("buzzer_enable",),
    entity_category=EntityCategory.CONFIG,
)
_LED_ENABLE = MilesightSwitchDescription(
    key="led_indicator_enable",
    name="LED indicator",
    icon="mdi:led-on",
    path=("led_indicator_enable",),
    entity_category=EntityCategory.CONFIG,
)
_OWD_ENABLE = MilesightSwitchDescription(
    key="open_window_detection",
    name="Open window detection",
    icon="mdi:window-open-variant",
    path=("open_window_detection", "enable"),
    defaults={"temperature_threshold": 5, "time": 10},
    entity_category=EntityCategory.CONFIG,
)
_FREEZE_PROTECTION = MilesightSwitchDescription(
    key="freeze_protection",
    name="Freeze protection",
    icon="mdi:snowflake-alert",
    path=("freeze_protection_config", "enable"),
    defaults={"temperature": 5},
    entity_category=EntityCategory.CONFIG,
)
_DISPLAY_AMBIENT = MilesightSwitchDescription(
    key="display_ambient_temperature",
    name="Display ambient temperature",
    icon="mdi:sun-thermometer-outline",
    path=("display_ambient_temperature",),
    entity_category=EntityCategory.CONFIG,
)
_CHANGE_REPORT = MilesightSwitchDescription(
    key="change_report_enable",
    name="Report on change",
    icon="mdi:swap-vertical-bold",
    path=("change_report_enable",),
    entity_category=EntityCategory.CONFIG,
)
_OTC_ENABLE = MilesightSwitchDescription(
    key="outside_temperature_control",
    name="Outside temperature control",
    icon="mdi:home-thermometer-outline",
    path=("outside_temperature_control", "enable"),
    defaults={"timeout": 30},
    entity_category=EntityCategory.CONFIG,
)
_CALIBRATION_ENABLE = MilesightSwitchDescription(
    key="temperature_calibration",
    name="Temperature calibration",
    icon="mdi:thermometer-check",
    path=("temperature_calibration_settings", "enable"),
    defaults={"calibration_value": 0},
    entity_category=EntityCategory.CONFIG,
)

DEVICE_SWITCHES: dict[str, tuple[MilesightSwitchDescription, ...]] = {
    "am307": (_BUZZER,),
    "am307l": (_BUZZER,),
    "am308": (_BUZZER,),
    "am308l": (_BUZZER,),
    "vs340": (_LED_ENABLE,),
    "vs341": (_LED_ENABLE,),
    "ws203": (_LED_ENABLE,),
    "wt101": (
        _CHILD_LOCK,
        _OWD_ENABLE,
        _FREEZE_PROTECTION,
        _DISPLAY_AMBIENT,
        _CHANGE_REPORT,
        _OTC_ENABLE,
        _CALIBRATION_ENABLE,
    ),
}


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: MilesightGatewayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create switch entities for every known setting of each device."""
    if data_source(config_entry.options) == SOURCE_LORAWAN:
        await lorawan_platforms.async_setup_switches(
            _hass, config_entry, async_add_entities
        )
        return
    coordinator = config_entry.runtime_data.coordinator
    gateway_url = config_entry.data[CONF_GATEWAY_URL]
    gateway_id = config_entry.runtime_data.gateway_device_id

    async_add_entities(
        MilesightSettingSwitch(
            coordinator, device, description, gateway_url, gateway_id
        )
        for device in coordinator.data.devices
        for description in DEVICE_SWITCHES.get(device.model_id, ())
    )


class MilesightSettingSwitch(MilesightDeviceEntity, SwitchEntity):
    """One on/off device setting, written as a downlink."""

    entity_description: MilesightSwitchDescription

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        description: MilesightSwitchDescription,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, device, configuration_url, gateway_id)
        self.entity_description = description
        self._attr_unique_id = f"{device.dev_eui}_{description.key}"
        # Sibling fields of the setting's object, as last reported.
        self._known: dict[str, Any] = {}

    @callback
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Take the reported value over, when a status report carries it."""
        path = self.entity_description.path
        if len(path) > 1:
            parent = self._read_path(msg, path[:-1])
            if isinstance(parent, dict):
                self._known.update(parent)
        value = self._read_path(msg, path)
        if value is None:
            return
        try:
            self._attr_is_on = int(value) == 1
        except TypeError, ValueError:
            _LOGGER.debug(
                "Unreadable setting %s on topic %s: %r",
                self.entity_description.key,
                self._data_topic,
                value,
            )
            return
        self.async_write_ha_state()

    async def _async_send(self, value: int) -> None:
        path = self.entity_description.path
        if len(path) == 1:
            command = nested_command(path, value)
        else:
            # The encoder wants the whole object; fill what the device has
            # not reported yet from the defaults.
            fields = {**self.entity_description.defaults, **self._known}
            fields[path[-1]] = value
            command = nested_command(path[:-1], fields)
        await self._async_send_downlink(command)
        self._known[path[-1]] = value
        self._attr_is_on = value == 1
        self.async_write_ha_state()

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Queue the enable downlink and show it right away."""
        await self._async_send(1)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Queue the disable downlink and show it right away."""
        await self._async_send(0)
