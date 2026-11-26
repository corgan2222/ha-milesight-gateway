"""Tests for how catalog values read when a payload codec already named them."""

from __future__ import annotations

import pytest

from custom_components.milesight_gateway.lorawan_entity import is_set, value_name

DOOR = {"type": "BOOL", "values": {"0": "close", "1": "open"}}
MODE = {"type": "ENUM", "values": {"0": "auto", "1": "manual"}}


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (DOOR, 0, False),
        (DOOR, 1, True),
        (DOOR, "1", True),
        (DOOR, "open", True),
        (DOOR, "Close", False),
        (DOOR, None, None),
        ({}, 2, True),
        ({}, "2.5", True),
        ({}, True, True),
        ({}, False, False),
        ({}, "OCCUPIED", True),
        ({}, "off", False),
        ({}, "", False),
        ({}, "garbage", False),
    ],
)
def test_is_set(field, value, expected):
    """Numbers count unless 0; names and the usual words count as they say."""
    assert is_set(field, value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "auto"), ("1", "manual"), ("manual", "manual"), ("other", None), (None, None)],
)
def test_value_name(value, expected):
    """A raw value gets its name; a name the codec sent stays."""
    assert value_name(MODE, value) == expected
