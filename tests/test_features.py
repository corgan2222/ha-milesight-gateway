"""
Tests for a gateway reached only by MQTT, and what the integration derives.

The gateway's application takes downlinks on its downlink topic and reports
the ACK of a confirmed one on its ACK topic, repeating the reference sent
along. The devices' uplinks feed the command status, the link statistics,
the battery forecast and the pulse meters.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_mqtt_message,
)

from custom_components.milesight_gateway.api import (
    APIConnectionError,
    MilesightGatewayAPI,
)
from custom_components.milesight_gateway.battery import BatteryLog, days_left
from custom_components.milesight_gateway.commands import EVENT_COMMAND
from custom_components.milesight_gateway.const import (
    CONF_MQTT_ACK_TOPIC,
    CONF_MQTT_DOWNLINK_TOPIC,
    CONF_MQTT_REQUEST_TOPIC,
    CONF_MQTT_RESPONSE_TOPIC,
    CONF_MQTT_TOPIC,
    DOMAIN,
)
from custom_components.milesight_gateway.link import LinkStats, link_margin, quality
from custom_components.milesight_gateway.meters import meter_value
from custom_components.milesight_gateway.mqtt_discovery import (
    downlink_error,
    sibling_topic,
)
from custom_components.milesight_gateway.nsapi import MqttNsApi, NsApiError

from .test_mqtt_discovery import ENTRY_DATA

# A VS340: it has a report interval to set.
EUI = "24e124787d000001"
# A UC1114 with two pulse counters.
COUNTER_EUI = "24e1241160000001"
UPLINK = "sensors/lora/uplink/{dev_eui}"
DOWNLINK = "sensors/lora/downlink/{dev_eui}"
ACK = "sensors/lora/ack/{dev_eui}"


def _id(hass, platform: str, eui: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{eui.upper()}_{key}"
    )


@pytest.fixture
def expected_lingering_timers() -> bool:
    """MQTT's own housekeeping timers outlive the test."""
    return True


@pytest.fixture
def unreachable():
    """The gateway answers nothing at all."""
    down = AsyncMock(side_effect=APIConnectionError("down"))
    with (
        patch.object(MilesightGatewayAPI, "async_get_gateway_info", down),
        patch.object(MilesightGatewayAPI, "async_get_gateway_details", down),
        patch.object(MilesightGatewayAPI, "async_get_system_status", down),
        patch(
            "custom_components.milesight_gateway.sensor._tcp_ping",
            AsyncMock(return_value=None),
        ),
    ):
        yield


async def _entry(hass, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options={CONF_MQTT_TOPIC: UPLINK, **options},
        version=1,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _uplink(hass, eui: str, payload: dict) -> None:
    async_fire_mqtt_message(hass, UPLINK.format(dev_eui=eui), json.dumps(payload))
    await hass.async_block_till_done()


def _downlinks(mqtt_mock) -> list[tuple[str, dict]]:
    return [
        (c.args[0], json.loads(c.args[1]))
        for c in mqtt_mock.async_publish.call_args_list
        if "/downlink/" in c.args[0]
    ]


async def _set_interval(hass, value: int) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: _id(hass, "number", EUI, "report_interval"), "value": value},
        blocking=True,
    )


# --- MQTT only -----------------------------------------------------------------


async def test_an_mqtt_entry_starts_without_its_gateway(hass, mqtt_mock, unreachable):
    """Devices come by MQTT; only the gateway's own entities are unavailable."""
    entry = await _entry(hass, {})
    assert entry.state is ConfigEntryState.LOADED

    await _uplink(hass, EUI, {"battery": 80})

    assert hass.states.get(_id(hass, "sensor", EUI, "battery")).state == "80"
    online = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_online"
    )
    assert hass.states.get(online).state == "off"
    # No downlink topic: no settings.
    assert hass.states.async_entity_ids("number") == []


@pytest.mark.parametrize(
    ("template", "error"),
    [
        (DOWNLINK, None),
        ("sensors/lora/downlink", "invalid_downlink_topic"),
        ("sensors/+/downlink/{dev_eui}", "invalid_downlink_topic"),
        ("{site}/downlink/{dev_eui}", "invalid_downlink_topic"),
        ("sensors/#", "invalid_downlink_topic"),
    ],
)
def test_a_downlink_topic_names_one_device(template, error):
    """The gateway needs the device in the topic, and nothing to match."""
    assert downlink_error(template) == error


def test_the_downlink_and_ack_topics_are_suggested():
    """Next to a Milesight uplink topic, the others differ in one level."""
    assert sibling_topic(UPLINK, "downlink") == DOWNLINK
    assert sibling_topic(UPLINK, "ack") == ACK
    assert sibling_topic("lab/{dev_eui}", "ack") is None


# --- downlinks and command status ------------------------------------------------


async def test_a_setting_goes_out_on_the_downlink_topic(hass, mqtt_mock, unreachable):
    """Encoded bytes, confirmed, with a reference the ACK repeats."""
    await _entry(hass, {CONF_MQTT_DOWNLINK_TOPIC: DOWNLINK, CONF_MQTT_ACK_TOPIC: ACK})
    await _uplink(hass, EUI, {"battery": 80})
    events = async_capture_events(hass, EVENT_COMMAND)
    status = _id(hass, "sensor", EUI, "command_status")

    await _set_interval(hass, 10)

    [(topic, body)] = _downlinks(mqtt_mock)
    assert topic == f"sensors/lora/downlink/{EUI}"
    assert body["confirmed"] is True
    assert body["fport"] == 85
    assert base64.b64decode(body["data"])
    assert hass.states.get(status).state == "pending"

    # An ACK for an older downlink does not count.
    ack = {"acknowledged": True, "devEUI": EUI, "reference": "ha-0"}
    async_fire_mqtt_message(hass, ACK.format(dev_eui=EUI), json.dumps(ack))
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "pending"

    ack["reference"] = body["reference"]
    async_fire_mqtt_message(hass, ACK.format(dev_eui=EUI), json.dumps(ack))
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "acknowledged"

    # The device reports the new interval back.
    await _uplink(hass, EUI, {"report_interval": 10})
    assert hass.states.get(status).state == "confirmed"
    assert [e.data["status"] for e in events] == ["confirmed"]


async def test_an_ack_without_a_report_back_is_not_confirmed(
    hass, mqtt_mock, unreachable
):
    """An ACK says the frame arrived; a value out of range is dropped all the same."""
    await _entry(hass, {CONF_MQTT_DOWNLINK_TOPIC: DOWNLINK, CONF_MQTT_ACK_TOPIC: ACK})
    await _uplink(hass, EUI, {"battery": 80})
    events = async_capture_events(hass, EVENT_COMMAND)
    status = _id(hass, "sensor", EUI, "command_status")

    await _set_interval(hass, 10)
    [(_topic, body)] = _downlinks(mqtt_mock)
    ack = {"acknowledged": True, "devEUI": EUI, "reference": body["reference"]}
    async_fire_mqtt_message(hass, ACK.format(dev_eui=EUI), json.dumps(ack))
    await hass.async_block_till_done()

    for level in (79, 78, 77, 76):
        await _uplink(hass, EUI, {"battery": level})

    assert hass.states.get(status).state == "acknowledged"
    assert [e.data["status"] for e in events] == ["acknowledged"]


async def test_the_uplink_that_fetches_the_downlink_does_not_differ(
    hass, mqtt_mock, unreachable
):
    """It still carries the old value; the one after it tells."""
    await _entry(hass, {CONF_MQTT_DOWNLINK_TOPIC: DOWNLINK})
    await _uplink(hass, EUI, {"battery": 80, "report_interval": 20})
    status = _id(hass, "sensor", EUI, "command_status")

    await _set_interval(hass, 10)
    await _uplink(hass, EUI, {"report_interval": 20})
    assert hass.states.get(status).state == "pending"

    await _uplink(hass, EUI, {"report_interval": 20})
    assert hass.states.get(status).state == "differs"


async def test_a_replaced_command_ends_as_sent(hass, mqtt_mock, unreachable):
    """A second command before the first settled closes the first one."""
    await _entry(hass, {CONF_MQTT_DOWNLINK_TOPIC: DOWNLINK})
    await _uplink(hass, EUI, {"battery": 80})
    events = async_capture_events(hass, EVENT_COMMAND)

    await _set_interval(hass, 10)
    await _set_interval(hass, 20)

    assert [(e.data["status"], e.data["command"]) for e in events] == [
        ("sent", {"report_interval": 10})
    ]
    assert (
        hass.states.get(_id(hass, "sensor", EUI, "command_status")).state == "pending"
    )


async def test_a_missing_ack_sends_again_then_gives_up(hass, mqtt_mock, unreachable):
    """Three uplinks without ACK repeat the downlink, twice at most."""
    await _entry(hass, {CONF_MQTT_DOWNLINK_TOPIC: DOWNLINK, CONF_MQTT_ACK_TOPIC: ACK})
    await _uplink(hass, EUI, {"battery": 80})
    status = _id(hass, "sensor", EUI, "command_status")
    await _set_interval(hass, 10)

    async def three_uplinks() -> None:
        for _ in range(3):
            await _uplink(hass, EUI, {"battery": 80})

    for attempt in (2, 3):
        await three_uplinks()
        assert len(_downlinks(mqtt_mock)) == attempt
        assert hass.states.get(status).state == "retrying"

    await three_uplinks()
    assert len(_downlinks(mqtt_mock)) == 3
    assert hass.states.get(status).state == "failed"


async def test_without_an_ack_topic_nothing_is_sent_twice(hass, mqtt_mock, unreachable):
    """Silence is no loss there: after three uplinks it was just sent."""
    await _entry(hass, {CONF_MQTT_DOWNLINK_TOPIC: DOWNLINK})
    await _uplink(hass, EUI, {"battery": 80})
    await _set_interval(hass, 10)
    assert _downlinks(mqtt_mock)[0][1]["confirmed"] is False

    for _ in range(3):
        await _uplink(hass, EUI, {"battery": 80})

    assert len(_downlinks(mqtt_mock)) == 1
    assert hass.states.get(_id(hass, "sensor", EUI, "command_status")).state == "sent"


# --- link ---------------------------------------------------------------------------


async def test_the_uplinks_tell_the_link(hass, mqtt_mock, unreachable):
    """Frame counter gaps, data rate and gateways from the enriched payload."""
    await _entry(hass, {})
    for f_cnt, gateway, datarate in (
        (10, "24e124fffe000001", 5),
        (13, "24e124fffe000002", "SF7BW125"),
    ):
        await _uplink(
            hass,
            EUI,
            {
                "frame_count": f_cnt,
                "datarate": datarate,
                "rssi": -80,
                "loRaSNR": 6.5,
                "mac": gateway,
            },
        )

    loss = hass.states.get(_id(hass, "sensor", EUI, "packet_loss"))
    assert loss.state == "50.0"
    assert loss.attributes == {
        **loss.attributes,
        "received": 2,
        "lost": 2,
    }
    signal = hass.states.get(_id(hass, "sensor", EUI, "signal_quality"))
    # SF7 demodulates down to -7.5 dB: 6.5 dB is 14 dB above.
    assert signal.state == "excellent"
    assert signal.attributes["link_margin"] == 14.0
    assert hass.states.get(_id(hass, "sensor", EUI, "gateways_in_range")).state == "2"


def test_link_margin_and_quality():
    """The floor depends on the spreading factor; no factor, no margin."""
    assert link_margin(-10, 12) == 10.0
    assert link_margin(-10, None) is None
    assert [quality(m) for m in (12, 6, 0, -1, None)] == [
        "excellent",
        "good",
        "fair",
        "poor",
        None,
    ]


def test_the_interval_is_the_gap_between_regular_uplinks():
    """A reply seconds after a downlink is no interval of its own."""
    from datetime import UTC, datetime, timedelta

    stats = LinkStats()
    start = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    stats.record(start, [])
    assert stats.interval is None
    stats.record(start + timedelta(minutes=10), [])
    assert stats.interval == 600
    stats.record(start + timedelta(minutes=10, seconds=6), [])
    assert stats.interval == 600


def test_a_rejoin_is_no_loss():
    """A counter that falls, or jumps by 1000 or more, counts nothing lost."""
    stats = LinkStats()
    now = dt_util.utcnow()
    for f_cnt in (500, 3, 4, 2000):
        stats.record(now, [], f_cnt=f_cnt)
    assert stats.lost_today == 0
    assert stats.packet_loss == 0.0


# --- battery ------------------------------------------------------------------------

DAY = 86400.0


def test_days_left_follows_the_trend():
    """Falling 1 % a day, at 49 % now: 49 days left."""
    assert days_left([(0, 52), (DAY, 51), (2 * DAY, 50), (3 * DAY, 49)]) == 49.0


@pytest.mark.parametrize(
    "samples",
    [
        [(0, 50), (DAY, 49)],  # too few
        [(0, 50), (3600, 49), (7200, 48)],  # too short
        [(0, 50), (DAY, 50), (4 * DAY, 50)],  # not falling
        # A new battery: only the samples after it count, and they are few.
        [(0, 30), (DAY, 29), (2 * DAY, 28), (4 * DAY, 100), (5 * DAY, 99)],
    ],
)
def test_days_left_needs_a_falling_trend(samples):
    """Too little, too short, flat or just replaced: no forecast."""
    assert days_left(samples) is None


async def test_the_battery_log_keeps_one_sample_per_six_hours(hass):
    """Frequent reports add nothing; a new battery always does."""
    log = BatteryLog(hass, "entry")
    start = dt_util.utcnow()
    for hours, level in ((0, 50), (1, 50), (7, 49), (8, 99)):
        log.add("eui", level, start + timedelta(hours=hours))

    assert [level for _, level in log._samples["eui"]] == [50, 49, 99]


# --- pulse meters --------------------------------------------------------------------


def test_meter_value():
    """Offset plus pulses per unit; nothing until a type is chosen."""
    config = {"kind": "water", "pulses_per_unit": 1000, "offset": 12.5}
    assert meter_value(1500, config) == 14.0
    assert meter_value(1500, {**config, "kind": "none"}) is None
    assert meter_value("x", config) is None
    assert meter_value(1500, {**config, "pulses_per_unit": 0}) is None


async def test_a_pulse_counter_becomes_a_water_meter(hass, mqtt_mock, unreachable):
    """Chosen on the device page, the meter takes the unit for the dashboard."""
    await _entry(hass, {})
    await _uplink(hass, COUNTER_EUI, {"gpio_counter_1": 1500})
    meter = _id(hass, "sensor", COUNTER_EUI, "gpio_counter_1_meter")
    assert hass.states.get(meter).state not in ("1.5", STATE_UNAVAILABLE)

    await hass.services.async_call(
        "select",
        "select_option",
        {
            ATTR_ENTITY_ID: _id(
                hass, "select", COUNTER_EUI, "gpio_counter_1_meter_type"
            ),
            "option": "water",
        },
        blocking=True,
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {
            ATTR_ENTITY_ID: _id(
                hass, "number", COUNTER_EUI, "gpio_counter_1_pulses_per_unit"
            ),
            "value": 1000,
        },
        blocking=True,
    )

    state = hass.states.get(meter)
    assert state.state == "1.5"
    assert state.attributes["unit_of_measurement"] == "m³"
    assert state.attributes["device_class"] == "water"
    assert state.attributes["state_class"] == "total_increasing"

    # The device starts its counter afresh: the meter counts on from 1.5.
    await _uplink(hass, COUNTER_EUI, {"gpio_counter_1": 0})
    assert hass.states.get(meter).state == "1.5"
    await _uplink(hass, COUNTER_EUI, {"gpio_counter_1": 250})
    assert hass.states.get(meter).state == "1.75"


async def _settle() -> None:
    """Let a started request publish, without waiting for its answer."""
    for _ in range(20):
        await asyncio.sleep(0)


# --- network server API over MQTT ------------------------------------------------

REQUEST = "sensors/lora/reqeust/$deveui"
RESPONSE = "sensors/lora/response/$deveui"


async def test_the_device_list_comes_by_mqtt(hass, mqtt_mock, unreachable):
    """Names and models from the gateway's answer, before any uplink."""
    entry = await _entry(
        hass, {CONF_MQTT_REQUEST_TOPIC: REQUEST, CONF_MQTT_RESPONSE_TOPIC: RESPONSE}
    )
    [request] = [
        json.loads(c.args[1])
        for c in mqtt_mock.async_publish.call_args_list
        if c.args[0] == REQUEST
    ]
    assert request["url"].startswith("/api/urdevices")
    answer = {
        "id": request["id"],
        "body": {
            "code": 200,
            "error": "",
            "deviceResult": [
                {"devEUI": EUI.upper(), "name": "desk 7", "payloadName": "VS340-1.5.72"}
            ],
        },
    }
    async_fire_mqtt_message(hass, RESPONSE, json.dumps(answer))
    await hass.async_block_till_done()

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, EUI.upper()), entry.entry_id
    )
    assert device.name == "desk 7"
    assert device.model_id == "VS340"

    # The sync reads the same list; the device is still there.
    sync = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_sync_devices"
    )
    task = hass.async_create_task(
        hass.services.async_call(
            "button", "press", {ATTR_ENTITY_ID: sync}, blocking=True
        )
    )
    await _settle()
    second = json.loads(mqtt_mock.async_publish.call_args_list[-1].args[1])
    async_fire_mqtt_message(hass, RESPONSE, json.dumps({**answer, "id": second["id"]}))
    await task
    assert dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, EUI.upper()), entry.entry_id
    )


async def test_an_unanswered_request_times_out(hass, mqtt_mock):
    """No answer within the timeout is an error, not a hang."""
    api = MqttNsApi(hass, REQUEST, RESPONSE)
    await api.async_start()
    with (
        patch("custom_components.milesight_gateway.nsapi.TIMEOUT", 0.01),
        pytest.raises(NsApiError),
    ):
        await api.async_request("GET", "/api/urapplications")


async def test_an_error_answer_raises(hass, mqtt_mock):
    """The gateway's error code reaches the caller."""
    api = MqttNsApi(hass, REQUEST, RESPONSE)
    await api.async_start()
    task = hass.async_create_task(api.async_request("DELETE", "/api/urdevices/x"))
    await _settle()
    async_fire_mqtt_message(
        hass,
        RESPONSE,
        json.dumps({"id": "ha-1", "body": {"code": 5, "error": "not found"}}),
    )
    with pytest.raises(NsApiError, match="not found"):
        await task


# --- settings kept over restarts ---------------------------------------------------


async def test_a_sent_setting_survives_a_restart(hass, mqtt_mock, unreachable):
    """Sent, acknowledged, kept: after a reload the value is still known."""
    entry = await _entry(
        hass, {CONF_MQTT_DOWNLINK_TOPIC: DOWNLINK, CONF_MQTT_ACK_TOPIC: ACK}
    )
    await _uplink(hass, EUI, {"battery": 80})
    await _set_interval(hass, 7)
    log = entry.runtime_data.settings
    assert log.get(EUI)["report_interval"]["src"] == "sent"

    [(_topic, body)] = _downlinks(mqtt_mock)
    ack = {"acknowledged": True, "devEUI": EUI, "reference": body["reference"]}
    async_fire_mqtt_message(hass, ACK.format(dev_eui=EUI), json.dumps(ack))
    await hass.async_block_till_done()
    assert log.get(EUI)["report_interval"] == {
        **log.get(EUI)["report_interval"],
        "v": 7,
        "src": "ack",
    }

    # The device reports it: that is what counts from then on.
    await _uplink(hass, EUI, {"report_interval": 7})
    assert log.get(EUI)["report_interval"]["src"] == "device"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    device = entry.runtime_data.mqtt.coordinators[EUI].data
    assert device.values["report_interval"] == 7


async def test_a_command_is_followed_on_after_a_reload(hass, mqtt_mock, unreachable):
    """Its downlink is still on its way; the ACK after the reload counts."""
    entry = await _entry(
        hass, {CONF_MQTT_DOWNLINK_TOPIC: DOWNLINK, CONF_MQTT_ACK_TOPIC: ACK}
    )
    await _uplink(hass, EUI, {"battery": 80})
    status = _id(hass, "sensor", EUI, "command_status")
    await _set_interval(hass, 10)
    [(_topic, body)] = _downlinks(mqtt_mock)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "pending"

    ack = {"acknowledged": True, "devEUI": EUI, "reference": body["reference"]}
    async_fire_mqtt_message(hass, ACK.format(dev_eui=EUI), json.dumps(ack))
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "acknowledged"

    await _uplink(hass, EUI, {"report_interval": 10})
    assert hass.states.get(status).state == "confirmed"
    [record] = entry.runtime_data.journal.records
    assert record["state"] == "confirmed"


async def test_a_command_taken_up_after_a_reload_is_not_sent_again(
    hass, mqtt_mock, unreachable
):
    """What went out before may be hours old; it is followed, never repeated."""
    entry = await _entry(
        hass, {CONF_MQTT_DOWNLINK_TOPIC: DOWNLINK, CONF_MQTT_ACK_TOPIC: ACK}
    )
    await _uplink(hass, EUI, {"battery": 80})
    await _set_interval(hass, 10)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    sent = len(_downlinks(mqtt_mock))

    for level in (79, 78, 77):
        await _uplink(hass, EUI, {"battery": level})
    await hass.async_block_till_done()

    assert len(_downlinks(mqtt_mock)) == sent
    # Its answer may have come during the reload: unknown, not failed.
    assert hass.states.get(_id(hass, "sensor", EUI, "command_status")).state == "sent"


async def test_only_the_newest_command_is_taken_up(hass):
    """An open one with a finished command after it was replaced long ago."""
    from types import SimpleNamespace

    from custom_components.milesight_gateway.commands import CommandTracker
    from custom_components.milesight_gateway.journal import Journal

    journal = Journal(hass, "entry")
    await journal.async_load()
    old = journal.add(EUI, "command", command={"report_interval": 60})
    old["state"] = "pending"
    journal.add(EUI, "command", command={"report_interval": 1})["state"] = "confirmed"
    device = SimpleNamespace(
        descriptor=SimpleNamespace(dev_eui=EUI.lower()), uplinks=0, fields={}
    )

    tracker = CommandTracker(hass, device, None, journal)

    assert old["state"] == "sent"
    assert tracker.state is None
