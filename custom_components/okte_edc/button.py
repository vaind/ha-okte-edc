"""Buttons for the OKTE EDC integration.

Two buttons live on the per-entry service device:

- **Check mailbox now**: trigger an immediate coordinator refresh
  outside the regular polling cadence. Useful after forwarding a fresh
  email or to recover from a transient IMAP error.
- **Full mailbox scan**: same refresh, but with the dynamic SINCE
  cutoff disabled — asks the IMAP server for every OKTE-subject
  message in the folder, not just the last ~week. Used to backfill
  older history (forwarded historical mail) or pick up an OKTE V2/V3
  correction whose original date has already slipped past the
  steady-state SINCE window.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OkteCoordinator
from .sensor import _service_device_info

POLL_NOW_DESCRIPTION = ButtonEntityDescription(
    key="poll_now",
    translation_key="poll_now",
    entity_category=EntityCategory.DIAGNOSTIC,
)

FULL_RESCAN_DESCRIPTION = ButtonEntityDescription(
    key="full_rescan",
    translation_key="full_rescan",
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OkteCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            OktePollNowButton(coordinator, entry),
            OkteFullRescanButton(coordinator, entry),
        ]
    )


class OktePollNowButton(CoordinatorEntity[OkteCoordinator], ButtonEntity):
    """Triggers an immediate coordinator refresh."""

    _attr_has_entity_name = True
    entity_description = POLL_NOW_DESCRIPTION

    def __init__(
        self, coordinator: OkteCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self.entity_id = f"button.{DOMAIN}_service_poll_now"
        self._attr_unique_id = f"{entry.entry_id}_service_poll_now"
        self._attr_device_info = _service_device_info(entry)

    async def async_press(self) -> None:
        """Run a coordinator refresh outside the normal interval."""
        await self.coordinator.async_request_refresh()


class OkteFullRescanButton(
    CoordinatorEntity[OkteCoordinator], ButtonEntity
):
    """Triggers a refresh that ignores the dynamic SINCE cutoff."""

    _attr_has_entity_name = True
    entity_description = FULL_RESCAN_DESCRIPTION

    def __init__(
        self, coordinator: OkteCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self.entity_id = f"button.{DOMAIN}_service_full_rescan"
        self._attr_unique_id = f"{entry.entry_id}_service_full_rescan"
        self._attr_device_info = _service_device_info(entry)

    async def async_press(self) -> None:
        await self.coordinator.async_trigger_full_rescan()
