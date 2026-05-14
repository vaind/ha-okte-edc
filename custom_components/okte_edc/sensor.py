"""Sensor entities for the OKTE EDC integration.

Each enabled EIC produces one HA device with energy + diagnostic
sensors. A separate per-entry service device exposes integration-wide
operational metrics (next/last poll, IMAP keyword-support flag).

Sensor state values are read from the coordinator's ``data`` mapping
and tracked attributes; long-term statistics are pushed independently
in :mod:`statistics`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    ROLE_OFFTAKE,
    ROLE_PRODUCER,
    statistic_id_for,
    SUFFIX_FILE_VERSION,
    SUFFIX_GRID_IMPORT,
    SUFFIX_GRID_RETURN,
    SUFFIX_LAST_IMPORT,
    SUFFIX_LAST_POLL,
    SUFFIX_LAST_POLL_ISSUES,
    SUFFIX_LAST_SUCCESSFUL_POLL,
    SUFFIX_MEASUREMENT_DATE,
    SUFFIX_NEXT_POLL,
    SUFFIX_PARSE_WARNINGS,
    SUFFIX_RECONCILIATION_DELTA,
    SUFFIX_SHARED_IN,
    SUFFIX_SHARED_OUT,
    SUFFIX_TOTAL_CONSUMPTION,
    SUFFIX_TOTAL_EXPORT,
    short_eic,
)
from .coordinator import OkteCoordinator

# Energy sensors: cumulative kWh, total_increasing for Energy dashboard.
ENERGY_DESCRIPTIONS: dict[str, dict[str, SensorEntityDescription]] = {
    ROLE_OFFTAKE: {
        SUFFIX_GRID_IMPORT: SensorEntityDescription(
            key=SUFFIX_GRID_IMPORT,
            translation_key=SUFFIX_GRID_IMPORT,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        ),
        SUFFIX_SHARED_IN: SensorEntityDescription(
            key=SUFFIX_SHARED_IN,
            translation_key=SUFFIX_SHARED_IN,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        ),
        SUFFIX_TOTAL_CONSUMPTION: SensorEntityDescription(
            key=SUFFIX_TOTAL_CONSUMPTION,
            translation_key=SUFFIX_TOTAL_CONSUMPTION,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        ),
    },
    ROLE_PRODUCER: {
        SUFFIX_GRID_RETURN: SensorEntityDescription(
            key=SUFFIX_GRID_RETURN,
            translation_key=SUFFIX_GRID_RETURN,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        ),
        SUFFIX_SHARED_OUT: SensorEntityDescription(
            key=SUFFIX_SHARED_OUT,
            translation_key=SUFFIX_SHARED_OUT,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        ),
        SUFFIX_TOTAL_EXPORT: SensorEntityDescription(
            key=SUFFIX_TOTAL_EXPORT,
            translation_key=SUFFIX_TOTAL_EXPORT,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        ),
    },
}

DIAG_LAST_IMPORT = SensorEntityDescription(
    key=SUFFIX_LAST_IMPORT,
    translation_key=SUFFIX_LAST_IMPORT,
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)

DIAG_FILE_VERSION = SensorEntityDescription(
    key=SUFFIX_FILE_VERSION,
    translation_key=SUFFIX_FILE_VERSION,
    entity_category=EntityCategory.DIAGNOSTIC,
)

DIAG_RECON_DELTA = SensorEntityDescription(
    key=SUFFIX_RECONCILIATION_DELTA,
    translation_key=SUFFIX_RECONCILIATION_DELTA,
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    entity_category=EntityCategory.DIAGNOSTIC,
)

DIAG_MEASUREMENT_DATE = SensorEntityDescription(
    key=SUFFIX_MEASUREMENT_DATE,
    translation_key=SUFFIX_MEASUREMENT_DATE,
    device_class=SensorDeviceClass.DATE,
    entity_category=EntityCategory.DIAGNOSTIC,
)

DIAG_PARSE_WARNINGS = SensorEntityDescription(
    key=SUFFIX_PARSE_WARNINGS,
    translation_key=SUFFIX_PARSE_WARNINGS,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
)

# ---------------------------------------------------------------------------
# Service-level (per-config-entry) descriptions

SVC_NEXT_POLL = SensorEntityDescription(
    key=SUFFIX_NEXT_POLL,
    translation_key=SUFFIX_NEXT_POLL,
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)

SVC_LAST_POLL = SensorEntityDescription(
    key=SUFFIX_LAST_POLL,
    translation_key=SUFFIX_LAST_POLL,
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)

SVC_LAST_SUCCESSFUL_POLL = SensorEntityDescription(
    key=SUFFIX_LAST_SUCCESSFUL_POLL,
    translation_key=SUFFIX_LAST_SUCCESSFUL_POLL,
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)

SVC_LAST_POLL_ISSUES = SensorEntityDescription(
    key=SUFFIX_LAST_POLL_ISSUES,
    translation_key=SUFFIX_LAST_POLL_ISSUES,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensor entities for each enabled EIC + service-level sensors."""
    coordinator: OkteCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = list(_iter_entities(entry, coordinator))
    async_add_entities(entities)


def _service_device_info(entry: ConfigEntry) -> DeviceInfo:
    """DeviceInfo for the per-config-entry hub.

    Deliberately no ``manufacturer`` field: this is an unofficial
    community integration, and HA's Device-info panel would otherwise
    render "by OKTE, a.s." beside the device name and falsely imply
    endorsement.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"OKTE EDC mailbox ({entry.data.get('host', 'unknown')})",
        model="IMAP coordinator",
        configuration_url="https://edc.okte.sk",
        entry_type=DeviceEntryType.SERVICE,
    )


def _eic_device_info(eic: str, role: str, entry: ConfigEntry) -> DeviceInfo:
    slug = short_eic(eic)
    return DeviceInfo(
        identifiers={(DOMAIN, eic)},
        name=f"OKTE EDC {slug}",
        # No `manufacturer`: see `_service_device_info`.
        model=f"SZE settlement ({role})",
        # Surface the full EIC under HA's Device-info panel. The
        # entity_ids / sensor names already use the short_eic slug
        # for stability and readability; serial_number gives users a
        # one-look way to recover the full 16-char OKTE identifier
        # whenever they need it (matching against a bill, the OKTE
        # portal, etc.).
        serial_number=eic,
        configuration_url="https://edc.okte.sk",
        via_device=(DOMAIN, entry.entry_id),
    )


def _iter_entities(
    entry: ConfigEntry, coordinator: OkteCoordinator
) -> Iterable[SensorEntity]:
    for eic, role in coordinator.enabled_eics.items():
        for description in ENERGY_DESCRIPTIONS[role].values():
            yield OkteEicSensor(coordinator, entry, eic, role, description)
        yield OkteEicSensor(coordinator, entry, eic, role, DIAG_LAST_IMPORT)
        yield OkteEicSensor(coordinator, entry, eic, role, DIAG_FILE_VERSION)
        yield OkteEicSensor(coordinator, entry, eic, role, DIAG_RECON_DELTA)
        yield OkteEicSensor(coordinator, entry, eic, role, DIAG_MEASUREMENT_DATE)
        yield OkteEicSensor(coordinator, entry, eic, role, DIAG_PARSE_WARNINGS)
    yield OkteServiceSensor(coordinator, entry, SVC_NEXT_POLL)
    yield OkteServiceSensor(coordinator, entry, SVC_LAST_POLL)
    yield OkteServiceSensor(coordinator, entry, SVC_LAST_SUCCESSFUL_POLL)
    yield OkteServiceSensor(coordinator, entry, SVC_LAST_POLL_ISSUES)


class OkteEicSensor(CoordinatorEntity[OkteCoordinator], SensorEntity):
    """Per-EIC sensor reading from coordinator.data[eic][suffix]."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OkteCoordinator,
        entry: ConfigEntry,
        eic: str,
        role: str,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._eic = eic
        # Force the entity_id explicitly so it matches `statistic_id_for`
        # exactly and doesn't depend on the user's HA locale. With
        # `has_entity_name=True` HA would otherwise derive entity_id by
        # slugifying the translated friendly name — which differs from
        # the translation_key when names like "Shared (imported)" round
        # to "shared_imported", causing imported statistics to land in
        # an orphan statistic_id no entity is linked to.
        self.entity_id = statistic_id_for(eic, description.key)
        self._attr_unique_id = f"{entry.entry_id}_{eic}_{description.key}"
        self._attr_device_info = _eic_device_info(eic, role, entry)

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        eic_state = data.get(self._eic) or {}
        return eic_state.get(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Surface rich per-sensor attributes for the diagnostic sensors.

        Keeps the device card uncluttered (no extra "filename" sensor),
        while making the detail available when a user drills in.
        """
        summary = self.coordinator.last_import_summary_for(self._eic)
        if not summary:
            return None
        key = self.entity_description.key
        if key == SUFFIX_LAST_IMPORT:
            attrs = {
                "filename": summary.get("filename"),
                "quarter_counts": summary.get("quarter_counts"),
                "daily_kwh": summary.get("per_lin_kwh"),
            }
            return {k: v for k, v in attrs.items() if v is not None}
        if key == SUFFIX_PARSE_WARNINGS:
            warnings = summary.get("warnings") or []
            return {"warnings": list(warnings)} if warnings else None
        return None

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._eic in self.coordinator.enabled_eics


class OkteServiceSensor(CoordinatorEntity[OkteCoordinator], SensorEntity):
    """Service-level sensor (lives on the per-entry hub device)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OkteCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        # Same rationale as OkteEicSensor — entity_id stays
        # locale-independent and predictable.
        self.entity_id = f"sensor.{DOMAIN}_service_{description.key}"
        self._attr_unique_id = f"{entry.entry_id}_service_{description.key}"
        self._attr_device_info = _service_device_info(entry)

    @property
    def native_value(self) -> Any:
        key = self.entity_description.key
        if key == SUFFIX_NEXT_POLL:
            return self.coordinator.next_poll_at
        if key == SUFFIX_LAST_POLL:
            return self.coordinator.last_poll_at
        if key == SUFFIX_LAST_SUCCESSFUL_POLL:
            return self.coordinator.last_successful_poll_at
        if key == SUFFIX_LAST_POLL_ISSUES:
            return len(self.coordinator.last_poll_issues or [])
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        key = self.entity_description.key
        if key == SUFFIX_LAST_POLL:
            attrs: dict[str, Any] = {
                "poll_interval_minutes": (
                    self.coordinator.update_interval.total_seconds() / 60
                    if self.coordinator.update_interval
                    else None
                ),
            }
            attrs.update(self.coordinator.last_poll_stats or {})
            return {k: v for k, v in attrs.items() if v is not None}
        if key == SUFFIX_LAST_POLL_ISSUES:
            issues = list(self.coordinator.last_poll_issues or [])
            if not issues:
                return None
            return {"issues": issues}
        return None
