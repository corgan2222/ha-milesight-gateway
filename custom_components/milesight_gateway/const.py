"""Constants for the Milesight Gateway integration."""

from datetime import timedelta

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
