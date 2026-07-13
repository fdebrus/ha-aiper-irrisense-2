"""Regression tests for the coordinator's MQTT frame parsing.

Covered:

* ``_extract_map_id`` — both wire shapes (top-level ``map_id`` from setWorkMode
  echoes; nested ``map_info.id`` from realTimeProgress frames) plus legacy
  spellings.
* ``_snap_to_preset`` — nearest-preset snapping.
* ``handle_mqtt_message`` — the upChan envelope normalisation (legacy
  ``{"type":"up_x","data":{}}`` vs symmetric ``{"x":{}}``) and topic bucketing.
* ``active_zone_state`` — the progress-spike filter and duration latch, which
  are the subtlest reverse-engineered behaviour in the integration.

The ``handle_mqtt_message`` / ``active_zone_state`` tests build a coordinator
via ``__new__`` and populate only the instance state those methods read. This
is a deliberate white-box unit test: it exercises the pure parsing logic
without a full Home Assistant bootstrap, keeping the regression net fast and
free of HA-lifecycle coupling. The methods under test only read
``self._data`` and the per-run dicts (plus a couple of stubbed collaborators),
so the isolation is faithful to production behaviour.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.aiper_irrisense.coordinator import (
    IrrisenseCoordinator,
    _extract_map_id,
    _snap_to_preset,
)
from custom_components.aiper_irrisense.const import (
    POINT_TIME_PRESETS,
    WATER_YIELD_PRESETS,
)


# --------------------------------------------------------------------------- #
# _extract_map_id
# --------------------------------------------------------------------------- #


def test_extract_map_id_top_level_map_id() -> None:
    assert _extract_map_id({"map_id": 3, "status": 1}) == 3


def test_extract_map_id_nested_map_info() -> None:
    assert _extract_map_id({"map_info": {"id": 7, "name": "Lawn", "type": 0}}) == 7


def test_extract_map_id_legacy_camelcase_and_region_id() -> None:
    assert _extract_map_id({"mapId": 4}) == 4
    assert _extract_map_id({"region_id": 5}) == 5


def test_extract_map_id_coerces_numeric_strings() -> None:
    assert _extract_map_id({"map_id": "9"}) == 9
    assert _extract_map_id({"map_info": {"id": "2"}}) == 2


def test_extract_map_id_top_level_wins_over_nested() -> None:
    # Top-level keys are checked before map_info.
    assert _extract_map_id({"map_id": 1, "map_info": {"id": 2}}) == 1


def test_extract_map_id_absent_returns_none() -> None:
    # workInfo/workInfoReport frames carry only status + mode.
    assert _extract_map_id({"status": 1, "mode": 0}) is None
    assert _extract_map_id({}) is None
    assert _extract_map_id(None) is None
    assert _extract_map_id("not a dict") is None


def test_extract_map_id_non_numeric_is_skipped() -> None:
    assert _extract_map_id({"map_id": "abc"}) is None
    assert _extract_map_id({"map_info": {"id": "xyz"}}) is None


# --------------------------------------------------------------------------- #
# _snap_to_preset
# --------------------------------------------------------------------------- #


def test_snap_to_preset_exact_value_unchanged() -> None:
    assert _snap_to_preset(0.25, WATER_YIELD_PRESETS, "waterYield") == 0.25
    assert _snap_to_preset(5, POINT_TIME_PRESETS, "point_time") == 5


def test_snap_to_preset_snaps_to_nearest() -> None:
    assert _snap_to_preset(0.2, WATER_YIELD_PRESETS, "waterYield") == 0.25
    assert _snap_to_preset(0.4, WATER_YIELD_PRESETS, "waterYield") == 0.5
    assert _snap_to_preset(2, POINT_TIME_PRESETS, "point_time") == 1
    assert _snap_to_preset(8, POINT_TIME_PRESETS, "point_time") == 10


# --------------------------------------------------------------------------- #
# Bare coordinator factory (white-box; no HA bootstrap)
# --------------------------------------------------------------------------- #


def _make_coordinator() -> IrrisenseCoordinator:
    coord = IrrisenseCoordinator.__new__(IrrisenseCoordinator)
    coord._data = {}
    # Collaborators handle_mqtt_message touches.
    coord.api = SimpleNamespace(ack_calls=[])
    coord.api.note_upchan_ack = lambda sn, cmd: coord.api.ack_calls.append((sn, cmd))
    coord.hass = SimpleNamespace(
        loop=SimpleNamespace(call_soon_threadsafe=lambda *a, **k: None)
    )
    # Attributes trigger_fast_poll writes/reads.
    coord._fast_until = 0.0
    coord._fast_interval = timedelta(seconds=5)
    coord._base_interval = timedelta(seconds=120)
    coord.update_interval = timedelta(seconds=120)
    # Per-run state active_zone_state reads/mutates.
    coord._run_start_ts = {}
    coord._run_start_zone = {}
    coord._run_duration = {}
    coord._run_duration_pct = {}
    coord._run_last_progress = {}
    coord._run_watchdog_tasks = {}
    return coord


# --------------------------------------------------------------------------- #
# handle_mqtt_message — upChan normalisation + bucketing
# --------------------------------------------------------------------------- #


def test_handle_upchan_symmetric_shape() -> None:
    coord = _make_coordinator()
    sn = "WRX1"
    coord.handle_mqtt_message(
        sn,
        {
            "realTimeProgress": {"status": 1, "map_info": {"id": 1}, "progress": 12},
            "_topic": f"aiper/things/{sn}/upChan",
            "_sn": sn,
        },
    )
    stored = coord._data[sn]["mqtt"]["up_realTimeProgress"]
    assert stored["type"] == "realTimeProgress"
    assert stored["data"] == {"status": 1, "map_info": {"id": 1}, "progress": 12}
    assert isinstance(stored["_ts"], float)


def test_handle_upchan_legacy_type_data_shape() -> None:
    coord = _make_coordinator()
    sn = "WRX1"
    coord.handle_mqtt_message(
        sn,
        {
            "type": "up_setWorkMode",
            "data": {"status": 1, "map_id": 2, "waterYield": 0.1},
            "_topic": f"aiper/things/{sn}/upChan",
        },
    )
    stored = coord._data[sn]["mqtt"]["up_setWorkMode"]
    # `up_` prefix stripped so it matches what we published.
    assert stored["type"] == "setWorkMode"
    assert stored["data"]["map_id"] == 2
    assert ("WRX1", "setWorkMode") in coord.api.ack_calls


def test_handle_upchan_status0_acks_pending_setworkmode() -> None:
    coord = _make_coordinator()
    sn = "WRX1"
    coord.handle_mqtt_message(
        sn,
        {
            "realTimeProgress": {"status": 0, "map_info": {"id": 1}},
            "_topic": f"aiper/things/{sn}/upChan",
        },
    )
    # A status:0 realtime frame is treated as the ACK for a stop command.
    assert ("WRX1", "realTimeProgress") in coord.api.ack_calls
    assert ("WRX1", "setWorkMode") in coord.api.ack_calls


def test_handle_cloud_report_bucketing() -> None:
    coord = _make_coordinator()
    sn = "WRX1"
    coord.handle_mqtt_message(
        sn,
        {"someHeartbeat": 1, "_topic": f"aiper/things/{sn}/WR/cloud/report"},
    )
    assert "cloud_report" in coord._data[sn]["mqtt"]
    assert "up_someHeartbeat" not in coord._data[sn]["mqtt"]


def test_handle_shadow_get_bucketing() -> None:
    coord = _make_coordinator()
    sn = "WRX1"
    coord.handle_mqtt_message(
        sn,
        {"state": {}, "_topic": f"$aws/things/{sn}/shadow/get/accepted"},
    )
    assert "shadow_get" in coord._data[sn]["mqtt"]


# --------------------------------------------------------------------------- #
# active_zone_state — status handling
# --------------------------------------------------------------------------- #


def _running_frame(zone_id: int, ts: float, **body) -> dict:
    """Build a normalised up_realTimeProgress frame as handle_mqtt_message would."""
    data = {"status": 1, "map_info": {"id": zone_id}, **body}
    return {"type": "realTimeProgress", "data": data, "_ts": ts, "_sn": "WRX1"}


def _seed_area_zone(coord: IrrisenseCoordinator, sn: str = "WRX1") -> None:
    coord._data.setdefault(sn, {})["map"] = {
        "regions": [{"id": 1, "name": "Lawn", "type": 0, "waterYield": 0.1}]
    }


def test_active_zone_idle_when_status_zero() -> None:
    coord = _make_coordinator()
    sn = "WRX1"
    _seed_area_zone(coord, sn)
    coord._data[sn]["mqtt"] = {
        "up_realTimeProgress": {
            "type": "realTimeProgress",
            "data": {"status": 0, "map_info": {"id": 1}},
            "_ts": 100.0,
        }
    }
    assert coord.active_zone_state(sn) is None


def test_active_zone_none_when_no_frames() -> None:
    coord = _make_coordinator()
    _seed_area_zone(coord)
    coord._data["WRX1"]["mqtt"] = {}
    assert coord.active_zone_state("WRX1") is None


def test_active_zone_running_basic_fields() -> None:
    coord = _make_coordinator()
    sn = "WRX1"
    _seed_area_zone(coord, sn)
    coord._data[sn]["mqtt"] = {
        "up_realTimeProgress": _running_frame(1, 100.0, time=100, progress=10, waterYield=0.1)
    }
    state = coord.active_zone_state(sn)
    assert state is not None
    assert state["is_running"] is True
    assert state["zone_id"] == 1
    assert state["zone_name"] == "Lawn"
    assert state["region_type"] == 0
    assert state["dose_label"] == "3 mm"
    assert state["time_sec"] == 100


# --------------------------------------------------------------------------- #
# active_zone_state — duration latch + progress-spike filter
# --------------------------------------------------------------------------- #


def test_duration_backsolve_and_latch() -> None:
    coord = _make_coordinator()
    sn = "WRX1"
    _seed_area_zone(coord, sn)

    # First frame: elapsed 100s at 10% -> back-solved duration 1000s, latched.
    coord._data[sn]["mqtt"] = {
        "up_realTimeProgress": _running_frame(1, 100.0, time=100, progress=10)
    }
    state = coord.active_zone_state(sn)
    assert state["duration_seconds"] == 1000
    assert state["duration_pending"] is False
    assert coord._run_duration[sn] == 1000


def test_progress_spike_is_filtered() -> None:
    coord = _make_coordinator()
    sn = "WRX1"
    _seed_area_zone(coord, sn)

    # Establish a latched 1000s duration and a last-good progress of 10%.
    coord._data[sn]["mqtt"] = {
        "up_realTimeProgress": _running_frame(1, 100.0, time=100, progress=10)
    }
    first = coord.active_zone_state(sn)
    assert first["progress"] == 10

    # Spike: 100% at only 120s elapsed (< 90% of the 1000s run). Must be
    # suppressed back to the last good value, not surfaced as end-of-run.
    coord._data[sn]["mqtt"]["up_realTimeProgress"] = _running_frame(
        1, 200.0, time=120, progress=100
    )
    spiked = coord.active_zone_state(sn)
    assert spiked["progress"] == 10.0  # filtered, not 100


def test_genuine_end_of_run_progress_passes_through() -> None:
    coord = _make_coordinator()
    sn = "WRX1"
    _seed_area_zone(coord, sn)

    coord._data[sn]["mqtt"] = {
        "up_realTimeProgress": _running_frame(1, 100.0, time=100, progress=10)
    }
    coord.active_zone_state(sn)  # latch duration=1000

    # 100% at 950s elapsed (>= 90% of 1000s) is a real finish, kept as-is.
    coord._data[sn]["mqtt"]["up_realTimeProgress"] = _running_frame(
        1, 300.0, time=950, progress=100
    )
    end = coord.active_zone_state(sn)
    assert end["progress"] == 100


def test_new_zone_resets_latched_duration() -> None:
    coord = _make_coordinator()
    sn = "WRX1"
    coord._data.setdefault(sn, {})["map"] = {
        "regions": [
            {"id": 1, "name": "Lawn", "type": 0, "waterYield": 0.1},
            {"id": 2, "name": "Beds", "type": 0, "waterYield": 0.25},
        ]
    }
    coord._data[sn]["mqtt"] = {
        "up_realTimeProgress": _running_frame(1, 100.0, time=100, progress=10)
    }
    coord.active_zone_state(sn)
    assert coord._run_duration.get(sn) == 1000

    # Switch to zone 2 — the prior run's latched duration must be dropped so
    # the new run re-solves from scratch.
    coord._data[sn]["mqtt"]["up_realTimeProgress"] = _running_frame(
        2, 200.0, time=50, progress=5
    )
    state = coord.active_zone_state(sn)
    assert state["zone_id"] == 2
    # 50s / 0.05 = 1000 again here, but the point is the pct/duration were
    # re-latched for the new zone, not carried over.
    assert coord._run_start_zone[sn] == 2
