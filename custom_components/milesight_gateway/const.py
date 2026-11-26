"""Constants for the Milesight Gateway integration."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

DOMAIN = "milesight_gateway"

CONF_GATEWAY_URL = "gateway_url"
CONF_GATEWAY_MQTT_BASE_TOPIC = "gateway_mqtt_base_topic"
CONF_SECRET_KEY = "secret_key"  # noqa: S105
CONF_IV = "iv"

DEFAULT_PORT = 8080
DEFAULT_SECRET_KEY = "1111111111111111"  # noqa: S105
DEFAULT_IV = "2222222222222222"
DEFAULT_GATEWAY_MQTT_BASE_TOPIC = "sensors/lora/uplink"
DEFAULT_SCAN_INTERVAL = 300  # 5 minutes — device list rarely changes
FIRMWARE_SCAN_INTERVAL = timedelta(hours=12)
FIRMWARE_RETRY_INTERVAL = timedelta(minutes=5)
MIN_SCAN_INTERVAL = 60

# Options. Absent from older entries, so every reader falls back to a default
# that keeps the behaviour from before the option existed.
CONF_STATUS_INTERVAL = "status_interval"
CONF_WEB_DATA = "web_data"
CONF_FIRMWARE_CHECK = "firmware_check"
CONF_DEVICES = "devices"
# Where sensor data comes from. Entries from before the option read MQTT, so an
# update changes nothing until the user switches.
CONF_DATA_SOURCE = "data_source"
SOURCE_MQTT = "mqtt"
SOURCE_LORAWAN = "lorawan"
# LoRaWAN source: an entity for every setting and command, not only the
# essential ones.
CONF_ALL_SETTINGS = "all_settings"
# LoRaWAN source: the network map in the sidebar. Off hides only the menu
# entry; the page keeps its address and the dashboard card works.
CONF_NETWORK_PANEL = "network_panel"
# Gateway-only entries: the MQTT topic the gateway publishes its devices'
# uplinks under, with {dev_eui} where the devEUI sits. Absent: no devices.
CONF_MQTT_TOPIC = "mqtt_topic"

# Entry data, set by the config flow: the gateway refuses the network server
# login but takes the account on its web interface (UG63 Mini). Such an entry
# never tries the network server login, since failed logins lock the account.
CONF_GATEWAY_ONLY = "gateway_only"
# Entry data: the gateway's MAC address, known once DHCP discovery has seen it.
# It is the entry's unique id from then on, so a new IP is matched to it.
CONF_MAC = "mac"
# Entry data: the model /islogin named at setup, for the gateway device's name.
# Entries from before it keep the generic name, so entity ids stay as they are.
CONF_MODEL = "model"
# Entry data: the hostname the web interface reported at setup (firmware
# 60.0.0.50 and later). It tells apart the many gateways of one fleet.
CONF_HOSTNAME = "hostname"


def gateway_name(data: Mapping[str, Any]) -> str | None:
    """Return the gateway's name from setup: "<hostname> - <model>" if known."""
    model, hostname = data.get(CONF_MODEL), data.get(CONF_HOSTNAME)
    if hostname:
        return f"{hostname} - {model}" if model else hostname
    return f"Milesight {model}" if model else None


def entry_title(data: Mapping[str, Any]) -> str:
    """Name an entry after hostname and model, falling back to the address."""
    url = data[CONF_GATEWAY_URL]
    if not data.get(CONF_HOSTNAME) and not data.get(CONF_MODEL):
        return url
    # The address stays in view: a fleet has many gateways of one model.
    return f"{gateway_name(data)} ({urlsplit(url).hostname})"


# Firmware families (first part of the version /islogin reports) whose network
# server refuses the login: the UG63 Mini on 63.x. Its successor with an
# external antenna runs 64.x and may still call itself a UG63, so the model
# name alone cannot decide. Setting one up asks only for the web login and
# never tries the network server.
WEB_ONLY_FIRMWARE = frozenset({"63"})


def data_source(options: Mapping[str, Any]) -> str:
    """Return the entry's data source; MQTT when the option is absent."""
    return options.get(CONF_DATA_SOURCE) or SOURCE_MQTT


def gateway_only(data: Mapping[str, Any]) -> bool:
    """Return whether the entry covers the gateway itself, without its devices."""
    return bool(data.get(CONF_GATEWAY_ONLY))


def uses_lorawan(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    """Return whether the entry runs the LoRaWAN source; never without its login."""
    return not gateway_only(data) and data_source(options) == SOURCE_LORAWAN


def uses_mqtt_discovery(data: Mapping[str, Any], options: Mapping[str, Any]) -> bool:
    """Return whether a gateway-only entry finds its devices on MQTT."""
    return gateway_only(data) and bool(options.get(CONF_MQTT_TOPIC))
