"""Regression tests for the const.py dose/label preset mappings.

These are the reverse-engineered wire values the firmware accepts, and the
exact UI labels the Aiper app shows for them:

    waterYield  0.1 / 0.25 / 0.5   <->  "3 mm" / "6 mm" / "13 mm"
    point_time  1   / 5    / 10    <->  "1 min" / "5 min" / "10 min"

The firmware silently drops off-preset values, so both directions of the map
(label -> wire, wire -> label) are load-bearing. Pin them exactly.
"""
from __future__ import annotations

import pytest

from custom_components.aiper_irrisense import const
from custom_components.aiper_irrisense.const import (
    NOZZLE_SERVER_TO_DEVICE,
    NOZZLE_TYPE_JET,
    NOZZLE_TYPE_LABELS,
    NOZZLE_TYPE_STANDARD,
    POINT_TIME_HIGH,
    POINT_TIME_LOW,
    POINT_TIME_MEDIUM,
    POINT_TIME_PRESETS,
    REGION_TYPE_AREA,
    REGION_TYPE_LINE,
    REGION_TYPE_POINT,
    WATER_YIELD_HIGH,
    WATER_YIELD_LOW,
    WATER_YIELD_MEDIUM,
    WATER_YIELD_PRESETS,
    default_dose_label_for_region_type,
    dose_options_for_region_type,
    label_for_point_time,
    label_for_water_yield,
    parse_dose_label,
)


# --------------------------------------------------------------------------- #
# The raw preset constants — wire values the firmware accepts
# --------------------------------------------------------------------------- #


def test_water_yield_presets_pinned() -> None:
    assert (WATER_YIELD_LOW, WATER_YIELD_MEDIUM, WATER_YIELD_HIGH) == (0.1, 0.25, 0.5)
    assert WATER_YIELD_PRESETS == (0.1, 0.25, 0.5)


def test_point_time_presets_pinned() -> None:
    assert (POINT_TIME_LOW, POINT_TIME_MEDIUM, POINT_TIME_HIGH) == (1, 5, 10)
    assert POINT_TIME_PRESETS == (1, 5, 10)


def test_region_type_enum_pinned() -> None:
    assert (REGION_TYPE_AREA, REGION_TYPE_LINE, REGION_TYPE_POINT) == (0, 1, 2)


def test_water_yield_labels_pinned() -> None:
    assert const.WATER_YIELD_LABELS == {0.1: "3 mm", 0.25: "6 mm", 0.5: "13 mm"}


def test_point_time_labels_pinned() -> None:
    assert const.POINT_TIME_LABELS == {1: "1 min", 5: "5 min", 10: "10 min"}


def test_default_dose_labels_pinned() -> None:
    assert const.DEFAULT_WATER_YIELD_LABEL == "3 mm"
    assert const.DEFAULT_POINT_TIME_LABEL == "1 min"


# --------------------------------------------------------------------------- #
# dose_options_for_region_type
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rtype", [REGION_TYPE_AREA, REGION_TYPE_LINE, None, 99])
def test_dose_options_non_point_are_mm(rtype) -> None:
    # Area, Line, unknown, and None all fall back to the mm presets.
    assert dose_options_for_region_type(rtype) == ["3 mm", "6 mm", "13 mm"]


def test_dose_options_point_are_minutes() -> None:
    assert dose_options_for_region_type(REGION_TYPE_POINT) == ["1 min", "5 min", "10 min"]


def test_default_dose_label_for_region_type() -> None:
    assert default_dose_label_for_region_type(REGION_TYPE_POINT) == "1 min"
    assert default_dose_label_for_region_type(REGION_TYPE_AREA) == "3 mm"
    assert default_dose_label_for_region_type(REGION_TYPE_LINE) == "3 mm"
    assert default_dose_label_for_region_type(None) == "3 mm"


# --------------------------------------------------------------------------- #
# parse_dose_label  (label -> wire value)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "label,expected",
    [
        ("3 mm", ("waterYield", 0.1)),
        ("6 mm", ("waterYield", 0.25)),
        ("13 mm", ("waterYield", 0.5)),
        ("1 min", ("point_time", 1)),
        ("5 min", ("point_time", 5)),
        ("10 min", ("point_time", 10)),
    ],
)
def test_parse_dose_label_known(label, expected) -> None:
    assert parse_dose_label(label) == expected


@pytest.mark.parametrize("label", ["", "9 mm", "2 min", "13mm", "3 MM", "foo"])
def test_parse_dose_label_unknown_returns_none(label) -> None:
    assert parse_dose_label(label) is None


# --------------------------------------------------------------------------- #
# label_for_water_yield  (wire value -> label, snap-to-nearest)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.1, "3 mm"),
        (0.25, "6 mm"),
        (0.5, "13 mm"),
        # Off-preset floats snap to the nearest preset.
        (0.12, "3 mm"),
        (0.2, "6 mm"),
        (0.4, "13 mm"),
        (0.0, "3 mm"),
        (99.0, "13 mm"),
        # Numeric strings coerce via float().
        ("0.25", "6 mm"),
    ],
)
def test_label_for_water_yield_snaps(value, expected) -> None:
    assert label_for_water_yield(value) == expected


@pytest.mark.parametrize("value", [None, "abc", "", object()])
def test_label_for_water_yield_garbage_returns_none(value) -> None:
    assert label_for_water_yield(value) is None


# --------------------------------------------------------------------------- #
# label_for_point_time  (wire value -> label, snap-to-nearest)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, "1 min"),
        (5, "5 min"),
        (10, "10 min"),
        (2, "1 min"),   # ties/near -> nearest preset
        (4, "5 min"),
        (8, "10 min"),
        (100, "10 min"),
        (0, "1 min"),
        (5.4, "5 min"),  # rounds to 5
        ("5", "5 min"),
    ],
)
def test_label_for_point_time_snaps(value, expected) -> None:
    assert label_for_point_time(value) == expected


@pytest.mark.parametrize("value", [None, "abc", "", object()])
def test_label_for_point_time_garbage_returns_none(value) -> None:
    assert label_for_point_time(value) is None


# --------------------------------------------------------------------------- #
# Nozzle encoding (server 1-indexed -> device 0-indexed)
# --------------------------------------------------------------------------- #


def test_nozzle_type_constants() -> None:
    assert (NOZZLE_TYPE_STANDARD, NOZZLE_TYPE_JET) == (0, 1)
    assert NOZZLE_TYPE_LABELS == {0: "Standard", 1: "Jet"}


def test_nozzle_server_to_device_mapping() -> None:
    # Server encoding is ambiguous on the Standard side: 0 OR 1 => Standard,
    # 2 => Jet.
    assert NOZZLE_SERVER_TO_DEVICE == {0: 0, 1: 0, 2: 1}
