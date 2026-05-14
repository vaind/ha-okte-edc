"""Sensor entities for the OKTE EDC integration.

Each enabled EIC produces one HA device with:
- Three energy sensors (kWh, total_increasing) driven by MSCONS LIN series.
- Three diagnostic sensors (last import time, file version, reconciliation delta).

Sensor state values are read from the coordinator's ``data`` mapping; long
term statistics are pushed independently in :mod:`statistics`.
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
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    ROLE_OFFTAKE,
    ROLE_PRODUCER,
    SUFFIX_FILE_VERSION,
    SUFFIX_GRID_IMPORT,
    SUFFIX_GRID_RETURN,
    SUFFIX_LAST_IMPORT,
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
    # Deliberately no device_class. The value is a per-file diagnostic
    # snapshot (the largest reconciliation drift observed in the most
    # recent MSCONS file), not a cumulative energy reading. HA rejects
    # device_class=ENERGY with state_class=MEASUREMENT — ENERGY requires
    # total / total_increasing. We want measurement semantics on a
    # value that happens to be expressed in kWh.
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensor entities for each enabled EIC."""
    coordinator: OkteCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = list(_iter_entities(entry, coordinator))
    async_add_entities(entities)


def _iter_entities(
    entry: ConfigEntry, coordinator: OkteCoordinator
) -> Iterable[SensorEntity]:
    for eic, role in coordinator.enabled_eics.items():
        for description in ENERGY_DESCRIPTIONS[role].values():
            yield OkteSensor(coordinator, entry, eic, role, description)
        yield OkteSensor(coordinator, entry, eic, role, DIAG_LAST_IMPORT)
        yield OkteSensor(coordinator, entry, eic, role, DIAG_FILE_VERSION)
        yield OkteSensor(coordinator, entry, eic, role, DIAG_RECON_DELTA)


class OkteSensor(CoordinatorEntity[OkteCoordinator], SensorEntity):
    """Generic sensor reading from coordinator.data[eic][suffix]."""

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
        slug = short_eic(eic)
        self._attr_unique_id = f"{entry.entry_id}_{eic}_{description.key}"
        # NOTE: deliberately no _attr_suggested_object_id. With
        # _attr_has_entity_name = True the entity_id is composed by HA
        # from the device-name slug ("OKTE EDC <slug>" → okte_edc_<slug>)
        # and the entity translation-key slug. The matching statistic_id
        # is constructed via const.statistic_id_for so the two stay in
        # sync.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, eic)},
            name=f"OKTE EDC {slug}",
            manufacturer="OKTE, a.s.",
            model=f"SZE settlement ({role})",
            configuration_url="https://edc.okte.sk",
        )

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        eic_state = data.get(self._eic) or {}
        return eic_state.get(self.entity_description.key)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._eic in self.coordinator.enabled_eics
