"""Smoke tests for the Milesight Gateway integration.

These validate the integration's static metadata and the bundled device
database without requiring a running Home Assistant instance, so they give a
fast, reliable CI baseline. Build behaviour tests (config flow, coordinator,
sensors) on top of this scaffold using the
``pytest-homeassistant-custom-component`` fixtures provided in ``conftest.py``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

INTEGRATION_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "milesight_gateway"
)


def _load_const():
    """Load ``const.py`` standalone (no Home Assistant import required)."""
    spec = importlib.util.spec_from_file_location(
        "milesight_gateway_const", INTEGRATION_DIR / "const.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict:
    return json.loads((INTEGRATION_DIR / "manifest.json").read_text("utf-8"))


def test_manifest_matches_domain():
    """manifest domain, const.DOMAIN and the package name all agree."""
    assert _manifest()["domain"] == _load_const().DOMAIN == "milesight_gateway"


def test_manifest_has_version_and_requirements():
    """HACS/hassfest require a version and a (possibly empty) requirements list."""
    manifest = _manifest()
    assert manifest.get("version"), "manifest.json must declare a version"
    assert "requirements" in manifest, "manifest.json must declare requirements"


def test_device_database_is_valid_json():
    """The bundled device database parses and is non-empty."""
    data = json.loads((INTEGRATION_DIR / "devices_ha.json").read_text("utf-8"))
    assert data, "devices_ha.json must not be empty"
