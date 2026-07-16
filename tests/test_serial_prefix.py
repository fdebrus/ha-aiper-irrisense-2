"""Regression test for the Irrisense serial-prefix device filter.

A real WRZ-prefix Irrisense 2 was being dropped by `get_devices` because the
filter only accepted the explicit WRX / WGX SKU prefixes. The filter now
matches the 2-letter batch *family* (WR / WG / WC / WL) so a new third letter
(WRZ, WCX, ...) is covered automatically. Pin that so it can't regress.
"""
from __future__ import annotations

from custom_components.aiper_irrisense.const import IRRISENSE_SERIAL_PREFIXES


def test_prefixes_are_two_letter_families() -> None:
    assert set(IRRISENSE_SERIAL_PREFIXES) == {"WR", "WG", "WC", "WL"}
    assert all(len(p) == 2 for p in IRRISENSE_SERIAL_PREFIXES)


def test_real_and_future_serials_match_filter() -> None:
    # Exactly the `str.startswith(tuple)` check get_devices uses. Includes the
    # WRZ serial that originally regressed, plus other family members.
    for sn in (
        "WRX60500001",  # original SKU
        "WGX12345678",  # big-box variant
        "WRZ61600001",  # the revision that regressed
        "WCX00000001",  # future WC-family batch
        "WL9999",       # WL family
    ):
        assert sn.upper().startswith(IRRISENSE_SERIAL_PREFIXES)


def test_non_irrisense_serial_rejected() -> None:
    # Pool-cleaner / other Aiper serials must still be filtered out.
    for sn in ("HJ1234", "S0123456", "AB000001"):
        assert not sn.upper().startswith(IRRISENSE_SERIAL_PREFIXES)
