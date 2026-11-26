"""Two-state device settings as downlinks; Wi-Fi and WireGuard of fleet gateways."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import lorawan_platforms
from .const import gateway_only
from .coordinator import MilesightDiagnosticsCoordinator
from .entity import fleet_device_info, on_first_status

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MilesightGatewayConfigEntry
    from .api import FleetGateway
    from .fleet import MemberStatus, WireGuardInterface

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MilesightGatewayConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the two-state settings of each device, and the gateways' switches."""
    await lorawan_platforms.async_setup_switches(hass, config_entry, async_add_entities)
    if gateway_only(config_entry.data):
        return
    diagnostics = config_entry.runtime_data.diagnostics
    entry_id = config_entry.entry_id
    gateway_id = config_entry.runtime_data.gateway_device_id
    # The gateways are only known once the status was read.
    on_first_status(
        config_entry,
        diagnostics,
        lambda data: [
            switch
            for gateway in data.network.gateways
            if (member := data.members.get(gateway.eui)) is not None
            for switch in (
                *(
                    [
                        MilesightFleetWifiSwitch(
                            diagnostics, gateway, entry_id, gateway_id
                        )
                    ]
                    if member.wifi_enabled is not None
                    else []
                ),
                *(
                    MilesightFleetWireGuardSwitch(
                        diagnostics, gateway, entry_id, gateway_id, interface
                    )
                    for interface in member.wireguard
                ),
            )
        ],
        async_add_entities,
    )


class _MemberSwitch(CoordinatorEntity[MilesightDiagnosticsCoordinator], SwitchEntity):
    """A setting of a gateway, switched through its own web interface."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        gateway: FleetGateway,
        entry_id: str,
        gateway_id: str,
        key: str,
    ) -> None:
        """Attach the switch to the gateway's device."""
        super().__init__(coordinator)
        self._eui = gateway.eui
        self._attr_unique_id = f"{entry_id}_{gateway.eui}_{key}"
        data = coordinator.data
        # The entry's own gateway carries its switches on the gateway device.
        local = data is not None and gateway.eui == data.network.gateway_eui
        self._attr_device_info = fleet_device_info(
            gateway, entry_id, gateway_id, local=local
        )

    @property
    def _member(self) -> MemberStatus | None:
        data = self.coordinator.data
        return data.members.get(self._eui) if data else None

    @property
    def available(self) -> bool:
        """Unavailable while the member does not answer."""
        return super().available and self._member is not None

    async def _async_switch(self, *, on: bool) -> None:
        raise NotImplementedError

    async def async_turn_on(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Switch it on at the gateway, then read its state again."""
        await self._async_switch(on=True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:  # noqa: ARG002
        """Switch it off at the gateway, then read its state again."""
        await self._async_switch(on=False)
        await self.coordinator.async_request_refresh()


class MilesightFleetWifiSwitch(_MemberSwitch):
    """The Wi-Fi of a fleet member: its access point, unless it is a client."""

    _attr_name = "Wi-Fi"
    _attr_icon = "mdi:wifi"

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        gateway: FleetGateway,
        entry_id: str,
        gateway_id: str,
    ) -> None:
        """Key it by the gateway."""
        super().__init__(coordinator, gateway, entry_id, gateway_id, "wifi")

    @property
    def is_on(self) -> bool | None:
        """Return whether the Wi-Fi is on."""
        member = self._member
        return member.wifi_enabled if member else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Name the network and whether the gateway opens it or joins it."""
        member = self._member
        if member is None:
            return None
        mode = member.wifi_access_point
        return {
            "ssid": member.wifi_ssid,
            "mode": None if mode is None else "access_point" if mode else "client",
        }

    async def _async_switch(self, *, on: bool) -> None:
        await self.coordinator.members.async_set_wifi(
            async_get_clientsession(self.hass), self._eui, enabled=on
        )


class MilesightFleetWireGuardSwitch(_MemberSwitch):
    """One WireGuard interface of a fleet member."""

    _attr_icon = "mdi:vpn"

    def __init__(
        self,
        coordinator: MilesightDiagnosticsCoordinator,
        gateway: FleetGateway,
        entry_id: str,
        gateway_id: str,
        interface: WireGuardInterface,
    ) -> None:
        """Key it by gateway and interface; only the first one starts enabled."""
        super().__init__(
            coordinator, gateway, entry_id, gateway_id, f"wireguard_{interface.index}"
        )
        self._index = interface.index
        self._attr_name = f"WireGuard {interface.name}"
        # A gateway has three; most setups use one tunnel.
        self._attr_entity_registry_enabled_default = interface.index == 1

    @property
    def _interface(self) -> WireGuardInterface | None:
        member = self._member
        return (
            next((i for i in member.wireguard if i.index == self._index), None)
            if member
            else None
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether the interface is enabled."""
        interface = self._interface
        return interface.enabled if interface else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Tell the tunnel address and how many peers are set up."""
        interface = self._interface
        if interface is None:
            return None
        return {"address": interface.address, "peers": interface.peers}

    async def _async_switch(self, *, on: bool) -> None:
        await self.coordinator.members.async_set_wireguard(
            async_get_clientsession(self.hass), self._eui, self._index, enabled=on
        )
