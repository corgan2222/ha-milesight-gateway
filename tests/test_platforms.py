"""Tests for setup, the entity platforms and the coordinator.

The gateway API is replaced at the wrapper level, and device values are
delivered through the mocked MQTT broker, as the gateway would publish them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.milesight_gateway import async_remove_config_entry_device
from custom_components.milesight_gateway.api import (
    APIAuthError,
    APIConnectionError,
    MilesightGatewayAPI,
    _build_device,
)
from custom_components.milesight_gateway.const import DOMAIN
from custom_components.milesight_gateway.sensor import _tcp_ping

from .test_init import ENTRY_DATA

TOPIC = "sensors/lora/uplink/24e124123456789a"
DEVICE = _build_device(
    {"devEUI": "24E124123456789A", "name": "vs321"},
    {
        "id": "vs321",
        "name": "VS321",
        "description": "People counter",
        "device_entities": [
            "temperature",
            "battery",
            "people_total_counts",
            "door",
            "tamper",
        ],
    },
    "sensors/lora/uplink",
)

TEMPERATURE = "sensor.vs321_temperature"
PEOPLE = "sensor.vs321_people_count"
OCCUPANCY = "binary_sensor.vs321_occupancy"
DOOR = "binary_sensor.vs321_door"
LAST_SEEN = "sensor.vs321_last_seen"
ONLINE = "sensor.milesight_gateway_online_devices"
OFFLINE = "sensor.milesight_gateway_offline_devices"
PING = "sensor.milesight_gateway_response_time"


async def _connect(self: MilesightGatewayAPI, _session: object) -> bool:
    """Stand in for a successful login."""
    self.connected = True
    return True


@pytest.fixture
def get_devices():
    """The gateway's device list: the counter online, two others offline."""
    mock = AsyncMock(return_value=([DEVICE], 1, 2))
    with (
        patch.object(MilesightGatewayAPI, "async_connect", _connect),
        patch.object(MilesightGatewayAPI, "async_get_devices", mock),
        patch(
            "custom_components.milesight_gateway.sensor._tcp_ping",
            AsyncMock(return_value=12.3),
        ),
    ):
        yield mock


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Let MQTT's own housekeeping timers outlive the test.

    The mocked MQTT client schedules them on setup and again on unload, so no
    teardown can avoid them; they are not timers of this integration.
    """
    return True


@pytest.fixture
def mqtt(mqtt_mock):
    """Mock the MQTT broker."""
    return mqtt_mock


@pytest.fixture
async def entry(hass, mqtt, get_devices) -> MockConfigEntry:
    """A loaded config entry."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _refresh(hass, entry: MockConfigEntry) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_entities_follow_the_mqtt_payload(hass, entry):
    """One uplink updates every entity of the device it is addressed to."""
    assert hass.states.get(TEMPERATURE).state == STATE_UNKNOWN
    assert hass.states.get(ONLINE).state == "1"
    assert hass.states.get(OFFLINE).state == "2"

    async_fire_mqtt_message(
        hass, TOPIC, '{"temperature": 22.5, "people_total_counts": 2, "door": "open"}'
    )
    await hass.async_block_till_done()

    assert hass.states.get(TEMPERATURE).state == "22.5"
    assert hass.states.get(PEOPLE).state == "2"
    assert hass.states.get(OCCUPANCY).state == STATE_ON
    assert hass.states.get(DOOR).state == STATE_ON
    assert hass.states.get(LAST_SEEN).state != STATE_UNKNOWN


async def test_diagnostic_entities_are_categorized(hass, entry):
    """Battery and tamper carry the diagnostic category from the mapping."""
    registry = er.async_get(hass)
    for entity_id in ("sensor.vs321_battery", "binary_sensor.vs321_tamper"):
        assert (
            registry.async_get(entity_id).entity_category is EntityCategory.DIAGNOSTIC
        )


@pytest.mark.parametrize("payload", ["not json", "[1, 2]", '{"battery": 90}'])
async def test_unusable_payloads_leave_the_state_alone(hass, entry, payload):
    """Broken JSON, a non-object and a payload without the key change nothing."""
    async_fire_mqtt_message(hass, TOPIC, '{"temperature": 21, "door": "closed"}')
    await hass.async_block_till_done()

    async_fire_mqtt_message(hass, TOPIC, payload)
    await hass.async_block_till_done()

    assert hass.states.get(TEMPERATURE).state == "21"
    assert hass.states.get(DOOR).state == "off"


async def test_unusable_payloads_are_logged(hass, entry, caplog):
    """A payload that cannot be read is worth a warning."""
    async_fire_mqtt_message(hass, TOPIC, "not json")
    async_fire_mqtt_message(hass, TOPIC, "[1, 2]")
    await hass.async_block_till_done()

    assert f"Invalid JSON on topic {TOPIC}" in caplog.text
    assert f"Unexpected payload on topic {TOPIC}" in caplog.text


async def test_entities_follow_the_gateway_device_list(hass, entry, get_devices):
    """A device the gateway stops listing as active becomes unavailable."""
    get_devices.return_value = ([], 0, 3)
    await _refresh(hass, entry)

    for entity_id in (TEMPERATURE, PEOPLE, OCCUPANCY, DOOR, LAST_SEEN):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(OFFLINE).state == "3"

    get_devices.return_value = ([DEVICE], 1, 2)
    await _refresh(hass, entry)

    assert hass.states.get(TEMPERATURE).state == STATE_UNKNOWN


@pytest.mark.parametrize(
    "error", [APIConnectionError("host down"), RuntimeError("unexpected")]
)
async def test_entities_are_unavailable_while_the_gateway_is_down(
    hass, entry, get_devices, error
):
    """Device and gateway entities all go unavailable when a refresh fails."""
    get_devices.side_effect = error
    await _refresh(hass, entry)

    assert not entry.runtime_data.coordinator.last_update_success
    for entity_id in (TEMPERATURE, OCCUPANCY, ONLINE):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


async def test_rejected_credentials_start_a_reauth(hass, entry, get_devices):
    """An auth failure during a refresh asks for new credentials."""
    get_devices.side_effect = APIAuthError("401")
    await _refresh(hass, entry)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_response_time_is_measured_on_each_refresh(hass, entry):
    """The ping runs after a refresh, not at setup."""
    assert hass.states.get(PING).state == STATE_UNKNOWN

    await _refresh(hass, entry)

    assert hass.states.get(PING).state == "12.3"


async def test_setup_retries_when_the_gateway_is_unreachable(hass, mqtt, get_devices):
    """A failing first refresh leaves the entry to retry."""
    get_devices.side_effect = APIConnectionError("host down")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2)
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_retries_without_a_login(hass, mqtt, get_devices):
    """A refresh that never logged in is not a working connection."""
    with patch.object(
        MilesightGatewayAPI, "async_connect", AsyncMock(return_value=True)
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload(hass, entry):
    """The entry unloads cleanly and its entities go away with it."""
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(TEMPERATURE).state == STATE_UNAVAILABLE


async def test_devices_can_be_removed(hass, entry):
    """Removing a device from the UI is allowed."""
    assert await async_remove_config_entry_device(hass, entry, MagicMock())


async def test_tcp_ping_reports_the_round_trip():
    """A connection that opens yields a time in milliseconds."""
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    with patch(
        "custom_components.milesight_gateway.sensor.asyncio.open_connection",
        AsyncMock(return_value=(MagicMock(), writer)),
    ):
        result = await _tcp_ping("192.168.2.180", 8080)

    assert isinstance(result, float)
    writer.close.assert_called_once()


@pytest.mark.parametrize("error", [OSError("refused"), TimeoutError()])
async def test_tcp_ping_reports_none_when_unreachable(error):
    """A refused or timed-out connection has no round-trip time."""
    with patch(
        "custom_components.milesight_gateway.sensor.asyncio.open_connection",
        AsyncMock(side_effect=error),
    ):
        assert await _tcp_ping("192.168.2.180", 8080) is None
