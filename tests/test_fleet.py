"""Tests for the fleet members: read and switched through their own web interface."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from milesight_gateway_api import WebLoginRefusedError

from custom_components.milesight_gateway.api import FleetGateway
from custom_components.milesight_gateway.fleet import FleetMembers, WireGuardInterface

SYSTEM = {
    "cpu_load": "12.50%",
    "memory_avail_percent": "40.00%",
    "firmware_ver": "60.0.0.50",
}
WIFI = {"enabled": 1, "mode": 0, "ssid": "Hall-2", "key": "never shown"}
WIREGUARD = [
    {"index": 1, "name": "wg0", "enable": 1, "address": "10.0.0.2/24", "peers": [{}]},
    {"index": 2, "name": "wg1", "enable": 0, "address": "", "peers": []},
    "junk",
]


@pytest.fixture(autouse=True)
def fleet_members():
    """These tests read the members themselves, through a client they hand in."""
    return


def _gateway(
    eui: str = "24E124FFFE000001", ip: str | None = "192.0.2.20"
) -> FleetGateway:
    return FleetGateway(eui=eui, name="Hall", connected=True, last_seen=None, ip=ip)


def _members(client: MagicMock) -> tuple[FleetMembers, MagicMock]:
    api = MagicMock()
    api.member_client.return_value = client
    return FleetMembers(api), api


def _client() -> MagicMock:
    client = MagicMock()
    client.get_system_info = AsyncMock(return_value=SYSTEM)
    client.get_wifi = AsyncMock(return_value=WIFI)
    client.get_wireguard = AsyncMock(return_value=WIREGUARD)
    client.set_wifi_enabled = AsyncMock()
    client.set_wireguard_interface = AsyncMock()
    return client


async def test_a_member_is_read_through_its_own_web_interface():
    """Load, firmware, Wi-Fi and WireGuard; the Wi-Fi key is not kept."""
    members, api = _members(_client())

    status = await members.async_read(MagicMock(), [_gateway(), _gateway("X", None)])

    api.member_client.assert_called_once_with("192.0.2.20")
    member = status["24E124FFFE000001"]
    assert member.system.cpu_load == 12.5
    assert member.firmware == "60.0.0.50"
    assert (member.wifi_enabled, member.wifi_ssid, member.wifi_access_point) == (
        True,
        "Hall-2",
        True,
    )
    assert member.wireguard == (
        WireGuardInterface(
            index=1, name="wg0", enabled=True, address="10.0.0.2/24", peers=1
        ),
        WireGuardInterface(index=2, name="wg1", enabled=False, address=None, peers=0),
    )
    assert "never shown" not in repr(member)
    # A member the hub names no address for is not asked.
    assert "X" not in status


async def test_a_member_that_refuses_the_account_is_asked_once(caplog):
    """The gateway locks an account after a few failed logins."""
    client = _client()
    client.get_system_info.side_effect = WebLoginRefusedError("no")
    members, _api = _members(client)

    assert await members.async_read(MagicMock(), [_gateway()]) == {}
    assert await members.async_read(MagicMock(), [_gateway()]) == {}

    assert caplog.text.count("refused the hub's account") == 1


async def test_a_member_without_wireguard_still_reports_the_rest():
    client = _client()
    client.get_wireguard.side_effect = TimeoutError
    members, _api = _members(client)

    status = await members.async_read(MagicMock(), [_gateway()])

    member = status["24E124FFFE000001"]
    assert member.wireguard == ()
    assert member.wifi_enabled is True


async def test_wifi_and_wireguard_are_switched_at_the_member():
    client = _client()
    members, _api = _members(client)
    session = MagicMock()
    await members.async_read(session, [_gateway()])

    await members.async_set_wifi(session, "24E124FFFE000001", enabled=False)
    await members.async_set_wireguard(session, "24E124FFFE000001", 1, enabled=True)

    client.set_wifi_enabled.assert_awaited_once_with(session, False)  # noqa: FBT003
    client.set_wireguard_interface.assert_awaited_once_with(session, 1, enable=True)


async def test_a_member_never_reached_cannot_be_switched():
    members, _api = _members(_client())

    with pytest.raises(HomeAssistantError):
        await members.async_set_wifi(MagicMock(), "24E124FFFE000001", enabled=True)


async def test_the_gateway_itself_is_asked_through_its_own_session():
    """It has no address in the hub's list; its own client reaches it."""
    client = _client()
    api = MagicMock()
    api.client = client
    api.web_login_refused = False
    members = FleetMembers(api)
    own = _gateway("24E124FFFE0000AA", None)

    status = await members.async_read(MagicMock(), [own], own.eui)
    await members.async_set_wifi(MagicMock(), own.eui, enabled=True)

    api.member_client.assert_not_called()
    assert status[own.eui].wifi_ssid == "Hall-2"
    client.set_wifi_enabled.assert_awaited_once()
