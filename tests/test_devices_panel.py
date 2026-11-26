"""Tests for the device page's commands: list, preview, send and log."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from custom_components.milesight_gateway.api import MilesightGatewayAPI

from .test_device_control import (  # noqa: F401  fixtures, found by their own names
    BUTTON_DEVICE,
    BUTTON_EUI,
    COUNTER_DEVICE,
    THERMOSTAT_DEVICE,
    THERMOSTAT_EUI,
    _publish,
    devices,
    entry,
    expected_lingering_timers,
    gateway_topics,
)


async def _ws(client, command: str, **data):
    await client.send_json_auto_id({"type": f"milesight_gateway/{command}", **data})
    reply = await client.receive_json()
    assert reply["success"], reply
    return reply["result"]


async def test_the_page_lists_devices_and_their_models(hass, entry, hass_ws_client):
    """Every device with its gateway, and each model's settings once."""
    client = await hass_ws_client(hass)

    result = await _ws(client, "devices")

    by_eui = {d["dev_eui"]: d for d in result["devices"]}
    assert set(by_eui) >= {THERMOSTAT_EUI, BUTTON_EUI}
    thermostat = by_eui[THERMOSTAT_EUI]
    assert thermostat["sends"] is True
    assert thermostat["active"] is True
    # For the links to the device's own page and to its gateway's.
    assert thermostat["device_id"]
    assert thermostat["gateway_device_id"] == entry.runtime_data.gateway_device_id
    assert thermostat["heard_by"] == []
    assert thermostat["model_id"] == "wt101"
    settings = result["models"]["wt101"]["settings"]
    assert settings["report_interval"]["unit"] == "min"
    # Milesight's factory value, where the catalog has one.
    assert settings["report_interval"]["default"] == "10"
    assert "reboot" in result["models"]["wt101"]["commands"]


async def test_preview_send_and_log(hass, entry, hass_ws_client):
    """The bytes first, then the downlink, then its record in the log."""
    client = await hass_ws_client(hass)
    item = {
        "entry_id": entry.entry_id,
        "dev_eui": THERMOSTAT_EUI,
        "changes": {"report_interval": 1},
    }

    preview = await _ws(client, "preview", items=[item])
    assert preview["results"][0]["hex"]

    queue = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", queue):
        sent = await _ws(client, "send", items=[item, {**item, "dev_eui": "0" * 16}])
    assert sent["results"][0] == {"dev_eui": THERMOSTAT_EUI, "ok": True}
    assert "error" in sent["results"][1]
    assert bytes.hex(queue.await_args.args[2]) == preview["results"][0]["hex"]

    log = await _ws(client, "log")
    [record] = log["records"]
    assert record["dev_eui"] == THERMOSTAT_EUI
    assert record["command"] == {"report_interval": 1}
    assert record["state"] == "pending"
    # The setting is kept, as sent.
    result = await _ws(client, "devices")
    thermostat = next(d for d in result["devices"] if d["dev_eui"] == THERMOSTAT_EUI)
    assert thermostat["settings"]["report_interval"]["src"] == "sent"


CHOICES = {
    "/api/urapplications?limit=100&offset=0&organizationID=0": {
        "result": [{"id": "5", "name": "cloud_home"}]
    },
    "/api/urprofiles?organizationID=0&offset=0&limit=100": {
        "result": [
            {"profile": {"profileID": "p-0", "supportsJoin": True}, "name": "spare"},
            {
                "profile": {
                    "profileID": "p-1",
                    "supportsJoin": True,
                    "rfRegion": "EU868",
                },
                "name": "cloud",
                "using": True,
            },
        ]
    },
    "/api/payloadcodecs-short": {
        "result": [{"id": "1513", "name": "WT101-1.5.72"}, {"id": "7", "name": "VS340"}]
    },
}


async def test_devices_are_created_on_the_gateway(hass, entry, hass_ws_client):
    """Choices from the gateway; each row becomes a device with its model's codec."""
    posted = []

    async def call(_session, method, path, body=None):
        if method == "POST":
            posted.append(body)
            return {"code": 200, "error": ""}
        return CHOICES[path]

    client = await hass_ws_client(hass)
    with patch.object(MilesightGatewayAPI, "async_ns_call", side_effect=call):
        choices = await _ws(client, "import_choices", entry_id=entry.entry_id)
        assert choices["profiles"] == [
            {"id": "p-1", "name": "cloud (class A, OTAA, EU868)"},
            {"id": "p-0", "name": "spare (class A, OTAA)"},
        ]
        result = await _ws(
            client,
            "import",
            entry_id=entry.entry_id,
            application_id="5",
            profile_id="p-1",
            codec_id="auto",
            rows=[
                {"dev_eui": "24E1247140000099", "name": "radiator 2"},
                {"dev_eui": "not an eui"},
            ],
        )

    assert result["results"][0] == {
        "dev_eui": "24E1247140000099",
        "ok": True,
        "codec": "1513",
    }
    assert "error" in result["results"][1]
    [body] = posted
    assert body["devEUI"] == "24e1247140000099"
    assert body["applicationID"] == "5"
    assert body["payloadCodecID"] == "1513"
    # Without an AppKey, Milesight's factory key.
    assert len(body["appKey"]) == 32


async def test_a_device_that_reports_is_online_whatever_the_gateway_says(
    hass, entry, devices, hass_ws_client
):
    """Another gateway may publish for it on the same topics; its uplink counts."""
    from dataclasses import replace

    silent = replace(BUTTON_DEVICE, online=False)
    devices.return_value = ([THERMOSTAT_DEVICE, silent, COUNTER_DEVICE], 2, 1)
    with patch.object(MilesightGatewayAPI, "async_get_devices", devices):
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()
    client = await hass_ws_client(hass)

    result = await _ws(client, "devices")
    button = next(d for d in result["devices"] if d["dev_eui"] == BUTTON_EUI)
    assert button["active"] is False

    await _publish(
        hass,
        BUTTON_EUI,
        '{"battery": 90, "rssi": -80, "loRaSNR": 5, "mac": "24e124fffe000001"}',
    )
    result = await _ws(client, "devices")
    button = next(d for d in result["devices"] if d["dev_eui"] == BUTTON_EUI)
    assert button["active"] is True
    # Which gateway it came through; one no entry knows keeps its EUI only.
    [heard] = button["heard_by"]
    assert heard["eui"] == "24E124FFFE000001"
    assert heard["rssi"] == -80
    assert heard["name"] is None


async def test_one_entry_can_take_the_page_out_of_the_sidebar(hass):
    """One menu entry for all gateways; off at one of them removes it."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.milesight_gateway.const import CONF_DEVICES_PANEL, DOMAIN
    from custom_components.milesight_gateway.devices_panel import show_in_sidebar

    MockConfigEntry(domain=DOMAIN, data={}).add_to_hass(hass)
    assert show_in_sidebar(hass) is True

    MockConfigEntry(
        domain=DOMAIN, data={}, options={CONF_DEVICES_PANEL: False}
    ).add_to_hass(hass)
    assert show_in_sidebar(hass) is False


async def test_a_value_outside_the_range_is_refused(hass, entry, hass_ws_client):
    """The bytes would go out; the device would keep its old value."""
    client = await hass_ws_client(hass)
    item = {
        "entry_id": entry.entry_id,
        "dev_eui": THERMOSTAT_EUI,
        "changes": {"report_interval": 2000},
    }

    preview = await _ws(client, "preview", items=[item])
    assert "between 1 and 1440 min" in preview["results"][0]["error"]

    queue = AsyncMock()
    with patch.object(MilesightGatewayAPI, "async_enqueue_downlink", queue):
        sent = await _ws(client, "send", items=[item])
    assert "error" in sent["results"][0]
    queue.assert_not_awaited()


async def test_gateways_are_named_from_the_entries(hass, entry):
    """The entry's own gateway by its title, a hub's fleet by its names."""
    from types import SimpleNamespace

    from custom_components.milesight_gateway.devices_panel import _known_gateways

    entry.runtime_data.diagnostics.data = SimpleNamespace(
        network=SimpleNamespace(
            gateway_eui="24e124fffe000002",
            gateways=[SimpleNamespace(eui="24e124fffe000001", name="Hall")],
        )
    )

    known = _known_gateways(hass)

    assert known["24E124FFFE000001"] == {"name": "Hall", "device_id": None}
    assert known["24E124FFFE000002"] == {
        "name": entry.title,
        "device_id": entry.runtime_data.gateway_device_id,
    }


def test_the_built_in_codec_is_found_by_its_name_only():
    """A codec the catalog does not know matches no model, whatever the devEUI."""
    from custom_components.milesight_gateway.importer import _auto_codec

    codecs = [{"id": "5", "name": "My custom decoder"}, {"id": "12", "name": "WT101"}]

    assert _auto_codec(THERMOSTAT_EUI, codecs) == "12"
    assert _auto_codec(THERMOSTAT_EUI, codecs[:1]) == "0"


async def test_a_reading_that_is_no_number_does_not_reach_the_page(
    hass, entry, hass_ws_client
):
    """MQTT payloads are foreign JSON; the page writes readings into HTML."""
    await _publish(hass, BUTTON_EUI, '{"battery": "<img src=x onerror=alert(1)>"}')
    client = await hass_ws_client(hass)

    result = await _ws(client, "devices")

    button = next(d for d in result["devices"] if d["dev_eui"] == BUTTON_EUI)
    assert button["battery"] is None


async def test_what_is_kept_is_written_when_the_entry_unloads(hass, entry):
    """A delayed write would be lost: the next setup reads the file again."""
    journal = entry.runtime_data.journal
    journal.add(THERMOSTAT_EUI, "command", command={"report_interval": 2})
    journal.saved()

    with patch.object(type(journal._store), "async_save") as save:
        assert await hass.config_entries.async_unload(entry.entry_id)

    assert any(
        call.args and call.args[0] is journal.records for call in save.call_args_list
    )
