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
    _build_device,
)
from custom_components.milesight_gateway.const import (
    CONF_DEVICES,
    CONF_FIRMWARE_CHECK,
    CONF_GATEWAY_MQTT_BASE_TOPIC,
    CONF_GATEWAY_URL,
    CONF_IV,
    CONF_SECRET_KEY,
    CONF_STATUS_INTERVAL,
    CONF_WEB_DATA,
    DOMAIN,
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


async def test_user_flow_creates_entry(hass, mock_setup_entry):
    """A valid submission creates an entry titled after the gateway URL."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "https://192.0.2.10"
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == "https://192.0.2.10:8080"


async def _submit_credentials(hass) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(CONNECT, AsyncMock(return_value=True)):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )


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
    assert result["data"] == USER_INPUT
    assert result["options"] == {CONF_DEVICES: stored}


async def test_an_unreadable_device_list_skips_the_choice(
    hass, mock_setup_entry, device_list
):
    """Without a list the entry takes all devices instead of failing."""
    device_list.side_effect = APIConnectionError("down")

    result = await _submit_credentials(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {}


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
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch(CONNECT, AsyncMock(side_effect=side_effect)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_aborts_on_duplicate_address(hass, mock_setup_entry):
    """The same gateway address cannot be added twice."""
    _entry().add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

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

    with patch(CONNECT, AsyncMock(side_effect=side_effect)):
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


async def test_options_flow_sets_status_and_sources(hass):
    """The status interval and both outside sources can be set."""
    entry = _entry()
    entry.add_to_hass(hass)
    options = {
        CONF_SCAN_INTERVAL: 300,
        CONF_STATUS_INTERVAL: 90,
        CONF_WEB_DATA: False,
        CONF_FIRMWARE_CHECK: False,
    }

    with patch(
        "custom_components.milesight_gateway.async_setup_entry", return_value=True
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], options
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == options


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

    with patch(CONNECT, AsyncMock(side_effect=side_effect)):
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
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_GATEWAY_URL: typed}
        )
        await hass.async_block_till_done()

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
    hass, mock_setup_entry, typed, expected
):
    """An unusable URL is named at the field, without trying to connect."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    connect = AsyncMock(return_value=True)
    with patch(CONNECT, connect):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_GATEWAY_URL: typed}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_GATEWAY_URL: expected}
    connect.assert_not_awaited()

    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("typed", ["http://192.0.2.10", "https://gateway.local"])
async def test_user_flow_accepts_http_and_host_names(hass, mock_setup_entry, typed):
    """Both schemes and host names are fine."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(CONNECT, AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_GATEWAY_URL: typed}
        )
        await hass.async_block_till_done()

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
