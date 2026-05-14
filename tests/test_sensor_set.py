"""Pin the set of sensors created per EIC and per service device.

If a future refactor accidentally drops one of these, the count fails
visibly here rather than only showing up as a missing tile in the HA
UI after install.
"""

from __future__ import annotations


class _FakeEntry:
    entry_id = "abc"
    data = {"host": "imap.example", "port": 993, "username": "u",
            "password": "p", "folder": "INBOX", "use_ssl": True,
            "eics": [
                {"eic": "24ZZS00000000001", "role": "offtake", "enabled": True},
                {"eic": "24ZZSVYR00000099", "role": "producer", "enabled": True},
            ]}
    options: dict = {}


class _FakeCoordinator:
    def __init__(self):
        self.enabled_eics = {
            "24ZZS00000000001": "offtake",
            "24ZZSVYR00000099": "producer",
        }


def test_sensor_count_per_eic_and_service():
    """Each offtake EIC: 3 energy + 5 diagnostic = 8. Producer same shape = 8.
    Service: 3 timestamps. Total 8 + 8 + 3 = 19 sensors."""
    from okte_edc.sensor import _iter_entities

    entities = list(_iter_entities(_FakeEntry(), _FakeCoordinator()))
    assert len(entities) == 19


def test_service_sensors_present():
    from okte_edc.const import (
        SUFFIX_LAST_POLL,
        SUFFIX_LAST_SUCCESSFUL_POLL,
        SUFFIX_NEXT_POLL,
    )
    from okte_edc.sensor import OkteServiceSensor, _iter_entities

    entities = list(_iter_entities(_FakeEntry(), _FakeCoordinator()))
    service_keys = {
        e.entity_description.key
        for e in entities
        if isinstance(e, OkteServiceSensor)
    }
    assert service_keys == {
        SUFFIX_NEXT_POLL,
        SUFFIX_LAST_POLL,
        SUFFIX_LAST_SUCCESSFUL_POLL,
    }


def test_new_per_eic_diagnostic_sensors_present():
    from okte_edc.const import SUFFIX_MEASUREMENT_DATE, SUFFIX_PARSE_WARNINGS
    from okte_edc.sensor import OkteEicSensor, _iter_entities

    entities = list(_iter_entities(_FakeEntry(), _FakeCoordinator()))
    keys_per_eic: dict[str, set[str]] = {}
    for e in entities:
        if not isinstance(e, OkteEicSensor):
            continue
        keys_per_eic.setdefault(e._eic, set()).add(e.entity_description.key)
    for eic, keys in keys_per_eic.items():
        assert SUFFIX_MEASUREMENT_DATE in keys, eic
        assert SUFFIX_PARSE_WARNINGS in keys, eic
