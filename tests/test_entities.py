"""Tests for how payload keys become entities and how binary values are read."""

from __future__ import annotations

import pytest

from custom_components.milesight_gateway.api import _build_device
from custom_components.milesight_gateway.binary_sensor import is_on


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, False),
        (1, True),
        (2, True),
        ("0", False),
        ("2", True),
        ("2.5", True),
        (True, True),
        (False, False),
        ("on", True),
        ("OCCUPIED", True),
        ("off", False),
        ("", False),
        ("garbage", False),
        ([], False),
    ],
)
def test_is_on(value, expected):
    """Counts above zero are on, words are matched, anything else is off."""
    assert is_on(value) is expected


def test_people_count_gives_occupancy_and_a_count():
    """One payload key can map to a binary sensor and a sensor."""
    device = _build_device(
        {"devEUI": "24E124123456789A", "name": "vs321"},
        {"id": "vs321", "name": "VS321", "device_entities": ["people_total_counts"]},
        "milesight",
    )

    assert [(e.key, e.platform, e.name) for e in device.entities] == [
        ("people_total_counts", "binary_sensor", "Occupancy"),
        ("people_total_counts", "sensor", "People count"),
    ]
