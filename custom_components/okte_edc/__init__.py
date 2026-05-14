"""The OKTE EDC integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN, statistic_id_for
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

    # Migrate any entity_ids registered before the integration started
    # forcing them explicitly. See `_migrate_entity_ids` for the why.
    await _migrate_entity_ids(hass, entry)

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


async def _migrate_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rename auto-derived entity_ids to the integration's canonical form.

    Earlier versions of the integration set ``_attr_has_entity_name = True``
    and let HA derive the entity_id automatically. HA's derivation
    slugifies the *translated friendly name* — which is locale-dependent
    and drifts away from the translation_key when a name contains
    punctuation that slugify strips ("Shared (imported)" becomes
    ``shared_imported``, not ``shared_in``). The coordinator's
    ``statistic_id_for`` always uses the translation_key, so every
    import landed in an orphan statistic_id that no entity was linked
    to.

    New entities now have ``self.entity_id`` set explicitly. This
    migration brings pre-existing registry entries into line so users
    don't have to remove + re-add the integration to recover.
    """
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if not ent.unique_id.startswith(prefix):
            continue
        rest = ent.unique_id[len(prefix):]

        expected: str | None = None
        if rest == "service_poll_now":
            expected = f"button.{DOMAIN}_service_poll_now"
        elif rest.startswith("service_"):
            suffix = rest[len("service_"):]
            expected = f"sensor.{DOMAIN}_service_{suffix}"
        elif rest.startswith("24ZZS") and len(rest) > 17 and rest[16] == "_":
            eic = rest[:16]
            suffix = rest[17:]
            expected = statistic_id_for(eic, suffix)

        if expected is None or ent.entity_id == expected:
            continue
        if registry.async_get(expected) is not None:
            _LOGGER.warning(
                "Cannot migrate %s -> %s: target entity_id already in use",
                ent.entity_id,
                expected,
            )
            continue
        try:
            registry.async_update_entity(
                ent.entity_id, new_entity_id=expected
            )
        except ValueError as exc:
            _LOGGER.warning(
                "Migration failed for %s -> %s: %s",
                ent.entity_id,
                expected,
                exc,
            )
        else:
            _LOGGER.info(
                "Migrated entity_id %s -> %s",
                ent.entity_id,
                expected,
            )


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
