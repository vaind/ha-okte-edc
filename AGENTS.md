# Notes for AI agents working on this repo

You're modifying a Home Assistant custom integration that imports OKTE
EDC settlement files from an IMAP mailbox and writes long-term
statistics into HA's recorder. Real users run this against their real
mailboxes; mistakes here have visible blast radius (wrong Energy
dashboard numbers, mutated mail state, leaked PII).

Read [CONTRIBUTING.md](./CONTRIBUTING.md) for the developer workflow.
This file documents the non-obvious constraints — the things you
*cannot* infer from a quick read of the code.

## Non-obvious constraints

**The statistic_id must equal the HA-derived entity_id.** Long-term
statistics with `source="recorder"` are linked to a sensor entity by
exact string match. Sensors use `_attr_has_entity_name = True`, which
makes HA compose the entity_id from the device-name slug
(`OKTE EDC <slug>` → `okte_edc_<slug>`) + the translation key. The
coordinator's `statistic_id_for(eic, suffix)` in `const.py` constructs
the matching string. If you change device naming, translation keys, or
this helper without updating all three together, every imported row
becomes an orphan that the Energy dashboard never sees. `tests/test_mscons.py::test_statistic_id_matches_ha_entity_id_derivation`
pins the format — keep it green.

**The mailbox is read-only by default.** The integration does **not**
set `\Seen`, does **not** set any custom keyword, and fetches with
`BODY.PEEK[]` specifically so the act of reading does not toggle
state. The user's mail-client state belongs to the user. The only
modifications allowed are the ones the user explicitly opted into via
the `email_cleanup` option (archive copy+delete, time-based delete).
Re-introducing `\Seen` or `$OkteProcessed` as a tracking mechanism is
a regression — processed-state lives in HA Store, keyed by
`(eic, measurement_date)`.

**IMAP search uses multi-variant fallback.** Real-world servers
disagree wildly: some reject `SUBJECT` entirely with "Only TEXT
keyword is currently supported"; some accept it but tokenize fulltext
on `[` `]` `/` so `TEXT "[EDC_SZE_7/SZE]"` matches nothing. The
`_subject_filter_variants` helper tries SUBJECT-full, TEXT-full, then
TEXT-`EDC_SZE_7` (punctuation-free token) and unions the UIDs. Don't
"simplify" to a single call without testing against multiple servers.
Downstream filename-regex + EIC cross-checks catch any false
positives the broader variants pick up.

**`tests/_anonymize.py` contains no real identifiers in its source.**
The substitution table is pattern-driven (EIC / partner-code /
reference-number shapes detected by regex). Adding a real-to-fake
hardcoded mapping is a regression — that approach previously leaked
production identifiers into git history. If you need to anonymize a
new fixture, run the script on it; don't edit the script to know
about your particular EICs.

**Tests run without HA installed.** `tests/conftest.py` installs
minimal stubs for `homeassistant.*` (Platform, ConfigEntry, Store,
DeviceInfo, ButtonEntity, …). If you add a module-level
`from homeassistant.…` import in production code, either add the
matching stub to conftest or — usually better — do the import lazily
inside the function that needs it. `okte_edc/statistics.py` is a
worked example of the lazy-import pattern.

**Size caps are a security feature, not a polite-ness.** `MAX_RAW_ATTACHMENT_BYTES`
(2 MB) and `MAX_DECOMPRESSED_XML_BYTES` (10 MB) bound the worst-case
memory cost of a poisoned attachment. The gunzip path is streaming
and stops early when the cap is hit. Real OKTE files are ~85 KB raw —
the caps are 20×+ over real-world need. Don't relax them.

**Filename ↔ XML cross-checks.** After parsing, the coordinator
verifies that the EIC and the measurement date encoded in the
filename match the XML's `PLACE_ID` and inferred date. Don't skip
either check; both are defense-in-depth against a crafted attachment
that declares one identity in the name and another in the body.

**Diagnostics dumps are public attack surface.** Users routinely
attach diagnostics to GitHub issues. Every EIC is redacted to its
`short_eic` slug, host/folder/username/password are stripped, and
some option values (archive_folder, sender_allowlist) are filtered.
If you add a new field to coordinator state, decide whether it's safe
to dump verbatim — if it's user-supplied or identifies a household,
add it to the redact list in `diagnostics.py`.

## Architecture pointer

Read in this order if you're new:

1. `const.py` — definitions, regexes, role detection, `statistic_id_for`.
2. `mscons.py` — pure parser, no HA dependency. Test against real fixtures.
3. `imap_client.py` — sync wrapper around `imaplib`. The fallback search logic
   and the gunzip cap live here.
4. `coordinator.py` — DataUpdateCoordinator. Dynamic SINCE cutoff, HA Store
   persistence, per-EIC dedup, cleanup actions.
5. `sensor.py` + `button.py` — entity layer. `_service_device_info` is shared
   by `__init__.py`'s pre-registration call.
6. `config_flow.py` — three-step initial flow (creds → folder → EICs)
   plus a post-setup info page, options flow with rescan, reauth.

## Style — minimum bar

- Comments only when the *why* isn't obvious from the name. No
  multi-paragraph docstrings for one-line helpers.
- New behaviour comes with a focused unit test, in the existing
  style (small, fast, HA-stub-friendly).
- One concern per commit; commit body explains *why* and the
  trade-off.
- Don't introduce backward-compat shims for features that have not
  yet shipped. The integration is pre-1.0.

## When you're tempted to take a destructive action

Force-pushing, deleting `tests/fixtures/`, `git reset --hard`,
re-creating the repo on the GitHub side: **don't** without a clear
reason and an explicit go-ahead from the user. The repo had one
PII-leak history rewrite already; further history surgery should be a
rare, reasoned act, not a routine cleanup.
