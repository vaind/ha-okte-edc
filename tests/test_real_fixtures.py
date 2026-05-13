"""Tests against anonymized real OKTE MSCONS files.

The fixtures under ``tests/fixtures/`` were produced from real production
files via ``tests/_anonymize.py`` — identifiers (EICs, partner codes,
reference numbers) were replaced with deterministic synthetic values,
but the structural XML and per-quarter quantities are unchanged. These
tests exist specifically to guard against parser drift away from the
shape of files OKTE actually emits.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from okte_edc.const import (
    LIN_CPM15,
    LIN_CPS15,
    LIN_PM15,
    LIN_PS15,
    LIN_SHA15,
    ROLE_OFFTAKE,
    ROLE_PRODUCER,
)
from okte_edc.mscons import parse_mscons
from okte_edc.statistics import quarters_to_hourly

FIXTURE_DIR = Path(__file__).parent / "fixtures"

OFFTAKE_FIXTURES = [
    FIXTURE_DIR / "24ZZS00000000001_20260501_D_V1.xml",
    FIXTURE_DIR / "24ZZS00000000002_20260501_D_V1.xml",
]
PRODUCER_FIXTURE = FIXTURE_DIR / "24ZZSVYR00000099_20260501_D_V1.xml"


def _read(path: Path) -> bytes:
    return path.read_bytes()


@pytest.mark.parametrize("path", OFFTAKE_FIXTURES)
def test_real_offtake_parses_and_reconciles(path: Path):
    data = parse_mscons(_read(path))
    assert data.role == ROLE_OFFTAKE
    assert data.eic in path.name
    assert set(data.series) == {LIN_PS15, LIN_SHA15, LIN_CPS15}
    assert all(len(qs) == 96 for qs in data.series.values())
    # Spec §2.7: per-interval invariant PS15 - SHA15 - CPS15 ≈ 0
    assert data.reconciliation_max_delta_kwh < 1e-6
    assert not data.warnings


def test_real_producer_parses_and_reconciles():
    data = parse_mscons(_read(PRODUCER_FIXTURE))
    assert data.role == ROLE_PRODUCER
    assert data.eic == "24ZZSVYR00000099"
    assert set(data.series) == {LIN_PM15, LIN_SHA15, LIN_CPM15}
    assert all(len(qs) == 96 for qs in data.series.values())
    assert data.reconciliation_max_delta_kwh < 1e-6
    assert not data.warnings


def test_real_files_use_root_element_named_mscons():
    """Guard against a regression where we tighten to MSCONSDOCUMENT only."""
    raw = OFFTAKE_FIXTURES[0].read_text(encoding="utf-8")
    assert "<MSCONS>" in raw  # OKTE's actual root element


def test_cross_eic_sharing_invariant_holds_for_2026_05_01():
    """Spec §2.7: Σ SHA15(producer) ≈ Σ SHA15(offtakes) over the same day."""
    producer = parse_mscons(_read(PRODUCER_FIXTURE))
    offtakes = [parse_mscons(_read(p)) for p in OFFTAKE_FIXTURES]
    producer_total = sum(q.value_kw for q in producer.series[LIN_SHA15])
    offtake_total = sum(
        q.value_kw for o in offtakes for q in o.series[LIN_SHA15]
    )
    assert producer_total == pytest.approx(offtake_total, abs=1e-6)


def test_real_offtake_hourly_aggregation_normal_day():
    data = parse_mscons(_read(OFFTAKE_FIXTURES[0]))
    hourly = quarters_to_hourly(data.series[LIN_PS15])
    assert len(hourly) == 24
    total_quarters_kwh = sum(q.value_kw for q in data.series[LIN_PS15]) * 0.25
    total_hourly_kwh = sum(h.kwh for h in hourly)
    assert total_hourly_kwh == pytest.approx(total_quarters_kwh, rel=1e-9)


def test_real_file_round_trips_through_gzip():
    """The IMAP client decompresses .xml.gz attachments before parsing.

    Verifies parse_mscons also handles gzip auto-detection on real bytes.
    """
    raw = _read(OFFTAKE_FIXTURES[0])
    gz = gzip.compress(raw)
    direct = parse_mscons(raw)
    via_gz = parse_mscons(gz)
    assert direct.eic == via_gz.eic
    assert len(direct.series[LIN_PS15]) == len(via_gz.series[LIN_PS15])
