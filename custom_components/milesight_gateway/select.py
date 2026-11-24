"""
Select entities for device settings with fixed choices, sent as downlinks.

Each option maps a codec value to its label. The payload path names where
the value sits in uplink reports; the downlink nests the same path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity, SelectEntityDescription
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
class MilesightSelectDescription(SelectEntityDescription):
    """A setting with fixed choices and its place in the payload."""

    path: tuple[str, ...]
    values: dict[int, str] = field(default_factory=dict)


_LED_MODE = MilesightSelectDescription(
    key="led_indicator_mode",
    name="LED indicator",
    icon="mdi:led-on",
    path=("led_indicator_mode",),
    values={0: "off", 1: "on", 2: "blink"},
    entity_category=EntityCategory.CONFIG,
)
_DETECTION_MODE = MilesightSelectDescription(
    key="detection_mode",
    name="Detection mode",
    icon="mdi:motion-sensor",
    path=("detection_mode",),
    values={0: "auto", 1: "on"},
    entity_category=EntityCategory.CONFIG,
)
# The VS321 takes only these collection intervals.
_COLLECTION_STEPS = MilesightSelectDescription(
    key="collection_interval",
    name="Collection interval",
    icon="mdi:timer-sand",
    path=("collection_interval",),
    values={
        2: "2 min",
        5: "5 min",
        10: "10 min",
        15: "15 min",
        30: "30 min",
        60: "60 min",
    },
    entity_category=EntityCategory.CONFIG,
)

_VALVE_ALGORITHM = MilesightSelectDescription(
    key="valve_control_algorithm",
    name="Valve control algorithm",
    icon="mdi:chart-bell-curve",
    path=("valve_control_algorithm",),
    values={0: "rate", 1: "pid"},
    entity_category=EntityCategory.CONFIG,
)
_OFFLINE_MODE = MilesightSelectDescription(
    key="offline_control_mode",
    name="Offline control mode",
    icon="mdi:lan-disconnect",
    path=("offline_control_mode",),
    values={0: "keep", 1: "embedded control", 2: "off"},
    entity_category=EntityCategory.CONFIG,
)
_WINDOW_STRATEGY = MilesightSelectDescription(
    key="window_detection_valve_strategy",
    name="Window open valve strategy",
    icon="mdi:window-closed-variant",
    path=("window_detection_valve_strategy",),
    values={0: "keep", 1: "close"},
    entity_category=EntityCategory.CONFIG,
)

DEVICE_SELECTS: dict[str, tuple[MilesightSelectDescription, ...]] = {
    "am307": (_LED_MODE,),
    "am307l": (_LED_MODE,),
    "am308": (_LED_MODE,),
    "am308l": (_LED_MODE,),
    "vs321": (_DETECTION_MODE, _COLLECTION_STEPS),
    "wt101": (_VALVE_ALGORITHM, _OFFLINE_MODE, _WINDOW_STRATEGY),
}


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: MilesightGatewayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create select entities for every known setting of each device."""
    if data_source(config_entry.options) == SOURCE_LORAWAN:
        await lorawan_platforms.async_setup_selects(
            _hass, config_entry, async_add_entities
        )
        return
    coordinator = config_entry.runtime_data.coordinator
    gateway_url = config_entry.data[CONF_GATEWAY_URL]
    gateway_id = config_entry.runtime_data.gateway_device_id

    async_add_entities(
        MilesightSettingSelect(
            coordinator, device, description, gateway_url, gateway_id
        )
        for device in coordinator.data.devices
        for description in DEVICE_SELECTS.get(device.model_id, ())
    )


class MilesightSettingSelect(MilesightDeviceEntity, SelectEntity):
    """One device setting with fixed choices, written as a downlink."""

    entity_description: MilesightSelectDescription

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        description: MilesightSelectDescription,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator, device, configuration_url, gateway_id)
        self.entity_description = description
        self._attr_unique_id = f"{device.dev_eui}_{description.key}"
        self._attr_options = list(description.values.values())
        self._attr_current_option = None

    @callback
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Take the reported value over, when a status report carries it."""
        value = self._read_path(msg, self.entity_description.path)
        if value is None:
            return
        try:
            option = self.entity_description.values[int(value)]
        except KeyError, TypeError, ValueError:
            _LOGGER.debug(
                "Unknown value for %s on topic %s: %r",
                self.entity_description.key,
                self._data_topic,
                value,
            )
            return
        self._attr_current_option = option
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Queue the chosen value as a downlink and show it right away."""
        value = next(
            v for v, label in self.entity_description.values.items() if label == option
        )
        await self._async_send_downlink(
            nested_command(self.entity_description.path, value)
        )
        self._attr_current_option = option
        self.async_write_ha_state()
