"""Tests for devices found on MQTT, for a gateway that lists none (UG63)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_ON
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.milesight_gateway.api import (
    GatewayDetails,
    GatewayInfo,
    MilesightGatewayAPI,
    SystemStatus,
)
from custom_components.milesight_gateway.const import (
    CONF_GATEWAY_ONLY,
    CONF_GATEWAY_URL,
    CONF_HOSTNAME,
    CONF_MAC,
    CONF_MODEL,
    CONF_MQTT_TOPIC,
    DOMAIN,
    entry_title,
)
from custom_components.milesight_gateway.hostname import short_hostname
from custom_components.milesight_gateway.mqtt_discovery import (
    TopicTemplate,
    guess_model,
    topic_error,
)

EUI = "24e124787d000001"
TOPIC = f"lab/site-a/{EUI}"
# A VS340 uplink as a UG63 publishes it, with a payload codec that adds the
# gateway's radio data.
UPLINK = {
    "applicationName": "VS340",
    "battery": 24,
    "devEUI": EUI,
    "deviceName": "desk 4",
    "gw": "Local Gateway",
    "loRaSNR": 14.2,
    "mac": "24e124fffe000001",
    "occupancy": 1,
    "rssi": -35,
    "timestamp": "2026-11-26T00:09:17.795386Z",
}
ENTRY_DATA = {
    CONF_GATEWAY_URL: "http://192.0.2.10",
    "port": 8080,
    "username": "admin",
    "password": "secret",
    "secret_key": "1111111111111111",
    "iv": "2222222222222222",
    "gateway_mqtt_base_topic": "sensors/lora/uplink",
    CONF_GATEWAY_ONLY: True,
    CONF_MODEL: "UG63",
}


# --- topic template ----------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "sensors/lora/uplink/{dev_eui}",
        "{site}/lora/{dev_eui}/uplink",
        "+/lora/{dev_eui}",
        "sensors/lora/uplink",
    ],
)
def test_usable_topics(template):
    """Placeholders and + as whole levels; {dev_eui} is optional."""
    assert topic_error(template) is None


@pytest.mark.parametrize(
    "template",
    [
        "",
        "  ",
        "sensors/#",
        "sensors/eui-{dev_eui}",
        "sensors/a+b/{dev_eui}",
        "{dev_eui}/{dev_eui}",
    ],
)
def test_unusable_topics(template):
    """MQTT cannot match a placeholder inside a level, and # hides the devEUI."""
    assert topic_error(template) == "invalid_topic"


def test_the_template_becomes_a_subscription():
    """Every placeholder matches one level; the devEUI is read from its own."""
    template = TopicTemplate.parse("{site}/lora/{dev_eui}/uplink")

    assert template.subscription == "+/lora/+/uplink"
    assert template.dev_eui("berlin/lora/24E124787D000001/uplink") == (
        "24E124787D000001"
    )
    assert template.dev_eui("berlin/lora") is None
    assert TopicTemplate.parse("lora/uplink").dev_eui("lora/uplink") is None


# --- model ---------------------------------------------------------------------


def test_the_prefix_names_the_model():
    """A prefix only one model has decides alone."""
    assert guess_model("24e124538d000001", set()) == "ws202"


def test_shared_prefixes_are_told_apart_by_the_fields():
    """The EM300 variants share a prefix; the magnet field makes it the MCS."""
    model = guess_model("24e124136d000001", {"battery", "magnet_status"})

    assert model == "em300_mcs"


def test_a_foreign_device_has_no_model():
    """Only Milesight devEUIs carry a model prefix."""
    assert guess_model("a84041000000beef", {"temperature"}) is None


# --- setup -----------------------------------------------------------------------


@pytest.fixture
def gateway():
    """The UG63 answers its login check and web interface."""
    with (
        patch.object(
            MilesightGatewayAPI,
            "async_get_gateway_info",
            AsyncMock(return_value=GatewayInfo(model="UG63", firmware="63.0.0.2-r2")),
        ),
        patch.object(
            MilesightGatewayAPI,
            "async_get_gateway_details",
            AsyncMock(
                return_value=GatewayDetails(
                    model_id="UG63-868M", serial_number=None, hw_version="V2.1"
                )
            ),
        ),
        patch.object(
            MilesightGatewayAPI,
            "async_get_system_status",
            AsyncMock(return_value=SystemStatus(3.0, 9.8, 85.7, 60)),
        ),
        patch.object(
            MilesightGatewayAPI,
            "async_connect",
            AsyncMock(side_effect=AssertionError("no network server login")),
        ),
        patch(
            "custom_components.milesight_gateway.sensor._tcp_ping",
            AsyncMock(return_value=5.0),
        ),
    ):
        yield


@pytest.fixture
def expected_lingering_timers() -> bool:
    """MQTT's own housekeeping timers outlive the test."""
    return True


async def _entry(hass, topic: str = "lab/{site}/{dev_eui}") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options={CONF_MQTT_TOPIC: topic},
        version=1,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity(hass, platform: str, key: str) -> str | None:
    """Entity IDs follow the LoRaWAN source: <devEUI>_<field>."""
    return er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{EUI.upper()}_{key}"
    )


async def test_a_device_appears_with_its_first_uplink(hass, mqtt_mock, gateway):
    """Model from the devEUI, name from the payload, under the gateway."""
    entry = await _entry(hass)
    assert _entity(hass, "sensor", "battery") is None

    async_fire_mqtt_message(hass, TOPIC, json.dumps(UPLINK))
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier(
        (DOMAIN, EUI.upper()), entry.entry_id
    )
    gateway_device = registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device is not None
    assert device.name == "desk 4"
    assert device.model_id == "VS340"
    # Listed on the gateway's page as one of its devices.
    assert device.via_device_id == gateway_device.id

    assert hass.states.get(_entity(hass, "sensor", "battery")).state == "24"
    assert hass.states.get(_entity(hass, "binary_sensor", "occupancy")).state == (
        STATE_ON
    )
    rssi = _entity(hass, "sensor", "rssi")
    assert hass.states.get(rssi).state == "-35"
    assert er.async_get(hass).async_get(rssi).entity_category is (
        EntityCategory.DIAGNOSTIC
    )
    assert hass.states.get(_entity(hass, "sensor", "gw")).state == "Local Gateway"
    # The uplink's time is no state of the device.
    assert _entity(hass, "sensor", "timestamp") is None


async def test_the_payload_names_the_device_when_the_topic_does_not(
    hass, mqtt_mock, gateway
):
    """Without {dev_eui} in the template, the payload's devEUI is used."""
    await _entry(hass, "lab/uplink")

    async_fire_mqtt_message(hass, "lab/uplink", json.dumps(UPLINK))
    await hass.async_block_till_done()

    assert _entity(hass, "sensor", "battery") is not None


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        (TOPIC, "not json"),
        (TOPIC, "[1, 2]"),
        ("lab/site-a/not-an-eui", json.dumps({"battery": 1})),
        ("lab/site-a/a84041000000beef", json.dumps({"battery": 1})),
    ],
)
async def test_unusable_uplinks_create_nothing(
    hass, mqtt_mock, gateway, topic, payload
):
    """Broken JSON, a topic without devEUI and foreign devices are ignored."""
    entry = await _entry(hass)

    async_fire_mqtt_message(hass, topic, payload)
    await hass.async_block_till_done()

    assert entry.runtime_data.mqtt.coordinators == {}


async def test_a_device_of_another_entry_is_left_to_it(hass, mqtt_mock, gateway):
    """Entity IDs are the same in every entry: the first owner keeps the device."""
    other = MockConfigEntry(domain=DOMAIN, data={}, title="UG65")
    other.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={(DOMAIN, EUI.upper())}
    )
    entry = await _entry(hass)

    async_fire_mqtt_message(hass, TOPIC, json.dumps(UPLINK))
    await hass.async_block_till_done()

    assert entry.runtime_data.mqtt.coordinators == {}


async def test_known_devices_are_back_after_a_restart(hass, mqtt_mock, gateway):
    """A reload restores devices and their entities before their next uplink."""
    entry = await _entry(hass)
    async_fire_mqtt_message(hass, TOPIC, json.dumps(UPLINK))
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert EUI in entry.runtime_data.mqtt.coordinators
    assert hass.states.get(_entity(hass, "sensor", "battery")).state == "24"


async def test_without_a_topic_the_gateway_stands_alone(hass, mqtt_mock, gateway):
    """No topic, no subscription: the entry shows the gateway only."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options={}, version=1, minor_version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.mqtt is None


# --- hostname ------------------------------------------------------------------


async def test_an_entry_learns_the_hostname_from_the_router(hass, mqtt_mock, gateway):
    """A UG63 reports no hostname; a router's device tracker knows its DHCP name."""
    hass.states.async_set(
        "device_tracker.router_gw",
        "home",
        {"mac": "24:E1:24:F5:00:01", "ip": "192.0.2.10", "host_name": "gw_mini.lan"},
    )
    data = {**ENTRY_DATA, CONF_MAC: "24:e1:24:f5:00:01"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        title=entry_title(data),
        version=1,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.data[CONF_HOSTNAME] == "gw_mini"
    assert entry.title == "gw_mini - UG63 (192.0.2.10)"
    gateway_device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert gateway_device.name == "gw_mini - UG63"


async def test_a_title_the_user_chose_stays(hass, mqtt_mock, gateway):
    """Only the title the setup gave is replaced."""
    hass.states.async_set(
        "device_tracker.router_gw", "home", {"ip": "192.0.2.10", "host_name": "gw"}
    )
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, title="Lab", version=1, minor_version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.data[CONF_HOSTNAME] == "gw"
    assert entry.title == "Lab"


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("ms_gw_mini.localdomain", "ms_gw_mini"),
        ("SITE_A_LEVEL_08_GW-8", "SITE_A_LEVEL_08_GW-8"),
        # Some trackers report the vendor where the hostname belongs.
        ("xiamen milesight iot co   ltd", None),
        ("", None),
        (None, None),
    ],
)
def test_only_real_hostnames_are_taken(reported, expected):
    """A DHCP hostname is one label: letters, digits, - and _."""
    assert short_hostname(reported) == expected


async def test_a_stored_vendor_text_is_dropped(hass, mqtt_mock, gateway):
    """A beta took a tracker's vendor text as hostname; it goes again."""
    bad = {**ENTRY_DATA, CONF_HOSTNAME: "xiamen milesight iot co   ltd"}
    entry = MockConfigEntry(
        domain=DOMAIN, data=bad, title=entry_title(bad), version=1, minor_version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert CONF_HOSTNAME not in entry.data
    assert entry.title == "Milesight UG63 (192.0.2.10)"


async def test_a_beta_title_gets_its_address_back(hass, mqtt_mock, gateway):
    """The 1.5.0 beta named an entry "<hostname> - <model>"; the address returns."""
    data = {**ENTRY_DATA, CONF_HOSTNAME: "gw_mini"}
    entry = MockConfigEntry(
        domain=DOMAIN, data=data, title="gw_mini - UG63", version=1, minor_version=2
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.title == "gw_mini - UG63 (192.0.2.10)"
