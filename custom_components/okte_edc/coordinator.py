"""DataUpdateCoordinator for the OKTE EDC integration.

The coordinator runs on a configurable polling interval (default 30 min)
but only actually contacts IMAP within a configurable window (default
09:00-13:00 Europe/Bratislava). Outside the window it returns the
previously cached state and skips network I/O.

State held in memory:

- ``last_processed_versions[(eic, date)] = file_version`` — used to skip
  files we've already imported at the same or higher version.
- ``last_cumulative[(eic, suffix)] = kWh`` — running cumulative used as the
  ``starting_sum`` when importing new hourly statistics. Seeded from the
  recorder on first poll so it survives HA restarts.
- ``data[eic][suffix]`` — values surfaced to sensor entities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CLEANUP_ARCHIVE,
    CLEANUP_DELETE,
    CONF_EICS,
    CONF_FOLDER,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USE_SSL,
    CONF_USERNAME,
    DEFAULT_ARCHIVE_FOLDER,
    DEFAULT_DELETE_AFTER_DAYS,
    DEFAULT_EMAIL_CLEANUP,
    DEFAULT_MAX_BACKFILL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POLL_TIMEZONE,
    DEFAULT_POLL_WINDOW_END,
    DEFAULT_POLL_WINDOW_START,
    DEFAULT_SCAN_WINDOW_DAYS,
    DEFAULT_SENDER_ALLOWLIST,
    DOMAIN,
    OPT_ARCHIVE_FOLDER,
    OPT_DELETE_AFTER_DAYS,
    OPT_EMAIL_CLEANUP,
    OPT_MAX_BACKFILL,
    OPT_POLL_INTERVAL,
    OPT_POLL_TIMEZONE,
    OPT_POLL_WINDOW_END,
    OPT_POLL_WINDOW_START,
    OPT_SCAN_WINDOW_DAYS,
    OPT_SENDER_ALLOWLIST,
    RECONCILIATION_THRESHOLD_KWH,
    ROLE_OFFTAKE,
    ROLE_PRODUCER,
    SENSOR_TO_LIN,
    SUFFIX_FILE_VERSION,
    SUFFIX_LAST_IMPORT,
    SUFFIX_MEASUREMENT_DATE,
    SUFFIX_PARSE_WARNINGS,
    SUFFIX_RECONCILIATION_DELTA,
    detect_role,
    parse_sender_allowlist,
    statistic_id_for,
    statistic_name_for,
)
from .imap_client import (
    Attachment,
    ImapAuthError,
    ImapClient,
    ImapConnectionError,
    ImapFolderError,
)
from .mscons import DailyData, MsconsParseError, parse_mscons
from .statistics import (
    HourlyBucket,
    get_last_cumulative,
    import_hourly_statistics,
    quarters_to_hourly,
)

_LOGGER = logging.getLogger(__name__)

# Persistence schema for ``Store`` — bump on any incompatible change to
# the on-disk shape.
_STORE_VERSION = 1


def _state_store_key(entry: ConfigEntry) -> str:
    """Per-entry storage key. One file per integration instance."""
    return f"{DOMAIN}.{entry.entry_id}.state"


class OkteCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Coordinator that ingests OKTE MSCONS files into HA statistics."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self._enabled_eics: dict[str, str] = _enabled_eics_from_entry(entry)
        self._client = _client_from_entry(entry)
        self._cumulative_seeded: set[tuple[str, str]] = set()
        self._last_cumulative: dict[tuple[str, str], float] = {}
        self._last_processed_versions: dict[tuple[str, date], int] = {}
        # Persistent processed-state. Survives HA restarts and is the
        # authoritative source of truth on keyword-fallback IMAP servers
        # (where we can't rely on an IMAP-side marker). Loaded lazily on
        # the first refresh; saved after every poll that touched it.
        self._store: Store = Store(hass, _STORE_VERSION, _state_store_key(entry))
        self._store_loaded = False
        # The first refresh always polls — otherwise restarting HA outside
        # the configured window would leave entities unavailable until the
        # next in-window cycle, even if a fresh email has already arrived.
        self._first_refresh_done = False
        # Per-EIC summary of the most recently processed file. Used by the
        # diagnostics dump for remote triage.
        self._last_import_summary: dict[str, dict[str, Any]] = {}
        # Operational metrics surfaced on the service-level device.
        self.last_poll_at: datetime | None = None
        self.last_successful_poll_at: datetime | None = None
        self.last_poll_stats: dict[str, Any] = {}
        # Per-poll list of human-readable issue descriptions for messages
        # that were rejected mid-flow (sender allowlist mismatch, parse
        # error, EIC/date cross-check failure). The service-level
        # `last_poll_issues` sensor surfaces ``len(self.last_poll_issues)``
        # as its state and the full list in its attributes, so users can
        # see at a glance that the last poll wasn't fully clean without
        # having to open the HA log.
        self.last_poll_issues: list[str] = []
        # One-shot override: the next poll bypasses the dynamic SINCE
        # cutoff and asks the IMAP server for everything matching the
        # subject filter. Set by the "Full mailbox scan" diagnostic
        # button; auto-reset after the poll completes.
        self._force_full_scan = False

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry.entry_id}",
            update_interval=_update_interval(entry),
            always_update=False,
        )

    @property
    def enabled_eics(self) -> dict[str, str]:
        """Return {eic: role} for currently enabled metering points."""
        return self._enabled_eics

    @property
    def next_poll_at(self) -> datetime | None:
        """Best-effort next-scheduled-poll timestamp.

        Computed as ``last_poll_at + update_interval``. HA tracks the
        actual scheduled callback internally but doesn't expose it; this
        is close enough for a "when will the integration check again"
        sensor (off by milliseconds at worst).
        """
        if self.last_poll_at is None or self.update_interval is None:
            return None
        return self.last_poll_at + self.update_interval

    def last_import_summary_for(self, eic: str) -> dict[str, Any]:
        """Return the most-recent-file summary for ``eic`` or an empty dict."""
        return self._last_import_summary.get(eic, {})

    async def async_trigger_full_rescan(self) -> None:
        """Run an immediate poll with the SINCE cutoff disabled.

        Used by the "Full mailbox scan" diagnostic button to pull in
        older OKTE mail that's outside the dynamic SINCE window —
        forwarded historical messages, V2 corrections that arrived after
        we'd already moved our SINCE bound past them, etc.
        """
        self._force_full_scan = True
        await self.async_request_refresh()

    # Number of days of slack to keep in the steady-state SINCE bound.
    # OKTE has been observed to issue V2/V3 correction files a few days
    # after the initial publication; this buffer makes sure the search
    # still picks them up.
    _CORRECTION_BUFFER_DAYS = 7

    def _compute_search_cutoff(self, scan_window_days: int) -> datetime:
        """Return the SINCE-bound timestamp for the runtime poll search.

        Steady-state: ``last_processed_date - 7 days`` so the server is
        only asked for a week or so of mail per cycle. On first run /
        empty store: fall back to ``today - scan_window_days`` so the
        initial backfill window is the user-configured default.

        The chosen cutoff is the *more recent* of the two — never wider
        than the configured backfill, and never narrower than the
        correction-buffer once steady-state.
        """
        now = datetime.now(tz=timezone.utc)
        scan_cutoff = now - timedelta(days=scan_window_days)
        if not self._last_processed_versions:
            return scan_cutoff
        latest_date = max(d for _eic, d in self._last_processed_versions.keys())
        steady_cutoff = datetime(
            latest_date.year,
            latest_date.month,
            latest_date.day,
            tzinfo=timezone.utc,
        ) - timedelta(days=self._CORRECTION_BUFFER_DAYS)
        return max(scan_cutoff, steady_cutoff)

    def update_from_options(self) -> None:
        """Apply config-entry option changes to runtime state.

        Called by the entry update-listener so polling interval / window
        / per-EIC enable toggles take effect on the next cycle.
        """
        self.update_interval = _update_interval(self.entry)
        self._enabled_eics = _enabled_eics_from_entry(self.entry)
        # Drop seeding for EICs that may have been disabled then re-enabled
        # so we re-query the recorder cleanly.
        seeded = set(self._cumulative_seeded)
        for key in seeded:
            if key[0] not in self._enabled_eics:
                self._cumulative_seeded.discard(key)
                self._last_cumulative.pop(key, None)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        if not self._enabled_eics:
            return self.data or {}

        await self._load_state_if_needed()

        now_local = _local_now(self.entry)
        in_window = _within_window(self.entry, now_local)
        bypass_window = self._force_full_scan
        if not in_window and self._first_refresh_done and not bypass_window:
            _LOGGER.debug(
                "Outside polling window (%s); skipping IMAP fetch",
                now_local.strftime("%H:%M"),
            )
            return self.data or {}
        if not in_window:
            _LOGGER.debug(
                "Outside polling window but %s — polling anyway",
                "full rescan requested" if bypass_window else "first refresh",
            )

        await self._seed_cumulative_if_needed()

        self.last_poll_at = datetime.now(tz=timezone.utc)
        try:
            updates, pending_by_stat = await self.hass.async_add_executor_job(
                self._poll_sync
            )
        except ImapAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except ImapFolderError as exc:
            raise ConfigEntryError(str(exc)) from exc
        except ImapConnectionError as exc:
            raise UpdateFailed(str(exc)) from exc

        # Async import path: each affected stat_id is recomputed from
        # its earliest changed date forward, so V2 corrections and out-
        # of-order backfills land at the correct cumulative offset.
        for (eic, suffix), items in pending_by_stat.items():
            final_sum = await self._recompute_and_import_stat(
                eic, suffix, items
            )
            if final_sum is not None:
                self._last_cumulative[(eic, suffix)] = final_sum
                updates.setdefault(eic, {})[suffix] = final_sum

        self.last_successful_poll_at = datetime.now(tz=timezone.utc)

        merged = dict(self.data or {})
        for eic, values in updates.items():
            merged.setdefault(eic, {}).update(values)
        self._first_refresh_done = True
        if pending_by_stat:
            await self._save_state()
        return merged

    # ----- persistent state ---------------------------------------------

    async def _load_state_if_needed(self) -> None:
        """Hydrate ``_last_processed_versions`` from HA Store on first use.

        Done lazily on the first refresh rather than in ``__init__`` so the
        coordinator constructor stays sync. Subsequent calls are no-ops.
        """
        if self._store_loaded:
            return
        raw = await self._store.async_load() or {}
        versions = raw.get("last_processed_versions", {}) or {}
        recovered = 0
        for eic, by_date in versions.items():
            if not isinstance(by_date, dict):
                continue
            for date_iso, version in by_date.items():
                try:
                    d = date.fromisoformat(date_iso)
                    v = int(version)
                except (TypeError, ValueError):
                    continue
                self._last_processed_versions[(eic, d)] = v
                recovered += 1
        self._store_loaded = True
        if recovered:
            _LOGGER.debug(
                "Loaded %d processed-state entries from %s",
                recovered,
                self._store.key,
            )

    async def _save_state(self) -> None:
        """Persist ``_last_processed_versions`` to HA Store."""
        nested: dict[str, dict[str, int]] = {}
        for (eic, d), version in self._last_processed_versions.items():
            nested.setdefault(eic, {})[d.isoformat()] = version
        await self._store.async_save({"last_processed_versions": nested})

    async def async_remove_storage(self) -> None:
        """Delete the persistent state file for this entry."""
        await self._store.async_remove()

    # ----- seeding ------------------------------------------------------

    async def _seed_cumulative_if_needed(self) -> None:
        """Initialise running-cumulative kWh from the recorder for each enabled sensor."""
        for eic, role in self._enabled_eics.items():
            for (mapped_role, suffix), _lin in SENSOR_TO_LIN.items():
                if mapped_role != role:
                    continue
                key = (eic, suffix)
                if key in self._cumulative_seeded:
                    continue
                statistic_id = statistic_id_for(eic, suffix)
                last_sum, _ = await get_last_cumulative(self.hass, statistic_id)
                self._last_cumulative[key] = last_sum or 0.0
                self._cumulative_seeded.add(key)

    # ----- async recompute & import -----------------------------------

    async def _recompute_and_import_stat(
        self,
        eic: str,
        suffix: str,
        new_items: list[tuple[date, list[HourlyBucket]]],
    ) -> float | None:
        """Rewrite hourly stats for ``(eic, suffix)`` from the earliest
        changed date forward.

        Algorithm (uniform for all imports — fresh data, V2 corrections,
        backfill of older dates):

        1. Pin a ``baseline_sum`` = the recorder's sum at the last hour
           strictly before the earliest changed date (or zero if no
           prior data).
        2. Read every existing hourly row at or after that date and
           recover its per-hour kWh delta from the existing sum series.
        3. Overlay the new hourly kWh values from this batch on top of
           that map, replacing any old values for hours we have new
           data for.
        4. Walk the merged hours in ascending order, recomputing the
           running sum from ``baseline_sum``.
        5. Push the recomputed rows to the recorder. ``async_import_statistics``
           upserts on ``(statistic_id, start)``, so unchanged hours
           round-trip with the same delta but a possibly different
           cumulative sum (correcting for any earlier overlay).

        Returns the final running sum, or None if there was no work to
        do (caller uses this to update ``_last_cumulative``).
        """
        if not new_items:
            return None
        statistic_id = statistic_id_for(eic, suffix)

        earliest_date = min(d for d, _ in new_items)
        from_utc = _local_date_start_utc(
            earliest_date, self.entry
        )

        baseline_sum = await self._get_sum_before(statistic_id, from_utc)
        existing = await self._get_existing_hourly_stats_from(
            statistic_id, from_utc
        )

        # Reconstruct per-hour deltas from the existing sum series.
        deltas: dict[datetime, float] = {}
        prev = baseline_sum
        for row in sorted(existing, key=lambda r: _row_start_dt(r["start"])):
            s = row.get("sum")
            if s is None:
                continue
            start_dt = _row_start_dt(row["start"])
            deltas[start_dt] = float(s) - prev
            prev = float(s)

        # Overlay the new buckets.
        for _d, buckets in new_items:
            for bucket in buckets:
                deltas[bucket.start_utc] = bucket.kwh

        if not deltas:
            return None

        rows: list[dict[str, Any]] = []
        running = baseline_sum
        for start_dt in sorted(deltas):
            running += deltas[start_dt]
            rows.append({"start": start_dt, "state": running, "sum": running})

        import_hourly_statistics(
            self.hass,
            statistic_id,
            statistic_name_for(eic, suffix),
            rows,
        )
        return running

    async def _get_sum_before(
        self, statistic_id: str, before_utc: datetime
    ) -> float:
        """Return the cumulative sum at the last hourly row strictly before ``before_utc``."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        # 30-day lookback is plenty: HA's hourly statistics are
        # contiguous for an active sensor, so the most recent prior
        # bucket is at most ~1 hour back in steady state.
        lookback_start = before_utc - timedelta(days=30)
        instance = get_instance(self.hass)
        result = await instance.async_add_executor_job(
            statistics_during_period,
            self.hass,
            lookback_start,
            before_utc,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = result.get(statistic_id, [])
        if not rows:
            return 0.0
        latest = max(rows, key=lambda r: _row_start_dt(r["start"]))
        sum_val = latest.get("sum")
        return float(sum_val) if sum_val is not None else 0.0

    async def _get_existing_hourly_stats_from(
        self, statistic_id: str, from_utc: datetime
    ) -> list[dict[str, Any]]:
        """Return all hourly rows for ``statistic_id`` with ``start >= from_utc``."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        # Open-ended end: query 'far enough into the future' to cover
        # every row HA has on file.
        end = datetime.now(tz=timezone.utc) + timedelta(days=1)
        instance = get_instance(self.hass)
        result = await instance.async_add_executor_job(
            statistics_during_period,
            self.hass,
            from_utc,
            end,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        return result.get(statistic_id, [])

    # ----- sync poll body ----------------------------------------------

    def _poll_sync(
        self,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[tuple[str, str], list[tuple[date, list[HourlyBucket]]]],
    ]:
        """Run one IMAP poll cycle (sync, executed in HA executor).

        Returns ``(updates, pending_by_stat)``:

        - ``updates`` is the per-EIC sensor-state dict the coordinator
          merges into ``self.data``. Non-cumulative diagnostic state
          (last_import timestamp, file_version, reconciliation_delta,
          measurement_date, parse_warnings) is populated here. The
          cumulative energy values are filled in later, after the
          async recompute pass writes the new statistic sums.
        - ``pending_by_stat`` is the work the async caller passes to
          ``_recompute_and_import_stat``: per (eic, suffix), the list
          of (date, hourly_buckets) tuples that need to land in the
          recorder. All cumulative-sum maths happens in the recompute
          path so that out-of-order imports (V2 corrections that arrive
          in a later poll than V1, or backfill of dates older than
          what's already in the recorder) end up with correct sums for
          every subsequent hour.
        """
        updates: dict[str, dict[str, Any]] = {}
        pending_by_stat: dict[
            tuple[str, str], list[tuple[date, list[HourlyBucket]]]
        ] = {}

        cleanup_mode = self.entry.options.get(
            OPT_EMAIL_CLEANUP, DEFAULT_EMAIL_CLEANUP
        )
        scan_window_days = int(
            self.entry.options.get(
                OPT_SCAN_WINDOW_DAYS, DEFAULT_SCAN_WINDOW_DAYS
            )
        )
        max_backfill = int(
            self.entry.options.get(OPT_MAX_BACKFILL, DEFAULT_MAX_BACKFILL)
        )
        sender_allowlist = parse_sender_allowlist(
            self.entry.options.get(
                OPT_SENDER_ALLOWLIST, DEFAULT_SENDER_ALLOWLIST
            )
        )

        # Consume the full-rescan flag (one-shot).
        force_full_scan = self._force_full_scan
        self._force_full_scan = False

        # Reset the per-poll issue list. Anything we append below shows
        # up on the service device's `Last poll issues` sensor at the
        # end of the cycle.
        issues: list[str] = []

        session = self._client.open_session()
        matched_count = 0
        processed_count = 0
        skipped_count = 0
        try:
            # 1. Delete-after-N-days cleanup. Semantically "old OKTE-
            # subject mail" — anything older than the cutoff that
            # matches the integration's subject filter is in scope.
            if cleanup_mode == CLEANUP_DELETE:
                delete_after = int(
                    self.entry.options.get(
                        OPT_DELETE_AFTER_DAYS, DEFAULT_DELETE_AFTER_DAYS
                    )
                )
                cutoff_delete = datetime.now(tz=timezone.utc) - timedelta(
                    days=delete_after
                )
                old_uids = session.search_subject_before(cutoff_delete)
                if old_uids:
                    session.mark_for_delete(old_uids)
                    session.expunge()

            # 2. Find new messages.
            if force_full_scan:
                _LOGGER.info(
                    "Full mailbox scan requested; ignoring dynamic SINCE cutoff"
                )
                cutoff = None
            else:
                # The SINCE bound is the more recent of the configured
                # scan_window_days fallback (first run / fresh install)
                # and `last_processed_date - 7 days` (steady-state),
                # so once the integration is caught up each poll only
                # asks the server for ~a week of mail. The 7-day buffer
                # covers OKTE V2/V3 correction files which can arrive a
                # few days after the original.
                cutoff = self._compute_search_cutoff(scan_window_days)
            uids = session.search_unprocessed_uids(since=cutoff)
            if max_backfill and len(uids) > max_backfill:
                _LOGGER.warning(
                    "Found %d unprocessed messages; truncating to "
                    "max_backfill=%d (older ones will be picked up "
                    "on subsequent cycles)",
                    len(uids),
                    max_backfill,
                )
                uids = uids[-max_backfill:]

            min_file_date = (
                None
                if force_full_scan
                else datetime.now(tz=timezone.utc).date()
                - timedelta(days=scan_window_days)
            )
            matched_count = len(uids)

            # Fetch + parse + filter all candidates first so we can sort
            # them chronologically before importing. Each item is
            # (uid, att, parsed_data).
            candidates: list[tuple[bytes, Attachment, DailyData]] = []
            for uid in uids:
                msg = session.fetch_message(uid)
                if msg is None:
                    skipped_count += 1
                    continue
                if sender_allowlist and msg.sender not in sender_allowlist:
                    sender = msg.sender or "<unknown>"
                    _LOGGER.warning(
                        "Rejecting UID %s: sender %r not in allowlist %r — "
                        "the message will not be processed. Adjust the "
                        "sender allowlist in the integration options if "
                        "this is a legitimate forwarder.",
                        uid,
                        sender,
                        sender_allowlist,
                    )
                    issues.append(
                        f"Rejected message from {sender}: not in sender allowlist"
                    )
                    skipped_count += 1
                    continue
                if not msg.attachments:
                    _LOGGER.debug(
                        "Skipping UID %s — subject matched but no MSCONS attachments",
                        uid,
                    )
                    skipped_count += 1
                    continue

                for att in msg.attachments:
                    try:
                        file_date = _parse_file_date(att.file_date)
                    except ValueError:
                        _LOGGER.warning(
                            "Skipping attachment with bad date %s",
                            att.filename,
                        )
                        issues.append(
                            f"Skipped attachment {att.filename}: "
                            f"unparseable date in filename"
                        )
                        continue
                    if (
                        min_file_date is not None
                        and file_date < min_file_date
                    ):
                        continue
                    if att.eic not in self._enabled_eics:
                        continue
                    prev_version = self._last_processed_versions.get(
                        (att.eic, file_date)
                    )
                    if (
                        prev_version is not None
                        and prev_version >= att.file_version
                    ):
                        continue
                    try:
                        data = parse_mscons(att.payload)
                    except MsconsParseError as exc:
                        _LOGGER.error(
                            "Failed to parse %s: %s",
                            att.filename,
                            exc,
                        )
                        issues.append(
                            f"Failed to parse {att.filename}: {exc}"
                        )
                        continue
                    if att.eic.upper() != data.eic.upper():
                        _LOGGER.warning(
                            "Attachment %s declares EIC %s in filename but %s "
                            "in XML PLACE_ID; rejecting to prevent EIC spoofing",
                            att.filename,
                            att.eic,
                            data.eic,
                        )
                        issues.append(
                            f"Rejected {att.filename}: filename EIC {att.eic} "
                            f"!= XML PLACE_ID {data.eic}"
                        )
                        continue
                    if file_date != data.measurement_date:
                        _LOGGER.warning(
                            "Attachment %s declares date %s in filename but "
                            "XML covers %s; rejecting due to date mismatch",
                            att.filename,
                            file_date.isoformat(),
                            data.measurement_date.isoformat(),
                        )
                        issues.append(
                            f"Rejected {att.filename}: filename date "
                            f"{file_date.isoformat()} != XML date "
                            f"{data.measurement_date.isoformat()}"
                        )
                        continue
                    candidates.append((uid, att, data))

            # Sort by (measurement_date asc, file_version desc) so higher
            # versions of the same date come first. The de-dup loop then
            # keeps only the first occurrence per (eic, date) = the
            # highest version available in this batch.
            candidates.sort(
                key=lambda c: (c[2].measurement_date, -c[1].file_version)
            )

            seen_keys: set[tuple[str, date]] = set()
            deduped: list[tuple[bytes, Attachment, DailyData]] = []
            for uid, att, data in candidates:
                key = (att.eic, data.measurement_date)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped.append((uid, att, data))

            # Re-sort ascending by date for chronological processing.
            deduped.sort(key=lambda c: c[2].measurement_date)

            for uid, att, data in deduped:
                self._collect_pending(att, data, updates, pending_by_stat)
                self._last_processed_versions[
                    (att.eic, data.measurement_date)
                ] = att.file_version
                processed_count += 1

                if cleanup_mode == CLEANUP_ARCHIVE:
                    archive_folder = self.entry.options.get(
                        OPT_ARCHIVE_FOLDER, DEFAULT_ARCHIVE_FOLDER
                    )
                    ok = session.archive(uid, archive_folder)
                    if not ok:
                        _LOGGER.warning(
                            "Archive folder %s missing; falling back "
                            "to leave-in-place",
                            archive_folder,
                        )

            if cleanup_mode == CLEANUP_ARCHIVE:
                session.expunge()
        finally:
            session.close()

        self.last_poll_stats = {
            "matched": matched_count,
            "processed": processed_count,
            "skipped": skipped_count,
        }
        self.last_poll_issues = issues
        return updates, pending_by_stat

    def _collect_pending(
        self,
        att: Attachment,
        data: DailyData,
        updates: dict[str, dict[str, Any]],
        pending_by_stat: dict[
            tuple[str, str], list[tuple[date, list[HourlyBucket]]]
        ],
    ) -> None:
        """Stage one parsed file's hourly buckets and diagnostic state.

        The cumulative kWh value of each energy sensor is *not* set
        here; the async recompute path fills that in once the new
        running-sum series is known.

        The per-EIC freshness/diagnostic sensors (last_import,
        file_version, measurement_date, reconciliation_delta,
        parse_warnings) only update when this file's measurement_date
        is at least as recent as any previously imported file for the
        same EIC. A backfill of an older day enriches the recorder
        but doesn't roll those sensors backward in time — the "what
        just ran" angle lives on the service device instead.
        """
        if data.warnings:
            for w in data.warnings:
                _LOGGER.warning("[%s] %s", att.filename, w)
        if data.reconciliation_max_delta_kwh > RECONCILIATION_THRESHOLD_KWH:
            _LOGGER.warning(
                "Reconciliation delta for %s on %s = %.6f kWh (>%.6f)",
                data.eic,
                data.measurement_date,
                data.reconciliation_max_delta_kwh,
                RECONCILIATION_THRESHOLD_KWH,
            )

        # Always enqueue the hourly buckets — backfills need to land
        # in the recorder regardless of date order; the recompute
        # path handles them correctly.
        for (mapped_role, suffix), lin in SENSOR_TO_LIN.items():
            if mapped_role != data.role:
                continue
            quarters = data.series.get(lin)
            if not quarters:
                continue
            buckets = quarters_to_hourly(quarters)
            pending_by_stat.setdefault((data.eic, suffix), []).append(
                (data.measurement_date, buckets)
            )

        known_max = max(
            (
                d
                for (eic, d) in self._last_processed_versions
                if eic == data.eic
            ),
            default=None,
        )
        if known_max is not None and data.measurement_date < known_max:
            # Older-than-latest backfill: stats land, freshness sensors
            # keep pointing at whatever the latest day was.
            return

        eic_state = updates.setdefault(data.eic, {})
        self._last_import_summary[data.eic] = {
            "filename": att.filename,
            "measurement_date": data.measurement_date.isoformat(),
            "file_version": att.file_version,
            "role": data.role,
            "reconciliation_max_delta_kwh": data.reconciliation_max_delta_kwh,
            "per_lin_kwh": {
                lin: round(sum(q.value_kw for q in qs) * 0.25, 6)
                for lin, qs in data.series.items()
            },
            "quarter_counts": {lin: len(qs) for lin, qs in data.series.items()},
            "warnings": list(data.warnings),
        }
        eic_state[SUFFIX_LAST_IMPORT] = datetime.now(tz=timezone.utc)
        eic_state[SUFFIX_FILE_VERSION] = att.file_version
        eic_state[SUFFIX_RECONCILIATION_DELTA] = (
            data.reconciliation_max_delta_kwh
        )
        eic_state[SUFFIX_MEASUREMENT_DATE] = data.measurement_date
        eic_state[SUFFIX_PARSE_WARNINGS] = len(data.warnings)


# ---------------------------------------------------------------------------
# Discovery (used by config flow)


@dataclass
class DiscoveryResult:
    """Outcome of a single mailbox scan during config / options flow."""

    eics: list[tuple[str, str]]  # sorted [(eic, role)]
    senders: list[str]  # unique, lowercased, sorted From addresses
    matched_uid_count: int  # how many subject-matched messages were inspected


def discover_eics(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str,
    use_ssl: bool,
    *,
    scan_window_days: int = DEFAULT_SCAN_WINDOW_DAYS,
    max_messages: int = 100,
) -> DiscoveryResult:
    """Connect to IMAP and discover EICs + observed senders.

    Runs synchronously; callers wrap in an executor job. The session is
    opened and closed within this function.

    Returned senders are deduped and lowercased, drawn from the From
    headers of every subject-matching message. Empty / unparseable From
    is skipped. The config flow seeds the sender_allowlist option with
    this list so forwarded mailboxes work out of the box.
    """
    client = ImapClient(host, port, username, password, folder, use_ssl)
    session = client.open_session()
    discovered: dict[str, str] = {}
    senders: set[str] = set()
    matched_count = 0
    try:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=scan_window_days)
        uids = session.search_recent_subject(cutoff)
        matched_count = len(uids)
        if max_messages and len(uids) > max_messages:
            uids = uids[-max_messages:]
        for uid in uids:
            msg = session.fetch_message(uid)
            if msg is None:
                continue
            if msg.sender:
                senders.add(msg.sender)
            for att in msg.attachments:
                if att.eic not in discovered:
                    discovered[att.eic] = detect_role(att.eic)
    finally:
        session.close()
    return DiscoveryResult(
        eics=sorted(discovered.items()),
        senders=sorted(senders),
        matched_uid_count=matched_count,
    )


# ---------------------------------------------------------------------------
# Helpers


def _client_from_entry(entry: ConfigEntry) -> ImapClient:
    return ImapClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        folder=entry.data[CONF_FOLDER],
        use_ssl=entry.data[CONF_USE_SSL],
    )


def _enabled_eics_from_entry(entry: ConfigEntry) -> dict[str, str]:
    result: dict[str, str] = {}
    for record in entry.data.get(CONF_EICS, []):
        if not record.get("enabled", True):
            continue
        eic = record["eic"]
        role = record.get("role") or detect_role(eic)
        if role not in (ROLE_OFFTAKE, ROLE_PRODUCER):
            role = detect_role(eic)
        result[eic] = role
    return result


def _update_interval(entry: ConfigEntry) -> timedelta:
    minutes = int(entry.options.get(OPT_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
    minutes = max(5, min(minutes, 1440))
    return timedelta(minutes=minutes)


def _local_now(entry: ConfigEntry) -> datetime:
    tz_name = entry.options.get(OPT_POLL_TIMEZONE, DEFAULT_POLL_TIMEZONE)
    return datetime.now(tz=ZoneInfo(tz_name))


def _within_window(entry: ConfigEntry, now_local: datetime) -> bool:
    start_str = entry.options.get(
        OPT_POLL_WINDOW_START, DEFAULT_POLL_WINDOW_START
    )
    end_str = entry.options.get(OPT_POLL_WINDOW_END, DEFAULT_POLL_WINDOW_END)
    start = _parse_hhmm(start_str)
    end = _parse_hhmm(end_str)
    current = now_local.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= current <= end
    # Window wraps over midnight (e.g. 22:00 - 06:00).
    return current >= start or current <= end


def _parse_hhmm(value: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return time(0, 0)


def _parse_file_date(yyyymmdd: str) -> date:
    return datetime.strptime(yyyymmdd, "%Y%m%d").date()


def _local_date_start_utc(d: date, entry: ConfigEntry) -> datetime:
    """Return the UTC instant of 00:00 local-time on date ``d``.

    Used by the recompute path to translate an MSCONS measurement date
    into the boundary timestamp the recorder's hourly buckets are
    keyed by.
    """
    tz_name = entry.options.get(OPT_POLL_TIMEZONE, DEFAULT_POLL_TIMEZONE)
    local = datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(tz_name))
    return local.astimezone(timezone.utc)


def _row_start_dt(value: Any) -> datetime:
    """Normalise a statistics row's ``start`` field to an aware UTC datetime.

    HA's recorder returns ``start`` as either a ``datetime`` (older HA)
    or a unix timestamp float (newer HA, since the statistics-storage
    rewrite).
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    raise TypeError(f"Cannot interpret statistics start as datetime: {value!r}")
