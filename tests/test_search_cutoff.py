"""Pin the dynamic SINCE-cutoff logic.

Steady-state polls should ask the server for ~one week of mail, not
the full scan_window backfill. First runs should still use the
configured scan_window so the initial backfill picks up history.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def _build_coordinator(monkeypatch):
    import okte_edc.coordinator as coord_module
    from okte_edc.coordinator import OkteCoordinator

    class _MemStore:
        def __init__(self, *_a, **_kw):
            self.payload = None
            self.key = "test-store"

        async def async_load(self):
            return self.payload

        async def async_save(self, _data):
            return None

        async def async_remove(self):
            return None

    monkeypatch.setattr(coord_module, "Store", _MemStore)

    class _FakeEntry:
        entry_id = "abc"
        data = {
            "host": "h", "port": 993, "username": "u", "password": "p",
            "folder": "INBOX", "use_ssl": True, "eics": [],
        }
        options: dict = {}

    return OkteCoordinator(None, _FakeEntry())


def test_first_run_uses_scan_window(monkeypatch):
    """No stored history → SINCE = today − scan_window_days."""
    coord = _build_coordinator(monkeypatch)
    now = datetime.now(tz=timezone.utc)
    cutoff = coord._compute_search_cutoff(scan_window_days=30)
    delta = now - cutoff
    assert 29 <= delta.days <= 30  # ~30, allow for sub-second drift


def test_steady_state_uses_correction_buffer(monkeypatch):
    """Stored history → SINCE = last_measurement_date − 7 days."""
    coord = _build_coordinator(monkeypatch)
    coord._last_processed_versions = {
        ("24ZZS00000000001", date(2026, 5, 12)): 1,
        ("24ZZS00000000002", date(2026, 5, 10)): 1,
    }
    cutoff = coord._compute_search_cutoff(scan_window_days=30)
    expected = datetime(2026, 5, 12, tzinfo=timezone.utc) - timedelta(days=7)
    assert cutoff == expected


def test_uses_more_recent_of_the_two(monkeypatch):
    """If the steady-state cutoff is older than scan_window, prefer scan_window.

    Edge case: an integration that was last run a year ago has stored
    state but the latest date is well outside the scan window. SINCE
    should NOT regress to a year-old date — it should stay bounded by
    the configured scan window.
    """
    coord = _build_coordinator(monkeypatch)
    coord._last_processed_versions = {
        # Pretend last run was a year ago.
        ("24ZZS00000000001", date(2025, 1, 1)): 1,
    }
    cutoff = coord._compute_search_cutoff(scan_window_days=30)
    now = datetime.now(tz=timezone.utc)
    delta = now - cutoff
    # ~30 days, not 365+
    assert 29 <= delta.days <= 30
