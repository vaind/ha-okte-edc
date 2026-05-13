"""Diagnostics support for OKTE EDC.

HA diagnostics dumps are routinely attached to GitHub issues by users
asking for help — anything we include here may end up public. We
redact:

- IMAP credentials (password, username).
- The mailbox host + folder, because they identify the mail provider
  and account boundary.
- Every full EIC, replaced with the same ``short_eic`` slug that
  appears in entity IDs. The user can correlate diagnostics to their
  HA install without the full EIC leaving the box.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_FOLDER,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    short_eic,
)
from .coordinator import OkteCoordinator

ENTRY_REDACT = {CONF_PASSWORD, CONF_USERNAME, CONF_HOST, CONF_FOLDER}


def _redact_eic(eic: str) -> str:
    """Return a stable, non-reversible-ish short identifier for an EIC.

    Uses :func:`short_eic` (the same last-8-alphanumeric slug that drives
    entity IDs) so users can still cross-reference diagnostics with
    their installation but the full EIC never appears in the dump.
    """
    return f"EIC_{short_eic(eic)}"


def _redact_eic_entries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**record, "eic": _redact_eic(record["eic"])} if "eic" in record else record
        for record in records
    ]


def _redact_keyed_by_eic(values: dict[str, Any]) -> dict[str, Any]:
    return {_redact_eic(eic): value for eic, value in values.items()}


def _redact_eic_tuple_keys(
    values: dict[tuple[str, Any], Any], separator: str = ":"
) -> dict[str, Any]:
    """Redact dicts whose keys are ``(eic, ...)`` tuples.

    Returned values keep their original shape; only the EIC component of
    the key is redacted. The tuple is flattened to ``<short>:<rest>``
    because diagnostics output is JSON, which has no tuple type.
    """
    redacted: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(key, tuple) and key and isinstance(key[0], str):
            tail = separator.join(str(part) for part in key[1:])
            new_key = (
                f"{_redact_eic(key[0])}{separator}{tail}" if tail else _redact_eic(key[0])
            )
            redacted[new_key] = value
        else:
            redacted[str(key)] = value
    return redacted


def _redact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Strip the EIC out of the recorded filename inside last_import_summary."""
    redacted: dict[str, Any] = {}
    for eic, entry in summary.items():
        new_entry = dict(entry)
        filename = new_entry.get("filename")
        if isinstance(filename, str):
            new_entry["filename"] = filename.replace(eic, _redact_eic(eic))
        redacted[_redact_eic(eic)] = new_entry
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: OkteCoordinator = hass.data[DOMAIN][entry.entry_id]
    entry_data = async_redact_data(dict(entry.data), ENTRY_REDACT)
    # entry.data["eics"] contains a list of {eic, role, enabled} records;
    # rewrite each EIC value individually.
    if isinstance(entry_data.get("eics"), list):
        entry_data["eics"] = _redact_eic_entries(entry_data["eics"])

    return {
        "entry": {
            "data": entry_data,
            "options": dict(entry.options),
        },
        "enabled_eics": _redact_keyed_by_eic(coordinator.enabled_eics),
        "last_processed_versions": _redact_eic_tuple_keys(
            {(eic, date.isoformat()): v
             for (eic, date), v in coordinator._last_processed_versions.items()}  # noqa: SLF001
        ),
        "last_cumulative": _redact_eic_tuple_keys(
            coordinator._last_cumulative  # noqa: SLF001
        ),
        "last_import_summary": _redact_summary(
            coordinator._last_import_summary  # noqa: SLF001
        ),
        "data": _redact_keyed_by_eic(coordinator.data or {}),
    }
