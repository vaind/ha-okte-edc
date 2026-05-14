"""Tests for the pure-logic helpers in coordinator.py.

We don't try to drive the whole DataUpdateCoordinator here — that
would require a working HA recorder. Instead we cover the small
deterministic helpers that the recompute path leans on.
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def test_row_start_dt_normalises_int_timestamp():
    from okte_edc.coordinator import _row_start_dt

    # HA's newer recorder returns start as a unix timestamp (seconds).
    result = _row_start_dt(1717804800)
    assert result == datetime(2024, 6, 8, 0, 0, tzinfo=timezone.utc)


def test_row_start_dt_passes_through_aware_datetime():
    from okte_edc.coordinator import _row_start_dt

    src = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert _row_start_dt(src) is src


def test_row_start_dt_attaches_utc_to_naive():
    from okte_edc.coordinator import _row_start_dt

    src = datetime(2026, 5, 1, 12, 0)
    assert _row_start_dt(src).tzinfo == timezone.utc


def test_local_date_start_utc_maps_via_bratislava():
    """May 1 00:00 in Bratislava (CEST, UTC+2) = April 30 22:00 UTC."""
    from okte_edc.coordinator import _local_date_start_utc

    class _Entry:
        options = {"poll_timezone": "Europe/Bratislava"}

    result = _local_date_start_utc(date(2026, 5, 1), _Entry())
    assert result == datetime(2026, 4, 30, 22, 0, tzinfo=timezone.utc)


def test_local_date_start_utc_handles_dst_winter():
    """January 1 00:00 in Bratislava (CET, UTC+1) = December 31 23:00 UTC."""
    from okte_edc.coordinator import _local_date_start_utc

    class _Entry:
        options = {"poll_timezone": "Europe/Bratislava"}

    result = _local_date_start_utc(date(2026, 1, 1), _Entry())
    assert result == datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)
