"""Constants for the OKTE EDC integration."""

from __future__ import annotations

import re
from typing import Final

DOMAIN: Final = "okte_edc"

# Config entry keys
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_FOLDER: Final = "folder"
CONF_USE_SSL: Final = "use_ssl"
CONF_EICS: Final = "eics"

# Options keys
OPT_POLL_INTERVAL: Final = "poll_interval_minutes"
OPT_POLL_WINDOW_START: Final = "poll_window_start"
OPT_POLL_WINDOW_END: Final = "poll_window_end"
OPT_POLL_TIMEZONE: Final = "poll_timezone"
OPT_EMAIL_CLEANUP: Final = "email_cleanup"
OPT_ARCHIVE_FOLDER: Final = "archive_folder"
OPT_DELETE_AFTER_DAYS: Final = "delete_after_days"
OPT_SCAN_WINDOW_DAYS: Final = "scan_window_days"
OPT_MAX_BACKFILL: Final = "max_backfill_emails"
OPT_SENDER_ALLOWLIST: Final = "sender_allowlist"

# Cleanup mode values
CLEANUP_LEAVE: Final = "leave_in_place"
CLEANUP_ARCHIVE: Final = "archive"
CLEANUP_DELETE: Final = "delete_after_days"

# Defaults
DEFAULT_PORT: Final = 993
DEFAULT_FOLDER: Final = "INBOX"
DEFAULT_USE_SSL: Final = True
DEFAULT_POLL_INTERVAL: Final = 30
DEFAULT_POLL_WINDOW_START: Final = "09:00"
DEFAULT_POLL_WINDOW_END: Final = "13:00"
DEFAULT_POLL_TIMEZONE: Final = "Europe/Bratislava"
DEFAULT_EMAIL_CLEANUP: Final = CLEANUP_LEAVE
DEFAULT_ARCHIVE_FOLDER: Final = "Archive/OKTE"
DEFAULT_DELETE_AFTER_DAYS: Final = 30
DEFAULT_SCAN_WINDOW_DAYS: Final = 30
DEFAULT_MAX_BACKFILL: Final = 1000
# Comma-separated list of sender addresses. Empty string = no filtering.
# Default is OKTE's documented production sender; users who rely on
# mailbox forwarding may need to either switch to auto-forwarding (which
# preserves the original From) or add their forwarder to this list.
DEFAULT_SENDER_ALLOWLIST: Final = "edc@okte.sk"

# OKTE email subject substring (RFC 3501 SUBJECT search is substring-match)
SUBJECT_SUBSTRING: Final = "[EDC_SZE_7/SZE]"

# LIN codes
LIN_PS15: Final = "PS15"
LIN_CPS15: Final = "CPS15"
LIN_SHA15: Final = "SHA15"
LIN_PM15: Final = "PM15"
LIN_CPM15: Final = "CPM15"

# Roles
ROLE_PRODUCER: Final = "producer"
ROLE_OFFTAKE: Final = "offtake"

# Producer EICs start with 24ZZSVYR; everything else with 24ZZS is offtake.
PRODUCER_EIC_RE: Final = re.compile(r"^24ZZSVYR", re.IGNORECASE)

# Filename: <EIC>_<YYYYMMDD>_D_V<n>.xml(.gz). Spec §10.5.
FILENAME_RE: Final = re.compile(
    r"^(?P<eic>24ZZS[A-Z0-9]+)_(?P<date>\d{8})_D_V(?P<version>\d+)\.xml(?:\.gz)?$",
    re.IGNORECASE,
)

# Per-EIC sensor suffixes
SUFFIX_GRID_IMPORT: Final = "grid_import"
SUFFIX_SHARED_IN: Final = "shared_in"
SUFFIX_TOTAL_CONSUMPTION: Final = "total_consumption"
SUFFIX_GRID_RETURN: Final = "grid_return"
SUFFIX_SHARED_OUT: Final = "shared_out"
SUFFIX_TOTAL_EXPORT: Final = "total_export"
SUFFIX_LAST_IMPORT: Final = "last_import"
SUFFIX_FILE_VERSION: Final = "file_version"
SUFFIX_RECONCILIATION_DELTA: Final = "reconciliation_delta"
SUFFIX_MEASUREMENT_DATE: Final = "measurement_date"
SUFFIX_PARSE_WARNINGS: Final = "parse_warnings"

# Service-level suffixes (live on a separate "IMAP coordinator" device,
# not on per-EIC devices)
SUFFIX_NEXT_POLL: Final = "next_poll_at"
SUFFIX_LAST_POLL: Final = "last_poll_at"
SUFFIX_LAST_SUCCESSFUL_POLL: Final = "last_successful_poll_at"
SUFFIX_LAST_POLL_ISSUES: Final = "last_poll_issues"

# Mapping: (role, sensor suffix) -> LIN code that feeds the sensor.
SENSOR_TO_LIN: Final[dict[tuple[str, str], str]] = {
    (ROLE_OFFTAKE, SUFFIX_GRID_IMPORT): LIN_CPS15,
    (ROLE_OFFTAKE, SUFFIX_SHARED_IN): LIN_SHA15,
    (ROLE_OFFTAKE, SUFFIX_TOTAL_CONSUMPTION): LIN_PS15,
    (ROLE_PRODUCER, SUFFIX_GRID_RETURN): LIN_CPM15,
    (ROLE_PRODUCER, SUFFIX_SHARED_OUT): LIN_SHA15,
    (ROLE_PRODUCER, SUFFIX_TOTAL_EXPORT): LIN_PM15,
}

# Reconciliation tolerance (kWh). Beyond this we surface a WARNING.
RECONCILIATION_THRESHOLD_KWH: Final = 1e-3

# Reasonable per-interval kWh ceiling used for sanity checks (a 15-min
# residential energy quantity above this is almost certainly a parser bug).
QUARTER_KWH_SANITY_CEILING: Final = 1000.0

# Size caps. Real OKTE SZE_7 daily files are ~85 KB raw / ~12 KB gzipped.
# These caps bound the worst-case memory cost of a poisoned or malformed
# attachment fed in through email, while leaving 20x+ headroom over the
# real shape.
MAX_RAW_ATTACHMENT_BYTES: Final = 2 * 1024 * 1024     # 2 MB raw / gzipped
MAX_DECOMPRESSED_XML_BYTES: Final = 10 * 1024 * 1024  # 10 MB decompressed


def detect_role(eic: str) -> str:
    """Return producer/offtake based on the EIC's prefix."""
    if PRODUCER_EIC_RE.match(eic):
        return ROLE_PRODUCER
    return ROLE_OFFTAKE


def parse_sender_allowlist(raw: str | None) -> list[str]:
    """Parse the comma-separated sender allowlist option.

    Returns a list of lowercased addresses. An empty/None input returns
    an empty list, which means "do not filter".
    """
    if not raw:
        return []
    return [
        item.strip().lower()
        for item in raw.split(",")
        if item.strip()
    ]


def statistic_id_for(eic: str, suffix: str) -> str:
    """Return the long-term statistics ID for ``(eic, suffix)``.

    Format: ``<DOMAIN>:<short_eic>_<suffix>``. The ``<DOMAIN>:``
    prefix marks the statistic as **external** — written via
    ``async_add_external_statistics`` and not linked to any sensor
    entity. This is critical: linking statistics to a ``total_increasing``
    sensor entity makes HA's recorder auto-compile hourly sums from the
    sensor's state history every 5 minutes, racing with and clobbering
    our explicit MSCONS-derived sums. External statistics live under
    a separate ``source`` key and HA's auto-compilation never touches
    them.

    See ``import_hourly_statistics`` in ``statistics.py`` for the
    write side and ``okte_edc.coordinator._get_sum_before`` /
    ``_get_existing_hourly_stats_from`` for the read side. All three
    share this format.
    """
    return f"{DOMAIN}:{short_eic(eic)}_{suffix}"


def statistic_name_for(eic: str, suffix: str) -> str:
    """Human-readable label shown for the external statistic.

    The Energy dashboard's source picker displays this name (since
    external statistics have no entity to inherit a friendly name from).
    Format is intentionally locale-independent so the dashboard label
    stays stable across HA language switches.
    """
    return f"OKTE EDC {short_eic(eic)} {suffix.replace('_', ' ').capitalize()}"


def entity_id_for(eic: str, suffix: str) -> str:
    """Return the per-EIC sensor entity_id.

    Distinct from :func:`statistic_id_for`: HA entity IDs use the form
    ``<domain>.<object_id>`` with a single dot separator and only
    ``[a-z0-9_]`` in ``<object_id>``. Statistic IDs for external
    statistics use ``<source>:<id>`` with a colon — illegal in entity
    IDs. The two helpers are deliberately different functions to make
    the contrast explicit and to keep callers from copy-pasting one
    into the other.
    """
    return f"sensor.{DOMAIN}_{short_eic(eic)}_{suffix}"


def short_eic(eic: str) -> str:
    """Return the lowercase last 8 alphanumeric characters of an EIC.

    Used for entity-id slug derivation; non-alphanumeric chars are filtered
    out first so the result is always a safe slug fragment.
    """
    alnum = "".join(c for c in eic if c.isalnum())
    return alnum[-8:].lower()
