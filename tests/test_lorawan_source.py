"""Tests for the LoRaWAN data source.

The packet-log connection is replaced by a fake that announces the device of
the MQTT tests (same devEUI) and delivers raw uplinks; decoding runs through
the bundled Milesight codecs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er
from lorawan_connection import (
    AddedEvent,
    DeviceDescriptor,
    GatewayReception,
    UplinkEvent,
)

from custom_components.milesight_gateway.const import (
    CONF_DATA_SOURCE,
    DOMAIN,
    SOURCE_LORAWAN,
    SOURCE_MQTT,
)

from .test_platforms import (  # noqa: F401  fixtures, found by their own names
    DOOR,
    TEMPERATURE,
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

    def on_disconnect(self, _listener):
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

    from custom_components.milesight_gateway.event import _PRESS_TYPES
    from custom_components.milesight_gateway.lorawan_entity import event_type

    values = decoder.models()["ws101"]["fields"]["button_event.status"]["values"]
    assert {event_type(name) for name in values.values()} == set(_PRESS_TYPES.values())


def test_mqtt_controls_keep_their_ids():
    """Every MQTT control maps to its catalog field, so a switch keeps it."""
    from milesight_gateway_api import decoder

    from custom_components.milesight_gateway.lorawan_entity import LEGACY_KEYS
    from custom_components.milesight_gateway.number import DEVICE_SETTINGS
    from custom_components.milesight_gateway.select import DEVICE_SELECTS
    from custom_components.milesight_gateway.switch import DEVICE_SWITCHES

    models = decoder.models()
    for table in (DEVICE_SETTINGS, DEVICE_SELECTS, DEVICE_SWITCHES):
        for model, descriptions in table.items():
            for description in descriptions:
                field_id = ".".join(getattr(description, "path", ()) or ())
                field_id = field_id or description.key
                assert field_id in models[model]["fields"], (model, field_id)
                assert LEGACY_KEYS.get(field_id, field_id) == description.key, (
                    f"Add {field_id!r}: {description.key!r} to LEGACY_KEYS"
                )


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
    """A field both sources know keeps its entity; an MQTT-only one goes."""
    registry = er.async_get(hass)
    unique_id = registry.async_get(TEMPERATURE).unique_id
    assert registry.async_get(DOOR) is not None

    fake = await _switch_to_lorawan(hass, mqtt_entry)
    fake.uplink(REPORT)
    await hass.async_block_till_done()

    assert registry.async_get(TEMPERATURE).unique_id == unique_id
    assert hass.states.get(TEMPERATURE).state == "24.3"
    # The VS321 codec has no door contact: no entity left behind.
    assert registry.async_get(DOOR) is None


async def test_switching_back_removes_lorawan_only_entities(
    hass, mqtt_entry, connection
):
    """Back on MQTT, a field only the LoRaWAN codec knows leaves no orphan."""
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

    assert registry.async_get(humidity) is None
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
