"""Config flow tests for the Milesight Gateway integration.

Covers the user step, the reconfigure step and the options flow, including the
error-recovery paths and the duplicate-entry guard that the Bronze
``config-flow-test-coverage`` rule asks for.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.milesight_gateway.api import (
    APIAuthError,
    APIConnectionError,
    GatewayDetails,
    GatewayInfo,
    _build_device,
)
from custom_components.milesight_gateway.const import (
    CONF_ALL_SETTINGS,
    CONF_DATA_SOURCE,
    CONF_DEVICES,
    CONF_FIRMWARE_CHECK,
    CONF_GATEWAY_MQTT_BASE_TOPIC,
    CONF_GATEWAY_URL,
    CONF_HOSTNAME,
    CONF_IV,
    CONF_MODEL,
    CONF_MQTT_TOPIC,
    CONF_NETWORK_PANEL,
    CONF_SECRET_KEY,
    CONF_STATUS_INTERVAL,
    CONF_WEB_DATA,
    DOMAIN,
    SOURCE_LORAWAN,
    SOURCE_MQTT,
)

USER_INPUT = {
    CONF_GATEWAY_URL: "https://192.0.2.10",
    CONF_PORT: 8080,
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "secret",
    CONF_SECRET_KEY: "1111111111111111",
    CONF_IV: "2222222222222222",
    CONF_GATEWAY_MQTT_BASE_TOPIC: "sensors/lora/uplink",
}

CONNECT = (
    "custom_components.milesight_gateway.config_flow.MilesightGatewayAPI.async_connect"
)
GET_DEVICES = (
    "custom_components.milesight_gateway.config_flow."
    "MilesightGatewayAPI.async_get_devices"
)
GET_DETAILS = (
    "custom_components.milesight_gateway.config_flow."
    "MilesightGatewayAPI.async_get_gateway_details"
)
GET_INFO = (
    "custom_components.milesight_gateway.config_flow."
    "MilesightGatewayAPI.async_get_gateway_info"
)
# The second step's input: everything but the address, which the first takes.
CREDENTIALS = {k: v for k, v in USER_INPUT.items() if k != CONF_GATEWAY_URL}
THERMOSTAT = _build_device(
    {"devEUI": "24E1247140000001", "name": "radiator"},
    {"id": "wt101", "description": "Wireless Thermostat", "device_entities": []},
    "sensors/lora/uplink",
)
BUTTON = _build_device(
    {"devEUI": "24E1245350000002", "name": "button1"},
    {"id": "ws101", "description": "Smart Button", "device_entities": []},
    "sensors/lora/uplink",
)


@pytest.fixture(autouse=True)
def device_list():
    """The gateway lists no devices, so the setup skips the device choice."""
    with patch(GET_DEVICES, AsyncMock(return_value=([], 0, 0))) as mock:
        yield mock


@pytest.fixture(autouse=True)
def gateway_info():
    """The address answers /islogin as a UG65, unless a test says otherwise."""
    with patch(
        GET_INFO, AsyncMock(return_value=GatewayInfo(model="UG65", firmware="1"))
    ) as mock:
        yield mock


DETAILS = GatewayDetails(model_id="ND65-868M-EA", serial_number=None, hw_version=None)


@pytest.fixture(autouse=True)
def gateway_details():
    """The web interface takes the account and reports no hostname."""
    with patch(GET_DETAILS, AsyncMock(return_value=DETAILS)) as mock:
        yield mock


@pytest.fixture
def mock_setup_entry():
    """Stop the flow from actually setting the integration up."""
    with patch(
        "custom_components.milesight_gateway.async_setup_entry", return_value=True
    ) as mock:
        yield mock


def _entry(**overrides) -> MockConfigEntry:
    data = {**USER_INPUT, **overrides}
    return MockConfigEntry(
        domain=DOMAIN,
        data=data,
        unique_id=f"{data[CONF_GATEWAY_URL]}:{data[CONF_PORT]}",
        version=1,
        minor_version=2,
    )


# --- user step -------------------------------------------------------------


async def _start(hass, url: str = USER_INPUT[CONF_GATEWAY_URL]) -> dict:
    """Enter the address in the first step; returns the next step's result."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GATEWAY_URL: url}
    )


async def test_user_flow_creates_entry(hass, mock_setup_entry):
    """The address, then the login, then the data source creates the entry."""
    result = await _start(hass)
    assert result["step_id"] == "credentials"
    assert result["description_placeholders"] == {"model": "UG65"}

    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
    # No device list to pick from: the step asks for the data source only.
    assert result["step_id"] == "devices"
    assert [str(key) for key in result["data_schema"].schema] == [CONF_DATA_SOURCE]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Milesight UG65 (192.0.2.10)"
    assert result["data"] == {**USER_INPUT, CONF_MODEL: "UG65"}
    # New entries default to the LoRaWAN source.
    assert result["options"] == {CONF_DATA_SOURCE: SOURCE_LORAWAN, CONF_DEVICES: []}
    assert result["result"].unique_id == "https://192.0.2.10:8080"


async def test_the_hostname_names_the_entry(hass, mock_setup_entry, gateway_details):
    """A hostname from the web interface tells the gateways of a fleet apart."""
    gateway_details.return_value = GatewayDetails(
        model_id=None, serial_number=None, hw_version=None, hostname="GW-8"
    )
    result = await _start(hass)
    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["title"] == "GW-8 - UG65 (192.0.2.10)"
    assert result["data"] == {**USER_INPUT, CONF_MODEL: "UG65", CONF_HOSTNAME: "GW-8"}


async def test_the_router_names_a_gateway_without_a_hostname(hass, mock_setup_entry):
    """Without one from the gateway, the DHCP name a router reports is used."""
    hass.states.async_set(
        "device_tracker.router_gw",
        "home",
        {"ip": "192.0.2.10", "host_name": "ms_gw_mini.localdomain"},
    )
    result = await _start(hass)
    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["title"] == "ms_gw_mini - UG65 (192.0.2.10)"
    assert result["data"][CONF_HOSTNAME] == "ms_gw_mini"


async def test_an_unreachable_address_asks_no_login(hass, gateway_info):
    """When /islogin does not answer, the first step says so and stays."""
    gateway_info.side_effect = APIConnectionError("down")

    result = await _start(hass)

    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_an_unknown_model_gets_the_full_form(hass, gateway_info):
    """Without a model name every field is asked, as before the detection."""
    gateway_info.return_value = GatewayInfo(model=None, firmware="1")

    result = await _start(hass)

    assert {str(key) for key in result["data_schema"].schema} == set(CREDENTIALS)


async def _submit_credentials(hass) -> dict:
    result = await _start(hass)
    with patch(CONNECT, AsyncMock(return_value=True)):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )


async def _finish(hass, result: dict) -> dict:
    """Accept the second step's defaults: the data source, all devices."""
    assert result["step_id"] == "devices"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    return result


@pytest.mark.parametrize(
    ("picked", "stored"),
    [({CONF_DEVICES: ["24E1247140000001"]}, ["24E1247140000001"]), ({}, [])],
)
async def test_devices_are_chosen_before_the_entry_exists(
    hass, mock_setup_entry, device_list, picked, stored
):
    """The second step lists the gateway's devices; the choice lands in options."""
    device_list.return_value = ([THERMOSTAT, BUTTON], 2, 0)

    result = await _submit_credentials(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"
    assert result["description_placeholders"] == {"count": "2"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], picked)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**USER_INPUT, CONF_MODEL: "UG65"}
    assert result["options"] == {CONF_DATA_SOURCE: SOURCE_LORAWAN, CONF_DEVICES: stored}


async def test_an_unreadable_device_list_skips_the_choice(
    hass, mock_setup_entry, device_list
):
    """Without a list only the data source is asked; all devices are taken."""
    device_list.side_effect = APIConnectionError("down")

    result = await _submit_credentials(hass)
    assert [str(key) for key in result["data_schema"].schema] == [CONF_DATA_SOURCE]
    result = await _finish(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_DATA_SOURCE: SOURCE_LORAWAN, CONF_DEVICES: []}


async def test_the_mqtt_source_can_be_chosen_at_setup(hass, mock_setup_entry):
    """Who keeps a broker running can stay on MQTT from the start."""
    result = await _submit_credentials(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DATA_SOURCE: SOURCE_MQTT}
    )

    assert result["options"][CONF_DATA_SOURCE] == SOURCE_MQTT


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (APIAuthError("bad credentials"), "invalid_auth"),
        (APIConnectionError("unreachable"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass, mock_setup_entry, side_effect, expected
):
    """Each failure shows its own error and the form still succeeds afterwards."""
    result = await _start(hass)

    with (
        patch(CONNECT, AsyncMock(side_effect=side_effect)),
        patch(GET_DETAILS, AsyncMock(side_effect=APIAuthError("no web login"))),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    assert result["errors"] == {"base": expected}

    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
    result = await _finish(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("typed", ["https://192.0.2.10", "http://192.0.2.10"])
async def test_user_flow_aborts_on_duplicate_address(hass, mock_setup_entry, typed):
    """The same gateway cannot be added twice, by either scheme; no login asked."""
    _entry().add_to_hass(hass)

    result = await _start(hass, typed)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- reconfigure step ------------------------------------------------------


async def test_reconfigure_changes_address(hass, mock_setup_entry):
    """The gateway address can be changed, and the unique id follows it."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    # The address fields are offered at all, which is the point of this change.
    schema_keys = {str(marker) for marker in result["data_schema"].schema}
    assert CONF_GATEWAY_URL in schema_keys
    assert CONF_PORT in schema_keys

    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**USER_INPUT, CONF_GATEWAY_URL: "https://192.0.2.11"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_GATEWAY_URL] == "https://192.0.2.11"
    assert entry.unique_id == "https://192.0.2.11:8080"
    # The title is the user's label, so reconfiguring must not rewrite it.
    assert entry.title == "Mock Title"


async def test_reconfigure_keeps_stored_password_when_left_empty(
    hass, mock_setup_entry
):
    """An empty password field must not wipe the stored password."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    submitted = {**USER_INPUT, CONF_GATEWAY_URL: "https://192.0.2.11"}
    submitted[CONF_PASSWORD] = ""

    with patch(CONNECT, AsyncMock(return_value=True)) as connect:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], submitted
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert entry.data[CONF_PASSWORD] == "secret"
    assert connect.called


async def test_reconfigure_prefills_address_but_not_password(hass, mock_setup_entry):
    """The address comes back prefilled; the stored password never does."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)

    suggested = {
        str(marker): (marker.description or {}).get("suggested_value")
        for marker in result["data_schema"].schema
    }
    assert suggested[CONF_GATEWAY_URL] == "https://192.0.2.10"
    assert suggested[CONF_USERNAME] == "admin"
    assert suggested[CONF_PASSWORD] is None


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (APIAuthError("bad credentials"), "invalid_auth"),
        (APIConnectionError("unreachable"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_reconfigure_errors_then_recovers(
    hass, mock_setup_entry, side_effect, expected
):
    """A failed reconfigure re-shows the form and can then succeed."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)

    with (
        patch(CONNECT, AsyncMock(side_effect=side_effect)),
        patch(GET_DETAILS, AsyncMock(side_effect=APIAuthError("no web login"))),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**USER_INPUT, CONF_GATEWAY_URL: "https://192.0.2.11"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert entry.data[CONF_GATEWAY_URL] == "https://192.0.2.10"
    # The re-shown form must offer back what was typed, not the stored address,
    # or a retry silently reverts the edit and still reports success.
    suggested = {
        str(marker): (marker.description or {}).get("suggested_value")
        for marker in result["data_schema"].schema
    }
    assert suggested[CONF_GATEWAY_URL] == "https://192.0.2.11"

    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**USER_INPUT, CONF_GATEWAY_URL: "https://192.0.2.11"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_GATEWAY_URL] == "https://192.0.2.11"


async def test_reconfigure_aborts_on_an_address_another_entry_uses(
    hass, mock_setup_entry
):
    """Moving a gateway onto an address another entry already has is refused."""
    entry = _entry()
    entry.add_to_hass(hass)
    _entry(gateway_url="https://192.0.2.11").add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**USER_INPUT, CONF_GATEWAY_URL: "https://192.0.2.11"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_GATEWAY_URL] == "https://192.0.2.10"


# --- options flow ----------------------------------------------------------


async def test_options_flow_sets_scan_interval(hass):
    """The polling interval can be changed through the options flow."""
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.milesight_gateway.async_setup_entry", return_value=True
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: 120}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 120


async def test_options_flow_sets_and_clears_the_device_topic(hass):
    """A gateway-only entry gets a topic field; emptied, the topic is gone."""
    entry = _entry(gateway_only=True)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.milesight_gateway.async_setup_entry", return_value=True
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert CONF_MQTT_TOPIC in {str(key) for key in result["data_schema"].schema}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_MQTT_TOPIC: "a/{dev_eui}/#"}
        )
        assert result["errors"] == {CONF_MQTT_TOPIC: "invalid_topic"}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_MQTT_TOPIC: "a/{dev_eui}"}
        )
        await hass.async_block_till_done()
        assert entry.options[CONF_MQTT_TOPIC] == "a/{dev_eui}"

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_MQTT_TOPIC] == ""


async def test_options_flow_sets_status_and_sources(hass):
    """The status interval, the outside sources and the data source can be set."""
    entry = _entry()
    entry.add_to_hass(hass)
    options = {
        CONF_SCAN_INTERVAL: 300,
        CONF_STATUS_INTERVAL: 90,
        CONF_WEB_DATA: False,
        CONF_FIRMWARE_CHECK: False,
        CONF_DATA_SOURCE: SOURCE_LORAWAN,
        CONF_ALL_SETTINGS: True,
        CONF_NETWORK_PANEL: False,
    }

    with patch(
        "custom_components.milesight_gateway.async_setup_entry", return_value=True
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], options
        )
        # A change of source asks first, saying what stays and what goes.
        assert result["step_id"] == "confirm_source"
        assert result["description_placeholders"] == {"old": "MQTT", "new": "LoRaWAN"}
        assert entry.options == {}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == options


async def test_the_same_source_needs_no_confirmation(hass):
    """Saving without a change of source saves at once."""
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(
        "custom_components.milesight_gateway.async_setup_entry", return_value=True
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_DATA_SOURCE: SOURCE_MQTT}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_gateway_only_entry_has_no_device_options(hass):
    """No device list and no network server: no source, no settings, no choice."""
    entry = _entry(gateway_only=True)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.milesight_gateway.async_setup_entry", return_value=True
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        result = await hass.config_entries.options.async_init(entry.entry_id)

    fields = {str(key) for key in result["data_schema"].schema}
    assert not fields & {CONF_DATA_SOURCE, CONF_ALL_SETTINGS, CONF_DEVICES}
    assert CONF_MQTT_TOPIC in fields


async def test_an_older_entry_keeps_its_behaviour(hass):
    """Without the new options, status follows the poll interval and all is on."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={CONF_SCAN_INTERVAL: 120})

    with patch(
        "custom_components.milesight_gateway.async_setup_entry", return_value=True
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_STATUS_INTERVAL] == 120
    assert entry.options[CONF_WEB_DATA] is True
    assert entry.options[CONF_FIRMWARE_CHECK] is True


def _loaded_with(hass, entry: MockConfigEntry, devices: list) -> None:
    """Make the entry look loaded, with the gateway listing ``devices``."""
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = SimpleNamespace(
        coordinator=SimpleNamespace(data=SimpleNamespace(matched=devices))
    )


def _choices(result) -> list[str]:
    field = next(
        key for key in result["data_schema"].schema if key.schema == CONF_DEVICES
    )
    selector = result["data_schema"].schema[field]
    return [option["value"] for option in selector.config["options"]]


@pytest.mark.parametrize(
    ("submitted", "stored"),
    [
        ({CONF_DEVICES: ["24E1245350000002"]}, ["24E1245350000002"]),
        # An emptied list may arrive without the key; it still means "all".
        ({}, []),
    ],
)
async def test_the_device_choice_can_be_changed(hass, submitted, stored):
    """The options list the gateway's devices and keep, change or clear a pick."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_DEVICES: ["24E1247140000001", "24E1249990000009"]}
    )
    _loaded_with(hass, entry, [THERMOSTAT, BUTTON])

    with patch(
        "custom_components.milesight_gateway.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        # A picked device the gateway no longer lists stays choosable.
        assert _choices(result) == [
            "24E1247140000001",
            "24E1245350000002",
            "24E1249990000009",
        ]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], submitted
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_DEVICES] == stored


async def test_an_unloaded_entry_keeps_its_device_choice(hass):
    """Without the gateway's list the choice is not shown and not touched."""
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_DEVICES: ["24E1247140000001"]}
    )

    with patch(
        "custom_components.milesight_gateway.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert CONF_DEVICES not in {key.schema for key in result["data_schema"].schema}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        await hass.async_block_till_done()

    assert entry.options[CONF_DEVICES] == ["24E1247140000001"]


# --- reauth flow -----------------------------------------------------------


async def test_reauth_updates_credentials_only(hass, mock_setup_entry):
    """Reauth replaces the credentials and leaves the address alone."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    # The address is not part of this form; reconfigure owns that.
    schema_keys = {str(marker) for marker in result["data_schema"].schema}
    assert CONF_GATEWAY_URL not in schema_keys
    assert schema_keys == {CONF_USERNAME, CONF_PASSWORD, CONF_SECRET_KEY, CONF_IV}

    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_USERNAME: "admin",
                CONF_PASSWORD: "rotated",
                CONF_SECRET_KEY: "1111111111111111",
                CONF_IV: "2222222222222222",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "rotated"
    assert entry.data[CONF_GATEWAY_URL] == "https://192.0.2.10"


async def test_reauth_does_not_prefill_the_password(hass, mock_setup_entry):
    """The rejected password is not offered back to the user."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    suggested = {
        str(marker): (marker.description or {}).get("suggested_value")
        for marker in result["data_schema"].schema
    }
    assert suggested[CONF_USERNAME] == "admin"
    assert suggested[CONF_PASSWORD] is None


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (APIAuthError("still wrong"), "invalid_auth"),
        (APIConnectionError("unreachable"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_reauth_errors_then_recovers(
    hass, mock_setup_entry, side_effect, expected
):
    """A rejected retype re-shows the form and can then succeed."""
    entry = _entry()
    entry.add_to_hass(hass)
    creds = {
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "rotated",
        CONF_SECRET_KEY: "1111111111111111",
        CONF_IV: "2222222222222222",
    }

    result = await entry.start_reauth_flow(hass)

    with (
        patch(CONNECT, AsyncMock(side_effect=side_effect)),
        patch(GET_DETAILS, AsyncMock(side_effect=APIAuthError("no web login"))),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], creds
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert entry.data[CONF_PASSWORD] == "secret"

    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], creds
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "rotated"


# --- URL normalization -----------------------------------------------------


@pytest.mark.parametrize(
    "typed",
    ["https://192.0.2.10/", "  https://192.0.2.10  ", "https://192.0.2.10//"],
)
async def test_user_flow_normalizes_the_url(hass, mock_setup_entry, typed):
    """A trailing slash breaks the client's URL building, so it is stripped."""
    result = await _start(hass, typed)
    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
    result = await _finish(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GATEWAY_URL] == "https://192.0.2.10"


async def test_reconfigure_normalizes_the_url_and_still_dedupes(hass, mock_setup_entry):
    """A trailing-slash retype of the stored address is not a new address."""
    entry = _entry()
    entry.add_to_hass(hass)
    _entry(gateway_url="https://192.0.2.11").add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {**USER_INPUT, CONF_GATEWAY_URL: "https://192.0.2.11/"},
        )

    # Normalized before comparing, so the collision with the other entry is seen.
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- URL validation --------------------------------------------------------

BAD_URLS = [
    ("192.0.2.10", "missing_scheme"),
    ("192.0.2.10:8080", "missing_scheme"),
    ("ftp://192.0.2.10", "missing_scheme"),
    ("https://192.0.2.10:8080", "url_with_port"),
    ("http://192.0.2.10:80", "url_with_port"),
    ("https://192.0.2.10:port", "invalid_url"),
    ("https://", "invalid_url"),
    ("https://admin:secret@192.0.2.10", "invalid_url"),
    ("https://192.0.2.10/api", "invalid_url"),
    ("https://192.0.2.10?x=1", "invalid_url"),
]


@pytest.mark.parametrize(("typed", "expected"), BAD_URLS)
async def test_user_flow_explains_an_unusable_url(
    hass, mock_setup_entry, gateway_info, typed, expected
):
    """An unusable URL is named at the field, without asking the gateway."""
    result = await _start(hass, typed)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_GATEWAY_URL: expected}
    gateway_info.assert_not_awaited()

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_GATEWAY_URL: USER_INPUT[CONF_GATEWAY_URL]}
    )
    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
        result = await _finish(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("typed", ["http://192.0.2.10", "https://gateway.local"])
async def test_user_flow_accepts_http_and_host_names(hass, mock_setup_entry, typed):
    """Both schemes and host names are fine."""
    result = await _start(hass, typed)
    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
        result = await _finish(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GATEWAY_URL] == typed


@pytest.mark.parametrize(("typed", "expected"), BAD_URLS[:4])
async def test_reconfigure_explains_an_unusable_url(
    hass, mock_setup_entry, typed, expected
):
    """Reconfigure names the URL problem too and keeps the stored address."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    connect = AsyncMock(return_value=True)
    with patch(CONNECT, connect):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_GATEWAY_URL: typed}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_GATEWAY_URL: expected}
    connect.assert_not_awaited()
    assert entry.data[CONF_GATEWAY_URL] == "https://192.0.2.10"


# --- gateway-only (UG63 Mini) and DHCP discovery ---------------------------

from homeassistant.config_entries import SOURCE_DHCP, SOURCE_IGNORE  # noqa: E402
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo  # noqa: E402

from custom_components.milesight_gateway.const import (  # noqa: E402
    CONF_GATEWAY_ONLY,
    CONF_MAC,
)

MAC = "24:e1:24:00:00:01"
DISCOVERY = DhcpServiceInfo(
    ip="192.0.2.10", hostname="milesight-gateway", macaddress="24e124000001"
)
WEB_LOGIN = {CONF_USERNAME: "admin", CONF_PASSWORD: "secret"}


async def test_gateway_only_when_only_the_web_login_works(hass, mock_setup_entry):
    """A gateway that takes the web login but not the network server one."""
    result = await _start(hass)
    with (
        patch(CONNECT, AsyncMock(side_effect=APIAuthError("no ns login"))),
        patch(GET_DETAILS, AsyncMock(return_value=DETAILS)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
        await hass.async_block_till_done()

    # No device step: the entry is created straight away, marked gateway-only.
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**USER_INPUT, CONF_MODEL: "UG65", CONF_GATEWAY_ONLY: True}
    assert result["options"] == {}


async def test_a_wrong_password_stays_invalid_auth(hass, mock_setup_entry):
    """When the web login is refused too, it is a bad login, not gateway-only."""
    result = await _start(hass)
    with (
        patch(CONNECT, AsyncMock(side_effect=APIAuthError("bad"))),
        patch(GET_DETAILS, AsyncMock(side_effect=APIAuthError("bad"))),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_a_ug63_is_asked_only_for_its_web_login(
    hass, mock_setup_entry, gateway_info
):
    """The Mini gets user name and password, and no network server login."""
    gateway_info.return_value = GatewayInfo(model="UG63", firmware="63.0.0.2-r2")

    result = await _start(hass, "http://192.0.2.10")
    # Its own step, whose text says that the devices are not shown.
    assert result["step_id"] == "web_login"
    assert {str(key) for key in result["data_schema"].schema} == {
        *WEB_LOGIN,
        CONF_MQTT_TOPIC,
    }

    connect = AsyncMock(return_value=True)
    with (
        patch(CONNECT, connect),
        patch(GET_DETAILS, AsyncMock(return_value=DETAILS)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEB_LOGIN
        )
        await hass.async_block_till_done()

    connect.assert_not_called()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Milesight UG63 (192.0.2.10)"
    assert result["data"][CONF_GATEWAY_ONLY] is True
    assert result["data"][CONF_USERNAME] == "admin"
    assert result["options"] == {}


async def test_a_ug63_takes_the_topic_of_its_devices(
    hass, mock_setup_entry, gateway_info
):
    """The topic goes into the options, where it can be changed later."""
    gateway_info.return_value = GatewayInfo(model="UG63", firmware="63.0.0.2-r2")
    result = await _start(hass, "http://192.0.2.10")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**WEB_LOGIN, CONF_MQTT_TOPIC: " lora/{x}/{dev_eui} "}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_MQTT_TOPIC not in result["data"]
    assert result["options"] == {CONF_MQTT_TOPIC: "lora/{x}/{dev_eui}"}


async def test_a_wrong_topic_costs_no_login(hass, gateway_info, gateway_details):
    """The topic is checked first: a failed login would count toward the lock."""
    gateway_info.return_value = GatewayInfo(model="UG63", firmware="63.0.0.2-r2")
    result = await _start(hass, "http://192.0.2.10")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**WEB_LOGIN, CONF_MQTT_TOPIC: "lora/#"}
    )

    assert result["errors"] == {CONF_MQTT_TOPIC: "invalid_topic"}
    gateway_details.assert_not_called()


async def test_a_ug63_on_newer_firmware_gets_the_full_form(hass, gateway_info):
    """The external-antenna UG63 runs 64.x; the firmware decides, not the name."""
    gateway_info.return_value = GatewayInfo(model="UG63", firmware="64.0.0.5")

    result = await _start(hass)

    assert result["step_id"] == "credentials"
    assert {str(key) for key in result["data_schema"].schema} == set(CREDENTIALS)


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (APIAuthError("bad"), "invalid_auth"),
        (APIConnectionError("down"), "cannot_connect"),
    ],
)
async def test_a_ug63_login_error_never_tries_the_network_server(
    hass, gateway_info, side_effect, expected
):
    """A refused web login is the answer; the network server is not asked."""
    gateway_info.return_value = GatewayInfo(model="UG63", firmware="63.0.0.2-r2")
    result = await _start(hass)

    connect = AsyncMock(return_value=True)
    with (
        patch(CONNECT, connect),
        patch(GET_DETAILS, AsyncMock(side_effect=side_effect)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], WEB_LOGIN
        )

    connect.assert_not_called()
    assert result["errors"] == {"base": expected}


async def test_a_gateway_only_reauth_asks_for_the_web_login(hass, mock_setup_entry):
    """Reauth of a Mini neither shows the AES fields nor logs in to its NS."""
    entry = _entry(gateway_only=True)
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert {str(key) for key in result["data_schema"].schema} == set(WEB_LOGIN)

    connect = AsyncMock(return_value=True)
    with (
        patch(CONNECT, connect),
        patch(GET_DETAILS, AsyncMock(return_value=DETAILS)),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**WEB_LOGIN, CONF_PASSWORD: "rotated"}
        )
        await hass.async_block_till_done()

    connect.assert_not_called()
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "rotated"


async def test_dhcp_discovery_confirms_then_asks_for_the_login(hass, mock_setup_entry):
    """A new gateway is confirmed with its model, then set up under its MAC."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"] == {
        "model": "UG65",
        "host": "192.0.2.10",
    }

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "credentials"

    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
    result = await _finish(hass, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_GATEWAY_URL] == "http://192.0.2.10"
    assert result["data"][CONF_MAC] == MAC
    assert result["result"].unique_id == MAC


async def test_dhcp_discovery_moves_a_known_gateway_to_its_new_address(hass):
    """A gateway known by its MAC takes the new IP and keeps its scheme."""
    entry = _entry(mac=MAC)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id=MAC)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data=DhcpServiceInfo(
            ip="192.0.2.99", hostname="gateway", macaddress="24e124000001"
        ),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_GATEWAY_URL] == "https://192.0.2.99"


async def test_dhcp_discovery_gives_a_hand_added_gateway_its_mac(hass):
    """An entry found at the discovered address takes the MAC as unique id."""
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.unique_id == MAC
    assert entry.data[CONF_MAC] == MAC
    assert entry.data[CONF_GATEWAY_URL] == "https://192.0.2.10"


async def test_dhcp_discovery_brings_the_hostname_and_the_title_follows(hass):
    """The DHCP name names an entry that had none, if its title is still the setup's."""
    entry = _entry(**{CONF_MODEL: "UG63"})
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, title="Milesight UG63 (192.0.2.10)")

    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )

    assert entry.data[CONF_HOSTNAME] == "milesight-gateway"
    assert entry.title == "milesight-gateway - UG63 (192.0.2.10)"


async def test_dhcp_discovery_respects_an_ignored_gateway(hass, gateway_info):
    """A discovery the user ignored is not offered again."""
    MockConfigEntry(domain=DOMAIN, source=SOURCE_IGNORE, unique_id=MAC).add_to_hass(
        hass
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    gateway_info.assert_not_awaited()


async def test_dhcp_discovery_aborts_when_the_gateway_does_not_answer(
    hass, gateway_info
):
    """A device that does not answer /islogin is not offered."""
    gateway_info.side_effect = APIConnectionError("down")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DISCOVERY
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_reconfigure_keeps_a_mac_unique_id(hass, mock_setup_entry):
    """A MAC unique id does not fall back to the address on reconfigure."""
    entry = _entry(mac=MAC)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, unique_id=MAC)

    result = await entry.start_reconfigure_flow(hass)
    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_GATEWAY_URL: "https://192.0.2.11"}
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == MAC
