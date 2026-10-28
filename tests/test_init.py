"""Tests for the config entry migration.

The migration rewrites the gateway device's registry identifier, which is the
riskiest part of making the gateway address editable: get it wrong on an
existing install and the gateway device is duplicated and its child devices
lose their ``via_device`` link.
"""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.milesight_gateway import (
    ENTRY_MINOR_VERSION,
    async_migrate_entry,
)
from custom_components.milesight_gateway.const import (
    CONF_GATEWAY_MQTT_BASE_TOPIC,
    CONF_GATEWAY_URL,
    CONF_IV,
    CONF_SECRET_KEY,
    DOMAIN,
)

ENTRY_DATA = {
    CONF_GATEWAY_URL: "https://192.168.2.180",
    CONF_PORT: 8080,
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "secret",
    CONF_SECRET_KEY: "1111111111111111",
    CONF_IV: "2222222222222222",
    CONF_GATEWAY_MQTT_BASE_TOPIC: "sensors/lora/uplink",
}

LEGACY_ID = "https://192.168.2.180:8080"


def _entry(minor_version: int = 1) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        unique_id=LEGACY_ID,
        version=1,
        minor_version=minor_version,
    )


async def test_migration_moves_the_gateway_device_to_the_entry_id(hass):
    """The address-derived identifier is rewritten, keeping the same device row."""
    entry = _entry()
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)

    gateway = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, LEGACY_ID)},
        name="Milesight Gateway (https://192.168.2.180)",
    )
    # A child device pointing at the gateway, as the platforms create them.
    child = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "24e124725e123456")},
        name="AM307",
        via_device=(DOMAIN, LEGACY_ID),
    )

    assert await async_migrate_entry(hass, entry)

    migrated = registry.async_get(gateway.id)
    assert migrated is not None
    # Same row, new identifier — not a second device.
    assert migrated.identifiers == {(DOMAIN, entry.entry_id)}
    assert registry.async_get_device(identifiers={(DOMAIN, LEGACY_ID)}) is None
    assert entry.minor_version == ENTRY_MINOR_VERSION

    # The child keeps pointing at the same gateway row.
    assert registry.async_get(child.id).via_device_id == gateway.id


async def test_migration_without_an_existing_device(hass):
    """A fresh install has no legacy device; the migration still completes."""
    entry = _entry()
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == ENTRY_MINOR_VERSION


async def test_migration_is_a_noop_when_already_current(hass):
    """An already-migrated entry is left alone, including its device."""
    entry = _entry(minor_version=ENTRY_MINOR_VERSION)
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)

    # A device still on the legacy identifier must not be touched, because the
    # entry claims to be migrated already.
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, LEGACY_ID)},
        name="left alone",
    )

    assert await async_migrate_entry(hass, entry)
    assert registry.async_get(stale.id).identifiers == {(DOMAIN, LEGACY_ID)}
    assert entry.minor_version == ENTRY_MINOR_VERSION
