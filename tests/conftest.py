"""Shared test fixtures for the Milesight Gateway integration."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.milesight_gateway.api import MilesightGatewayAPI, MqttTopics

pytest_plugins = ["pytest_homeassistant_custom_component"]

# The application of the MQTT tests, as the gateway reports its topics.
GATEWAY_TOPICS = MqttTopics(
    uplink="sensors/lora/uplink/$deveui",
    downlink="sensors/lora/downlink/$deveui",
    ack="sensors/lora/ack/$deveui",
    error="sensors/lora/error/$deveui",
    join="sensors/lora/join/$deveui",
    request="sensors/lora/reqeust/$deveui",
    response="sensors/lora/response/$deveui",
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    return


@pytest.fixture(autouse=True)
def gateway_topics():
    """The gateway names its MQTT topics without a network call."""
    with patch.object(
        MilesightGatewayAPI,
        "async_get_mqtt_topics",
        AsyncMock(return_value=[GATEWAY_TOPICS]),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def fleet_members():
    """No gateway's web interface is asked for Wi-Fi and WireGuard."""
    from custom_components.milesight_gateway.fleet import FleetMembers

    with patch.object(FleetMembers, "async_read", AsyncMock(return_value={})) as mock:
        yield mock
