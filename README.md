# Milesight Gateway — Home Assistant Integration

[![GitHub Repository](https://img.shields.io/badge/GitHub-corgan2222%2Fha-milesight-gateway-blue?logo=github)](https://github.com/corgan2222/ha-milesight-gateway)


A custom Home Assistant integration that bridges **Milesight LoRaWAN gateways** with Home Assistant, providing real-time sensor data from 130+ Milesight LoRaWAN devices.

---

## How It Works

The integration uses two communication channels:

| Channel            | Purpose                                                                                    |
| ------------------ | ------------------------------------------------------------------------------------------ |
| **HTTP REST API**  | Authenticates locally with the gateway (JWT) and fetches the list of connected LoRaWAN devices from the gateway. Only online devices will be synced.    |
| **MQTT (push)**    | Receives real-time sensor telemetry from LoRaWAN devices via the gateway's uplink topic   |

Device entity definitions are matched from a built-in database of 130+ Milesight device models (`devices_ha.json`). Once matched, the integration automatically creates the appropriate sensor and binary sensor entities for each device.

---



## Requirements

- Home Assistant 2026.3.2 or newer
- A running [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) in Home Assistant
- A Milesight LoRaWAN gateway reachable from your Home Assistant instance
- Gateway credentials (username & password)
- Gateway AES encryption settings (Secret Key & IV — found in the gateway's security settings)
- The gateway must be configured to publish LoRaWAN uplinks to your MQTT broker


---

## Installation

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=corgan2222&repository=milesight_gateway&category=integration)

then 

[![Add Integration to your Home Assistant instance.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=milesight_gateway)


### HACS (Recommended)

1. Open HACS in your Home Assistant sidebar.
2. Go to **Integrations** → click the three-dot menu → **Custom repositories**.
3. Add this repository URL https://github.com/corgan2222/ha-milesight-gateway with category **Integration**.
4. Search for **Milesight Gateway** and install it.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_components/milesight_gateway` folder into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

---

## Configuration

The integration is configured entirely through the UI — no YAML needed.

### Initial Setup

Go to **Settings → Devices & Services → Add Integration** and search for **Milesight Gateway**.

| Field                      | Description                                                           | Default               |
| -------------------------- | --------------------------------------------------------------------- | --------------------- |
| **Gateway URL**            | IP or hostname of the gateway (e.g. `https://192.168.2.180`)         | —                     |
| **Port**                   | Gateway HTTP API port                                                 | `8080`                |
| **Username**               | Gateway login username                                                | —                     |
| **Password**               | Gateway login password                                                | —                     |
| **Secret Key**             | 16-character AES encryption key (from gateway security settings)      | `1111111111111111`    |
| **IV**                     | 16-character AES initialization vector (from gateway security settings) | `2222222222222222`  |
| **MQTT Uplink Base Topic** | Base MQTT topic where the gateway publishes device data               | `sensors/lora/uplink` |

> The gateway URL and port cannot be changed after setup to preserve existing device associations. Use **Reconfigure** to update credentials, Secret Key, IV, or MQTT topic at any time.

### Options

After setup, you can adjust the polling interval:

| Option               | Description                                                          | Default | Min  |
| -------------------- | -------------------------------------------------------------------- | ------- | ---- |
| **Polling Interval** | How often to re-fetch the device list from the gateway (seconds)     | `300`   | `60` |

> This only controls device list refresh. Sensor state updates are pushed via MQTT and are always real-time.

---

## Entities

### Gateway Diagnostic Sensors

These entities represent the gateway itself and are always created:

| Entity              | Description                                    | Unit    |
| ------------------- | ---------------------------------------------- | ------- |
| **Online Devices**  | Number of devices currently online             | count   |
| **Offline Devices** | Number of devices currently offline            | count   |
| **Response Time**   | TCP round-trip time to the gateway API         | ms      |

### Per-Device Sensors

For each discovered LoRaWAN device, entities are created based on the device model's capabilities.

#### Measurement Sensors (`sensor` platform)

| Key           | Description                    | Unit  | Device Class              |
| ------------- | ------------------------------ | ----- | ------------------------- |
| `battery`     | Battery level                  | %     | `battery`                 |
| `temperature` | Temperature                    | °C    | `temperature`             |
| `humidity`    | Relative humidity              | %     | `humidity`                |
| `co2`         | CO₂ concentration              | ppm   | `carbon_dioxide`          |
| `pressure`    | Atmospheric pressure           | hPa   | `atmospheric_pressure`    |
| `rssi`        | Signal strength                | dBm   | `signal_strength`         |
| `loRaSNR`     | LoRa signal-to-noise ratio     | dB    | —                         |

#### Binary Sensors (`binary_sensor` platform)

| Key                          | Description           | Device Class  |
| ---------------------------- | --------------------- | ------------- |
| `occupancy`                  | Room occupancy        | `occupancy`   |
| `pir` / `pir_trigger`        | Motion detection      | `motion`      |
| `door`                       | Door/window contact   | `door`        |
| `liquid` / `leakage_status`  | Water/liquid leakage  | `moisture`    |
| `tamper`                     | Tamper detection      | `tamper`      |

#### Special Sensors

| Entity          | Description                                                   |
| --------------- | ------------------------------------------------------------- |
| **Last Seen**   | Timestamp of the last MQTT message received from the device   |

> Metadata entities (devEUI, serial number, application name, etc.) are created as diagnostic sensors but disabled by default.

---

## Supported Devices

**I could only test a few sensors! Some sensor types may not work, or individual data points may be missing**

The integration includes a built-in database of 130+ Milesight LoRaWAN device models. Device matching is done automatically by devEUI prefix. Supported families include:

- **AM series** — Ambience monitoring (temperature, humidity, CO₂, TVOC, light, pressure)
- **CT series** — CO₂ sensors
- **PT series** — Pressure sensors
- **HR series** — Humidity & temperature sensors
- **PM / PM25 series** — Particulate matter sensors
- **AT series** — Ambient temperature sensors
- Motion, occupancy, door/window, water leakage, and more

If a device's devEUI prefix is not found in the database, it will be skipped (a debug message is logged).

---

## Custom Payload Codec Library

The standard Milesight codecs only expose a subset of available sensor datapoints — metadata like device name, devEUI, gateway, RSSI, SNR, and timestamps are missing from the uplink payload.

This integration is designed to work with a custom **[Payload Codec Library](https://github.com/corgan2222/codec)** that enriches every uplink with the full set of available fields.

**Standard Milesight codec output (before):**

```json
{
  "battery": 96,
  "co2": 421,
  "humidity": 56,
  "light_level": 4,
  "pir": 1,
  "pressure": 1025.4,
  "temperature": 27.8
}
```

**With the custom codec (after):**

```json
{
  "devEUI": "24e124707e111005",
  "deviceName": "Multisensor AM307L",
  "type": "AM307L",
  "gw": "Local Gateway",
  "mac": "24e124fffef8706c",
  "timestamp": "2024-09-17T18:04:35.151882Z",
  "applicationID": 5,
  "applicationName": "cloud",
  "loRaSNR": 10.5,
  "rssi": -89,

  "battery": 96,
  "co2": 421,
  "humidity": 56,
  "light_level": 4,
  "pir": 1,
  "pressure": 1025.4,
  "temperature": 27.8,
  "tvoc": 1
}
```

The enriched payload enables all diagnostic entities (RSSI, SNR, Last Seen, device metadata) and additional sensor fields like `tvoc` that the standard codec omits.

**Installation:** Download a release from [github.com/corgan2222/codec](https://github.com/corgan2222/codec) and install it via the **Payload Codec** configuration page on your Milesight gateway.

---

## Troubleshooting

### Integration fails to set up

- Verify the gateway URL, port, and credentials are correct.
- Confirm the gateway is reachable from your Home Assistant host.
- Check that the Secret Key and IV match the gateway's AES settings.

### No sensor data / entities stuck as unavailable

- Confirm the MQTT integration is running in Home Assistant.
- Verify the MQTT broker is receiving uplink messages from the gateway.
- Check that the **MQTT Uplink Base Topic** matches the topic the gateway publishes to (full topic per device is `{base_topic}/{devEUI}`).

### Devices not appearing

- Only active/online devices are imported. Offline devices are skipped.
- If a device model is not in the built-in database, it will be silently skipped. Enable debug logging for `custom_components.milesight_gateway` to see which devices were not matched.

### Updating credentials

- Use **Settings → Devices & Services → Milesight Gateway → Reconfigure** to update your username, password, Secret Key, IV, or MQTT topic without removing the integration.

---

## Roadmap / ToDo

- **Auto-discover MQTT topic** — read the uplink topic directly from the gateway API instead of requiring manual configuration
- **Downlink / device control** — send configuration commands to devices via MQTT downlink, using the [Milesight remote control API](https://support.milesight-iot.com/support/solutions/articles/73000514114-how-to-remotely-control-devices-via-mqtt-on-milesight-gateway). This would allow adjusting sensor settings (reporting interval, thresholds, etc.) directly from Home Assistant.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to contribute to this project.



## Credits

This integration is built on top of my [Milesight-Gateway-API](https://github.com/corgan2222/Milesight-Gateway-API) Python package, which handles authentication and communication with the Milesight gateway HTTP API.

https://pypi.org/project/Milesight-Gateway-API/

```bash
pip install Milesight-Gateway-API
```

---

## License

This project is licensed under the terms in [LICENSE](LICENSE).
