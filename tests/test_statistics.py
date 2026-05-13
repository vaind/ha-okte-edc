"""Tests for the pure aggregation helpers in :mod:`statistics`."""

from __future__ import annotations

from datetime import date

import pytest

from okte_edc.mscons import parse_mscons
from okte_edc.statistics import build_statistic_data, quarters_to_hourly

from ._builder import (
    FileSpec,
    SeriesSpec,
    build_mscons_xml,
    offtake_series,
    producer_series,
)


def _quarters(lin: str, data) -> list:
    return data.series[lin]


def test_quarters_to_hourly_normal_day():
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 5, 3),
        series=offtake_series(96),
    )
    data = parse_mscons(build_mscons_xml(spec))
    hourly = quarters_to_hourly(_quarters("PS15", data))
    assert len(hourly) == 24
    # The total energy should equal sum(kw_values) * 0.25
    total_hourly = sum(h.kwh for h in hourly)
    total_kw = sum(q.value_kw for q in _quarters("PS15", data))
    assert total_hourly == pytest.approx(total_kw * 0.25, rel=1e-9)


def test_quarters_to_hourly_spring_forward_23h():
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 3, 29),
        series=offtake_series(92),
    )
    data = parse_mscons(build_mscons_xml(spec))
    hourly = quarters_to_hourly(_quarters("PS15", data))
    assert len(hourly) == 23


def test_quarters_to_hourly_fall_back_25h():
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 10, 25),
        series=offtake_series(100),
    )
    data = parse_mscons(build_mscons_xml(spec))
    hourly = quarters_to_hourly(_quarters("PS15", data))
    assert len(hourly) == 25


def test_quarters_to_hourly_known_values():
    """Hand-checked: every quarter has value 1 kW → every hour has 1 kWh."""
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 5, 3),
        series=[
            SeriesSpec("PS15", [1.0] * 96),
            SeriesSpec("SHA15", [0.0] * 96),
            SeriesSpec("CPS15", [1.0] * 96),
        ],
    )
    data = parse_mscons(build_mscons_xml(spec))
    hourly = quarters_to_hourly(_quarters("PS15", data))
    assert len(hourly) == 24
    assert all(h.kwh == pytest.approx(1.0) for h in hourly)


def test_build_statistic_data_running_sum():
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 5, 3),
        series=[
            SeriesSpec("PS15", [1.0] * 96),
            SeriesSpec("SHA15", [0.0] * 96),
            SeriesSpec("CPS15", [1.0] * 96),
        ],
    )
    data = parse_mscons(build_mscons_xml(spec))
    hourly = quarters_to_hourly(_quarters("PS15", data))
    rows = build_statistic_data(hourly, starting_sum=10.0)
    # 24 hours of 1 kWh added to a starting sum of 10 = 11..34
    assert [r["sum"] for r in rows[:3]] == pytest.approx([11.0, 12.0, 13.0])
    assert rows[-1]["sum"] == pytest.approx(34.0)


def test_correction_v2_round_trip_replaces_v1_values():
    """A V2 file produces equivalent hourly buckets to V1 but with different sums.

    The recorder upsert is idempotent on (statistic_id, start), so callers
    only need to ensure the buckets have the same start anchors. This test
    verifies the parser/aggregator are deterministic on the same input.
    """
    base_series = offtake_series(96)
    v1 = parse_mscons(
        build_mscons_xml(
            FileSpec(
                eic="24ZZS00000000001",
                measurement_date=date(2026, 5, 3),
                version=1,
                series=base_series,
            )
        )
    )
    # V2 with a doubled PS15 (and matching CPS15) — pretend OKTE corrected
    # the data.
    base_series[0] = SeriesSpec("PS15", [v * 2 for v in base_series[0].values])
    base_series[2] = SeriesSpec(
        "CPS15", [v * 2 for v in base_series[2].values]
    )
    base_series[1] = SeriesSpec(
        "SHA15", [v * 2 for v in base_series[1].values]
    )
    v2 = parse_mscons(
        build_mscons_xml(
            FileSpec(
                eic="24ZZS00000000001",
                measurement_date=date(2026, 5, 3),
                version=2,
                series=base_series,
            )
        )
    )
    v1_hourly = quarters_to_hourly(_quarters("PS15", v1))
    v2_hourly = quarters_to_hourly(_quarters("PS15", v2))
    assert [h.start_utc for h in v1_hourly] == [h.start_utc for h in v2_hourly]
    assert all(
        v2.kwh == pytest.approx(v1.kwh * 2)
        for v1, v2 in zip(v1_hourly, v2_hourly)
    )
    assert v2.file_version == 2


def test_producer_aggregation_total_matches_sum_kw_quarters():
    spec = FileSpec(
        eic="24ZZSVYR00000099",
        measurement_date=date(2026, 5, 3),
        series=producer_series(96),
    )
    data = parse_mscons(build_mscons_xml(spec))
    hourly = quarters_to_hourly(_quarters("PM15", data))
    total_kw = sum(q.value_kw for q in _quarters("PM15", data))
    assert sum(h.kwh for h in hourly) == pytest.approx(total_kw * 0.25)
