"""Buttons for the OKTE EDC integration.

A single ``Poll now`` button lives on the per-entry service device.
Pressing it triggers an immediate coordinator refresh regardless of
the polling window, useful when:

- The user just forwarded a new email and doesn't want to wait for the
  next scheduled poll.
- Debugging / verifying the integration is alive.
- Recovering after a transient IMAP error.
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OkteCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OktePollNowButton(coordinator, entry)])


class OktePollNowButton(CoordinatorEntity[OkteCoordinator], ButtonEntity):
    """Triggers an immediate coordinator refresh."""

    _attr_has_entity_name = True
    entity_description = POLL_NOW_DESCRIPTION

    def __init__(
        self, coordinator: OkteCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_service_poll_now"
        self._attr_device_info = _service_device_info(entry)

    async def async_press(self) -> None:
        """Run a coordinator refresh outside the normal interval."""
        await self.coordinator.async_request_refresh()
