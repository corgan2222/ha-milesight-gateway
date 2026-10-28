"""Tests for the API wrapper's error translation.

``MilesightGatewayClient`` reports failures in two awkward ways: it re-raises
raw aiohttp exceptions from ``get_jwt_token``, and it swallows everything in
``get_all_devices`` and returns ``([], 0)`` instead. Both are translated here,
and these tests pin that translation down.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientConnectionError, ClientResponseError, ContentTypeError
from aiohttp.client_reqrep import RequestInfo
from yarl import URL

from custom_components.milesight_gateway.api import (
    APIAuthError,
    APIConnectionError,
    MilesightGatewayAPI,
)


def _request_info() -> RequestInfo:
    url = URL("https://192.168.2.180:8080/api/internal/login")
    return RequestInfo(url=url, method="POST", headers=MagicMock(), real_url=url)


def _response_error(status: int) -> ClientResponseError:
    return ClientResponseError(_request_info(), (), status=status)


def _content_type_error() -> ContentTypeError:
    return ContentTypeError(_request_info(), (), status=200)


def _api(client: MagicMock) -> MilesightGatewayAPI:
    """Build the wrapper with the underlying client replaced."""
    with patch(
        "custom_components.milesight_gateway.api.MilesightGatewayClient",
        return_value=client,
    ):
        return MilesightGatewayAPI(
            gateway_url="https://192.168.2.180",
            port=8080,
            username="admin",
            password="secret",  # noqa: S106
            secret_key="1111111111111111",  # noqa: S106
            iv="2222222222222222",
            mqtt_base_topic="sensors/lora/uplink",
        )


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        # 401/403 are credentials; every other HTTP status is a reachability
        # problem, not a wrong password.
        (_response_error(401), APIAuthError),
        (_response_error(403), APIAuthError),
        (_response_error(500), APIConnectionError),
        (_response_error(404), APIConnectionError),
        # ContentTypeError subclasses ClientResponseError and carries the real
        # status (200 here), so it must not be read as an auth code.
        (_content_type_error(), APIConnectionError),
        (ClientConnectionError("host down"), APIConnectionError),
        (TimeoutError(), APIConnectionError),
        (json.JSONDecodeError("bad", "{}", 0), APIConnectionError),
        # The client builds its AES cipher before any request, so a bad key
        # length surfaces as a plain ValueError.
        (ValueError("Incorrect AES key length"), APIAuthError),
    ],
)
async def test_connect_translates_client_errors(raised, expected):
    """Everything the client can raise becomes one of our two error types."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(side_effect=raised)
    api = _api(client)

    with pytest.raises(expected):
        await api.async_connect(MagicMock())

    assert api.connected is False


async def test_connect_rejects_a_missing_token():
    """A 200 response with no JWT is an auth failure, not a success."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(return_value=None)
    api = _api(client)

    with pytest.raises(APIAuthError):
        await api.async_connect(MagicMock())

    assert api.connected is False


async def test_connect_succeeds_with_a_token():
    """A token marks the API connected."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(return_value="a.jwt.token")
    api = _api(client)

    assert await api.async_connect(MagicMock()) is True
    assert api.connected is True


async def test_empty_device_list_triggers_one_reauth_attempt():
    """An empty result is ambiguous, so it is retried behind a fresh login."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(return_value="a.jwt.token")
    client.get_all_devices = AsyncMock(side_effect=[([], 0), ([], 0)])
    api = _api(client)
    await api.async_connect(MagicMock())
    client.get_jwt_token.reset_mock()

    devices, online, offline = await api.async_get_devices(MagicMock())

    # A gateway with genuinely no devices reports zero without raising.
    assert devices == []
    assert (online, offline) == (0, 0)
    assert client.get_jwt_token.await_count == 1
    assert client.get_all_devices.await_count == 2


async def test_empty_device_list_surfaces_the_real_error():
    """If the re-login fails, the swallowed failure finally becomes visible."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(return_value="a.jwt.token")
    client.get_all_devices = AsyncMock(return_value=([], 0))
    api = _api(client)
    await api.async_connect(MagicMock())

    client.get_jwt_token = AsyncMock(side_effect=_response_error(401))

    with pytest.raises(APIAuthError):
        await api.async_get_devices(MagicMock())

    assert api.connected is False


async def test_non_empty_device_list_does_not_reauth():
    """The happy path costs exactly one request and no extra login."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(return_value="a.jwt.token")
    client.get_all_devices = AsyncMock(
        return_value=([{"devEUI": "24e124725e123456", "active": False}], 1)
    )
    api = _api(client)
    await api.async_connect(MagicMock())
    client.get_jwt_token.reset_mock()

    devices, online, offline = await api.async_get_devices(MagicMock())

    assert devices == []
    assert (online, offline) == (0, 1)
    assert client.get_jwt_token.await_count == 0
    assert client.get_all_devices.await_count == 1


async def test_connect_translates_a_non_object_json_body():
    """A 200 response whose body is not an object fails inside the client."""
    client = MagicMock()
    # The client does data.get("jwt") on the decoded body, so a list raises
    # AttributeError rather than any aiohttp or value error.
    client.get_jwt_token = AsyncMock(
        side_effect=AttributeError("'list' object has no attribute 'get'")
    )
    api = _api(client)

    with pytest.raises(APIConnectionError):
        await api.async_connect(MagicMock())


async def test_truncated_pagination_is_an_error():
    """A short device list means the client aborted its pagination loop."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(return_value="a.jwt.token")
    # 10 devices collected, 25 reported: pages 2+ were lost to a swallowed error.
    client.get_all_devices = AsyncMock(
        return_value=([{"devEUI": f"eui{i}", "active": True} for i in range(10)], 25)
    )
    api = _api(client)
    await api.async_connect(MagicMock())

    with pytest.raises(APIConnectionError, match="10 of 25"):
        await api.async_get_devices(MagicMock())


async def test_a_gateway_that_had_devices_and_now_reports_none_is_an_error():
    """Going from devices to an empty list is a failure, not an empty gateway."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(return_value="a.jwt.token")
    client.get_all_devices = AsyncMock(
        return_value=([{"devEUI": "24e124725e123456", "active": True}], 1)
    )
    api = _api(client)
    await api.async_connect(MagicMock())
    # First poll sees a device, so the gateway is known to have some.
    await api.async_get_devices(MagicMock())

    client.get_all_devices = AsyncMock(return_value=([], 0))

    with pytest.raises(APIConnectionError, match="stopped returning"):
        await api.async_get_devices(MagicMock())


async def test_a_gateway_that_never_had_devices_stays_empty_without_error():
    """A genuinely empty gateway must still set up, with a warning logged."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(return_value="a.jwt.token")
    client.get_all_devices = AsyncMock(return_value=([], 0))
    api = _api(client)
    await api.async_connect(MagicMock())

    devices, online, offline = await api.async_get_devices(MagicMock())

    assert devices == []
    assert (online, offline) == (0, 0)


@pytest.mark.parametrize(
    ("raw_total", "expected_devices"),
    [
        # The real gateway sends devTotalCount as a string, which used to be
        # compared against a length and raised TypeError.
        ("1", 1),
        (1, 1),
        ("", 1),
        (None, 1),
        ("not a number", 1),
    ],
)
async def test_device_count_is_normalised(raw_total, expected_devices):
    """A non-integer device count must not break the device fetch."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(return_value="a.jwt.token")
    client.get_all_devices = AsyncMock(
        return_value=(
            [{"devEUI": "24e124725e123456", "active": True}],
            raw_total,
        )
    )
    api = _api(client)
    await api.async_connect(MagicMock())

    devices, online, offline = await api.async_get_devices(MagicMock())

    assert online == expected_devices
    assert offline == 0
    assert isinstance(devices, list)


async def test_string_device_count_still_detects_truncation():
    """Normalising the count must not lose the truncation check."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(return_value="a.jwt.token")
    client.get_all_devices = AsyncMock(
        return_value=(
            [{"devEUI": f"eui{i}", "active": True} for i in range(10)],
            "25",
        )
    )
    api = _api(client)
    await api.async_connect(MagicMock())

    with pytest.raises(APIConnectionError, match="10 of 25"):
        await api.async_get_devices(MagicMock())
