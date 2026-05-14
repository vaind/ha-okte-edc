"""Unit tests for the MSCONS parser."""

from __future__ import annotations

import gzip
from datetime import date

import pytest

from okte_edc.const import (
    LIN_CPM15,
    LIN_CPS15,
    LIN_PM15,
    LIN_PS15,
    LIN_SHA15,
    ROLE_OFFTAKE,
    ROLE_PRODUCER,
    detect_role,
    short_eic,
)
from okte_edc.mscons import MsconsParseError, parse_mscons

from ._builder import (
    FileSpec,
    SeriesSpec,
    build_mscons_xml,
    offtake_series,
    producer_series,
)


# ---------------------------------------------------------------------------
# Role detection / helpers


@pytest.mark.parametrize(
    "eic,expected",
    [
        ("24ZZS00000000001", ROLE_OFFTAKE),
        ("24ZZS00000000002", ROLE_OFFTAKE),
        ("24ZZSVYR00000099", ROLE_PRODUCER),
        ("24ZZSvyr00000099", ROLE_PRODUCER),  # case-insensitive
        ("24ZZS-something", ROLE_OFFTAKE),
    ],
)
def test_detect_role(eic, expected):
    assert detect_role(eic) == expected


def test_short_eic_is_lowercase_8_alnum():
    assert short_eic("24ZZS00000000001") == "00000001"
    assert short_eic("24ZZSVYR00000099") == "00000099"


def test_statistic_id_uses_external_format():
    """Pin the external-statistic id format ``<DOMAIN>:<short_eic>_<suffix>``.

    Critical because HA's recorder requires the ``<source>:`` prefix to
    match the ``source`` field in StatisticMetaData when calling
    ``async_add_external_statistics`` — drift between the two silently
    drops every write.
    """
    from okte_edc.const import statistic_id_for

    assert (
        statistic_id_for("24ZZS00000000001", "grid_import")
        == "okte_edc:00000001_grid_import"
    )
    assert (
        statistic_id_for("24ZZSVYR00000099", "shared_out")
        == "okte_edc:00000099_shared_out"
    )


def test_entity_id_uses_sensor_dot_form_not_colon_form():
    """`entity_id_for` and `statistic_id_for` are deliberately different.

    Entity IDs are ``<domain>.<object_id>`` and must use ``[a-z0-9_]``
    in the object part — colons are illegal and HA logs an
    `Error adding entity` + a deprecation warning. The statistic_id
    side uses the ``<source>:<id>`` form HA requires for external
    statistics. Make sure these two helpers don't drift back into a
    single function.
    """
    from okte_edc.const import entity_id_for, statistic_id_for

    assert (
        entity_id_for("24ZZS00000000001", "grid_import")
        == "sensor.okte_edc_00000001_grid_import"
    )
    assert ":" not in entity_id_for("24ZZS00000000001", "grid_import")
    # The two helpers must NEVER be the same string for the same input.
    assert entity_id_for(
        "24ZZS00000000001", "grid_import"
    ) != statistic_id_for("24ZZS00000000001", "grid_import")


def test_statistic_name_is_human_readable():
    """The Energy-dashboard source picker shows the metadata `name`.

    External statistics have no entity to inherit a friendly name from,
    so the picker label is literally what we put in `name`. Keep this
    locale-independent so the dashboard doesn't relabel when the user
    switches HA language.
    """
    from okte_edc.const import statistic_name_for

    assert (
        statistic_name_for("24ZZS00000000001", "grid_import")
        == "OKTE EDC 00000001 Grid import"
    )
    assert (
        statistic_name_for("24ZZSVYR00000099", "total_export")
        == "OKTE EDC 00000099 Total export"
    )


# ---------------------------------------------------------------------------
# Parsing


def test_parse_basic_offtake_day():
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 5, 3),
        version=1,
        series=offtake_series(96),
    )
    xml = build_mscons_xml(spec)
    data = parse_mscons(xml)
    assert data.eic == "24ZZS00000000001"
    assert data.role == ROLE_OFFTAKE
    assert data.measurement_date == date(2026, 5, 3)
    assert data.file_version == 1
    assert set(data.series) == {LIN_PS15, LIN_SHA15, LIN_CPS15}
    assert all(len(qs) == 96 for qs in data.series.values())
    # Reconciliation invariant: PS = SHA + CPS within rounding
    assert data.reconciliation_max_delta_kwh < 1e-6


def test_parse_basic_producer_day():
    spec = FileSpec(
        eic="24ZZSVYR00000099",
        measurement_date=date(2026, 5, 3),
        series=producer_series(96),
    )
    data = parse_mscons(build_mscons_xml(spec))
    assert data.role == ROLE_PRODUCER
    assert set(data.series) == {LIN_PM15, LIN_SHA15, LIN_CPM15}
    assert data.reconciliation_max_delta_kwh < 1e-6


def test_parse_gzipped():
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 5, 3),
        series=offtake_series(96),
    )
    raw = build_mscons_xml(spec).encode("utf-8")
    data = parse_mscons(gzip.compress(raw))
    assert data.eic == "24ZZS00000000001"
    assert len(data.series[LIN_PS15]) == 96


def test_parse_extended_iso_dates():
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 5, 3),
        series=offtake_series(96),
        extended_iso=True,
    )
    data = parse_mscons(build_mscons_xml(spec))
    # Extended ISO parsing path; result is otherwise equivalent
    assert len(data.series[LIN_PS15]) == 96


def test_measurement_date_is_inferred_from_quarter_timestamps():
    """measurement_date comes from the XML's quarter timestamps in local time.

    The coordinator cross-checks this against the date encoded in the
    filename. Drifting the XML date relative to the filename's date
    must surface here, not silently flow through.
    """
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 5, 3),
        series=offtake_series(96),
    )
    data = parse_mscons(build_mscons_xml(spec))
    assert data.measurement_date == date(2026, 5, 3)


def test_parse_version_number_from_bgm():
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 5, 3),
        version=3,
        series=offtake_series(96),
    )
    data = parse_mscons(build_mscons_xml(spec))
    assert data.file_version == 3


def test_parse_rejects_unparseable_xml():
    with pytest.raises(MsconsParseError):
        parse_mscons("<not really xml")


def test_parse_rejects_wrong_root():
    with pytest.raises(MsconsParseError):
        parse_mscons("<Other></Other>")


def test_parse_skips_unknown_lin_codes():
    # File with only an unrecognised LIN code → MsconsParseError because
    # we require at least one recognised series.
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 5, 3),
        series=[SeriesSpec("XXX99", [0.1] * 96)],
    )
    with pytest.raises(MsconsParseError):
        parse_mscons(build_mscons_xml(spec))


# ---------------------------------------------------------------------------
# DST handling


def test_dst_spring_forward_yields_92_quarters():
    # 2026-03-29 is the last Sunday in March; spring-forward.
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 3, 29),
        series=offtake_series(92),
    )
    data = parse_mscons(build_mscons_xml(spec))
    assert len(data.series[LIN_PS15]) == 92
    assert not data.warnings  # 92 is a known DST count


def test_dst_fall_back_yields_100_quarters():
    # 2026-10-25 is the last Sunday in October; fall-back.
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 10, 25),
        series=offtake_series(100),
    )
    data = parse_mscons(build_mscons_xml(spec))
    assert len(data.series[LIN_PS15]) == 100


# ---------------------------------------------------------------------------
# Reconciliation


def test_parse_rejects_oversized_payload():
    from okte_edc.const import MAX_RAW_ATTACHMENT_BYTES

    with pytest.raises(MsconsParseError, match="exceeds raw cap"):
        parse_mscons(b"<MSCONS>" + b"x" * (MAX_RAW_ATTACHMENT_BYTES + 1))


def test_role_sanity_check_overrides_eic_pattern_when_lins_disagree():
    """If the EIC says producer but the file carries offtake LINs, trust the LINs.

    Real-world rationale: the EIC regex is a heuristic; the LIN codes are
    the actual data. Disagreement is surfaced as a warning.
    """
    spec = FileSpec(
        eic="24ZZSVYR99999999",  # EIC pattern → producer
        measurement_date=date(2026, 5, 3),
        series=offtake_series(96),  # but the LINs are offtake (PS15/CPS15)
    )
    data = parse_mscons(build_mscons_xml(spec))
    assert data.role == ROLE_OFFTAKE
    assert any("LIN codes indicate" in w for w in data.warnings)


def test_role_sanity_check_silent_when_lins_agree():
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 5, 3),
        series=offtake_series(96),
    )
    data = parse_mscons(build_mscons_xml(spec))
    assert data.role == ROLE_OFFTAKE
    assert not any("LIN codes indicate" in w for w in data.warnings)


def test_reconciliation_detects_drift():
    series = offtake_series(96)
    # Introduce a deliberate per-interval mismatch in CPS15.
    bad_cps = list(series[2].values)
    bad_cps[5] += 0.5  # kW; 0.5 * 0.25 = 0.125 kWh drift
    series[2] = SeriesSpec("CPS15", bad_cps)
    spec = FileSpec(
        eic="24ZZS00000000001",
        measurement_date=date(2026, 5, 3),
        series=series,
    )
    data = parse_mscons(build_mscons_xml(spec))
    assert data.reconciliation_max_delta_kwh == pytest.approx(0.125, rel=1e-9)
