"""Long-term-statistics helpers.

Two responsibilities:

1. Convert a list of 15-minute :class:`Quarter` values to hourly buckets,
   anchored on the UTC start of each *local* hour. The local-hour grouping
   produces 23 / 24 / 25 buckets on DST-affected days without special-casing.
2. Push these hourly buckets to HA's recorder via
   ``async_import_statistics`` so they show up in the Energy dashboard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .mscons import Quarter

# HA imports are deferred to keep the pure aggregation helpers
# (quarters_to_hourly / build_statistic_data) importable in unit tests
# that don't have homeassistant installed.

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HourlyBucket:
    """One hourly statistics row."""

    start_utc: datetime
    kwh: float


def quarters_to_hourly(
    quarters: Iterable[Quarter],
    *,
    local_tz: str = "Europe/Bratislava",
) -> list[HourlyBucket]:
    """Aggregate 15-minute kW values to hourly kWh buckets.

    Grouping key: each quarter's local-time hour (so DST spring-forward
    yields 23 buckets, fall-back yields 25). Each kWh = kW * 0.25h.
    """
    tz = ZoneInfo(local_tz)
    by_hour: dict[datetime, float] = {}
    order: list[datetime] = []
    for quarter in quarters:
        energy_kwh = quarter.value_kw * 0.25
        local_start = quarter.period_start_utc.astimezone(tz)
        local_hour_start = local_start.replace(minute=0, second=0, microsecond=0)
        hour_start_utc = local_hour_start.astimezone(timezone.utc)
        if hour_start_utc not in by_hour:
            by_hour[hour_start_utc] = 0.0
            order.append(hour_start_utc)
        by_hour[hour_start_utc] += energy_kwh
    return [HourlyBucket(start_utc=h, kwh=by_hour[h]) for h in order]


async def get_last_cumulative(
    hass,
    statistic_id: str,
) -> tuple[float | None, datetime | None]:
    """Return ``(last_sum, last_start_utc)`` for ``statistic_id``.

    Used to seed the running-cumulative-kWh counter when importing a new
    day's buckets so the resulting ``state``/``sum`` series stays
    monotonically increasing across days and restarts.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import get_last_statistics

    instance = get_instance(hass)
    last = await instance.async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        statistic_id,
        True,
        {"sum", "start"},
    )
    if not last or statistic_id not in last:
        return None, None
    entries = last[statistic_id]
    if not entries:
        return None, None
    entry = entries[0]
    sum_value = entry.get("sum")
    start = entry.get("start")
    start_dt: datetime | None
    if isinstance(start, (int, float)):
        start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
    elif isinstance(start, datetime):
        start_dt = (
            start
            if start.tzinfo is not None
            else start.replace(tzinfo=timezone.utc)
        )
    else:
        start_dt = None
    return (
        float(sum_value) if sum_value is not None else None,
        start_dt,
    )


def build_statistic_data(
    buckets: Iterable[HourlyBucket],
    *,
    starting_sum: float,
) -> list[dict]:
    """Compose a sequence of StatisticData rows with a running sum.

    Each row is a plain ``dict`` matching the keys of
    :class:`homeassistant.components.recorder.models.StatisticData`
    (a TypedDict at runtime). Returning plain dicts keeps this helper
    importable without homeassistant installed.
    """
    rows: list[dict] = []
    running = starting_sum
    for bucket in buckets:
        running += bucket.kwh
        rows.append(
            {
                "start": bucket.start_utc,
                "state": running,
                "sum": running,
            }
        )
    return rows


def import_hourly_statistics(
    hass,
    statistic_id: str,
    statistic_name: str | None,
    rows: list[dict],
) -> None:
    """Push hourly rows to HA's recorder. Idempotent on (statistic_id, start)."""
    if not rows:
        return
    from homeassistant.components.recorder.models import StatisticMetaData
    from homeassistant.components.recorder.statistics import (
        async_import_statistics,
    )

    metadata = StatisticMetaData(
        has_mean=False,
        has_sum=True,
        name=statistic_name,
        source="recorder",
        statistic_id=statistic_id,
        unit_of_measurement="kWh",
    )
    async_import_statistics(hass, metadata, rows)


__all__ = [
    "HourlyBucket",
    "build_statistic_data",
    "get_last_cumulative",
    "import_hourly_statistics",
    "quarters_to_hourly",
]
