"""Constants for the Milesight Gateway integration."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

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


def data_source(options: Mapping[str, Any]) -> str:
    """Return the entry's data source; MQTT when the option is absent."""
    return options.get(CONF_DATA_SOURCE) or SOURCE_MQTT
