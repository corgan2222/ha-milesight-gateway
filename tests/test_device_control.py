"""
Tests for a gateway with a device list whose values come by MQTT (UG65, UG67).

The device list names each device's model through its payload codec. Values
arrive through the mocked broker under ``<base topic>/<devEUI>``, as the
gateway publishes them. Settings leave as bytes the bundled encoder built, on
the network server's queue endpoint.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_TEMPERATURE,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_mqtt_message,
    mock_restore_cache,
)

from custom_components.milesight_gateway.api import (
    APIAuthError,
    APIConnectionError,
    MilesightDevice,
    MilesightGatewayAPI,
)
from custom_components.milesight_gateway.const import DOMAIN

from .conftest import GATEWAY_TOPICS
from .test_init import ENTRY_DATA

THERMOSTAT_EUI = "24E1247140000001"
BUTTON_EUI = "24E1245350000002"
COUNTER_EUI = "24E1244430000003"
THERMOSTAT_DEVICE = MilesightDevice(THERMOSTAT_EUI, "radiator", "Thermostat", "wt101")
BUTTON_DEVICE = MilesightDevice(BUTTON_EUI, "button1", "Smart Button", "ws101")
COUNTER_DEVICE = MilesightDevice(COUNTER_EUI, "door count", "People Counter", "vs321")
ALL = [THERMOSTAT_DEVICE, BUTTON_DEVICE, COUNTER_DEVICE]

THERMOSTAT = "climate.radiator"


def _topic(eui: str) -> str:
    return f"sensors/lora/uplink/{eui.lower()}"


def _entity(hass, domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


async def _connect(self: MilesightGatewayAPI, _session: object) -> bool:
    self.connected = True
    return True


@pytest.fixture
def gateway_topics(gateway_topics):
    """
    The application names no downlink topic: settings go by the queue.

    A test that wants MQTT sets the full topics and reloads.
    """
    gateway_topics.return_value = [replace(GATEWAY_TOPICS, downlink=None)]
    return gateway_topics


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Let MQTT's own housekeeping timers outlive the test."""
    return True


@pytest.fixture
def devices() -> AsyncMock:
    """The gateway's device list; a test changes it to drop a device."""
    return AsyncMock(return_value=(ALL, 3, 0))


@pytest.fixture
async def entry(hass, mqtt_mock, devices) -> MockConfigEntry:
    """A loaded MQTT entry with three devices; status calls fail."""
    down = AsyncMock(side_effect=APIConnectionError("down"))
    with (
        patch.object(MilesightGatewayAPI, "async_connect", _connect),
        patch.object(MilesightGatewayAPI, "async_get_devices", devices),
        patch.object(MilesightGatewayAPI, "async_get_gateway_info", down),
        patch.object(MilesightGatewayAPI, "async_get_network_status", down),
        patch(
            "custom_components.milesight_gateway.sensor._tcp_ping",
            AsyncMock(return_value=12.3),
        ),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry


async def _publish(hass, eui: str, payload: str) -> None:
    async_fire_mqtt_message(hass, _topic(eui), payload)
    await hass.async_block_till_done()


# --- values ------------------------------------------------------------------


async def test_the_thermostat_follows_the_payload(hass, entry):
    """Catalog fields arrive by MQTT and feed the thermostat."""
    await _publish(
        hass, THERMOSTAT_EUI, '{"temperature": 21.5, "target_temperature": 22}'
    )

    state = hass.states.get(THERMOSTAT)
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 21.5
    assert state.attributes[ATTR_TEMPERATURE] == 22


async def test_radio_fields_of_an_enriched_codec_get_sensors(hass, entry):
    """RSSI and SNR from the payload codec become diagnostic sensors."""
    await _publish(hass, THERMOSTAT_EUI, '{"rssi": -97, "snr": 7.5, "battery": 80}')
    # Codecs that send both names still get one SNR entity, the older loRaSNR.
    await _publish(hass, BUTTON_EUI, '{"loRaSNR": 9, "snr": 9}')

    rssi = _entity(hass, "sensor", f"{THERMOSTAT_EUI}_rssi")
    assert hass.states.get(rssi).state == "-97"
    for eui, snr in ((THERMOSTAT_EUI, "7.5"), (BUTTON_EUI, "9")):
        assert hass.states.get(_entity(hass, "sensor", f"{eui}_loRaSNR")).state == snr
        assert _entity(hass, "sensor", f"{eui}_snr") is None


async def test_a_press_fires_an_event_also_by_name(hass, entry):
    """The press type maps to the old event types; a codec may send the name."""
    await _publish(hass, BUTTON_EUI, '{"button_event": {"status": 1}}')
    button = _entity(hass, "event", f"{BUTTON_EUI}_button_event")
    assert hass.states.get(button).attributes["event_type"] == "single_press"

    await _publish(hass, BUTTON_EUI, '{"button_event": {"status": "double press"}}')
    assert hass.states.get(button).attributes["event_type"] == "double_press"


async def test_a_people_count_also_tells_occupancy(hass, entry):
    """From one person on, the occupancy sensor is on."""
    await _publish(hass, COUNTER_EUI, '{"people_total_counts": 2}')
    occupancy = _entity(hass, "binary_sensor", f"{COUNTER_EUI}_people_total_counts")
    count = _entity(hass, "sensor", f"{COUNTER_EUI}_people_total_counts")
    assert hass.states.get(occupancy).state == "on"
    assert hass.states.get(count).state == "2"

    await _publish(hass, COUNTER_EUI, '{"people_total_counts": 0}')
    assert hass.states.get(occupancy).state == "off"


async def test_raw_bytes_without_a_gateway_codec_are_decoded(hass, entry):
    """Without a payload codec the gateway sends bytes; the bundled codec reads them."""
    # A VS321 report: battery 89 %, 24.3 °C, 27 % humidity, nobody counted.
    raw = base64.b64encode(bytes.fromhex("0175590367f30007ff0008f4020004683605fd0000"))
    await _publish(hass, COUNTER_EUI, json.dumps({"data": raw.decode()}))

    battery = _entity(hass, "sensor", f"{COUNTER_EUI}_battery")
    assert hass.states.get(battery).state == "89"


async def test_an_unlisted_device_on_the_topic_is_ignored(hass, entry):
    """Only the gateway's device list makes devices."""
    await _publish(hass, "24E1245350000009", '{"battery": 50}')

    assert _entity(hass, "sensor", "24E1245350000009_battery") is None


async def test_a_dropped_device_becomes_unavailable(hass, entry, devices):
    """A device the list leaves out keeps its entities, unavailable."""
    await _publish(hass, BUTTON_EUI, '{"battery": 90}')
    battery = _entity(hass, "sensor", f"{BUTTON_EUI}_battery")
    assert hass.states.get(battery).state == "90"

    devices.return_value = ([THERMOSTAT_DEVICE, COUNTER_DEVICE], 2, 1)
    with patch.object(MilesightGatewayAPI, "async_get_devices", devices):
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get(battery).state == STATE_UNAVAILABLE
    assert hass.states.get(THERMOSTAT).state != STATE_UNAVAILABLE


async def test_an_unreachable_gateway_makes_the_devices_unavailable(
    hass, entry, devices
):
    """Without the device list nothing is known to be active."""
    devices.side_effect = APIConnectionError("down")
    with patch.object(MilesightGatewayAPI, "async_get_devices", devices):
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get(THERMOSTAT).state == STATE_UNAVAILABLE


# --- settings ----------------------------------------------------------------


async def test_setting_the_target_queues_the_encoded_bytes(hass, entry):
    """The bundled encoder builds the downlink, so any gateway codec works."""
    device = entry.runtime_data.mqtt.coordinators[THERMOSTAT_EUI.lower()].data
    await hass.async_add_executor_job(device.encode, {"report_interval": 10})
    downlink = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: THERMOSTAT, ATTR_TEMPERATURE: 23.4},
            blocking=True,
        )

    _session, eui, data = downlink.await_args.args
    assert eui == THERMOSTAT_EUI
    assert data == device.encode(
        {
            "target_temperature": 23,
            "temperature_tolerance": 1.0,
            "temperature_control.enable": 1,
            "temperature_control.mode": 0,
        }
    )
    # The application reports ACKs, so the downlink is confirmed and named.
    assert downlink.await_args.kwargs["fport"] == 85
    assert downlink.await_args.kwargs["reference"].startswith("ha-")
    assert hass.states.get(THERMOSTAT).attributes[ATTR_TEMPERATURE] == 23


@pytest.mark.parametrize("error", [APIConnectionError("down"), APIAuthError("401")])
async def test_a_failed_downlink_is_a_readable_error(hass, entry, error):
    """The user sees why the setting did not go out."""
    interval = _entity(hass, "number", f"{THERMOSTAT_EUI}_report_interval")
    downlink = AsyncMock(side_effect=error)
    with (
        patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call(
            "number",
            "set_value",
            {ATTR_ENTITY_ID: interval, "value": 5},
            blocking=True,
        )

    assert err.value.translation_key == "downlink_failed"
    assert hass.states.get(interval).state == STATE_UNKNOWN


async def test_mqtt_downlinks_use_the_gateways_topics(
    hass, entry, mqtt_mock, gateway_topics
):
    """With a downlink topic, settings go out on MQTT, not on the queue."""
    gateway_topics.return_value = [GATEWAY_TOPICS]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    rest = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", rest):
        await hass.services.async_call(
            "number",
            "set_value",
            {
                ATTR_ENTITY_ID: _entity(
                    hass, "number", f"{THERMOSTAT_EUI}_report_interval"
                ),
                "value": 1,
            },
            blocking=True,
        )

    rest.assert_not_awaited()
    [call] = [
        c for c in mqtt_mock.async_publish.call_args_list if "/downlink/" in c.args[0]
    ]
    assert call.args[0] == f"sensors/lora/downlink/{THERMOSTAT_EUI.lower()}"
    assert json.loads(call.args[1])["confirmed"] is True
    gateway_topics.assert_awaited()


async def test_an_error_report_fails_the_command(
    hass, entry, mqtt_mock, gateway_topics
):
    """The gateway's error topic ends a pending command with its reason."""
    gateway_topics.return_value = [GATEWAY_TOPICS]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    events = async_capture_events(hass, "milesight_gateway_join")
    await hass.services.async_call(
        "number",
        "set_value",
        {
            ATTR_ENTITY_ID: _entity(
                hass, "number", f"{THERMOSTAT_EUI}_report_interval"
            ),
            "value": 1,
        },
        blocking=True,
    )
    eui = THERMOSTAT_EUI.lower()
    async_fire_mqtt_message(
        hass, f"sensors/lora/error/{eui}", json.dumps({"error": "payload too large"})
    )
    async_fire_mqtt_message(hass, f"sensors/lora/join/{eui}", json.dumps({}))
    await hass.async_block_till_done()

    status = hass.states.get(
        _entity(hass, "sensor", f"{THERMOSTAT_EUI}_command_status")
    )
    assert status.state == "failed"
    assert status.attributes["reported"] == {"error": "payload too large"}
    assert [e.data["dev_eui"] for e in events] == [THERMOSTAT_EUI]


async def test_the_sync_removes_what_the_gateway_deleted(hass, entry):
    """A device the gateway no longer knows goes; an offline one stays."""
    registry = dr.async_get(hass)
    assert (
        registry.async_get_device_by_identifier((DOMAIN, BUTTON_EUI), entry.entry_id)
        is not None
    )
    known = AsyncMock(return_value={THERMOSTAT_EUI, COUNTER_EUI})
    button = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_sync_devices"
    )
    with patch.object(MilesightGatewayAPI, "async_get_device_euis", known):
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: button}, blocking=True
        )

    assert (
        registry.async_get_device_by_identifier((DOMAIN, BUTTON_EUI), entry.entry_id)
        is None
    )
    # Gone from the device page too.
    assert BUTTON_EUI.lower() not in entry.runtime_data.mqtt.coordinators
    assert registry.async_get_device_by_identifier(
        (DOMAIN, THERMOSTAT_EUI), entry.entry_id
    )
    # The gateway itself stays.
    assert registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )


async def test_an_empty_device_list_removes_nothing(hass, entry):
    """No device at all is more likely a fault: refuse."""
    button = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_sync_devices"
    )
    with (
        patch.object(
            MilesightGatewayAPI, "async_get_device_euis", AsyncMock(return_value=set())
        ),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: button}, blocking=True
        )

    assert dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, BUTTON_EUI), entry.entry_id
    )


async def test_a_name_given_on_the_gateway_reaches_the_device(hass, entry, devices):
    """Renamed on the gateway, renamed here; a name set in Home Assistant stays."""
    registry = dr.async_get(hass)
    renamed = MilesightDevice(THERMOSTAT_EUI, "nursery", "Thermostat", "wt101")
    devices.return_value = ([renamed, BUTTON_DEVICE, COUNTER_DEVICE], 3, 0)
    button = registry.async_get_device_by_identifier(
        (DOMAIN, BUTTON_EUI), entry.entry_id
    )
    registry.async_update_device(button.id, name_by_user="my button")
    devices.return_value[0].append(
        MilesightDevice(BUTTON_EUI, "button renamed", "Smart Button", "ws101")
    )
    devices.return_value[0].remove(BUTTON_DEVICE)
    with patch.object(MilesightGatewayAPI, "async_get_devices", devices):
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

    thermostat = registry.async_get_device_by_identifier(
        (DOMAIN, THERMOSTAT_EUI), entry.entry_id
    )
    assert thermostat.name == "nursery"
    button = registry.async_get_device_by_identifier(
        (DOMAIN, BUTTON_EUI), entry.entry_id
    )
    assert button.name_by_user == "my button"


async def test_topics_the_gateway_names_late_are_taken_then(
    hass, entry, mqtt_mock, gateway_topics
):
    """A gateway too busy at setup is asked again; its answer is kept for next time."""
    gateway_topics.side_effect = [APIConnectionError("busy"), [GATEWAY_TOPICS]]
    with patch("custom_components.milesight_gateway.TOPICS_RETRY", 0):
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data.mqtt.path == "mqtt+rest"
        await hass.async_block_till_done(wait_background_tasks=True)
    runtime = entry.runtime_data.mqtt
    assert runtime.path == "mqtt"
    device = runtime.coordinators[THERMOSTAT_EUI.lower()].data
    assert device.confirms is True

    await hass.services.async_call(
        "number",
        "set_value",
        {
            ATTR_ENTITY_ID: _entity(
                hass, "number", f"{THERMOSTAT_EUI}_report_interval"
            ),
            "value": 1,
        },
        blocking=True,
    )
    assert any(
        "/downlink/" in call.args[0] for call in mqtt_mock.async_publish.call_args_list
    )

    # The next start finds the gateway busy again: the stored topics serve.
    gateway_topics.side_effect = APIConnectionError("busy")
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data.mqtt.path == "mqtt"


async def test_a_listed_device_that_stopped_reporting_is_unavailable(
    hass, entry, devices
):
    """Offline on the gateway means unavailable here, with its entities kept."""
    await _publish(hass, BUTTON_EUI, '{"battery": 90}')
    battery = _entity(hass, "sensor", f"{BUTTON_EUI}_battery")
    silent = replace(BUTTON_DEVICE, online=False)
    devices.return_value = ([THERMOSTAT_DEVICE, silent, COUNTER_DEVICE], 2, 1)
    with patch.object(MilesightGatewayAPI, "async_get_devices", devices):
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

    assert hass.states.get(battery).state == STATE_UNAVAILABLE
    assert hass.states.get(THERMOSTAT).state != STATE_UNAVAILABLE

    # Its uplink still arrives, say through another gateway on the same
    # topic: the value counts, and the next look at the list keeps it.
    await _publish(hass, BUTTON_EUI, '{"battery": 89}')
    assert hass.states.get(battery).state == "89"
    with patch.object(MilesightGatewayAPI, "async_get_devices", devices):
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()
    assert hass.states.get(battery).state == "89"


WORKPLACE_EUI = "24E1246000000004"
WORKPLACE_DEVICE = MilesightDevice(WORKPLACE_EUI, "desk", "Workplace Sensor", "vs121")


@pytest.fixture
def workplace(devices) -> AsyncMock:
    """The gateway lists one workplace counter."""
    devices.return_value = ([WORKPLACE_DEVICE], 1, 0)
    return devices


async def test_a_workplace_counter_starts_with_its_counts_only(hass, workplace, entry):
    """Regions and line crossings exist only where someone drew them."""
    await _publish(
        hass,
        WORKPLACE_EUI,
        '{"people_count_all": 1, "people_count_max": 3, "region_count": 0}',
    )
    registry = er.async_get(hass)

    def disabled(key: str) -> bool:
        entity_id = _entity(hass, "sensor", f"{WORKPLACE_EUI}_{key}")
        assert entity_id is not None, key
        return registry.async_get(entity_id).disabled

    assert not disabled("people_count_all")
    assert not disabled("people_count_max")
    for key in ("region_count", "region_1", "region_16_count", "a_to_d"):
        assert disabled(key), key
    occupancy = _entity(hass, "binary_sensor", f"{WORKPLACE_EUI}_people_count_all")
    assert hass.states.get(occupancy).state == "on"

    await _publish(hass, WORKPLACE_EUI, '{"people_count_all": 0}')
    assert hass.states.get(occupancy).state == "off"


async def test_region_readings_of_an_older_version_are_disabled_once(
    hass, workplace, entry
):
    """Made enabled back then: the ones without a value go, the used one stays."""
    registry = er.async_get(hass)
    empty = _entity(hass, "sensor", f"{WORKPLACE_EUI}_region_1_count")
    used = _entity(hass, "sensor", f"{WORKPLACE_EUI}_a_to_b")
    assert await hass.config_entries.async_unload(entry.entry_id)
    # As an older version left them.
    registry.async_update_entity(empty, disabled_by=None)
    registry.async_update_entity(used, disabled_by=None)
    mock_restore_cache(hass, [State(used, "3")])

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(empty).disabled
    assert not registry.async_get(used).disabled
    assert hass.states.get(used).state == "3"
