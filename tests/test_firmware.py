"""Tests for the gateway's model and firmware and the newest firmware.

Parsing the download page is tested in the client library; here it is the
fetch through Home Assistant's session and the translation of its failures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import ClientError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from milesight_gateway_api.firmware import FIRMWARE_PAGE_URL

from custom_components.milesight_gateway.api import (
    APIConnectionError,
    GatewayInfo,
    MilesightGatewayAPI,
    async_get_latest_firmware,
)

PAGE = (Path(__file__).parent / "fixtures" / "firmware_page.html").read_text("utf-8")

GATEWAY = "http://192.0.2.11"


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


async def test_latest_firmware_is_read_from_the_download_page(hass, aioclient_mock):
    """The page is fetched and parsed for the family."""
    aioclient_mock.get(FIRMWARE_PAGE_URL, text=PAGE)

    release = await async_get_latest_firmware(async_get_clientsession(hass), "60")

    assert release is not None
    assert release.version == "60.0.0.50"


@pytest.mark.parametrize(
    "response", [{"status": 503}, {"exc": ClientError()}, {"exc": TimeoutError()}]
)
async def test_an_unreachable_download_page_is_a_connection_error(
    hass, aioclient_mock, response
):
    """Any failure to load the page surfaces as a connection error."""
    aioclient_mock.get(FIRMWARE_PAGE_URL, **response)

    with pytest.raises(APIConnectionError):
        await async_get_latest_firmware(async_get_clientsession(hass), "60")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"model": "UG65", "rtver": "60.0.0.48-r3", "status": -2},
            GatewayInfo(model="UG65", firmware="60.0.0.48-r3"),
        ),
        (
            {"model": "", "rtver": "60.0.0.48-r3"},
            GatewayInfo(model=None, firmware="60.0.0.48-r3"),
        ),
        ({"rtver": "60.0.0.48-r3"}, GatewayInfo(model=None, firmware="60.0.0.48-r3")),
    ],
)
async def test_gateway_info_is_read_without_a_login(
    hass, aioclient_mock, payload, expected
):
    """The web interface's login check reports the model and the firmware."""
    aioclient_mock.post(f"{GATEWAY}/islogin", json=payload)

    info = await _api().async_get_gateway_info(async_get_clientsession(hass))

    assert info == expected


@pytest.mark.parametrize(
    "response",
    [
        {"json": {"model": "UG65", "status": -2}},
        {"json": {"rtver": ""}},
        {"json": ["60.0.0.48-r3"]},
        {"text": "not json"},
        {"status": 500},
        {"exc": ClientError()},
    ],
)
async def test_a_missing_installed_version_is_a_connection_error(
    hass, aioclient_mock, response
):
    """No usable version in the answer, or no answer, is a connection error."""
    aioclient_mock.post(f"{GATEWAY}/islogin", **response)

    with pytest.raises(APIConnectionError):
        await _api().async_get_gateway_info(async_get_clientsession(hass))
