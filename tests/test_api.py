"""Tests for the API wrapper's error translation.

Since Milesight-Gateway-API 0.0.2 the client raises instead of swallowing its
own errors, so the wrapper's job is to translate those into the two types the
config flow and the coordinator act on, and to retry a login once when a token
has merely expired.
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
    url = URL("https://192.0.2.10:8080/api/internal/login")
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
            gateway_url="https://192.0.2.10",
            port=8080,
            username="admin",
            password="secret",  # noqa: S106
            secret_key="1111111111111111",  # noqa: S106
            iv="2222222222222222",
        )


def _ok_client(devices: list[dict] | None = None) -> MagicMock:
    """A client that logs in and returns the given raw device list."""
    client = MagicMock()
    client.get_jwt_token = AsyncMock(return_value="a.jwt.token")
    client.get_all_devices = AsyncMock(return_value=(devices or [], len(devices or [])))
    return client


# Everything the client can throw, and the type it must arrive as.
TRANSLATIONS = [
    # 401 and 403 are credentials; any other HTTP status is a reachability
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
    # The client calls .get() on the decoded body, so a JSON response that is
    # not an object fails with AttributeError inside the client.
    (AttributeError("'list' object has no attribute 'get'"), APIConnectionError),
]


# --- connect ---------------------------------------------------------------


@pytest.mark.parametrize(("raised", "expected"), TRANSLATIONS)
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
    api = _api(_ok_client())

    assert await api.async_connect(MagicMock()) is True
    assert api.connected is True


# --- fetching devices ------------------------------------------------------


@pytest.mark.parametrize(("raised", "expected"), TRANSLATIONS)
async def test_device_fetch_translates_client_errors(raised, expected):
    """The device call gets the same translation as the login."""
    client = _ok_client()
    api = _api(client)
    await api.async_connect(MagicMock())
    # The retry path logs in again, so keep that failing the same way.
    client.get_jwt_token = AsyncMock(side_effect=raised)
    client.get_all_devices = AsyncMock(side_effect=raised)

    with pytest.raises(expected):
        await api.async_get_devices(MagicMock())


async def test_expired_token_is_retried_behind_a_fresh_login():
    """A 401 while fetching means the token expired, not a wrong password."""
    devices = [{"devEUI": "24e124538b223213", "active": True}]
    client = _ok_client()
    api = _api(client)
    await api.async_connect(MagicMock())
    client.get_jwt_token.reset_mock()

    client.get_all_devices = AsyncMock(side_effect=[_response_error(401), (devices, 1)])

    found, online, offline = await api.async_get_devices(MagicMock())

    assert len(found) == 1
    assert (online, offline) == (1, 0)
    assert client.get_jwt_token.await_count == 1
    assert client.get_all_devices.await_count == 2


async def test_credentials_that_stay_rejected_surface_as_an_auth_error():
    """If the retry is rejected too, the credentials really are wrong."""
    client = _ok_client()
    api = _api(client)
    await api.async_connect(MagicMock())

    client.get_all_devices = AsyncMock(side_effect=_response_error(401))

    with pytest.raises(APIAuthError):
        await api.async_get_devices(MagicMock())

    assert client.get_all_devices.await_count == 2


async def test_the_device_list_is_read_in_one_request():
    """Paging is no snapshot on a busy gateway, so the whole list is asked for."""
    client = _ok_client([])
    await _api(client).async_get_devices(MagicMock())

    assert client.get_all_devices.await_args.kwargs["limit"] >= 10000


async def test_a_gateway_with_no_devices_is_not_an_error():
    """An empty list is now unambiguous: the gateway really has nothing."""
    client = _ok_client([])
    api = _api(client)
    await api.async_connect(MagicMock())
    client.get_jwt_token.reset_mock()

    devices, online, offline = await api.async_get_devices(MagicMock())

    assert devices == []
    assert (online, offline) == (0, 0)
    # Nothing to guess at any more, so no second login.
    assert client.get_jwt_token.await_count == 0
    assert client.get_all_devices.await_count == 1


async def test_offline_devices_are_counted_but_not_returned():
    """Only active devices become entities; the rest are counted as offline."""
    client = _ok_client(
        [
            {"devEUI": "24e124725e123456", "active": False},
            {"devEUI": "24e124725e654321", "active": False},
        ]
    )
    api = _api(client)
    await api.async_connect(MagicMock())

    devices, online, offline = await api.async_get_devices(MagicMock())

    assert devices == []
    assert (online, offline) == (0, 2)


async def test_devices_without_an_eui_or_a_known_model_are_skipped():
    """Only active devices with an EUI and a model definition become devices."""
    client = _ok_client(
        [
            {"active": True},
            {"devEUI": "ffffffffffffffff", "active": True},
            {"devEUI": "24e124538b223213", "active": True, "name": "sensor"},
        ]
    )
    api = _api(client)
    await api.async_connect(MagicMock())

    devices, online, offline = await api.async_get_devices(MagicMock())

    assert [device.dev_eui for device in devices] == ["24e124538b223213"]
    assert (online, offline) == (3, 0)


async def test_null_fields_in_the_device_list_are_tolerated():
    """The gateway sends null for a missing codec or name, and junk is skipped."""
    client = _ok_client(
        [
            "not a device",
            {"devEUI": 12345, "active": True},
            # Null must not break the whole device list; the devEUI prefix
            # stands in for the codec name.
            {
                "devEUI": "24E124538B223213",
                "active": True,
                "name": None,
                "payloadName": None,
            },
        ]
    )
    api = _api(client)
    await api.async_connect(MagicMock())

    devices, online, offline = await api.async_get_devices(MagicMock())

    assert [(device.dev_eui, device.name) for device in devices] == [
        ("24E124538B223213", "24E124538B223213")
    ]
    assert (online, offline) == (2, 0)


async def test_a_device_that_stopped_reporting_is_listed_as_offline():
    """ "active" stays true for it; the network server's status 3 tells."""
    client = _ok_client(
        [
            {"devEUI": "24E124538B223213", "active": True, "status": 2},
            {"devEUI": "24E124538B223214", "active": True, "status": 3},
            {"devEUI": "24E124538B223215", "active": False, "status": 3},
        ]
    )

    devices, online, offline = await _api(client).async_get_devices(MagicMock())

    assert [(d.dev_eui[-2:], d.online) for d in devices] == [
        ("13", True),
        ("14", False),
    ]
    assert (online, offline) == (1, 2)


# --- catalog models and downlinks -----------------------------------------


async def test_the_payload_codec_names_the_model():
    """The codec name decides, suffixes of custom codecs aside."""
    client = _ok_client(
        [
            {
                "devEUI": "24E124707E111005",
                "active": True,
                "name": "office",
                "payloadName": "AM307L_esec",
            }
        ]
    )

    devices, _, _ = await _api(client).async_get_devices(MagicMock())

    assert [(d.model_id, d.model, d.name) for d in devices] == [
        ("am307l", devices[0].model, "office")
    ]
    assert devices[0].model


async def test_downlinks_go_out_as_encoded_bytes():
    """Base64 on the queue; a reference makes it confirmed, so the ACK can follow."""
    api = _api(_ok_client())
    call = AsyncMock(return_value={})
    with patch.object(api, "async_ns_call", call):
        await api.async_enqueue_downlink(
            MagicMock(), "24E124538B223213", bytes([0xFF, 0x10]), reference="ha-7"
        )
        await api.async_enqueue_downlink(
            MagicMock(), "24E124538B223213", bytes([0xFF, 0x10])
        )

    (_, method, path, body), (*_, plain) = (c.args for c in call.await_args_list)
    assert (method, path) == ("POST", "/api/devices/24E124538B223213/queue")
    assert body == {
        "confirmed": True,
        "devEUI": "24E124538B223213",
        "fPort": 85,
        "data": "/xA=",
        "reference": "ha-7",
    }
    assert plain["confirmed"] is False
    assert "reference" not in plain


async def test_an_expired_login_is_renewed_for_a_call():
    """One 401 renews the login, then the call goes through."""
    client = _ok_client()
    api = _api(client)
    seen = AsyncMock(side_effect=[APIAuthError("401"), {"ok": 1}])
    with patch.object(api, "_async_ns_call", seen):
        assert await api.async_ns_call(MagicMock(), "GET", "/api/x") == {"ok": 1}
    assert client.get_jwt_token.await_count == 1
