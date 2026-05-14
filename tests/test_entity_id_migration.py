"""Pin the entity_id migration logic.

If a future refactor changes the unique_id format or the expected
entity_id derivation, the migration would silently stop renaming and
existing installs would drift. Cover the three shapes (per-EIC sensor,
service-level sensor, service button) plus the no-op case.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class _RegistryEntry:
    entity_id: str
    unique_id: str


class _FakeRegistry:
    def __init__(self, entries: list[_RegistryEntry]):
        self._entries = entries
        # entity_id -> entry, so we can detect collisions
        self._by_entity_id = {e.entity_id: e for e in entries}
        self.renames: list[tuple[str, str]] = []

    def async_get(self, entity_id):
        return self._by_entity_id.get(entity_id)

    def async_update_entity(self, old_entity_id, *, new_entity_id):
        entry = self._by_entity_id.pop(old_entity_id)
        entry.entity_id = new_entity_id
        self._by_entity_id[new_entity_id] = entry
        self.renames.append((old_entity_id, new_entity_id))


def _patch_registry(monkeypatch, entries):
    import okte_edc as init_module

    registry = _FakeRegistry(entries)
    monkeypatch.setattr(
        init_module.er,
        "async_get",
        lambda hass: registry,
    )
    monkeypatch.setattr(
        init_module.er,
        "async_entries_for_config_entry",
        lambda reg, entry_id: list(entries),
    )
    return registry


def _entry():
    class _Entry:
        entry_id = "abc123"
    return _Entry()


def test_per_eic_sensor_with_auto_derived_id_is_renamed(monkeypatch):
    from okte_edc import _migrate_entity_ids

    entries = [
        _RegistryEntry(
            entity_id="sensor.okte_edc_00000002_shared_imported",
            unique_id="abc123_24ZZS00000000002_shared_in",
        )
    ]
    registry = _patch_registry(monkeypatch, entries)
    asyncio.run(_migrate_entity_ids(None, _entry()))
    assert registry.renames == [
        (
            "sensor.okte_edc_00000002_shared_imported",
            "sensor.okte_edc_00000002_shared_in",
        )
    ]


def test_per_eic_sensor_already_canonical_is_left_alone(monkeypatch):
    from okte_edc import _migrate_entity_ids

    entries = [
        _RegistryEntry(
            entity_id="sensor.okte_edc_00000001_grid_import",
            unique_id="abc123_24ZZS00000000001_grid_import",
        )
    ]
    registry = _patch_registry(monkeypatch, entries)
    asyncio.run(_migrate_entity_ids(None, _entry()))
    assert registry.renames == []


def test_service_sensor_gets_renamed(monkeypatch):
    from okte_edc import _migrate_entity_ids

    entries = [
        _RegistryEntry(
            entity_id="sensor.okte_edc_mailbox_example_com_last_mailbox_check",
            unique_id="abc123_service_last_poll_at",
        )
    ]
    registry = _patch_registry(monkeypatch, entries)
    asyncio.run(_migrate_entity_ids(None, _entry()))
    assert registry.renames == [
        (
            "sensor.okte_edc_mailbox_example_com_last_mailbox_check",
            "sensor.okte_edc_service_last_poll_at",
        )
    ]


def test_poll_now_button_gets_renamed(monkeypatch):
    from okte_edc import _migrate_entity_ids

    entries = [
        _RegistryEntry(
            entity_id="button.okte_edc_mailbox_example_com_check_mailbox_now",
            unique_id="abc123_service_poll_now",
        )
    ]
    registry = _patch_registry(monkeypatch, entries)
    asyncio.run(_migrate_entity_ids(None, _entry()))
    assert registry.renames == [
        (
            "button.okte_edc_mailbox_example_com_check_mailbox_now",
            "button.okte_edc_service_poll_now",
        )
    ]


def test_collision_is_skipped_not_overwritten(monkeypatch):
    """If the target id is already taken (by an orphan), leave the
    source alone and log instead of overwriting. The user can resolve
    in the UI."""
    from okte_edc import _migrate_entity_ids

    old = _RegistryEntry(
        entity_id="sensor.okte_edc_00000002_shared_imported",
        unique_id="abc123_24ZZS00000000002_shared_in",
    )
    collider = _RegistryEntry(
        entity_id="sensor.okte_edc_00000002_shared_in",
        unique_id="totally_unrelated",
    )
    registry = _patch_registry(monkeypatch, [old, collider])
    asyncio.run(_migrate_entity_ids(None, _entry()))
    assert registry.renames == []  # no rename attempted
