"""Round-trip the persistent processed-state store.

The integration's coordinator uses HA's ``Store`` to remember which
``(eic, measurement_date)`` pairs have already been imported, so a
restart doesn't reprocess every recent file. The shape on disk has to
survive the load/save cycle intact.
"""

from __future__ import annotations

import asyncio
from datetime import date


def _build_coordinator(monkeypatch):
    """Return an OkteCoordinator wired to an in-memory Store stand-in."""
    # Lazy imports — conftest already installs the homeassistant stubs.
    import okte_edc.coordinator as coord_module
    from okte_edc.coordinator import OkteCoordinator

    class _MemStore:
        def __init__(self, *_args, **_kwargs):
            self.payload = None
            self.key = "test-store"

        async def async_load(self):
            return self.payload

        async def async_save(self, data):
            self.payload = data

        async def async_remove(self):
            self.payload = None

    # Patch the Store symbol the coordinator imported.
    monkeypatch.setattr(coord_module, "Store", _MemStore)

    class _FakeEntry:
        entry_id = "abc"
        data = {
            "host": "h",
            "port": 993,
            "username": "u",
            "password": "p",
            "folder": "INBOX",
            "use_ssl": True,
            "eics": [],
        }
        options: dict = {}

    return OkteCoordinator(None, _FakeEntry())


def test_load_save_roundtrip_preserves_processed_state(monkeypatch):
    coord = _build_coordinator(monkeypatch)

    # Populate the in-memory map as if a poll cycle had run
    coord._last_processed_versions = {
        ("24ZZS00000000001", date(2026, 5, 1)): 1,
        ("24ZZS00000000001", date(2026, 5, 2)): 2,
        ("24ZZSVYR00000099", date(2026, 5, 1)): 1,
    }

    asyncio.run(coord._save_state())

    # New coordinator instance, same fake store payload
    coord2 = _build_coordinator(monkeypatch)
    # Inject the same Store payload by reusing the patched class instance
    coord2._store.payload = coord._store.payload
    asyncio.run(coord2._load_state_if_needed())

    assert coord2._last_processed_versions == coord._last_processed_versions


def test_load_with_empty_store_is_a_noop(monkeypatch):
    coord = _build_coordinator(monkeypatch)
    asyncio.run(coord._load_state_if_needed())
    assert coord._last_processed_versions == {}
    assert coord._store_loaded is True


def test_load_skips_malformed_entries(monkeypatch):
    coord = _build_coordinator(monkeypatch)
    coord._store.payload = {
        "last_processed_versions": {
            "24ZZS00000000001": {
                "2026-05-01": 1,
                "not-a-date": 99,    # bad date → skipped
                "2026-05-02": "x",   # bad version → skipped
            },
            "24ZZSVYR00000099": "not-a-dict",  # bad shape → skipped
        }
    }
    asyncio.run(coord._load_state_if_needed())
    assert coord._last_processed_versions == {
        ("24ZZS00000000001", date(2026, 5, 1)): 1,
    }
