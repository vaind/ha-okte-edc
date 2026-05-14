"""The OKTE EDC integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .coordinator import (
    OkteCoordinator,
    _STORE_VERSION,
    _state_store_key,
)
from .sensor import _service_device_info

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OKTE EDC from a config entry."""
    if not entry.data.get("use_ssl", True):
        _LOGGER.warning(
            "OKTE EDC: IMAP SSL is disabled for %s — the password is being "
            "sent over the network in plaintext. Re-add the integration "
            "with SSL enabled unless you specifically need plaintext IMAP.",
            entry.title,
        )

    # Pre-register the per-entry "mailbox" service device so that the
    # per-EIC devices the sensor platform registers (with
    # `via_device=(DOMAIN, entry.entry_id)`) don't reference a
    # non-existent parent. HA logs a deprecation warning otherwise; the
    # behaviour is slated to become an error in 2025.12.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        **_service_device_info(entry),
    )

    coordinator = OkteCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up the per-entry persistent state file when the user removes the integration."""
    store = Store(hass, _STORE_VERSION, _state_store_key(entry))
    await store.async_remove()


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options or data updates.

    Cheap changes (polling interval, cleanup mode, EIC enable flags) are
    applied in-place by the coordinator. Discovery / EIC-list changes need
    a full reload so platforms re-create their entities.
    """
    coordinator: OkteCoordinator = hass.data[DOMAIN][entry.entry_id]
    # If the EIC list changed (rescan added/removed EICs), reload so the
    # sensor platform re-runs and (re)registers entities.
    enabled_changed = set(coordinator.enabled_eics) != {
        record["eic"]
        for record in entry.data.get("eics", [])
        if record.get("enabled", True)
    }
    if enabled_changed:
        await hass.config_entries.async_reload(entry.entry_id)
        return

    coordinator.update_from_options()
    await coordinator.async_request_refresh()
