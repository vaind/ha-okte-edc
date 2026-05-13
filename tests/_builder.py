"""Synthetic MSCONS XML builder for tests.

The real schema is described in the integration spec §2.4. This builder
generates files that are structurally identical to what OKTE produces,
honoring the basic-ISO DATUM format with CET/CEST suffixes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

BRATISLAVA = ZoneInfo("Europe/Bratislava")


@dataclass
class SeriesSpec:
    code: str  # PS15 / CPS15 / SHA15 / PM15 / CPM15
    values: Sequence[float]  # kW per quarter


@dataclass
class FileSpec:
    eic: str
    measurement_date: date
    version: int = 1
    series: list[SeriesSpec] = field(default_factory=list)
    extended_iso: bool = False


def build_mscons_xml(spec: FileSpec) -> str:
    """Return MSCONS XML matching the integration's parser expectations."""
    quarters_starts = _generate_local_starts(spec.measurement_date)
    quarter_count = len(quarters_starts)
    for s in spec.series:
        if len(s.values) != quarter_count:
            raise ValueError(
                f"Series {s.code} has {len(s.values)} values; "
                f"expected {quarter_count} for date {spec.measurement_date}"
            )

    lin_xml = []
    for s in spec.series:
        qty_xml = []
        for i, start_local in enumerate(quarters_starts):
            end_local = start_local + timedelta(minutes=15)
            start_str = _format_datum(start_local, spec.extended_iso)
            end_str = _format_datum(end_local, spec.extended_iso)
            qty_xml.append(
                f"""<QTY>
                    <QUANTITY_QUALIFIER>136</QUANTITY_QUALIFIER>
                    <QUANTITY>{s.values[i]:.6f}</QUANTITY>
                    <DTM>
                      <DATUMQUALIFIER>158</DATUMQUALIFIER>
                      <DATUM>{start_str}</DATUM>
                    </DTM>
                    <DTM>
                      <DATUMQUALIFIER>159</DATUMQUALIFIER>
                      <DATUM>{end_str}</DATUM>
                    </DTM>
                </QTY>"""
            )
        lin_xml.append(
            f"""<LIN>
                <ITEM_NUMBER>{s.code}</ITEM_NUMBER>
                <CODE_LIST_RESPONSIBLE_AGENCY>SKE</CODE_LIST_RESPONSIBLE_AGENCY>
                <MEA>
                  <MEASURMENT_APPLICATION>AAZ</MEASURMENT_APPLICATION>
                  <MEASURMENT_UNIT_QUALIFIER>KWT</MEASURMENT_UNIT_QUALIFIER>
                  <MEASURMENT_VALUE>0</MEASURMENT_VALUE>
                </MEA>
                {"".join(qty_xml)}
            </LIN>"""
        )

    doc_now = datetime.now(tz=timezone.utc).astimezone(BRATISLAVA)
    doc_dt_str = _format_datum(doc_now, spec.extended_iso)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<MSCONSDOCUMENT>
  <UNH><MESSAGEREFNUMBER>1</MESSAGEREFNUMBER></UNH>
  <BGM>
    <DOCUMENTNUMBER>SYNTHETIC_{spec.eic}_V{spec.version}</DOCUMENTNUMBER>
    <DOCUMENTFUNC>9</DOCUMENTFUNC>
    <RESPONSETYPE>AB</RESPONSETYPE>
  </BGM>
  <DTM>
    <DATUMQUALIFIER>137</DATUMQUALIFIER>
    <DATUM>{doc_dt_str}</DATUM>
  </DTM>
  <NAD><ACTION>MS</ACTION><PARTNER>24X-EXAMPLE-OPER</PARTNER></NAD>
  <NAD><ACTION>MR</ACTION><PARTNER>24Y-EXAMPLE-MEMB</PARTNER></NAD>
  <NAD>
    <ACTION>GN</ACTION>
    <PARTNER>24X-EXAMPLE-OPER</PARTNER>
    <LOC>
      <PLACE_QUALIFIER>90</PLACE_QUALIFIER>
      <PLACE_ID>{spec.eic}</PLACE_ID>
      {"".join(lin_xml)}
    </LOC>
  </NAD>
</MSCONSDOCUMENT>
"""


def _generate_local_starts(day: date) -> list[datetime]:
    """Return the list of 15-min local-time starts for ``day`` in Bratislava.

    On normal days that's 96 entries. Spring-forward day produces 92,
    fall-back 100.
    """
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=BRATISLAVA)
    next_day = datetime(
        day.year, day.month, day.day, tzinfo=BRATISLAVA
    ) + timedelta(days=1)
    # Walk in UTC to avoid TZ-arithmetic pitfalls.
    starts: list[datetime] = []
    cursor = start.astimezone(timezone.utc)
    end_utc = next_day.astimezone(timezone.utc)
    while cursor < end_utc:
        starts.append(cursor.astimezone(BRATISLAVA))
        cursor += timedelta(minutes=15)
    return starts


def _format_datum(dt: datetime, extended_iso: bool) -> str:
    if extended_iso:
        return dt.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S.000")
    tz_abbr = dt.tzname() or "CET"
    return dt.strftime("%Y%m%d%H%M") + tz_abbr


def offtake_series(
    quarter_count: int,
    *,
    base_kw: float = 0.4,
    shared_fraction: float = 0.3,
) -> list[SeriesSpec]:
    """Build a self-consistent off-take triple (PS15, SHA15, CPS15)."""
    ps_values = [base_kw + 0.1 * (i % 4) for i in range(quarter_count)]
    sha_values = [v * shared_fraction for v in ps_values]
    cps_values = [
        round(ps - sh, 6) for ps, sh in zip(ps_values, sha_values)
    ]
    return [
        SeriesSpec("PS15", ps_values),
        SeriesSpec("SHA15", sha_values),
        SeriesSpec("CPS15", cps_values),
    ]


def producer_series(
    quarter_count: int,
    *,
    base_kw: float = 1.2,
    shared_fraction: float = 0.7,
) -> list[SeriesSpec]:
    """Build a self-consistent producer triple (PM15, SHA15, CPM15)."""
    pm_values = [base_kw + 0.05 * (i % 8) for i in range(quarter_count)]
    sha_values = [v * shared_fraction for v in pm_values]
    cpm_values = [
        round(pm - sh, 6) for pm, sh in zip(pm_values, sha_values)
    ]
    return [
        SeriesSpec("PM15", pm_values),
        SeriesSpec("SHA15", sha_values),
        SeriesSpec("CPM15", cpm_values),
    ]
