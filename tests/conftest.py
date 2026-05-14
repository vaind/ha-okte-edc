"""Shared pytest fixtures and minimal HA stubs.

The integration's HA-facing modules (``__init__``, ``coordinator``,
``sensor``, ``config_flow``, ``diagnostics``) import from
``homeassistant.*`` at module load time. Installing tiny stubs here lets
us unit-test the pure domain logic without depending on the full HA test
harness.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

# Make the integration importable without installing it.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "custom_components"))


def _install_ha_stubs() -> None:
    """Install minimal homeassistant.* stubs needed for module imports.

    Tests in this suite exercise pure-logic modules. The presence of HA
    imports in sibling modules forces a stub here because Python loads
    sibling submodules' parents (the package's ``__init__``) on first
    import.
    """
    if "homeassistant" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")

    # homeassistant.const
    const = types.ModuleType("homeassistant.const")

    class _Platform:
        SENSOR = "sensor"
        BUTTON = "button"

    class _EntityCategory:
        DIAGNOSTIC = "diagnostic"

    class _UnitOfEnergy:
        KILO_WATT_HOUR = "kWh"

    const.Platform = _Platform
    const.EntityCategory = _EntityCategory
    const.UnitOfEnergy = _UnitOfEnergy
    const.CONF_HOST = "host"
    const.CONF_PORT = "port"
    const.CONF_USERNAME = "username"
    const.CONF_PASSWORD = "password"

    # homeassistant.core
    core = types.ModuleType("homeassistant.core")

    class _HomeAssistant:
        pass

    def callback(func):
        return func

    core.HomeAssistant = _HomeAssistant
    core.callback = callback

    # homeassistant.config_entries
    config_entries = types.ModuleType("homeassistant.config_entries")

    class _ConfigEntry:
        pass

    class _ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    class _OptionsFlow:
        pass

    config_entries.ConfigEntry = _ConfigEntry
    config_entries.ConfigFlow = _ConfigFlow
    config_entries.OptionsFlow = _OptionsFlow

    # homeassistant.exceptions
    exceptions = types.ModuleType("homeassistant.exceptions")

    class _ConfigEntryAuthFailed(Exception):
        pass

    class _ConfigEntryError(Exception):
        pass

    exceptions.ConfigEntryAuthFailed = _ConfigEntryAuthFailed
    exceptions.ConfigEntryError = _ConfigEntryError

    # homeassistant.data_entry_flow
    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict

    # homeassistant.helpers.update_coordinator
    helpers = types.ModuleType("homeassistant.helpers")
    update_coordinator = types.ModuleType(
        "homeassistant.helpers.update_coordinator"
    )

    class _DataUpdateCoordinator:
        def __init__(self, *args, **kwargs):
            self.data = None

        def __class_getitem__(cls, item):
            return cls

    class _CoordinatorEntity:
        def __init__(self, *args, **kwargs):
            pass

        def __class_getitem__(cls, item):
            return cls

    class _UpdateFailed(Exception):
        pass

    update_coordinator.DataUpdateCoordinator = _DataUpdateCoordinator
    update_coordinator.CoordinatorEntity = _CoordinatorEntity
    update_coordinator.UpdateFailed = _UpdateFailed

    # homeassistant.helpers.storage
    storage = types.ModuleType("homeassistant.helpers.storage")

    class _Store:
        def __init__(self, hass, version, key):
            self.key = key
            self._version = version

        async def async_load(self):
            return None

        async def async_save(self, data):
            return None

        async def async_remove(self):
            return None

    storage.Store = _Store

    # homeassistant.helpers.entity_platform
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object

    # homeassistant.helpers.device_registry
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict

    class _DeviceEntryType:
        SERVICE = "service"

    class _DeviceRegistry:
        def async_get_or_create(self, **kwargs):
            return None

    def _async_get(_hass):
        return _DeviceRegistry()

    device_registry.DeviceEntryType = _DeviceEntryType
    device_registry.async_get = _async_get

    # homeassistant.helpers.entity_registry
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")

    class _EntityRegistry:
        def async_get(self, entity_id):
            return None

        def async_update_entity(self, entity_id, **kwargs):
            return None

    def _entity_registry_async_get(_hass):
        return _EntityRegistry()

    def _entries_for_entry(_registry, _entry_id):
        return []

    entity_registry.async_get = _entity_registry_async_get
    entity_registry.async_entries_for_config_entry = _entries_for_entry

    # homeassistant.helpers.selector
    selector = types.ModuleType("homeassistant.helpers.selector")

    class _SelectOptionDict(dict):
        def __init__(self, value, label):
            super().__init__(value=value, label=label)

    class _SelectSelectorConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _SelectSelector:
        def __init__(self, config):
            self.config = config

    class _SelectSelectorMode:
        LIST = "list"
        DROPDOWN = "dropdown"

    class _TimeSelector:
        pass

    selector.SelectOptionDict = _SelectOptionDict
    selector.SelectSelectorConfig = _SelectSelectorConfig
    selector.SelectSelector = _SelectSelector
    selector.SelectSelectorMode = _SelectSelectorMode
    selector.TimeSelector = _TimeSelector

    helpers.update_coordinator = update_coordinator
    helpers.entity_platform = entity_platform
    helpers.device_registry = device_registry
    helpers.selector = selector
    helpers.storage = storage

    # homeassistant.components.sensor / recorder
    components = types.ModuleType("homeassistant.components")
    sensor = types.ModuleType("homeassistant.components.sensor")

    class _SensorEntityDescription:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.key = kwargs.get("key")

    class _SensorEntity:
        pass

    class _SensorDeviceClass:
        ENERGY = "energy"
        TIMESTAMP = "timestamp"
        DATE = "date"

    class _SensorStateClass:
        TOTAL_INCREASING = "total_increasing"
        MEASUREMENT = "measurement"

    sensor.SensorEntity = _SensorEntity
    sensor.SensorEntityDescription = _SensorEntityDescription
    sensor.SensorDeviceClass = _SensorDeviceClass
    sensor.SensorStateClass = _SensorStateClass

    button = types.ModuleType("homeassistant.components.button")

    class _ButtonEntityDescription:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.key = kwargs.get("key")

    class _ButtonEntity:
        pass

    button.ButtonEntity = _ButtonEntity
    button.ButtonEntityDescription = _ButtonEntityDescription

    diagnostics_mod = types.ModuleType("homeassistant.components.diagnostics")
    diagnostics_mod.async_redact_data = lambda data, _: data

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.const"] = const
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.exceptions"] = exceptions
    sys.modules["homeassistant.data_entry_flow"] = data_entry_flow
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator
    sys.modules["homeassistant.helpers.entity_platform"] = entity_platform
    sys.modules["homeassistant.helpers.device_registry"] = device_registry
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry
    sys.modules["homeassistant.helpers.selector"] = selector
    sys.modules["homeassistant.helpers.storage"] = storage
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.sensor"] = sensor
    sys.modules["homeassistant.components.button"] = button
    sys.modules["homeassistant.components.diagnostics"] = diagnostics_mod

    # voluptuous (used by config_flow); fall back to dummy if not installed.
    if "voluptuous" not in sys.modules:
        try:
            import voluptuous  # noqa: F401
        except ImportError:
            vol = types.ModuleType("voluptuous")

            class _Schema:
                def __init__(self, *a, **kw):
                    pass

            class _Required:
                def __init__(self, *a, **kw):
                    pass

            class _Optional:
                def __init__(self, *a, **kw):
                    pass

            class _All:
                def __init__(self, *a, **kw):
                    pass

            class _Range:
                def __init__(self, *a, **kw):
                    pass

            vol.Schema = _Schema
            vol.Required = _Required
            vol.Optional = _Optional
            vol.All = _All
            vol.Range = _Range
            sys.modules["voluptuous"] = vol


_install_ha_stubs()
