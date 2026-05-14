# Contributing

## Quick start

```bash
python3 -m pip install --user defusedxml pytest
python3 -m pytest tests/
```

Tests cover the MSCONS parser, hourly aggregation (including
spring-forward 23-hour and fall-back 25-hour days), reconciliation, the
V1→V2 correction flow, filename regex, attachment extraction, the
dynamic SINCE cutoff, Store-backed processed-state persistence, and the
sensor inventory. The test suite uses minimal stubs of `homeassistant.*`
under `tests/conftest.py` so it runs without a full HA install.

## Test fixtures

The anonymized OKTE files under `tests/fixtures/` were produced from
real production files via `tests/_anonymize.py`. To regenerate or
extend them with your own samples:

```bash
python3 tests/_anonymize.py path/to/your_real_file.xml
```

The anonymizer is pattern-driven and contains **no real identifiers in
its source**. Each EIC, partner code, and per-message reference number
encountered in the input is mapped to a deterministic synthetic
counterpart. Per-quarter quantities are kept so reconciliation
invariants remain testable.

Anything under `tests/fixtures/real/` is `.gitignore`-d so unprocessed
real files can be kept locally without risk of accidental commits.

## Testing against a live mailbox

For end-to-end verification, place a `.env` file (gitignored) in the
repository root with:

```
IMAP_SERVER=imap.example.com
IMAP_USER=address@example.com
IMAP_PASS=...
```

There is no committed runner — write small ad-hoc Python scripts that
load the env, call into `okte_edc.imap_client` / `okte_edc.coordinator`,
and print only structural information (counts, redacted samples). Never
log full From addresses, full EICs, or message bodies; the integration's
own diagnostics output is a good model for what to redact and what to
show.

## Code style

- Comments explain *why*, not *what*. Names should carry the *what*.
  See [CLAUDE.md](./CLAUDE.md) (if present in your environment) and the
  user-level instructions for the broader philosophy.
- Fail fast at boundaries; trust internal invariants. No `try`/`except`
  around things that should never raise.
- No backward-compatibility shims for features that have not yet
  shipped.
- New behaviour gets a focused unit test. Match the existing test
  style: small, fast, no HA dependency.

## Commits

- One concern per commit.
- Commit message body explains the *why* and the trade-off, not the
  diff (the diff is in the commit). The body for "fix EIC spoofing"
  should describe the attack and the defense, not just "added a
  check".
- Don't `--amend` or rewrite history that's already been pushed unless
  there's an explicit reason (e.g. PII leak).

## Pull requests

Welcome but expect review feedback before merge. The integration is in
early stages and a lot of the design is still load-bearing on
particular invariants (e.g. statistic_id = HA-derived entity_id;
processed-state lives in HA Store, not on the IMAP server). When in
doubt, check [AGENTS.md](./AGENTS.md) and the surrounding tests before
proposing a refactor.
