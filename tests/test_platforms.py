"""Tests for setup, the entity platforms and the coordinator.

The gateway API is replaced at the wrapper level, and device values are
delivered through the mocked MQTT broker, as the gateway would publish them.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError
from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.milesight_gateway import async_remove_config_entry_device
from custom_components.milesight_gateway.api import (
    APIAuthError,
    APIConnectionError,
    ApplicationLink,
    FirmwareRelease,
    FleetGateway,
    GatewayDetails,
    GatewayInfo,
    MilesightDevice,
    MilesightGatewayAPI,
    NetworkStatus,
    Packet,
    SystemStatus,
    _packet,
)
from custom_components.milesight_gateway.const import (
    CONF_DEVICES,
    CONF_FIRMWARE_CHECK,
    CONF_GATEWAY_ONLY,
    CONF_GATEWAY_URL,
    CONF_IV,
    CONF_MAC,
    CONF_MODEL,
    CONF_SECRET_KEY,
    CONF_STATUS_INTERVAL,
    CONF_WEB_DATA,
    DOMAIN,
    FIRMWARE_SCAN_INTERVAL,
)
from custom_components.milesight_gateway.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.milesight_gateway.fleet import FleetMembers, MemberStatus
from custom_components.milesight_gateway.sensor import _tcp_ping

from .test_init import ENTRY_DATA

TOPIC = "sensors/lora/uplink/24e124123456789a"
DEVICE = MilesightDevice("24E124123456789A", "vs321", "People counter", "vs321")
# A full report, as the gateway's payload codec publishes it.
REPORT = (
    '{"temperature": 22.5, "battery": 90, "people_total_counts": 2, '
    '"region_1_enable": "enable"}'
)

TEMPERATURE = "sensor.vs321_temperature"
PEOPLE = "sensor.vs321_people_total_counts"
OCCUPANCY = "binary_sensor.vs321_occupancy"
DOOR = "binary_sensor.vs321_region_1_enable"
LAST_SEEN = "sensor.vs321_last_seen"
ONLINE = "sensor.milesight_gateway_online_devices"
OFFLINE = "sensor.milesight_gateway_offline_devices"
PING = "sensor.milesight_gateway_response_time"
FIRMWARE = "update.milesight_gateway_firmware"

RELEASE = FirmwareRelease(
    version="60.0.0.50",
    title="UG65&UG67 LoRaWAN® Gateway",
    release_url="https://www.milesight.com/iot/resources/download/release-note/?type=ug6x",
    download_url="https://resource.milesight.com/milesight/iot/firmware/60.0.0.50.bin",
    notes="Update to 60.0.0.45 or later first.",
)
DETAILS = GatewayDetails(
    model_id="ND65-868M-EA", serial_number="6221D4100788", hw_version="V1.3"
)


async def _connect(self: MilesightGatewayAPI, _session: object) -> bool:
    """Stand in for a successful login."""
    self.connected = True
    return True


@pytest.fixture
def firmware():
    """The gateway is a UG65 on 60.0.0.48-r3; Milesight offers 60.0.0.50."""
    installed = AsyncMock(
        return_value=GatewayInfo(model="UG65", firmware="60.0.0.48-r3")
    )
    latest = AsyncMock(return_value=[RELEASE])
    details = AsyncMock(return_value=DETAILS)
    with (
        patch.object(MilesightGatewayAPI, "async_get_gateway_info", installed),
        patch.object(MilesightGatewayAPI, "async_get_gateway_details", details),
        patch(
            "custom_components.milesight_gateway.coordinator.async_get_firmware_releases",
            latest,
        ),
    ):
        yield installed, latest, details


def _uplink(time: str, fcnt: int, *, kind: str = "UpUnc", sf: int = 7) -> Packet:
    packet = _packet(
        {
            "time": time,
            "devEUI": "24E124123456789A",
            "type": kind,
            "fCnt": fcnt,
            "spreadFactor": sf,
            "frequency": 868500000,
        }
    )
    assert packet is not None
    return packet


def _network(packets: list[Packet], **changes) -> NetworkStatus:
    status = NetworkStatus(
        gateway_eui="24E124FFFEF8706C",
        forwarder_connected=True,
        pending_packets=0,
        gateways=[
            FleetGateway(
                eui="24E124FFFEF8706C",
                name="Local Gateway",
                connected=True,
                last_seen=datetime(2026, 11, 6, 16, 36, 30),  # noqa: DTZ001
            ),
            FleetGateway(
                eui="24E124FFFE000001", name="Shed", connected=False, last_seen=None
            ),
        ],
        links=[
            ApplicationLink(
                app_id="5", name="cloud_home", host="192.0.2.30", connected=True
            ),
            ApplicationLink(
                app_id="2", name="cloud_milesight", host=None, connected=False
            ),
        ],
        application_count=3,
        codec_count=162,
        profile_count=3,
        codec_library_version="1.5.31",
        packets=packets,
    )
    return replace(status, **changes)


SYSTEM_STATUS = SystemStatus(
    cpu_load=3.0, memory_available=25.2, storage_available=80.25, uptime=3600
)


@pytest.fixture
def status():
    """The network server and the web interface answer."""
    network = AsyncMock(
        return_value=_network([_uplink("2026-11-06T16:44:41+01:00", 1)])
    )
    system = AsyncMock(return_value=SYSTEM_STATUS)
    with (
        patch.object(MilesightGatewayAPI, "async_get_network_status", network),
        patch.object(MilesightGatewayAPI, "async_get_system_status", system),
    ):
        yield network, system


@pytest.fixture
def get_devices(firmware, status):
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


async def _report(hass, payload: str = REPORT) -> None:
    async_fire_mqtt_message(hass, TOPIC, payload)
    await hass.async_block_till_done()


async def test_entities_follow_the_mqtt_payload(hass, entry):
    """One uplink updates every entity of the device it is addressed to."""
    assert hass.states.get(TEMPERATURE) is None
    assert hass.states.get(ONLINE).state == "1"
    assert hass.states.get(OFFLINE).state == "2"

    await _report(hass)

    assert hass.states.get(TEMPERATURE).state == "22.5"
    assert hass.states.get(PEOPLE).state == "2"
    assert hass.states.get(OCCUPANCY).state == STATE_ON
    assert hass.states.get(DOOR).state == STATE_ON
    assert hass.states.get(LAST_SEEN).state != STATE_UNKNOWN


async def test_diagnostic_entities_are_categorized(hass, entry):
    """Identity fields and the radio values of a payload codec are diagnostic."""
    await _report(hass, '{"sn": "6723A1", "rssi": -90}')
    registry = er.async_get(hass)
    for entity_id in ("sensor.vs321_sn", "sensor.vs321_rssi"):
        assert (
            registry.async_get(entity_id).entity_category is EntityCategory.DIAGNOSTIC
        )


@pytest.mark.parametrize("payload", ["not json", "[1, 2]", '{"battery": 90}'])
async def test_unusable_payloads_leave_the_state_alone(hass, entry, payload):
    """Broken JSON, a non-object and a payload without the key change nothing."""
    await _report(hass, '{"temperature": 21, "region_1_enable": "disable"}')
    await _report(hass, payload)

    assert hass.states.get(TEMPERATURE).state == "21"
    assert hass.states.get(DOOR).state == "off"


async def test_entities_follow_the_gateway_device_list(hass, entry, get_devices):
    """A device the gateway stops listing as active becomes unavailable."""
    await _report(hass)
    get_devices.return_value = ([], 0, 3)
    await _refresh(hass, entry)

    for entity_id in (TEMPERATURE, PEOPLE, OCCUPANCY, DOOR, LAST_SEEN):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(OFFLINE).state == "3"

    get_devices.return_value = ([DEVICE], 1, 2)
    await _refresh(hass, entry)

    assert hass.states.get(TEMPERATURE).state == "22.5"


@pytest.mark.parametrize(
    "error", [APIConnectionError("host down"), RuntimeError("unexpected")]
)
async def test_entities_are_unavailable_while_the_gateway_is_down(
    hass, entry, get_devices, error
):
    """Device and gateway entities all go unavailable when a refresh fails."""
    await _report(hass)
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
    await _report(hass)
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
        result = await _tcp_ping("192.0.2.10", 8080)

    assert isinstance(result, float)
    writer.close.assert_called_once()


@pytest.mark.parametrize("error", [OSError("refused"), TimeoutError()])
async def test_tcp_ping_reports_none_when_unreachable(error):
    """A refused or timed-out connection has no round-trip time."""
    with patch(
        "custom_components.milesight_gateway.sensor.asyncio.open_connection",
        AsyncMock(side_effect=error),
    ):
        assert await _tcp_ping("192.0.2.10", 8080) is None


# --- firmware --------------------------------------------------------------


async def test_newer_firmware_is_offered(hass, entry, firmware):
    """The update entity compares the gateway's firmware with Milesight's."""
    state = hass.states.get(FIRMWARE)

    assert state.state == STATE_ON
    assert state.attributes["installed_version"] == "60.0.0.48-r3"
    assert state.attributes["latest_version"] == "60.0.0.50"
    assert state.attributes["release_url"] == RELEASE.release_url
    assert state.attributes["release_summary"] == RELEASE.notes
    assert state.attributes["title"] == RELEASE.title
    # The family is taken from the installed version.
    firmware[1].assert_awaited_once()
    assert firmware[1].await_args.args[1] == "60"


def _gateway_device(hass, entry: MockConfigEntry) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get(entry.runtime_data.gateway_device_id)


async def test_gateway_device_shows_model_and_firmware(hass, entry, firmware):
    """The gateway device carries what the gateway reports, also after upgrades."""
    device = _gateway_device(hass, entry)
    assert (device.model, device.sw_version) == ("UG65", "60.0.0.48-r3")

    firmware[0].return_value = GatewayInfo(model=None, firmware="60.0.0.50")
    async_fire_time_changed(hass, dt_util.utcnow() + FIRMWARE_SCAN_INTERVAL)
    await hass.async_block_till_done()

    device = _gateway_device(hass, entry)
    # A model the gateway stops reporting is kept rather than cleared.
    assert (device.model, device.sw_version) == ("UG65", "60.0.0.50")
    assert hass.states.get(FIRMWARE).state == "off"


async def test_gateway_device_is_updated_even_if_the_download_page_fails(
    hass, mqtt, get_devices, firmware
):
    """The gateway's own report does not depend on Milesight's site."""
    firmware[1].side_effect = APIConnectionError("down")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device = _gateway_device(hass, entry)
    assert (device.model, device.sw_version) == ("UG65", "60.0.0.48-r3")


async def _next_firmware_check(hass) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + FIRMWARE_SCAN_INTERVAL)
    await hass.async_block_till_done()


async def test_gateway_device_shows_the_web_interface_details(hass, entry):
    """Model number, serial number and hardware version come from the web login."""
    device = _gateway_device(hass, entry)

    assert (device.model_id, device.serial_number, device.hw_version) == (
        "ND65-868M-EA",
        "6221D4100788",
        "V1.3",
    )


async def test_a_refused_web_login_is_not_retried(hass, entry, firmware, status):
    """Once the API reports a refusal, neither coordinator asks the web again."""
    firmware[2].reset_mock()
    status[1].reset_mock()

    async def refuse(*_args) -> None:
        entry.runtime_data.coordinator.api.web_login_refused = True
        msg = "refused"
        raise APIAuthError(msg)

    firmware[2].side_effect = refuse
    await _next_firmware_check(hass)
    # The status may have refreshed in the same tick, before the refusal.
    status[1].reset_mock()
    await _next_firmware_check(hass)
    await entry.runtime_data.diagnostics.async_refresh()

    firmware[2].assert_awaited_once()
    status[1].assert_not_awaited()
    # The firmware check itself carries on, and known details are kept.
    assert hass.states.get(FIRMWARE).state == STATE_ON
    assert _gateway_device(hass, entry).serial_number == "6221D4100788"


async def test_an_unreachable_web_interface_is_retried(hass, entry, firmware):
    """A connection failure is not a refusal, so the next check tries again."""
    firmware[2].reset_mock()
    firmware[2].side_effect = APIConnectionError("down")
    await _next_firmware_check(hass)
    firmware[2].side_effect = None
    await _next_firmware_check(hass)

    assert firmware[2].await_count == 2
    assert hass.states.get(FIRMWARE).state == STATE_ON


async def test_a_failed_firmware_check_is_retried_soon(
    hass, mqtt, get_devices, firmware, freezer
):
    """A check that times out at a busy start comes back in minutes, not 12 h."""
    firmware[0].side_effect = APIConnectionError("Timeout talking to gateway")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(FIRMWARE).state == STATE_UNAVAILABLE

    firmware[0].side_effect = None
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(FIRMWARE).state == STATE_ON


@pytest.mark.parametrize(
    ("installed", "expected"), [("60.0.0.50", "off"), ("60.0.0.50-r1", "off")]
)
async def test_current_firmware_is_not_offered(
    hass, mqtt, get_devices, firmware, installed, expected
):
    """The same version, or a revision of it, is up to date."""
    firmware[0].return_value = GatewayInfo(model="UG65", firmware=installed)
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(FIRMWARE).state == expected


async def test_firmware_without_a_listed_release_is_unknown(
    hass, mqtt, get_devices, firmware
):
    """A family the download page does not list leaves the latest unknown."""
    firmware[1].return_value = None
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(FIRMWARE)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["installed_version"] == "60.0.0.48-r3"
    assert state.attributes["release_url"] is None


# --- gateway status --------------------------------------------------------

GW = "sensor.milesight_gateway_"


async def test_gateway_status_entities(hass, entry):
    """Network server and web interface values land on the gateway device."""
    expected = {
        f"{GW}pending_packets": "0",
        f"{GW}applications": "3",
        f"{GW}payload_codecs": "162",
        f"{GW}device_profiles": "3",
        f"{GW}codec_library_version": "1.5.31",
        f"{GW}cpu_load": "3.0",
        f"{GW}memory_available": "25.2",
        f"{GW}storage_available": "80.25",
        "binary_sensor.milesight_gateway_packet_forwarder": "on",
        "binary_sensor.milesight_gateway_mqtt_cloud_home": "on",
        "binary_sensor.milesight_gateway_mqtt_cloud_milesight": "off",
        "binary_sensor.milesight_gateway_radio": "on",
        "binary_sensor.shed_radio": "off",
        "sensor.shed_radio_last_seen": STATE_UNKNOWN,
        "sensor.vs321_spreading_factor": "7",
    }
    assert {eid: hass.states.get(eid).state for eid in expected} == expected
    # The first refresh has nothing to compare with yet.
    assert hass.states.get(f"{GW}uplinks_per_minute").state == STATE_UNKNOWN
    link = hass.states.get("binary_sensor.milesight_gateway_mqtt_cloud_home")
    assert link.attributes["broker"] == "192.0.2.30"
    assert (
        "broker"
        not in hass.states.get(
            "binary_sensor.milesight_gateway_mqtt_cloud_milesight"
        ).attributes
    )
    # The gateway's local time is read in Home Assistant's zone.
    last_seen = dt_util.parse_datetime(hass.states.get(f"{GW}radio_last_seen").state)
    assert last_seen == datetime(
        2026, 11, 6, 16, 36, 30, tzinfo=dt_util.get_default_time_zone()
    )
    assert hass.states.get(f"{GW}last_boot").state != STATE_UNKNOWN
    # A second fleet gateway gets its own device behind the gateway.
    shed = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "24E124FFFE000001"), entry.entry_id
    )
    assert shed.via_device_id == entry.runtime_data.gateway_device_id
    # The frequency is there, but disabled until someone wants it.
    frequency = er.async_get(hass).async_get("sensor.vs321_frequency")
    assert frequency.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_packets_per_minute(hass, entry, status, freezer):
    """New packets since the last refresh are counted per minute."""
    old = _uplink("2026-11-06T16:44:41+01:00", 1)
    status[0].return_value = _network(
        [
            _uplink("2026-11-06T16:49:00+01:00", 3, sf=9),
            _uplink("2026-11-06T16:48:00+01:00", 2),
            _uplink("2026-11-06T16:47:00+01:00", 7, kind="DnUnc"),
            old,
        ]
    )
    freezer.tick(timedelta(minutes=2))
    await entry.runtime_data.diagnostics.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(f"{GW}uplinks_per_minute").state == "1.0"
    assert hass.states.get(f"{GW}downlinks_per_minute").state == "0.5"
    # The newest uplink decides the spreading factor.
    assert hass.states.get("sensor.vs321_spreading_factor").state == "9"


async def test_a_refresh_at_the_same_moment_counts_nothing(hass, entry, status):
    """Without time passing, no rate can be computed."""
    await entry.runtime_data.diagnostics.async_refresh()
    await entry.runtime_data.diagnostics.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(f"{GW}uplinks_per_minute").state in (
        STATE_UNKNOWN,
        "0.0",
    )


async def test_a_failed_status_leaves_the_devices_alone(hass, entry, status):
    """Status entities go unavailable; the devices are not affected."""
    status[0].side_effect = APIConnectionError("down")
    await entry.runtime_data.diagnostics.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(f"{GW}cpu_load").state == STATE_UNAVAILABLE
    assert (
        hass.states.get("binary_sensor.milesight_gateway_packet_forwarder").state
        == STATE_UNAVAILABLE
    )
    assert hass.states.get(LAST_SEEN).state == STATE_UNKNOWN


async def test_an_unreadable_web_interface_keeps_the_network_status(
    hass, entry, status
):
    """Without the web values, the network server values still come through."""
    status[1].side_effect = APIConnectionError("down")
    await entry.runtime_data.diagnostics.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(f"{GW}cpu_load").state == STATE_UNKNOWN
    assert hass.states.get(f"{GW}last_boot").state == STATE_UNKNOWN
    assert hass.states.get(f"{GW}payload_codecs").state == "162"


async def test_a_status_that_was_never_read_creates_only_fixed_entities(
    hass, mqtt, get_devices, status
):
    """Links and fleet gateways need a first status; the rest is there anyway."""
    status[0].side_effect = APIConnectionError("down")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(f"{GW}cpu_load").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.vs321_spreading_factor").state == STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.milesight_gateway_mqtt_cloud_home") is None
    assert hass.states.get("binary_sensor.milesight_gateway_radio") is None

    # The status answers later: links and fleet gateways arrive then.
    status[0].side_effect = None
    await entry.runtime_data.diagnostics.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.milesight_gateway_mqtt_cloud_home").state == (
        "on"
    )
    assert hass.states.get("sensor.shed_radio_last_seen") is not None
    # Unloading after that must not stop the listener a second time.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def _entry_with(hass, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=options, version=1, minor_version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_without_web_data_the_web_interface_is_left_alone(
    hass, mqtt, get_devices, firmware, status
):
    """No web login at all, and no entities that only the web could fill."""
    entry = await _entry_with(hass, {CONF_WEB_DATA: False})
    await entry.runtime_data.diagnostics.async_refresh()
    await hass.async_block_till_done()

    status[1].assert_not_awaited()
    firmware[2].assert_not_awaited()
    for key in ("cpu_load", "memory_available", "storage_available", "last_boot"):
        assert hass.states.get(f"{GW}{key}") is None
    # The network server values do not need the web interface.
    assert hass.states.get(f"{GW}payload_codecs").state == "162"
    # The firmware still comes from the login check, which needs no login.
    assert _gateway_device(hass, entry).sw_version == "60.0.0.48-r3"


async def test_without_the_firmware_check_milesight_is_not_asked(
    hass, mqtt, get_devices, firmware
):
    """No download page, no update entity; the gateway device is still filled."""
    entry = await _entry_with(hass, {CONF_FIRMWARE_CHECK: False})

    firmware[1].assert_not_awaited()
    assert hass.states.get(FIRMWARE) is None
    device = _gateway_device(hass, entry)
    assert (device.model, device.sw_version) == ("UG65", "60.0.0.48-r3")


async def test_diagnostics_help_without_giving_anything_away(hass, entry):
    """The file shows matches and status, but no credential, address or name."""
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["device_list"]["online"] == 1
    assert diagnostics["device_list"]["matched"][0]["model_id"] == "vs321"
    assert diagnostics["status"]["network"]["codec_count"] == 162
    assert diagnostics["status"]["network"]["packets_in_window"] == 1
    assert diagnostics["gateway_device"]["sw_version"] == "60.0.0.48-r3"
    for key in (CONF_PASSWORD, CONF_USERNAME, CONF_SECRET_KEY, CONF_IV):
        assert diagnostics["entry"]["data"][key] == REDACTED
    dumped = json.dumps(diagnostics, default=str)
    for secret in (
        ENTRY_DATA[CONF_GATEWAY_URL],
        "6221D4100788",  # serial number
        "24E124FFFEF8706C",  # gateway EUI
        "192.0.2.30",  # MQTT broker
        "cloud_home",  # application name
        "24E124123456789A",  # device EUI
    ):
        assert secret not in dumped


async def test_only_picked_devices_get_entities(hass, mqtt, get_devices):
    """A device left out of the choice gets nothing; the counts stay whole."""
    await _entry_with(hass, {CONF_DEVICES: ["24E1240000000099"]})

    assert hass.states.get(TEMPERATURE) is None
    assert hass.states.get(ONLINE).state == "1"


def _counter_device(hass, entry: MockConfigEntry) -> dr.DeviceEntry | None:
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "24E124123456789A"), entry.entry_id
    )


async def test_a_device_taken_out_of_the_choice_is_removed(hass, entry):
    """Narrowing the choice removes the device with its entities."""
    assert _counter_device(hass, entry)

    hass.config_entries.async_update_entry(
        entry, options={CONF_DEVICES: ["24E1240000000099"]}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert not _counter_device(hass, entry)
    assert er.async_get(hass).async_get(TEMPERATURE) is None


async def test_an_offline_device_is_not_removed(hass, entry, get_devices):
    """A device the gateway only lists as inactive keeps its history."""
    get_devices.return_value = ([], 0, 3)
    hass.config_entries.async_update_entry(
        entry, options={CONF_DEVICES: ["24E1240000000099"]}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _counter_device(hass, entry)


async def test_switching_a_source_off_removes_its_entities(hass, entry):
    """An option turned off later leaves no orphaned entities behind."""
    registry = er.async_get(hass)
    assert registry.async_get(FIRMWARE) is not None
    assert registry.async_get(f"{GW}cpu_load") is not None

    hass.config_entries.async_update_entry(
        entry, options={CONF_WEB_DATA: False, CONF_FIRMWARE_CHECK: False}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(FIRMWARE) is None
    assert registry.async_get(f"{GW}cpu_load") is None
    assert registry.async_get(f"{GW}payload_codecs") is not None


@pytest.mark.parametrize(
    ("options", "seconds"),
    [
        ({}, 300),
        ({CONF_SCAN_INTERVAL: 120}, 120),
        ({CONF_SCAN_INTERVAL: 120, CONF_STATUS_INTERVAL: 600}, 600),
    ],
)
async def test_the_status_interval(hass, mqtt, get_devices, options, seconds):
    """Its own option wins; older entries share the poll interval."""
    entry = await _entry_with(hass, options)

    assert entry.runtime_data.diagnostics.update_interval == timedelta(seconds=seconds)


@pytest.mark.parametrize("failing", [0, 1])
async def test_a_failed_firmware_check_does_not_block_setup(
    hass, mqtt, get_devices, firmware, failing
):
    """The entry loads; only the firmware entity is unavailable."""
    firmware[failing].side_effect = APIConnectionError("down")
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(FIRMWARE).state == STATE_UNAVAILABLE
    assert hass.states.get(LAST_SEEN).state == STATE_UNKNOWN


async def test_gateway_only_entry_never_tries_the_network_login(
    hass, mqtt, firmware, status
):
    """A gateway-only entry loads via the login check, not the network login.

    Failed network server logins lock the gateway's account, so the mode must
    never reach that login: not at setup and not on a later poll.
    """
    connect = AsyncMock(return_value=True)
    no_devices = AsyncMock(side_effect=AssertionError("must not fetch the device list"))
    with (
        patch.object(MilesightGatewayAPI, "async_connect", connect),
        patch.object(MilesightGatewayAPI, "async_get_devices", no_devices),
        patch.object(MilesightGatewayAPI, "async_get_network_status", no_devices),
        patch(
            "custom_components.milesight_gateway.sensor._tcp_ping",
            AsyncMock(return_value=5.0),
        ),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                **ENTRY_DATA,
                CONF_GATEWAY_ONLY: True,
                CONF_MAC: "24:e1:24:00:00:01",
                CONF_MODEL: "UG63",
            },
            version=1,
            minor_version=2,
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    connect.assert_not_called()

    registry = er.async_get(hass)
    eid = entry.entry_id

    def has(platform: str, key: str) -> bool:
        uid = f"{eid}_{key}"
        return registry.async_get_entity_id(platform, DOMAIN, uid) is not None

    # The gateway itself: reachable, firmware, and the web-interface values.
    assert has("binary_sensor", "online")
    assert has("update", "firmware")
    assert has("sensor", "cpu_load")
    assert has("sensor", "last_boot")
    # Nothing that needs the network server login.
    assert not has("sensor", "online_devices")
    assert not has("sensor", "offline_devices")
    assert not has("binary_sensor", "packet_forwarder")
    assert not has("sensor", "uplinks_per_minute")
    assert _gateway_device(hass, entry).name == "Milesight UG63"
    # The MAC lets DHCP discovery find the gateway again at a new address.
    assert (dr.CONNECTION_NETWORK_MAC, "24:e1:24:00:00:01") in _gateway_device(
        hass, entry
    ).connections


STEP = FirmwareRelease(
    version="60.0.0.45",
    title="UG65&UG67 LoRaWAN® Gateway",
    release_url=None,
    download_url="https://resource.milesight.com/milesight/iot/firmware/60.0.0.45.bin",
    notes="Please reset the gateway if you would like to downgrade to this version.",
)
NEWEST = FirmwareRelease(
    version="60.0.0.51",
    title="UG65&UG67 LoRaWAN® Gateway",
    release_url=None,
    download_url="https://resource.milesight.com/milesight/iot/firmware/60.0.0.51.bin",
    notes="Ensure that your gateway is updated to v60.0.0.45 or later.",
    requires="60.0.0.45",
)


async def _setup_on(hass, firmware, installed: str) -> MockConfigEntry:
    firmware[0].return_value = GatewayInfo(model="UG65", firmware=installed)
    firmware[1].return_value = [STEP, NEWEST]
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_gateway_below_the_minimum_is_offered_the_step_first(
    hass, mqtt, get_devices, firmware
):
    """The newest firmware would fail on it; Milesight names what to install first."""
    await _setup_on(hass, firmware, "60.0.0.44")

    state = hass.states.get(FIRMWARE)
    assert state.state == STATE_ON
    assert state.attributes["latest_version"] == "60.0.0.45"
    assert state.attributes["release_summary"].startswith(
        "Step towards 60.0.0.51: Milesight requires 60.0.0.45 first."
    )


async def test_installing_backs_up_downloads_uploads_and_waits(
    hass, mqtt, get_devices, firmware
):
    """One step per call, the backup before anything is changed."""
    await _setup_on(hass, firmware, "60.0.0.45")
    assert hass.states.get(FIRMWARE).attributes["latest_version"] == "60.0.0.51"
    calls: list[str] = []

    async def backup(_self, _session):
        calls.append("backup")
        return b"backup"

    def save(_folder, url, version, data):
        calls.append(f"save {version} {data!r}")
        return "/config/x.backup"

    async def download(_session, release):
        calls.append(f"download {release.version}")
        return b"image"

    async def upload(_self, _session, image):
        calls.append(f"upload {image!r}")
        # The gateway comes back on the new firmware.
        firmware[0].return_value = GatewayInfo(model="UG65", firmware="60.0.0.51")

    with (
        patch.object(MilesightGatewayAPI, "async_download_backup", backup),
        patch.object(MilesightGatewayAPI, "async_upgrade_firmware", upload),
        patch("custom_components.milesight_gateway.coordinator.save_backup", save),
        patch(
            "custom_components.milesight_gateway.coordinator.async_download_firmware",
            download,
        ),
        patch(
            "custom_components.milesight_gateway.coordinator.asyncio.sleep", AsyncMock()
        ),
    ):
        await hass.services.async_call(
            "update", "install", {"entity_id": FIRMWARE}, blocking=True
        )
        await hass.async_block_till_done()

    assert calls == [
        "backup",
        "save 60.0.0.45 b'backup'",
        "download 60.0.0.51",
        "upload b'image'",
    ]
    state = hass.states.get(FIRMWARE)
    assert state.attributes["installed_version"] == "60.0.0.51"
    assert state.state == "off"


async def test_a_failed_backup_stops_the_installation(
    hass, mqtt, get_devices, firmware
):
    """Without a backup nothing is uploaded."""
    await _setup_on(hass, firmware, "60.0.0.45")
    upload = AsyncMock()

    with (
        patch.object(
            MilesightGatewayAPI,
            "async_download_backup",
            AsyncMock(side_effect=APIConnectionError("no backup")),
        ),
        patch.object(MilesightGatewayAPI, "async_upgrade_firmware", upload),
        pytest.raises(HomeAssistantError, match="the backup failed: no backup"),
    ):
        await hass.services.async_call(
            "update", "install", {"entity_id": FIRMWARE}, blocking=True
        )

    upload.assert_not_awaited()


async def test_the_end_of_life_ug63_is_not_offered_an_installation(
    hass, mqtt, get_devices, firmware
):
    """Milesight publishes nothing for it, and its upload was never tried."""
    await _setup_on(hass, firmware, "63.0.0.2-r2")

    # Only the release notes are left; 1 is the installation.
    assert not hass.states.get(FIRMWARE).attributes["supported_features"] & 1


SHED = "24E124FFFE000001"
SHED_FIRMWARE = "update.shed_firmware"


def _member(version: str | None) -> MemberStatus:
    return MemberStatus(
        system=None,
        firmware=version,
        wifi_enabled=None,
        wifi_ssid=None,
        wifi_access_point=None,
        wireguard=(),
    )


async def test_a_fleet_member_is_offered_its_own_step(
    hass, mqtt, get_devices, firmware, fleet_members
):
    """The hub is past the minimum version, the member is not."""
    fleet_members.return_value = {SHED: _member("60.0.0.44")}
    await _setup_on(hass, firmware, "60.0.0.51")

    assert hass.states.get(FIRMWARE).state == "off"
    state = hass.states.get(SHED_FIRMWARE)
    assert state.state == STATE_ON
    assert state.attributes["installed_version"] == "60.0.0.44"
    assert state.attributes["latest_version"] == "60.0.0.45"
    assert state.attributes["supported_features"]


async def test_a_member_of_another_family_gets_no_offer(
    hass, mqtt, get_devices, firmware, fleet_members
):
    """The releases read for the hub say nothing about another product line."""
    fleet_members.return_value = {SHED: _member("56.0.0.3")}
    await _setup_on(hass, firmware, "60.0.0.51")

    state = hass.states.get(SHED_FIRMWARE)
    assert state.attributes["installed_version"] == "56.0.0.3"
    assert state.attributes["latest_version"] is None


async def test_installing_on_a_member_goes_through_its_own_web_interface(
    hass, mqtt, get_devices, firmware, fleet_members
):
    """Backup and upload at the member, not at the hub."""
    fleet_members.return_value = {SHED: _member("60.0.0.45")}
    await _setup_on(hass, firmware, "60.0.0.51")
    member = MagicMock()
    member.async_download_backup = AsyncMock(return_value=b"backup")
    member.async_get_gateway_info = AsyncMock(
        return_value=GatewayInfo(model="UG65", firmware="60.0.0.51")
    )

    async def upload(_session, _image):
        fleet_members.return_value = {SHED: _member("60.0.0.51")}

    member.async_upgrade_firmware = AsyncMock(side_effect=upload)
    hub_upload = AsyncMock()
    save = MagicMock(return_value="/config/x.backup")

    with (
        patch.object(FleetMembers, "api", return_value=member) as api,
        patch.object(MilesightGatewayAPI, "async_upgrade_firmware", hub_upload),
        patch("custom_components.milesight_gateway.coordinator.save_backup", save),
        patch(
            "custom_components.milesight_gateway.coordinator.async_download_firmware",
            AsyncMock(return_value=b"image"),
        ),
        patch(
            "custom_components.milesight_gateway.coordinator.asyncio.sleep", AsyncMock()
        ),
    ):
        await hass.services.async_call(
            "update", "install", {"entity_id": SHED_FIRMWARE}, blocking=True
        )
        await hass.async_block_till_done()

    api.assert_called_once_with(SHED)
    member.async_download_backup.assert_awaited_once()
    assert save.call_args.args[2:] == ("60.0.0.45", b"backup")
    assert member.async_upgrade_firmware.await_args.args[1] == b"image"
    hub_upload.assert_not_awaited()
    state = hass.states.get(SHED_FIRMWARE)
    assert state.attributes["installed_version"] == "60.0.0.51"
    assert state.state == "off"


async def test_the_dialog_gets_the_notes_in_full(
    hass, mqtt, get_devices, firmware, hass_ws_client
):
    """The state holds 255 characters of them; Milesight writes more."""
    notes = "\n".join(f"{n}. A note about this firmware release." for n in range(1, 12))
    firmware[0].return_value = GatewayInfo(model="UG65", firmware="60.0.0.45")
    firmware[1].return_value = [replace(NEWEST, notes=notes)]
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert len(hass.states.get(FIRMWARE).attributes["release_summary"]) == 255
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "update/release_notes", "entity_id": FIRMWARE}
    )
    assert (await client.receive_json())["result"] == notes


@pytest.mark.parametrize(
    ("failure", "said"),
    [
        (ClientError("Server disconnected"), "Cannot download from Milesight"),
        (ValueError("The download of firmware 60.0.0.51 is incomplete"), "incomplete"),
    ],
)
async def test_a_failed_download_names_milesight_not_the_gateway(
    hass, mqtt, get_devices, firmware, failure, said
):
    """The image comes from Milesight; neither the gateway nor a key is to blame."""
    await _setup_on(hass, firmware, "60.0.0.45")
    upload = AsyncMock()

    with (
        patch.object(
            MilesightGatewayAPI, "async_download_backup", AsyncMock(return_value=b"x")
        ),
        patch(
            "custom_components.milesight_gateway.coordinator.save_backup",
            MagicMock(return_value="/config/x.backup"),
        ),
        patch(
            "custom_components.milesight_gateway.api.download_firmware",
            AsyncMock(side_effect=failure),
        ),
        patch.object(MilesightGatewayAPI, "async_upgrade_firmware", upload),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            "update", "install", {"entity_id": FIRMWARE}, blocking=True
        )

    assert "the download from Milesight failed" in str(raised.value)
    assert said in str(raised.value)
    assert "gateway" not in str(raised.value).split("failed:")[1]
    upload.assert_not_awaited()


async def test_a_failed_upload_says_so(hass, mqtt, get_devices, firmware):
    await _setup_on(hass, firmware, "60.0.0.45")

    with (
        patch.object(
            MilesightGatewayAPI, "async_download_backup", AsyncMock(return_value=b"x")
        ),
        patch(
            "custom_components.milesight_gateway.coordinator.save_backup",
            MagicMock(return_value="/config/x.backup"),
        ),
        patch(
            "custom_components.milesight_gateway.coordinator.async_download_firmware",
            AsyncMock(return_value=b"image"),
        ),
        patch.object(
            MilesightGatewayAPI,
            "async_upgrade_firmware",
            AsyncMock(side_effect=APIConnectionError("Server disconnected")),
        ),
        pytest.raises(HomeAssistantError, match="the upload to the gateway failed"),
    ):
        await hass.services.async_call(
            "update", "install", {"entity_id": FIRMWARE}, blocking=True
        )
