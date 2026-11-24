"""
Button entities for one-shot device commands, sent as downlinks.

A press queues the command; the device acts on it after its next uplink.
The status request is the quickest way to fill the setting entities, since
the device names all its settings in the answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory

from . import lorawan_platforms
from .const import CONF_GATEWAY_URL, SOURCE_LORAWAN, data_source
from .entity import MilesightDeviceEntity

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry
    from .api import MilesightDevice
    from .coordinator import MilesightGatewayCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MilesightButtonDescription(ButtonEntityDescription):
    """A one-shot command and the downlink that triggers it."""

    command: dict[str, Any] = field(default_factory=dict)


_REBOOT = MilesightButtonDescription(
    key="reboot",
    name="Reboot",
    device_class=ButtonDeviceClass.RESTART,
    command={"reboot": 1},
    entity_category=EntityCategory.DIAGNOSTIC,
)
_REPORT_STATUS = MilesightButtonDescription(
    key="report_status",
    name="Request status report",
    icon="mdi:refresh",
    command={"report_status": 1},
    entity_category=EntityCategory.DIAGNOSTIC,
)
_VALVE_CALIBRATION = MilesightButtonDescription(
    key="valve_calibration",
    name="Calibrate valve",
    icon="mdi:tune-vertical",
    command={"valve_calibration": 1},
    entity_category=EntityCategory.CONFIG,
)
_SYNC_TIME = MilesightButtonDescription(
    key="sync_time",
    name="Sync time",
    icon="mdi:clock-check-outline",
    command={"sync_time": 1},
    entity_category=EntityCategory.CONFIG,
)

DEVICE_BUTTONS: dict[str, tuple[MilesightButtonDescription, ...]] = {
    "wt101": (_REPORT_STATUS, _VALVE_CALIBRATION, _SYNC_TIME, _REBOOT),
}


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: MilesightGatewayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create button entities for every known command of each device."""
    if data_source(config_entry.options) == SOURCE_LORAWAN:
        await lorawan_platforms.async_setup_buttons(
            _hass, config_entry, async_add_entities
        )
        return
    coordinator = config_entry.runtime_data.coordinator
    gateway_url = config_entry.data[CONF_GATEWAY_URL]
    gateway_id = config_entry.runtime_data.gateway_device_id

    async_add_entities(
        MilesightCommandButton(
            coordinator, device, description, gateway_url, gateway_id
        )
        for device in coordinator.data.devices
        for description in DEVICE_BUTTONS.get(device.model_id, ())
    )


class MilesightCommandButton(MilesightDeviceEntity, ButtonEntity):
    """One one-shot device command, written as a downlink."""

    entity_description: MilesightButtonDescription

    def __init__(
        self,
        coordinator: MilesightGatewayCoordinator,
        device: MilesightDevice,
        description: MilesightButtonDescription,
        configuration_url: str,
        gateway_id: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, device, configuration_url, gateway_id)
        self.entity_description = description
        self._attr_unique_id = f"{device.dev_eui}_{description.key}"

    @callback
    def _handle_mqtt_message(self, msg: ReceiveMessage) -> None:
        """Ignore the topic; a command has no state to read."""

    async def async_press(self) -> None:
        """Queue the command as a downlink."""
        await self._async_send_downlink(dict(self.entity_description.command))
