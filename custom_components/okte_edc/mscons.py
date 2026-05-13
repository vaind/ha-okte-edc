"""MSCONS XML parser for OKTE EDC (SZE_7 daily files).

The parser accepts raw XML bytes or strings (already decompressed) and
returns a :class:`DailyData` record. Domain-level invariants and DST
handling are documented in the integration spec; see CLAUDE/README-level
docs for the encoding conventions of these files.

Only the subset of MSCONS the integration cares about is parsed. Other
segments (UNH, NAD-sender/receiver) are accepted but ignored.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from defusedxml.ElementTree import fromstring

from .const import (
    LIN_CPM15,
    LIN_CPS15,
    LIN_PM15,
    LIN_PS15,
    LIN_SHA15,
    QUARTER_KWH_SANITY_CEILING,
    ROLE_OFFTAKE,
    ROLE_PRODUCER,
    detect_role,
)

OFFTAKE_LINS = (LIN_PS15, LIN_SHA15, LIN_CPS15)
PRODUCER_LINS = (LIN_PM15, LIN_SHA15, LIN_CPM15)

# Fixed UTC offsets for the abbreviations OKTE actually emits. Per spec
# §2.4 the file embeds the offset in the suffix, so no DST computation is
# needed here.
_TZ_OFFSETS: dict[str, timedelta] = {
    "CET": timedelta(hours=1),
    "CEST": timedelta(hours=2),
    "UTC": timedelta(0),
    "GMT": timedelta(0),
}

_BASIC_ISO_RE = re.compile(r"^(\d{12})([A-Z]{2,5})$")


class MsconsParseError(ValueError):
    """Raised when an MSCONS XML payload cannot be parsed."""


@dataclass(frozen=True)
class Quarter:
    """A single 15-minute interval value.

    ``value_kw`` is the raw QTY (average power in kW over the interval),
    as it appears in the file. Energy is ``value_kw * 0.25 kWh``.
    """

    period_start_utc: datetime
    period_end_utc: datetime
    value_kw: float


@dataclass
class DailyData:
    """Parsed MSCONS daily payload for a single metering point."""

    eic: str
    role: str
    measurement_date: date
    file_version: int
    document_number: str
    document_datetime_utc: datetime | None
    series: dict[str, list[Quarter]] = field(default_factory=dict)
    reconciliation_max_delta_kwh: float = 0.0
    warnings: list[str] = field(default_factory=list)


def parse_mscons(payload: bytes | str) -> DailyData:
    """Parse a single MSCONS XML payload.

    ``payload`` may be raw XML bytes/text or gzipped XML bytes (auto-detected).
    """
    if isinstance(payload, bytes) and payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    try:
        root = fromstring(payload)
    except Exception as exc:  # defusedxml wraps multiple parse errors
        raise MsconsParseError(f"XML parse failure: {exc}") from exc

    # Real OKTE files use `<MSCONS>` as the root; the integration spec
    # documented `<MSCONSDOCUMENT>`. Accept both.
    if root.tag not in ("MSCONS", "MSCONSDOCUMENT"):
        raise MsconsParseError(f"Unexpected root element: {root.tag}")

    document_number = _text(root.find("./BGM/DOCUMENTNUMBER")) or ""
    version_match = re.search(r"_V(\d+)", document_number, re.IGNORECASE)
    file_version = int(version_match.group(1)) if version_match else 1

    # Document timestamp (DTM[DATUMQUALIFIER=137]). Optional; useful for logs.
    document_datetime_utc: datetime | None = None
    for dtm in root.findall("./DTM"):
        if _text(dtm.find("DATUMQUALIFIER")) == "137":
            datum = _text(dtm.find("DATUM"))
            if datum:
                try:
                    document_datetime_utc = _parse_datum(datum)
                except MsconsParseError:
                    pass
            break

    # Find the NAD[ACTION=GN] block (subject of the message). Per spec
    # there is exactly one such block per file.
    nad_gn = _find_nad_gn(root)
    if nad_gn is None:
        raise MsconsParseError("No NAD[ACTION=GN] block found")

    loc = nad_gn.find("LOC")
    if loc is None:
        raise MsconsParseError("No LOC inside NAD[ACTION=GN]")

    eic = _text(loc.find("PLACE_ID")) or ""
    if not eic:
        raise MsconsParseError("Missing PLACE_ID (EIC)")

    role = detect_role(eic)
    series: dict[str, list[Quarter]] = {}

    for lin in loc.findall("LIN"):
        item_number = _text(lin.find("ITEM_NUMBER"))
        if not item_number:
            continue
        item_number = item_number.upper()
        if item_number not in (
            LIN_PS15,
            LIN_CPS15,
            LIN_SHA15,
            LIN_PM15,
            LIN_CPM15,
        ):
            continue
        quarters = list(_parse_quarters(lin))
        if not quarters:
            continue
        series[item_number] = quarters

    if not series:
        raise MsconsParseError(f"No recognised LIN series for EIC {eic}")

    # Cross-check: the LIN codes we found should agree with the role the
    # EIC pattern suggested. If they disagree (e.g. a producer EIC carrying
    # offtake LINs) we trust the LIN evidence over the regex but warn so
    # the discrepancy surfaces in diagnostics.
    role_warnings: list[str] = []
    lin_role = _classify_by_lins(series)
    if lin_role is not None and lin_role != role:
        role_warnings.append(
            f"EIC pattern role={role} but LIN codes indicate {lin_role}; "
            f"using {lin_role}"
        )
        role = lin_role

    # Infer measurement date from the first quarter's local start. We
    # convert the UTC start back to the file's local time using the
    # offset embedded in its DATUM; if absent, fall back to UTC date.
    measurement_date = _infer_measurement_date(series)

    data = DailyData(
        eic=eic,
        role=role,
        measurement_date=measurement_date,
        file_version=file_version,
        document_number=document_number,
        document_datetime_utc=document_datetime_utc,
        series=series,
        warnings=role_warnings,
    )

    _validate_quarter_counts(data)
    _reconcile_in_place(data)
    return data


def _find_nad_gn(root) -> object | None:
    for nad in root.findall("./NAD"):
        if _text(nad.find("ACTION")) == "GN":
            return nad
    return None


def _parse_quarters(lin_el) -> Iterable[Quarter]:
    for qty in lin_el.findall("QTY"):
        qualifier = _text(qty.find("QUANTITY_QUALIFIER"))
        # 136 = delivered quantity. Other qualifiers are unexpected for SZE_7;
        # accept them silently rather than rejecting the whole file.
        del qualifier
        value_text = _text(qty.find("QUANTITY"))
        if value_text is None:
            continue
        try:
            value_kw = float(value_text)
        except ValueError as exc:
            raise MsconsParseError(
                f"Non-numeric QUANTITY value: {value_text!r}"
            ) from exc

        start_dt: datetime | None = None
        end_dt: datetime | None = None
        for dtm in qty.findall("DTM"):
            qualifier_text = _text(dtm.find("DATUMQUALIFIER"))
            datum_text = _text(dtm.find("DATUM"))
            if datum_text is None:
                continue
            parsed = _parse_datum(datum_text)
            if qualifier_text == "158":
                start_dt = parsed
            elif qualifier_text == "159":
                end_dt = parsed
        if start_dt is None:
            raise MsconsParseError("QTY without period-start DTM(158)")
        if end_dt is None:
            end_dt = start_dt + timedelta(minutes=15)

        yield Quarter(
            period_start_utc=start_dt,
            period_end_utc=end_dt,
            value_kw=value_kw,
        )


def _parse_datum(datum: str) -> datetime:
    """Parse a DATUM string into a tz-aware UTC datetime.

    Two encodings:
    - Basic ISO with TZ abbreviation suffix: ``202605030000CEST``
    - Extended ISO 8601: ``2026-05-03T00:00:00.000`` (optional offset/Z)
    """
    datum = datum.strip()
    m = _BASIC_ISO_RE.match(datum)
    if m:
        ts, tz_abbr = m.groups()
        try:
            naive = datetime.strptime(ts, "%Y%m%d%H%M")
        except ValueError as exc:
            raise MsconsParseError(f"Bad basic DATUM: {datum!r}") from exc
        offset = _TZ_OFFSETS.get(tz_abbr)
        if offset is None:
            raise MsconsParseError(f"Unknown TZ abbreviation: {tz_abbr!r}")
        local = naive.replace(tzinfo=timezone(offset))
        return local.astimezone(timezone.utc)
    # Extended ISO. Python's fromisoformat handles 'Z' from 3.11+.
    iso = datum.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise MsconsParseError(f"Unparseable DATUM: {datum!r}") from exc
    if parsed.tzinfo is None:
        # No offset given; the file is from OKTE, so interpret as local
        # Bratislava time. Folded-hour case on fall-back resolves to the
        # first occurrence (CEST), which is the saner default for daily
        # files because they're aligned to clock-quarters.
        parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Bratislava"))
    return parsed.astimezone(timezone.utc)


def _classify_by_lins(series: Mapping[str, list[Quarter]]) -> str | None:
    """Return the role implied by which LIN codes are present, or None if ambiguous.

    PS15 / CPS15 are offtake-exclusive; PM15 / CPM15 are producer-exclusive.
    SHA15 appears for both roles, so it's not used for classification.
    """
    keys = set(series.keys())
    has_offtake = bool(keys & {LIN_PS15, LIN_CPS15})
    has_producer = bool(keys & {LIN_PM15, LIN_CPM15})
    if has_offtake and not has_producer:
        return ROLE_OFFTAKE
    if has_producer and not has_offtake:
        return ROLE_PRODUCER
    return None


def _infer_measurement_date(series: Mapping[str, list[Quarter]]) -> date:
    first_quarter = next(iter(series.values()))[0]
    local = first_quarter.period_start_utc.astimezone(
        ZoneInfo("Europe/Bratislava")
    )
    return local.date()


def _validate_quarter_counts(data: DailyData) -> None:
    """Warn about unexpected quarter counts.

    Valid counts: 96 (normal), 92 (spring-forward DST), 100 (fall-back DST).
    Anything else: log a warning but do not reject; downstream may still
    aggregate it usefully.
    """
    counts = {lin: len(qs) for lin, qs in data.series.items()}
    unique = set(counts.values())
    if not unique.issubset({92, 96, 100}):
        data.warnings.append(
            f"Unexpected quarter count(s): {counts}"
        )
    if len(unique) > 1:
        data.warnings.append(
            f"Inconsistent quarter counts across series: {counts}"
        )
    for lin, qs in data.series.items():
        for q in qs:
            if abs(q.value_kw) * 0.25 > QUARTER_KWH_SANITY_CEILING:
                data.warnings.append(
                    f"Quarter value out of sanity range in {lin}: {q.value_kw}"
                )
                break


def _reconcile_in_place(data: DailyData) -> None:
    """Compute the largest per-interval invariant deviation.

    Offtake invariant: PS15 - SHA15 - CPS15 ≈ 0
    Producer invariant: PM15 - SHA15 - CPM15 ≈ 0

    Stored in ``data.reconciliation_max_delta_kwh`` (kWh, i.e. kW * 0.25).
    """
    if data.role == ROLE_OFFTAKE:
        keys = (LIN_PS15, LIN_SHA15, LIN_CPS15)
    else:
        keys = (LIN_PM15, LIN_SHA15, LIN_CPM15)

    if not all(k in data.series for k in keys):
        return

    base, shared, residual = (data.series[k] for k in keys)
    if not (len(base) == len(shared) == len(residual)):
        data.warnings.append(
            "Reconciliation skipped: series length mismatch"
        )
        return

    max_delta_kwh = 0.0
    for b, s, r in zip(base, shared, residual):
        delta_kw = b.value_kw - s.value_kw - r.value_kw
        delta_kwh = abs(delta_kw) * 0.25
        if delta_kwh > max_delta_kwh:
            max_delta_kwh = delta_kwh
    data.reconciliation_max_delta_kwh = max_delta_kwh


def _text(element) -> str | None:
    if element is None:
        return None
    text = element.text
    if text is None:
        return None
    return text.strip()
