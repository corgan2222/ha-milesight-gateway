"""Tests for the gateway status: web interface session and network server calls."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.milesight_gateway.api import (
    APIAuthError,
    APIConnectionError,
    ApplicationLink,
    FleetGateway,
    GatewayDetails,
    MilesightGatewayAPI,
    SystemStatus,
)

GATEWAY = "http://192.0.2.11"
API = f"{GATEWAY}:8080"

SYSTEM = {
    "model": "ND65-868M-EA",
    "sn": "6221D4100788",
    "firmware_ver": "60.0.0.48-r3",
    "hardware_ver": "V1.3",
    "ups_sn": "-",
    "uptime": 5087048,
    "cpu_load": "3%",
    "memory_avail_percent": "25.20%",
    "emmc_avail_percent": "80.25%",
}
SUMMARY = {
    "status": 0,
    "result": [{"get": [{"type": "system", "index": 1, "value": SYSTEM}]}],
}
NO_SESSION = {"status": -2, "result": [-32001, "Session not found"]}


def _api() -> MilesightGatewayAPI:
    return MilesightGatewayAPI(
        gateway_url=f"{GATEWAY}/",
        port=8080,
        username="admin",
        password="secret",  # noqa: S106
        secret_key="1111111111111111",  # noqa: S106
        iv="2222222222222222",
        mqtt_base_topic="sensors/lora/uplink",
    )


class _Web:
    """Answer /cgi like the gateway: a login sets the session cookie."""

    def __init__(self, login=None, queries=None) -> None:
        self.login = login if login is not None else {"status": 0}
        self.queries = list(queries) if queries is not None else []

    async def __call__(self, method, url, data):
        body = json.loads(data)
        if body["core"] == "user":
            return AiohttpClientMockResponse(
                method, url, json=self.login, cookies={"session": "abc"}
            )
        answer = self.queries.pop(0) if self.queries else SUMMARY
        return AiohttpClientMockResponse(method, url, json=answer)


def _cores(aioclient_mock) -> list[str]:
    return [json.loads(call[2])["core"] for call in aioclient_mock.mock_calls]


# --- web interface session ---------------------------------------------------


async def test_details_are_read_behind_the_web_login(hass, aioclient_mock):
    """Log in, then read the summary with the session cookie."""
    aioclient_mock.post(f"{GATEWAY}/cgi", side_effect=_Web())

    details = await _api().async_get_gateway_details(async_get_clientsession(hass))

    assert details == GatewayDetails(
        model_id="ND65-868M-EA", serial_number="6221D4100788", hw_version="V1.3"
    )
    login, summary = aioclient_mock.mock_calls
    credentials = json.loads(login[2])["values"][0]
    assert credentials["username"] == "admin"
    # The password goes out AES-encrypted, never in clear text.
    assert credentials["password"] not in ("", "secret")
    assert credentials["base"] == "web_login"
    assert summary[3]["Cookie"] == "session=abc"


async def test_the_web_session_is_kept(hass, aioclient_mock):
    """A second query reuses the session instead of logging in again."""
    aioclient_mock.post(f"{GATEWAY}/cgi", side_effect=_Web())
    api = _api()
    session = async_get_clientsession(hass)

    await api.async_get_gateway_details(session)
    await api.async_get_system_status(session)

    assert _cores(aioclient_mock) == ["user", "yruo_status", "yruo_status"]


async def test_a_lost_web_session_is_renewed_once(hass, aioclient_mock):
    """When the gateway forgets the session, one new login follows."""
    aioclient_mock.post(f"{GATEWAY}/cgi", side_effect=_Web(queries=[NO_SESSION]))

    await _api().async_get_gateway_details(async_get_clientsession(hass))

    assert _cores(aioclient_mock) == ["user", "yruo_status", "user", "yruo_status"]


async def test_a_session_that_keeps_dropping_is_a_connection_error(
    hass, aioclient_mock
):
    """Two lost sessions in a row stop the query."""
    aioclient_mock.post(
        f"{GATEWAY}/cgi", side_effect=_Web(queries=[NO_SESSION, NO_SESSION])
    )

    with pytest.raises(APIConnectionError):
        await _api().async_get_gateway_details(async_get_clientsession(hass))


@pytest.mark.parametrize(
    "login",
    [
        {"status": -2, "result": [{"locktime": 0, "chance": 4}]},
        {"status": -1},
        ["not", "an", "object"],
    ],
)
async def test_a_refused_web_login_is_not_retried(hass, aioclient_mock, caplog, login):
    """A refusal is reported once, and no further login is attempted."""
    aioclient_mock.post(f"{GATEWAY}/cgi", side_effect=_Web(login=login))
    api = _api()
    session = async_get_clientsession(hass)

    with pytest.raises(APIAuthError):
        await api.async_get_gateway_details(session)
    with pytest.raises(APIAuthError):
        await api.async_get_system_status(session)

    assert api.web_login_refused
    assert len(aioclient_mock.mock_calls) == 1
    assert caplog.text.count("refused the configured account") == 1


async def test_a_non_object_web_answer_is_a_connection_error(hass, aioclient_mock):
    """The gateway always answers /cgi with an object; anything else is broken."""
    aioclient_mock.post(f"{GATEWAY}/cgi", side_effect=_Web(queries=[["?"]]))

    with pytest.raises(APIConnectionError):
        await _api().async_get_system_status(async_get_clientsession(hass))


@pytest.mark.parametrize(
    "summary",
    [
        {"status": 0, "result": []},
        {"status": 0, "result": [{"get": [{"type": "network", "value": {}}]}]},
        {"status": 0, "result": [{"get": [{"type": "system", "value": "?"}]}]},
        {"status": 0, "result": [{"get": "?"}]},
    ],
)
async def test_a_summary_without_system_details_gives_none(
    hass, aioclient_mock, summary
):
    """A summary in an unexpected shape yields empty values, not an error."""
    aioclient_mock.post(f"{GATEWAY}/cgi", side_effect=_Web(queries=[summary]))

    details = await _api().async_get_gateway_details(async_get_clientsession(hass))

    assert details == GatewayDetails(model_id=None, serial_number=None, hw_version=None)


async def test_system_status_is_parsed(hass, aioclient_mock):
    """Percentages come as text, the uptime as seconds."""
    aioclient_mock.post(f"{GATEWAY}/cgi", side_effect=_Web())

    status = await _api().async_get_system_status(async_get_clientsession(hass))

    assert status == SystemStatus(
        cpu_load=3.0, memory_available=25.2, storage_available=80.25, uptime=5087048
    )


async def test_unusable_system_values_are_none(hass, aioclient_mock):
    """Placeholders, junk and negative uptimes are dropped."""
    junk = {**SYSTEM, "cpu_load": "-", "memory_avail_percent": 25, "uptime": -1}
    summary = {"status": 0, "result": [{"get": [{"type": "system", "value": junk}]}]}
    aioclient_mock.post(f"{GATEWAY}/cgi", side_effect=_Web(queries=[summary]))

    status = await _api().async_get_system_status(async_get_clientsession(hass))

    assert (status.cpu_load, status.memory_available, status.uptime) == (
        None,
        None,
        None,
    )


# --- network server status ---------------------------------------------------

PACKETS = [
    {
        "time": "2026-11-06T16:44:41+01:00",
        "devEUI": "24E124787F474173",
        "type": "UpUnc",
        "fCnt": 125544,
        "spreadFactor": 7,
        "frequency": 868500000,
    },
    {
        "time": "2026-11-06T16:44:30+01:00",
        "devEUI": "24E124787F474173",
        "type": "DnUnc",
        "fCnt": 12,
        "spreadFactor": 9,
        "frequency": 869525000,
    },
    # Entries without a usable time or device are skipped.
    {"time": "yesterday", "devEUI": "24E124787F474173", "type": "UpUnc"},
    {"time": "2026-11-06T16:44:00", "devEUI": "24E124787F474173", "type": "UpUnc"},
    {"time": "2026-11-06T16:44:00+01:00", "devEUI": "", "type": "UpUnc"},
]

NS = {
    "/api/packet-forwarder/network-servers": {
        "gatewayEui": "24E124FFFEF8706C",
        "status": 1,
        "pendingData": 0,
    },
    "/api/gateways": {
        "totalCount": 2,
        "result": [
            {
                "mac": "24E124FFFEF8706C",
                "name": "Local Gateway",
                "connected": True,
                "lastSeenAt": "2026-11-06 16:36:30",
            },
            {"mac": "24E124FFFE000001", "name": "", "lastSeenAt": "never"},
            {"name": "no mac"},
            "junk",
        ],
    },
    "/api/urapplications": {
        "totalCount": "3",
        "result": [
            {"id": "5", "name": "cloud_home", "using": True, "kinds": ["URMQTT"]},
            {"id": "2", "name": "", "using": True, "kinds": ["URMQTT"]},
            {"id": "6", "name": "cloud_s4", "using": False, "kinds": ["URMQTT"]},
            {"id": "7", "name": "http_app", "using": True, "kinds": ["URHTTP"]},
        ],
    },
    "/api/urapplications/5/integrations/mqtt": {
        "host": "192.0.2.30",
        "connectStatus": 3,
    },
    "/api/urapplications/2/integrations/mqtt": {
        "host": "192.0.2.254",
        "connectStatus": 4,
    },
    "/api/payloadcodecs-short": {"totalCount": "162"},
    "/api/payloadcodecs-setting": {"version": "1.5.31"},
    "/api/urprofiles": {"totalCount": "3"},
    "/api/urpackets": {"packets": PACKETS},
}


def _mock_ns(aioclient_mock, overrides=None) -> None:
    for path, payload in {**NS, **(overrides or {})}.items():
        if isinstance(payload, dict) and "status_code" in payload:
            aioclient_mock.get(f"{API}{path}", status=payload["status_code"])
        else:
            aioclient_mock.get(f"{API}{path}", json=payload)


async def test_network_status_is_read(hass, aioclient_mock):
    """Every status call lands in the right field."""
    _mock_ns(aioclient_mock)

    status = await _api().async_get_network_status(async_get_clientsession(hass))

    assert status.gateway_eui == "24E124FFFEF8706C"
    assert status.forwarder_connected is True
    assert status.pending_packets == 0
    assert status.gateways == [
        FleetGateway(
            eui="24E124FFFEF8706C",
            name="Local Gateway",
            connected=True,
            last_seen=datetime(2026, 11, 6, 16, 36, 30),  # noqa: DTZ001
        ),
        FleetGateway(
            eui="24E124FFFE000001",
            name="24E124FFFE000001",
            connected=None,
            last_seen=None,
        ),
    ]
    # Only applications with devices and an MQTT link; 3 means connected.
    assert status.links == [
        ApplicationLink(
            app_id="5", name="cloud_home", host="192.0.2.30", connected=True
        ),
        ApplicationLink(
            app_id="2", name="Application 2", host="192.0.2.254", connected=False
        ),
    ]
    assert (status.application_count, status.codec_count, status.profile_count) == (
        3,
        162,
        3,
    )
    assert status.codec_library_version == "1.5.31"
    uplink, downlink = status.packets
    assert (uplink.uplink, uplink.dev_eui, uplink.spread_factor) == (
        True,
        "24e124787f474173",
        7,
    )
    assert (downlink.uplink, downlink.frequency) == (False, 869525000)


async def test_missing_status_values_are_none(hass, aioclient_mock):
    """A forwarder without status and odd counts do not break the read."""
    _mock_ns(
        aioclient_mock,
        {
            "/api/packet-forwarder/network-servers": {},
            "/api/payloadcodecs-short": {"totalCount": True},
            "/api/urprofiles": {"totalCount": "many"},
            "/api/urapplications/5/integrations/mqtt": {"connectStatus": "3"},
        },
    )

    status = await _api().async_get_network_status(async_get_clientsession(hass))

    assert status.forwarder_connected is None
    assert (status.codec_count, status.profile_count) == (None, None)
    assert status.links[0].connected is None


async def test_an_expired_token_is_renewed_for_the_status(hass, aioclient_mock):
    """A 401 on a status call logs in once more and reads again."""
    _mock_ns(
        aioclient_mock,
        {"/api/packet-forwarder/network-servers": {"status_code": 401}},
    )
    api = _api()
    connect = AsyncMock(return_value=True)

    with (
        patch.object(api, "async_connect", connect),
        pytest.raises(APIAuthError),
    ):
        await api.async_get_network_status(async_get_clientsession(hass))

    connect.assert_awaited_once()


@pytest.mark.parametrize(
    "path",
    [
        "/api/packet-forwarder/network-servers",
        "/api/payloadcodecs-setting",
        "/api/urapplications/5/integrations/mqtt",
    ],
)
async def test_a_non_object_status_answer_is_a_connection_error(
    hass, aioclient_mock, path
):
    """Each status call must answer with an object."""
    _mock_ns(aioclient_mock, {path: ["?"]})

    with pytest.raises(APIConnectionError):
        await _api().async_get_network_status(async_get_clientsession(hass))
