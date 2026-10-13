"""Per-EIC devices hang off the per-entry mailbox device in HA's registry."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


class _FakeEntry:
    entry_id = "abc"
    data = {"host": "imap.example"}
    options: dict = {}


class _FakeCoordinator:
    enabled_eics = {
        "24ZZS00000000001": "offtake",
        "24ZZSVYR00000099": "producer",
    }


class _RecordingRegistry:
    def __init__(self):
        self.created: list[dict] = []

    def async_get_or_create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id="service-device-id")


def test_eic_devices_link_to_service_device_by_registry_id(monkeypatch):
    from okte_edc import sensor as sensor_module
    from okte_edc.const import DOMAIN
    from okte_edc.sensor import OkteEicSensor

    registry = _RecordingRegistry()
    monkeypatch.setattr(sensor_module.dr, "async_get", lambda _hass: registry)
    entry = _FakeEntry()
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: _FakeCoordinator()}})
    added: list = []

    asyncio.run(sensor_module.async_setup_entry(hass, entry, added.extend))

    assert [c["identifiers"] for c in registry.created] == [
        {(DOMAIN, entry.entry_id)}
    ]
    eic_device_infos = [
        e._attr_device_info for e in added if isinstance(e, OkteEicSensor)
    ]
    assert eic_device_infos
    for info in eic_device_infos:
        assert info["via_device_id"] == "service-device-id"
        # Deprecated since HA 2026.9, removed in 2027.8.
        assert "via_device" not in info
