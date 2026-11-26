"""
The gateways of an entry, read and switched one by one: itself and its fleet.

The hub's network server names each member and its address. Load, firmware,
Wi-Fi and WireGuard are only known to a gateway's own web interface, so each
member is logged in to with the hub's account; the gateway itself is asked
through the session it has anyway. A member that refuses that account is left
alone after the one attempt: a gateway locks an account after a few failed
logins.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError

from .api import APIAuthError, APIConnectionError, SystemStatus, as_api_error
from .api import system_status as read_system_status

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from milesight_gateway_api import MilesightGatewayClient

    from .api import FleetGateway, MilesightGatewayAPI

_LOGGER = logging.getLogger(__name__)

WIFI_ACCESS_POINT = 0
# In place of an address: the gateway the entry itself talks to.
OWN = "own"


@dataclass(frozen=True)
class WireGuardInterface:
    """One of a gateway's WireGuard interfaces (wg0 to wg2)."""

    index: int
    name: str
    enabled: bool | None
    address: str | None
    peers: int


@dataclass(frozen=True)
class MemberStatus:
    """What a fleet member's own web interface reports."""

    system: SystemStatus | None
    firmware: str | None
    wifi_enabled: bool | None
    wifi_ssid: str | None
    # False for a gateway that joins a Wi-Fi as a client.
    wifi_access_point: bool | None
    wireguard: tuple[WireGuardInterface, ...]


def _flag(value: Any) -> bool | None:
    """Read a 0 or 1 of the web interface; anything else is not known."""
    return bool(value) if value in (0, 1, True, False) else None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _wireguard(interfaces: Any) -> tuple[WireGuardInterface, ...]:
    """Read the interfaces; foreign JSON, so every field may be off."""
    found = []
    for interface in interfaces if isinstance(interfaces, list) else []:
        if not isinstance(interface, dict) or not isinstance(
            interface.get("index"), int
        ):
            continue
        peers = interface.get("peers")
        found.append(
            WireGuardInterface(
                index=interface["index"],
                name=_text(interface.get("name")) or f"wg{interface['index'] - 1}",
                enabled=_flag(interface.get("enable")),
                address=_text(interface.get("address")),
                peers=len(peers) if isinstance(peers, list) else 0,
            )
        )
    return tuple(found)


class FleetMembers:
    """The web sessions to a hub's fleet members, kept between status reads."""

    def __init__(self, api: MilesightGatewayAPI) -> None:
        """Prepare; a session is opened with a member's first read."""
        self._api = api
        self._own: str | None = None
        # Per gateway EUI: the address the client was made for, and the client.
        self._clients: dict[str, tuple[str, MilesightGatewayClient]] = {}
        self._refused: set[str] = set()

    def _client(self, eui: str, address: str) -> MilesightGatewayClient:
        if address == OWN:
            self._clients[eui] = (OWN, self._api.client)
            return self._api.client
        known = self._clients.get(eui)
        if known is None or known[0] != address:
            # A new address is a new chance: the member may be another device.
            known = self._clients[eui] = (address, self._api.member_client(address))
            self._refused.discard(eui)
        return known[1]

    async def async_read(
        self,
        session: ClientSession,
        gateways: list[FleetGateway],
        own: str | None = None,
    ) -> dict[str, MemberStatus]:
        """
        Return the status of every gateway that can be asked and answers.

        ``own`` is the EUI of the gateway the entry talks to; the others need
        the address the hub names for them.
        """
        self._own = own
        members = [
            gateway
            for gateway in gateways
            if gateway.ip or (gateway.eui == own and not self._api.web_login_refused)
        ]
        results = await asyncio.gather(
            *(self._async_read_one(session, gateway) for gateway in members)
        )
        return {
            gateway.eui: status
            for gateway, status in zip(members, results, strict=True)
            if status is not None
        }

    async def _async_read_one(
        self, session: ClientSession, gateway: FleetGateway
    ) -> MemberStatus | None:
        client = self._client(
            gateway.eui, OWN if gateway.eui == self._own else gateway.ip or ""
        )
        own = gateway.eui == self._own
        try:
            with as_api_error():
                # The entry reads its own gateway's system status anyway.
                system = {} if own else await client.get_system_info(session)
        except APIAuthError:
            if gateway.eui not in self._refused:
                self._refused.add(gateway.eui)
                _LOGGER.warning(
                    "Fleet gateway %s refused the hub's account, so its load, "
                    "Wi-Fi and WireGuard are not shown. Not retried until the "
                    "integration is reloaded, so the account is not locked",
                    gateway.name,
                )
            return None
        except APIConnectionError as err:
            _LOGGER.debug("Cannot read fleet gateway %s: %s", gateway.name, err)
            return None
        wifi: dict[str, Any] = {}
        interfaces: list[dict[str, Any]] = []
        # Either may be missing on a model or firmware; the rest still counts.
        try:
            with as_api_error():
                wifi = await client.get_wifi(session)
        except (APIAuthError, APIConnectionError) as err:
            _LOGGER.debug("No Wi-Fi settings of %s: %s", gateway.name, err)
        try:
            with as_api_error():
                interfaces = await client.get_wireguard(session)
        except (APIAuthError, APIConnectionError) as err:
            _LOGGER.debug("No WireGuard settings of %s: %s", gateway.name, err)
        mode = wifi.get("mode")
        return MemberStatus(
            system=read_system_status(system) if system else None,
            firmware=_text(system.get("firmware_ver")),
            wifi_enabled=_flag(wifi.get("enabled")),
            wifi_ssid=_text(wifi.get("ssid")),
            wifi_access_point=mode == WIFI_ACCESS_POINT
            if isinstance(mode, int) and not isinstance(mode, bool)
            else None,
            wireguard=_wireguard(interfaces),
        )

    def _known(self, eui: str) -> MilesightGatewayClient:
        known = self._clients.get(eui)
        if known is None:
            msg = "This fleet gateway has not been reached yet"
            raise HomeAssistantError(msg)
        return known[1]

    def api(self, eui: str) -> MilesightGatewayAPI:
        """Return the calls of a member, through the session kept for it."""
        return self._api.around(self._known(eui))

    async def async_set_wifi(
        self, session: ClientSession, eui: str, *, enabled: bool
    ) -> None:
        """Switch a member's Wi-Fi on or off."""
        client = self._known(eui)
        try:
            with as_api_error():
                await client.set_wifi_enabled(session, enabled)
        except (APIAuthError, APIConnectionError) as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_wireguard(
        self, session: ClientSession, eui: str, index: int, *, enabled: bool
    ) -> None:
        """Switch one of a member's WireGuard interfaces on or off."""
        client = self._known(eui)
        try:
            with as_api_error():
                await client.set_wireguard_interface(session, index, enable=enabled)
        except (APIAuthError, APIConnectionError) as err:
            raise HomeAssistantError(str(err)) from err
