"""Tests for the device control platforms: event, climate and number.

Presses and reports arrive through the mocked MQTT broker, as the gateway
would publish them. Settings leave as downlinks on the gateway's queue
endpoint, which the gateway encodes with the device's codec.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_TEMPERATURE,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
)
from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.components.number import (
    ATTR_VALUE,
    SERVICE_SET_VALUE,
)
from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.milesight_gateway.api import (
    APIAuthError,
    APIConnectionError,
    MilesightGatewayAPI,
    _build_device,
)
from custom_components.milesight_gateway.const import DOMAIN

from .test_init import ENTRY_DATA

WT_TOPIC = "sensors/lora/uplink/24e1247140000001"
WS_TOPIC = "sensors/lora/uplink/24e1245350000002"

THERMOSTAT_DEVICE = _build_device(
    {"devEUI": "24E1247140000001", "name": "radiator"},
    {
        "id": "wt101",
        "name": "WT101",
        "description": "Wireless Thermostat",
        "device_entities": [
            "battery",
            "temperature",
            "target_temperature",
            "valve_opening",
            "window_detection",
            "tamper_status",
        ],
    },
    "sensors/lora/uplink",
)
BUTTON_DEVICE = _build_device(
    {"devEUI": "24E1245350000002", "name": "button1"},
    {
        "id": "ws101",
        "name": "WS101",
        "description": "Smart Button",
        "device_entities": ["battery", "button_event"],
    },
    "sensors/lora/uplink",
)

THERMOSTAT = "climate.radiator"
INTERVAL = "number.radiator_reporting_interval"
VALVE = "sensor.radiator_valve_opening"
WINDOW = "binary_sensor.radiator_window_open"
BUTTON = "event.button1_button"


async def _connect(self: MilesightGatewayAPI, _session: object) -> bool:
    self.connected = True
    return True


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Let MQTT's own housekeeping timers outlive the test."""
    return True


@pytest.fixture
async def entry(hass, mqtt_mock) -> MockConfigEntry:
    """A loaded entry with the thermostat and the button; status calls fail."""
    down = AsyncMock(side_effect=APIConnectionError("down"))
    with (
        patch.object(MilesightGatewayAPI, "async_connect", _connect),
        patch.object(
            MilesightGatewayAPI,
            "async_get_devices",
            AsyncMock(return_value=([THERMOSTAT_DEVICE, BUTTON_DEVICE], 2, 0)),
        ),
        patch.object(MilesightGatewayAPI, "async_get_gateway_info", down),
        patch.object(MilesightGatewayAPI, "async_get_network_status", down),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, data=ENTRY_DATA, version=1, minor_version=2
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry


# --- event -------------------------------------------------------------------


async def test_a_press_fires_an_event(hass, entry):
    """The gateway wraps the press in an object; the number maps to the type."""
    assert hass.states.get(BUTTON).state == STATE_UNKNOWN

    async_fire_mqtt_message(
        hass, WS_TOPIC, '{"button_event": {"msgid": 225828, "status": 1}}'
    )
    await hass.async_block_till_done()
    assert hass.states.get(BUTTON).attributes["event_type"] == "single_press"

    # A codec without the wrapper sends the press number directly.
    async_fire_mqtt_message(hass, WS_TOPIC, '{"button_event": 3}')
    await hass.async_block_till_done()
    assert hass.states.get(BUTTON).attributes["event_type"] == "double_press"


async def test_a_retained_press_is_history(hass, entry):
    """A retained message is an old press and must not fire again."""
    async_fire_mqtt_message(
        hass, WS_TOPIC, '{"button_event": {"msgid": 1, "status": 2}}', retain=True
    )
    await hass.async_block_till_done()

    assert hass.states.get(BUTTON).state == STATE_UNKNOWN


@pytest.mark.parametrize(
    "payload",
    [
        '{"button_event": 9}',
        '{"button_event": "x"}',
        '{"button_event": {"msgid": 1}}',
        '{"a": 1}',
    ],
)
async def test_unknown_press_values_are_ignored(hass, entry, payload):
    """A value outside the press map changes nothing."""
    async_fire_mqtt_message(hass, WS_TOPIC, payload)
    await hass.async_block_till_done()

    assert hass.states.get(BUTTON).state == STATE_UNKNOWN


# --- climate -----------------------------------------------------------------


async def test_the_thermostat_follows_the_payload(hass, entry):
    """Temperatures and the valve arrive by MQTT, also as strings."""
    async_fire_mqtt_message(
        hass,
        WT_TOPIC,
        '{"temperature": "21.5", "target_temperature": 22, "valve_opening": 0}',
    )
    await hass.async_block_till_done()

    state = hass.states.get(THERMOSTAT)
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 21.5
    assert state.attributes[ATTR_TEMPERATURE] == 22.0
    assert state.attributes["hvac_action"] == HVACAction.IDLE
    assert state.attributes["valve_opening"] == 0

    async_fire_mqtt_message(hass, WT_TOPIC, '{"valve_opening": 80}')
    await hass.async_block_till_done()

    state = hass.states.get(THERMOSTAT)
    assert state.attributes["hvac_action"] == HVACAction.HEATING
    assert state.attributes["valve_opening"] == 80
    # A partial payload leaves the other values alone.
    assert state.attributes[ATTR_TEMPERATURE] == 22.0
    # The valve is also a sensor, and the window state a binary sensor.
    assert hass.states.get(VALVE).state == "80"
    async_fire_mqtt_message(hass, WT_TOPIC, '{"window_detection": 1}')
    await hass.async_block_till_done()
    assert hass.states.get(WINDOW).state == "on"


async def test_setting_the_target_queues_a_downlink(hass, entry):
    """The new target goes out as a downlink and shows right away."""
    downlink = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: THERMOSTAT, ATTR_TEMPERATURE: 23.4},
            blocking=True,
        )

    assert downlink.await_args.args[1] == "24E1247140000001"
    assert downlink.await_args.args[2] == {
        "target_temperature": 23,
        "temperature_tolerance": 1.0,
    }
    assert hass.states.get(THERMOSTAT).attributes[ATTR_TEMPERATURE] == 23.0


async def test_the_reported_tolerance_is_sent_along(hass, entry):
    """A tolerance the device reported replaces the default."""
    async_fire_mqtt_message(hass, WT_TOPIC, '{"temperature_tolerance": 0.5}')
    await hass.async_block_till_done()

    downlink = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: THERMOSTAT, ATTR_TEMPERATURE: 20},
            blocking=True,
        )

    assert downlink.await_args.args[2]["temperature_tolerance"] == 0.5


# --- number ------------------------------------------------------------------


async def test_the_setting_follows_the_status_report(hass, entry):
    """The reporting interval stays unknown until the device reports it."""
    assert hass.states.get(INTERVAL).state == STATE_UNKNOWN

    async_fire_mqtt_message(hass, WT_TOPIC, '{"report_interval": 10}')
    await hass.async_block_till_done()

    assert hass.states.get(INTERVAL).state == "10.0"


async def test_an_unreadable_setting_is_logged_and_skipped(hass, entry, caplog):
    """A report the device garbles leaves the state alone."""
    async_fire_mqtt_message(hass, WT_TOPIC, '{"report_interval": "soon"}')
    await hass.async_block_till_done()

    assert hass.states.get(INTERVAL).state == STATE_UNKNOWN
    assert "Unreadable setting report_interval" in caplog.text


async def test_changing_the_setting_queues_a_downlink(hass, entry):
    """The new interval goes out as a downlink and shows right away."""
    downlink = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: INTERVAL, ATTR_VALUE: 30},
            blocking=True,
        )

    assert downlink.await_args.args[1] == "24E1247140000001"
    assert downlink.await_args.args[2] == {"report_interval": 30}
    assert hass.states.get(INTERVAL).state == "30.0"


# --- switch and select -------------------------------------------------------

CHILD_LOCK = "switch.radiator_child_lock"
WINDOW_DETECTION = "switch.radiator_open_window_detection"
WINDOW_THRESHOLD = "number.radiator_window_detection_threshold"
VALVE_ALGORITHM = "select.radiator_valve_control_algorithm"
STATUS_BUTTON = "button.radiator_request_status_report"


async def test_the_switch_follows_the_status_report(hass, entry):
    """The nested child lock value arrives in a status report."""
    assert hass.states.get(CHILD_LOCK).state == STATE_UNKNOWN

    async_fire_mqtt_message(hass, WT_TOPIC, '{"child_lock_config": {"enable": 1}}')
    await hass.async_block_till_done()

    assert hass.states.get(CHILD_LOCK).state == "on"


async def test_a_flat_value_where_an_object_belongs_is_ignored(hass, entry):
    """A report with the wrong shape does not set the nested setting."""
    async_fire_mqtt_message(hass, WT_TOPIC, '{"child_lock_config": 1}')
    await hass.async_block_till_done()

    assert hass.states.get(CHILD_LOCK).state == STATE_UNKNOWN


async def test_switching_queues_a_nested_downlink(hass, entry):
    """Turning the child lock off nests the command like the codec wants."""
    downlink = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {ATTR_ENTITY_ID: CHILD_LOCK},
            blocking=True,
        )

    assert downlink.await_args.args[2] == {"child_lock_config": {"enable": 0}}
    assert hass.states.get(CHILD_LOCK).state == "off"


async def test_a_coupled_switch_keeps_the_reported_siblings(hass, entry):
    """Enabling window detection reuses the thresholds the device reported."""
    async_fire_mqtt_message(
        hass,
        WT_TOPIC,
        '{"open_window_detection":'
        ' {"enable": 0, "temperature_threshold": 3, "time": 25}}',
    )
    await hass.async_block_till_done()
    assert hass.states.get(WINDOW_DETECTION).state == "off"

    downlink = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {ATTR_ENTITY_ID: WINDOW_DETECTION},
            blocking=True,
        )

    assert downlink.await_args.args[2] == {
        "open_window_detection": {
            "enable": 1,
            "temperature_threshold": 3,
            "time": 25,
        }
    }


async def test_a_coupled_number_falls_back_to_the_defaults(hass, entry):
    """Without a report, the sibling fields come from the defaults."""
    downlink = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: WINDOW_THRESHOLD, ATTR_VALUE: 4},
            blocking=True,
        )

    assert downlink.await_args.args[2] == {
        "open_window_detection": {"enable": 1, "time": 10, "temperature_threshold": 4}
    }


async def test_the_select_follows_the_status_report(hass, entry):
    """The algorithm value maps to its option label."""
    assert hass.states.get(VALVE_ALGORITHM).state == STATE_UNKNOWN

    async_fire_mqtt_message(hass, WT_TOPIC, '{"valve_control_algorithm": 1}')
    await hass.async_block_till_done()

    assert hass.states.get(VALVE_ALGORITHM).state == "pid"


async def test_selecting_queues_a_downlink(hass, entry):
    """Choosing an option sends its codec value."""
    downlink = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink):
        await hass.services.async_call(
            "select",
            "select_option",
            {ATTR_ENTITY_ID: VALVE_ALGORITHM, "option": "rate"},
            blocking=True,
        )

    assert downlink.await_args.args[2] == {"valve_control_algorithm": 0}
    assert hass.states.get(VALVE_ALGORITHM).state == "rate"


async def test_a_command_button_queues_its_downlink(hass, entry):
    """Pressing the status button asks the device to report."""
    downlink = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink):
        await hass.services.async_call(
            "button",
            "press",
            {ATTR_ENTITY_ID: STATUS_BUTTON},
            blocking=True,
        )

    assert downlink.await_args.args[2] == {"report_status": 1}


async def test_the_hvac_mode_follows_the_control_state(hass, entry):
    """Enable and mode combine into off, auto and heat."""
    assert hass.states.get(THERMOSTAT).state == STATE_UNKNOWN

    async_fire_mqtt_message(
        hass, WT_TOPIC, '{"temperature_control": {"enable": 1, "mode": 0}}'
    )
    await hass.async_block_till_done()
    assert hass.states.get(THERMOSTAT).state == "auto"

    async_fire_mqtt_message(hass, WT_TOPIC, '{"temperature_control": {"enable": 0}}')
    await hass.async_block_till_done()
    assert hass.states.get(THERMOSTAT).state == "off"


@pytest.mark.parametrize("error", [APIConnectionError("down"), APIAuthError("401")])
async def test_a_failed_downlink_is_a_readable_error(hass, entry, error):
    """The user sees why the command failed, and the card keeps the old state."""
    async_fire_mqtt_message(
        hass, WT_TOPIC, '{"temperature_control": {"enable": 1, "mode": 0}}'
    )
    await hass.async_block_till_done()

    downlink = AsyncMock(side_effect=error)
    with (
        patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_hvac_mode",
            {ATTR_ENTITY_ID: THERMOSTAT, "hvac_mode": "off"},
            blocking=True,
        )

    assert err.value.translation_key == "downlink_failed"
    assert hass.states.get(THERMOSTAT).state == "auto"


async def test_switching_the_thermostat_off(hass, entry):
    """Off disables the device's temperature control."""
    downlink = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_hvac_mode",
            {ATTR_ENTITY_ID: THERMOSTAT, "hvac_mode": "off"},
            blocking=True,
        )

    assert downlink.await_args.args[2] == {"temperature_control": {"enable": 0}}
    state = hass.states.get(THERMOSTAT)
    assert state.state == "off"
    assert state.attributes["hvac_action"] == HVACAction.OFF


async def test_an_unknown_select_value_is_ignored(hass, entry):
    """A value outside the known choices leaves the option alone."""
    async_fire_mqtt_message(hass, WT_TOPIC, '{"valve_control_algorithm": 7}')
    await hass.async_block_till_done()

    assert hass.states.get(VALVE_ALGORITHM).state == STATE_UNKNOWN


async def test_setting_the_hvac_mode_queues_a_downlink(hass, entry):
    """Heat means control on in manual mode."""
    downlink = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", downlink):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_hvac_mode",
            {ATTR_ENTITY_ID: THERMOSTAT, "hvac_mode": "heat"},
            blocking=True,
        )

    assert downlink.await_args.args[2] == {
        "temperature_control": {"enable": 1, "mode": 1}
    }
    assert hass.states.get(THERMOSTAT).state == "heat"


# --- the queue call ----------------------------------------------------------


def _api() -> MilesightGatewayAPI:
    return MilesightGatewayAPI(
        gateway_url="http://192.0.2.11",
        port=8080,
        username="admin",
        password="secret",  # noqa: S106
        secret_key="1111111111111111",  # noqa: S106
        iv="2222222222222222",
        mqtt_base_topic="sensors/lora/uplink",
    )


async def test_the_queue_body_is_flat(hass, aioclient_mock):
    """The gateway wants a flat body; the command travels as a JSON string."""
    url = "http://192.0.2.11:8080/api/devices/24E1247140000001/queue"
    aioclient_mock.post(url, json={})

    await _api().async_enqueue_downlink(
        async_get_clientsession(hass),
        "24E1247140000001",
        {"target_temperature": 21, "temperature_tolerance": 1.0},
    )

    body = aioclient_mock.mock_calls[0][2]
    assert body["devEUI"] == "24E1247140000001"
    assert body["fPort"] == 85
    assert body["confirmed"] is False
    assert json.loads(body["jsonObject"]) == {
        "target_temperature": 21,
        "temperature_tolerance": 1.0,
    }


async def test_an_expired_token_is_renewed_for_the_downlink(hass):
    """One auth failure triggers one fresh login, then the retry."""
    api = _api()
    post = AsyncMock(side_effect=[APIAuthError("401"), None])
    connect = AsyncMock()
    with (
        patch.object(api, "_async_post_downlink", post),
        patch.object(api, "async_connect", connect),
    ):
        await api.async_enqueue_downlink(
            async_get_clientsession(hass), "24E1247140000001", {"report_status": 1}
        )

    connect.assert_awaited_once()
    assert post.await_count == 2
