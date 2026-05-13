"""Diagnostics support for OKTE EDC."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)
from .coordinator import OkteCoordinator

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: OkteCoordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": {
            "data": async_redact_data(entry.data, TO_REDACT),
            "options": dict(entry.options),
        },
        "enabled_eics": coordinator.enabled_eics,
        "last_processed_versions": {
            f"{eic}:{date.isoformat()}": v
            for (eic, date), v in coordinator._last_processed_versions.items()  # noqa: SLF001
        },
        "last_cumulative": {
            f"{eic}:{suffix}": v
            for (eic, suffix), v in coordinator._last_cumulative.items()  # noqa: SLF001
        },
        "last_import_summary": coordinator._last_import_summary,  # noqa: SLF001
        "data": coordinator.data,
    }
