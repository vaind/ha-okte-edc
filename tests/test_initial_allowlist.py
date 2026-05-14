"""Pin the initial sender-allowlist composition.

Regression target: pre-fix, `_compose_initial_allowlist` returned only
the discovered senders if any existed (so a user whose mailbox had
only forwarded mail got an allowlist of just the forwarder, and a
direct OKTE message later got rejected). Post-fix: the documented
OKTE production sender is unconditionally included, deduplicated
against whatever discovery also saw.
"""

from __future__ import annotations


def test_includes_okte_default_even_when_only_forwards_were_seen():
    from okte_edc.config_flow import _compose_initial_allowlist

    result = _compose_initial_allowlist(["forwarder@example.com"])
    parts = [p.strip() for p in result.split(",")]
    assert "edc@okte.sk" in parts
    assert "forwarder@example.com" in parts
    assert len(parts) == 2


def test_includes_only_default_when_discovery_saw_nothing():
    from okte_edc.config_flow import _compose_initial_allowlist

    result = _compose_initial_allowlist([])
    assert result == "edc@okte.sk"


def test_dedupes_when_discovery_already_saw_okte_directly():
    from okte_edc.config_flow import _compose_initial_allowlist

    result = _compose_initial_allowlist(
        ["edc@okte.sk", "forwarder@example.com"]
    )
    parts = [p.strip() for p in result.split(",")]
    assert sorted(parts) == ["edc@okte.sk", "forwarder@example.com"]
    # No duplicates even though the default + a discovered match collide.
    assert parts.count("edc@okte.sk") == 1


def test_normalises_case_during_dedup():
    """A discovered address in mixed case must dedupe against the default."""
    from okte_edc.config_flow import _compose_initial_allowlist

    result = _compose_initial_allowlist(["EDC@OKTE.sk"])
    assert result == "edc@okte.sk"
