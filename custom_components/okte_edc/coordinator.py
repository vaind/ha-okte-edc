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
)
from .imap_client import (
    Attachment,
    FetchedMessage,
    ImapAuthError,
    ImapClient,
    ImapConnectionError,
    ImapFolderError,
)
from .mscons import DailyData, MsconsParseError, parse_mscons
from .statistics import (
    build_statistic_data,
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
        self.keyword_support: bool | None = None

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
        if not in_window and self._first_refresh_done:
            _LOGGER.debug(
                "Outside polling window (%s); skipping IMAP fetch",
                now_local.strftime("%H:%M"),
            )
            return self.data or {}
        if not in_window:
            _LOGGER.debug(
                "Outside polling window but this is the first refresh "
                "after setup/restart — polling anyway"
            )

        await self._seed_cumulative_if_needed()

        self.last_poll_at = datetime.now(tz=timezone.utc)
        try:
            new_data = await self.hass.async_add_executor_job(self._poll_sync)
        except ImapAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except ImapFolderError as exc:
            raise ConfigEntryError(str(exc)) from exc
        except ImapConnectionError as exc:
            raise UpdateFailed(str(exc)) from exc
        self.last_successful_poll_at = datetime.now(tz=timezone.utc)

        merged = dict(self.data or {})
        for eic, values in new_data.items():
            merged.setdefault(eic, {}).update(values)
        self._first_refresh_done = True
        if new_data:
            # Only write the store when something actually changed in
            # `_last_processed_versions`. The `new_data` dict is populated
            # in `_import_data`, which is the same path that mutates the
            # versions map, so non-empty `new_data` implies a change.
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

    # ----- sync poll body ----------------------------------------------

    def _poll_sync(self) -> dict[str, dict[str, Any]]:
        """Run one IMAP poll cycle (sync, executed in HA executor)."""
        updates: dict[str, dict[str, Any]] = {}
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

        session = self._client.open_session()
        self.keyword_support = session.keyword_supported
        matched_count = 0
        processed_count = 0
        skipped_count = 0
        try:
            # 1. Delete-after-N-days cleanup (only for processed messages).
            if cleanup_mode == CLEANUP_DELETE:
                delete_after = int(
                    self.entry.options.get(
                        OPT_DELETE_AFTER_DAYS, DEFAULT_DELETE_AFTER_DAYS
                    )
                )
                cutoff = datetime.now(tz=timezone.utc) - timedelta(
                    days=delete_after
                )
                old_uids = session.search_processed_before(cutoff)
                if old_uids:
                    session.mark_for_delete(old_uids)
                    session.expunge()

            # 2. Find new messages to process. The SINCE bound keeps the
            # search cheap on keyword-fallback servers where we have no
            # server-side processed-state filter at all.
            cutoff = datetime.now(tz=timezone.utc) - timedelta(
                days=scan_window_days
            )
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
                datetime.now(tz=timezone.utc).date()
                - timedelta(days=scan_window_days)
            )
            matched_count = len(uids)
            for uid in uids:
                msg = session.fetch_message(uid)
                if msg is None:
                    skipped_count += 1
                    continue
                if sender_allowlist and msg.sender not in sender_allowlist:
                    _LOGGER.warning(
                        "Rejecting UID %s: sender %r not in allowlist %r — "
                        "the message will not be processed. Adjust the "
                        "sender allowlist in the integration options if "
                        "this is a legitimate forwarder.",
                        uid,
                        msg.sender or "<unknown>",
                        sender_allowlist,
                    )
                    # Don't mark as processed; if the user later adjusts
                    # the allowlist the message can still be picked up.
                    skipped_count += 1
                    continue
                if not msg.attachments:
                    _LOGGER.debug(
                        "Skipping UID %s — subject matched but no MSCONS attachments",
                        uid,
                    )
                    # Don't mark as processed; let it be retried in case
                    # the user re-uploads / forwards a corrected version.
                    skipped_count += 1
                    continue
                successful = self._process_message(msg, min_file_date, updates)
                if successful:
                    processed_count += 1
                    session.mark_processed(uid)
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
        return updates

    def _process_message(
        self,
        msg: FetchedMessage,
        min_file_date: date,
        updates: dict[str, dict[str, Any]],
    ) -> bool:
        """Process attachments in one email.

        Returns True iff at least one attachment was imported (or was
        already up-to-date for a known EIC) — i.e. it's safe to mark the
        message as processed.
        """
        had_imported = False
        for att in msg.attachments:
            try:
                file_date = _parse_file_date(att.file_date)
            except ValueError:
                _LOGGER.warning(
                    "Skipping attachment with bad date %s",
                    att.filename,
                )
                continue
            if file_date < min_file_date:
                _LOGGER.debug(
                    "Skipping old attachment %s (outside scan_window_days)",
                    att.filename,
                )
                had_imported = True
                continue
            if att.eic not in self._enabled_eics:
                _LOGGER.debug(
                    "Skipping attachment for EIC %s — not enabled",
                    att.eic,
                )
                had_imported = True  # don't keep refetching this email
                continue

            prev_version = self._last_processed_versions.get(
                (att.eic, file_date)
            )
            if prev_version is not None and prev_version >= att.file_version:
                _LOGGER.debug(
                    "Already imported %s at V%d (>= V%d)",
                    att.filename,
                    prev_version,
                    att.file_version,
                )
                had_imported = True
                continue

            try:
                data = parse_mscons(att.payload)
            except MsconsParseError as exc:
                _LOGGER.error(
                    "Failed to parse %s: %s — leaving email unprocessed",
                    att.filename,
                    exc,
                )
                # Don't set had_imported; the email stays unprocessed so a
                # parser fix on next release picks it up.
                continue

            # Defense-in-depth: the filename gates the enabled-EIC allowlist
            # check above, but the XML PLACE_ID is what we'd actually write
            # statistics for. A spoofed file could declare one EIC in the
            # name and another in the payload to write under an EIC the user
            # didn't enable — reject the mismatch.
            if att.eic.upper() != data.eic.upper():
                _LOGGER.warning(
                    "Attachment %s declares EIC %s in filename but %s in "
                    "XML PLACE_ID; rejecting to prevent EIC spoofing",
                    att.filename,
                    att.eic,
                    data.eic,
                )
                continue

            # Same defense for the measurement date: the filename's
            # YYYYMMDD is what we use to key the dedup map, but the XML's
            # quarter timestamps determine which hour bucket statistics
            # land in. If the two disagree the file is either misnamed
            # by OKTE (very rare) or tampered with — reject either way.
            if file_date != data.measurement_date:
                _LOGGER.warning(
                    "Attachment %s declares date %s in filename but XML "
                    "measurements cover %s; rejecting due to date mismatch",
                    att.filename,
                    file_date.isoformat(),
                    data.measurement_date.isoformat(),
                )
                continue

            try:
                self._import_data(att, data, updates)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception(
                    "Failed to import statistics for %s: %s",
                    att.filename,
                    exc,
                )
                continue

            self._last_processed_versions[(att.eic, file_date)] = att.file_version
            had_imported = True
        return had_imported

    def _import_data(
        self,
        att: Attachment,
        data: DailyData,
        updates: dict[str, dict[str, Any]],
    ) -> None:
        """Push hourly statistics for one parsed file and update sensor state."""
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

        role = data.role
        eic_state = updates.setdefault(data.eic, {})

        for (mapped_role, suffix), lin in SENSOR_TO_LIN.items():
            if mapped_role != role:
                continue
            quarters = data.series.get(lin)
            if not quarters:
                continue
            buckets = quarters_to_hourly(quarters)
            key = (data.eic, suffix)
            starting_sum = self._last_cumulative.get(key, 0.0)
            rows = build_statistic_data(buckets, starting_sum=starting_sum)
            statistic_id = statistic_id_for(data.eic, suffix)
            import_hourly_statistics(self.hass, statistic_id, None, rows)
            if rows:
                self._last_cumulative[key] = rows[-1].get("sum", starting_sum)
                eic_state[suffix] = self._last_cumulative[key]

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
        # Surface the *filename* version (att.file_version) rather than the
        # BGM version: in real OKTE files the BGM DOCUMENTNUMBER doesn't
        # encode a `_V<n>` suffix, so parsing it always returns 1. The
        # filename is the actual version-of-record for SZE_7 publication.
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
