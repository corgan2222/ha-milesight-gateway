"""Tests for the LoRaWAN data source.

The packet-log connection is replaced by a fake that announces the device of
the MQTT tests (same devEUI) and delivers raw uplinks; decoding runs through
the bundled Milesight codecs.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from lorawan_connection import (
    AckEvent,
    AddedEvent,
    DeviceDescriptor,
    GatewayReception,
    TxAckEvent,
    UplinkEvent,
)
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from custom_components.milesight_gateway.const import (
    CONF_DATA_SOURCE,
    DOMAIN,
    SOURCE_LORAWAN,
    SOURCE_MQTT,
)

from .test_platforms import (  # noqa: F401  fixtures, found by their own names
    DEVICE,
    TEMPERATURE,
    TOPIC,
    expected_lingering_timers,
    firmware,
    get_devices,
    mqtt,
    status,
)

# Registered under this name: an entry set up with the MQTT source.
from .test_platforms import entry as mqtt_entry  # noqa: F401

LORAWAN = "custom_components.milesight_gateway.lorawan"

EUI = "24e124123456789a"
GATEWAY = "24E124FFFEF8706C"
# A VS321 report: battery 89 %, 24.3 °C, 27 % humidity, nobody counted.
REPORT = bytes.fromhex("0175590367f30007ff0008f4020004683605fd0000")


class FakeConnection:
    """The packet-log connection, reduced to what the integration touches."""

    stack_type = "milesight"
    latest: FakeConnection | None = None
    # An uplink that arrives while the integration is still starting up.
    early_uplink: bytes | None = None
    # The catalog model the device announces; same devEUI either way.
    model = "vs321"

    def __init__(self, *_args: object, network_id: str, **_kwargs: object) -> None:
        self.network_id = network_id
        self.devices = {
            EUI: DeviceDescriptor(
                network_id,
                EUI,
                "vs321",
                "5",
                "p",
                self.model,
                "milesight-iot",
                model=self.model.upper(),
                stack="milesight",
            )
        }
        self.last_uplinks: dict[str, UplinkEvent] = {}
        self.sent: list = []
        self._listeners: list = []
        FakeConnection.latest = self

    async def async_connect(self) -> None:
        """Nothing to log in to."""

    async def async_subscribe(self, *, brands=None, listener):  # noqa: ARG002
        for descriptor in self.devices.values():
            listener(AddedEvent(descriptor=descriptor))
        self._listeners.append(listener)
        if self.early_uplink is not None:
            self.uplink(self.early_uplink)
        return lambda: self._listeners.remove(listener)

    def on_disconnect(self, listener):
        self.disconnected = listener
        return lambda: None

    async def async_send_downlink(self, downlink) -> str:
        self.sent.append(downlink)
        return "queued"

    async def async_get_gateways(self) -> list[dict]:
        return [
            {
                "id": GATEWAY,
                "name": "Local Gateway",
                "connected": True,
                "last_seen": None,
            }
        ]

    async def close(self) -> None:
        """Nothing to stop."""

    def transmitted(self) -> None:
        """Tell that the newest downlink left the gateway."""
        event = TxAckEvent(
            network_id=self.network_id,
            dev_eui=EUI,
            received_at=datetime(2026, 11, 24, 12, 0, 30, tzinfo=UTC),
            gateway_id=GATEWAY,
            downlink_id=7,
        )
        for listener in tuple(self._listeners):
            listener(event)

    def ack(self) -> None:
        """Deliver the ACK of the newest confirmed downlink, as the packet log does."""
        event = AckEvent(
            network_id=self.network_id,
            dev_eui=EUI,
            received_at=datetime(2026, 11, 24, 12, 1, tzinfo=UTC),
            queue_item_id="queued",
            acknowledged=True,
        )
        for listener in tuple(self._listeners):
            listener(event)

    def uplink(self, data: bytes) -> None:
        """Deliver one uplink heard by the local gateway."""
        event = UplinkEvent(
            network_id=self.network_id,
            dev_eui=EUI,
            received_at=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
            data=data,
            f_port=85,
            receptions=(GatewayReception(GATEWAY, -80.0, 9.5),),
        )
        self.last_uplinks[EUI] = event
        for listener in tuple(self._listeners):
            listener(event)


@pytest.fixture
def connection():
    """Use the fake connection; skip registering with HA's lorawan."""
    with (
        patch(f"{LORAWAN}.MilesightConnection", FakeConnection),
        patch(f"{LORAWAN}.LoRaWANRuntime._async_register_with_lorawan", AsyncMock()),
    ):
        yield


async def _switch_to_lorawan(hass, entry) -> FakeConnection:
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_DATA_SOURCE: SOURCE_LORAWAN}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert FakeConnection.latest is not None
    return FakeConnection.latest


def test_button_events_keep_the_mqtt_names():
    """Automations on single_press keep working after a switch."""
    from milesight_gateway_api import decoder

    from custom_components.milesight_gateway.lorawan_entity import event_type

    values = decoder.models()["ws101"]["fields"]["button_event.status"]["values"]
    assert {event_type(name) for name in values.values()} == {
        "single_press",
        "long_press",
        "double_press",
    }


async def test_a_thermostat_extra_survives_the_switch(hass, mqtt_entry, connection):
    """The MQTT child lock becomes the catalog's child_lock_config.enable."""
    registry = er.async_get(hass)
    child_lock = registry.async_get_or_create(
        "switch",
        DOMAIN,
        "24E124123456789A_child_lock",
        config_entry=mqtt_entry,
        suggested_object_id="heating_child_lock",
    ).entity_id

    with patch.object(FakeConnection, "model", "wt101"):
        await _switch_to_lorawan(hass, mqtt_entry)

    assert registry.async_get(child_lock) is not None
    assert hass.states.get(child_lock).state == "unknown"


async def test_switching_keeps_shared_entities(hass, mqtt_entry, connection):
    """A catalog field keeps its entity; a payload codec's extra one goes."""
    async_fire_mqtt_message(hass, TOPIC, '{"temperature": 21, "gw": "Local Gateway"}')
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    unique_id = registry.async_get(TEMPERATURE).unique_id
    snr = registry.async_get_entity_id("sensor", DOMAIN, "24E124123456789A_gw")
    assert snr is not None

    fake = await _switch_to_lorawan(hass, mqtt_entry)
    fake.uplink(REPORT)
    await hass.async_block_till_done()

    assert registry.async_get(TEMPERATURE).unique_id == unique_id
    assert hass.states.get(TEMPERATURE).state == "24.3"
    # The packet log has no codec extras: no entity left behind.
    assert registry.async_get(snr) is None


async def test_rssi_and_snr_come_from_the_packet_log(hass, mqtt_entry, connection):
    """The strongest reception of the newest uplink, under the MQTT keys."""
    registry = er.async_get(hass)
    fake = await _switch_to_lorawan(hass, mqtt_entry)
    fake.uplink(REPORT)
    await hass.async_block_till_done()

    for key, value in (("rssi", "-80.0"), ("loRaSNR", "9.5")):
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"24E124123456789A_{key}"
        )
        assert entity_id is not None, key
        assert hass.states.get(entity_id).state == value
        assert registry.async_get(entity_id).entity_category is (
            EntityCategory.DIAGNOSTIC
        )


async def test_switching_back_keeps_the_catalog_entities(hass, mqtt_entry, connection):
    """Both sources build from the catalog, so a field keeps its entity."""
    registry = er.async_get(hass)
    fake = await _switch_to_lorawan(hass, mqtt_entry)
    fake.uplink(REPORT)
    await hass.async_block_till_done()
    humidity = registry.async_get_entity_id(
        "sensor", DOMAIN, "24E124123456789A_humidity"
    )
    assert humidity is not None

    hass.config_entries.async_update_entry(
        mqtt_entry, options={**mqtt_entry.options, CONF_DATA_SOURCE: SOURCE_MQTT}
    )
    assert await hass.config_entries.async_reload(mqtt_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(humidity) is not None
    assert registry.async_get(TEMPERATURE) is not None


# A mounted WT101 in manual mode: 25.4 °C, valve held at 50 %.
WT101_REPORT = bytes.fromhex("0175630367fe000592320b90f7000990ef01")


async def test_a_reported_setting_gets_an_entity(hass, mqtt_entry, connection):
    """The valve opening is a setting, but the WT101 reports it: it shows."""
    with patch.object(FakeConnection, "model", "wt101"):
        fake = await _switch_to_lorawan(hass, mqtt_entry)
    fake.uplink(WT101_REPORT)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    valve = registry.async_get_entity_id(
        "number", DOMAIN, "24E124123456789A_set_valve_opening"
    )
    assert valve is not None
    assert registry.async_get(valve).disabled_by is None
    assert hass.states.get(valve).state == "50"


async def test_the_motor_position_tells_whether_it_heats(hass, mqtt_entry, connection):
    """In auto mode only the motor shows the valve: at full stroke it is shut."""
    with patch.object(FakeConnection, "model", "wt101"):
        fake = await _switch_to_lorawan(hass, mqtt_entry)
    climate = er.async_get(hass).async_get_entity_id(
        "climate", DOMAIN, "24E124123456789A_thermostat"
    )

    fake.uplink(WT101_REPORT)  # manual, motor at 247 of 495
    await hass.async_block_till_done()
    assert hass.states.get(climate).attributes["hvac_action"] == "heating"

    # Auto, 10 °C target in a 15.3 °C room: motor at 489 of 489.
    fake.uplink(bytes.fromhex("01756304676400036799000b90e9010990e901"))
    await hass.async_block_till_done()
    assert hass.states.get(climate).attributes["hvac_action"] == "idle"


async def test_a_target_temperature_switches_to_regulation(
    hass, mqtt_entry, connection
):
    """In manual mode a target is ignored, so setting one also selects auto."""
    from milesight_gateway_api import decoder

    with patch.object(FakeConnection, "model", "wt101"):
        fake = await _switch_to_lorawan(hass, mqtt_entry)
    climate = er.async_get(hass).async_get_entity_id(
        "climate", DOMAIN, "24E124123456789A_thermostat"
    )

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": climate, "temperature": 21},
        blocking=True,
    )

    (downlink,) = fake.sent
    assert downlink.data == decoder.encode(
        "wt101",
        {
            "target_temperature": 21,
            "temperature_tolerance": 1.0,
            "temperature_control": {"enable": 1, "mode": 0},
        },
    )


async def test_set_values_survive_a_restart(hass, mqtt_entry, connection):
    """Settings arrive only on request; the last set ones stay after a reload."""
    registry = er.async_get(hass)
    with patch.object(FakeConnection, "model", "wt101"):
        await _switch_to_lorawan(hass, mqtt_entry)
        climate = registry.async_get_entity_id(
            "climate", DOMAIN, "24E124123456789A_thermostat"
        )
        number = registry.async_get_entity_id(
            "number", DOMAIN, "24E124123456789A_report_interval"
        )
        target = registry.async_get_entity_id(
            "number", DOMAIN, "24E124123456789A_target_temperature"
        )
        # Nothing known yet: a target anyway, or the dial is missing.
        assert hass.states.get(climate).attributes["temperature"] == 21
        assert float(hass.states.get(target).state) == 21
        assert hass.states.get(number).state == "unknown"

        await hass.services.async_call(
            "number", "set_value", {"entity_id": number, "value": 20}, blocking=True
        )
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": climate, "temperature": 23},
            blocking=True,
        )
        assert await hass.config_entries.async_reload(mqtt_entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get(number).state == "20"
    assert hass.states.get(climate).attributes["temperature"] == 23
    assert float(hass.states.get(target).state) == 23


async def test_fields_sent_on_change_come_back_after_a_restart(
    hass, mqtt_entry, connection
):
    """Tamper and window come only on change; a restart keeps them and their state."""
    registry = er.async_get(hass)
    with patch.object(FakeConnection, "model", "wt101"):
        fake = await _switch_to_lorawan(hass, mqtt_entry)
        fake.uplink(WT101_REPORT)
        fake.uplink(bytes.fromhex("060001"))  # tamper: uninstalled
        fake.uplink(bytes.fromhex("07000100"))  # window open
        await hass.async_block_till_done()
        assert await hass.config_entries.async_reload(mqtt_entry.entry_id)
        await hass.async_block_till_done()

    def state(platform: str, key: str) -> str:
        entity_id = registry.async_get_entity_id(
            platform, DOMAIN, f"24E124123456789A_{key}"
        )
        return hass.states.get(entity_id).state

    assert state("binary_sensor", "tamper_status") == "on"
    assert state("binary_sensor", "window_detection") == "on"
    assert state("number", "set_valve_opening") == "50"
    assert state("sensor", "battery") == "99"
    assert state("sensor", "temperature") == "25.4"


async def test_an_uplink_during_startup_is_kept(hass, mqtt_entry, connection, caplog):
    """An uplink before the runtime is stored on the entry still lands."""
    with patch.object(FakeConnection, "early_uplink", REPORT):
        await _switch_to_lorawan(hass, mqtt_entry)

    assert "LoRaWAN listener failed" not in caplog.text
    assert hass.states.get(TEMPERATURE).state == "24.3"


async def test_a_setting_goes_out_as_raw_bytes(hass, mqtt_entry, connection):
    """The report interval is encoded by Milesight's encoder, on FPort 85."""
    fake = await _switch_to_lorawan(hass, mqtt_entry)
    number = er.async_get(hass).async_get_entity_id(
        "number", DOMAIN, "24E124123456789A_report_interval"
    )
    assert number is not None

    await hass.services.async_call(
        "number", "set_value", {"entity_id": number, "value": 20}, blocking=True
    )

    (downlink,) = fake.sent
    assert downlink.f_port == 85
    assert downlink.data  # the encoder's bytes, not JSON for a gateway codec
    # Confirmed: the packet log reports the device's ACK.
    assert downlink.confirmed is True
    status = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, "24E124123456789A_command_status"
    )
    assert hass.states.get(status).state == "pending"

    # The device fetched it with an uplink; the ACK only comes with the next.
    fake.transmitted()
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "transmitted"

    fake.ack()
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "acknowledged"

    # A new command: the ACK that comes before its downlink left belongs to
    # the command before and does not acknowledge this one.
    await hass.services.async_call(
        "number", "set_value", {"entity_id": number, "value": 30}, blocking=True
    )
    fake.ack()
    await hass.async_block_till_done()
    assert hass.states.get(status).state == "pending"


async def test_gateways_are_read_right_after_a_boot(hass, mqtt_entry, connection):
    """The monotonic clock starts near zero after a boot; the first read happens."""
    await _switch_to_lorawan(hass, mqtt_entry)
    runtime = mqtt_entry.runtime_data.lorawan

    with patch(f"{LORAWAN}.time.monotonic", return_value=5.0):
        gateways = await runtime.async_gateways()

    assert [gateway["id"] for gateway in gateways] == [GATEWAY]


async def test_the_network_command_lists_gateways_and_receptions(
    hass, hass_ws_client, mqtt_entry, connection
):
    """The map reads gateways and each device's newest receptions."""
    fake = await _switch_to_lorawan(hass, mqtt_entry)
    fake.uplink(REPORT)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": f"{DOMAIN}/network"})
    reply = await client.receive_json()

    (item,) = reply["result"]["connections"]
    assert item["gateways"][0]["id"] == GATEWAY
    (device,) = item["devices"]
    assert device["dev_eui"] == EUI
    assert device["receptions"] == [{"gateway_id": GATEWAY, "rssi": -80.0, "snr": 9.5}]
    # A double click on the map opens these devices.
    assert item["device_id"] == mqtt_entry.runtime_data.gateway_device_id
    registry = dr.async_get(hass)
    assert registry.async_get(device["device_id"]).identifiers == {
        (DOMAIN, EUI.upper())
    }
    assert device["selected"] is True
    assert device["stored"] is False


async def test_the_map_starts_from_the_stored_receptions(
    hass, hass_ws_client, mqtt_entry, connection
):
    """After a restart the newest receptions come from the store until fresh ones."""
    fake = await _switch_to_lorawan(hass, mqtt_entry)
    fake.uplink(REPORT)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": f"{DOMAIN}/network"})
    await client.receive_json()

    fake.last_uplinks.clear()  # as after a restart
    await client.send_json({"id": 2, "type": f"{DOMAIN}/network"})
    reply = await client.receive_json()

    (device,) = reply["result"]["connections"][0]["devices"]
    assert device["stored"] is True
    assert device["receptions"] == [{"gateway_id": GATEWAY, "rssi": -80.0, "snr": 9.5}]


async def test_one_gateway_can_take_the_map_out_of_the_sidebar(hass):
    """One menu entry for all gateways; off at one LoRaWAN entry removes it."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.milesight_gateway.const import CONF_NETWORK_PANEL
    from custom_components.milesight_gateway.network import show_in_sidebar

    lorawan = {CONF_DATA_SOURCE: SOURCE_LORAWAN}
    MockConfigEntry(domain=DOMAIN, data={}, options=lorawan).add_to_hass(hass)
    # An MQTT entry has no say: the map shows LoRaWAN entries only.
    MockConfigEntry(
        domain=DOMAIN, data={}, options={CONF_NETWORK_PANEL: False}
    ).add_to_hass(hass)
    assert show_in_sidebar(hass) is True

    MockConfigEntry(
        domain=DOMAIN, data={}, options={**lorawan, CONF_NETWORK_PANEL: False}
    ).add_to_hass(hass)
    assert show_in_sidebar(hass) is False


async def test_last_seen_comes_from_the_network_server_until_an_uplink(
    hass, mqtt_entry, connection, get_devices
):
    """After a restart a rarely reporting device shows when the gateway heard it."""
    seen = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    get_devices.return_value = ([replace(DEVICE, last_seen=seen)], 1, 0)
    fake = await _switch_to_lorawan(hass, mqtt_entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, "24E124123456789A_last_seen"
    )

    assert hass.states.get(entity_id).state == seen.isoformat()

    fake.uplink(REPORT)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "2026-11-24T12:00:00+00:00"


async def test_a_lost_gateway_waits_while_it_installs_firmware(
    hass, mqtt_entry, connection
):
    """It reboots into the new firmware; a reload would end the wait for it."""
    fake = await _switch_to_lorawan(hass, mqtt_entry)
    runtime = mqtt_entry.runtime_data

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        runtime.installing = True
        fake.disconnected()
        reload.assert_not_called()
        assert runtime.reload_after_install

        runtime.installing = runtime.reload_after_install = False
        fake.disconnected()
        reload.assert_called_once_with(mqtt_entry.entry_id)
