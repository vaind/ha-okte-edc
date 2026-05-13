# OKTE EDC for Home Assistant

A Home Assistant custom integration that ingests electricity sharing
settlement data (process `SZE_7`) from
[OKTE](https://www.okte.sk/) — the Slovak short-term electricity market
operator — and exposes it as native Home Assistant sensors compatible with
the Energy dashboard.

> **Unofficial.** This integration is not endorsed by, affiliated with, or
> supported by OKTE, a.s.

## ⚠️ Early version — use at your own risk

This is a pre-1.0 release that has not been battle-tested against a
broad range of mailboxes, OKTE files, or Home Assistant versions. By
installing it you accept the following risks. No warranty is provided;
see [LICENSE](./LICENSE).

- **Email loss or unintended modification.** The integration logs into
  your IMAP mailbox with the credentials you supply, marks messages
  with custom flags, and — depending on your cleanup setting — can
  move them to another folder or *permanently delete* them. A
  misconfiguration (wrong archive folder, overly aggressive
  delete-after-N-days), an IMAP server quirk, or a bug in this code
  could in the worst case affect or destroy emails beyond OKTE
  settlement messages. **Strongly recommended:** point it at a
  dedicated mailbox you can afford to lose, not your primary inbox.
- **Mailbox lockout.** Repeated failed login attempts (e.g. after a
  password rotation that hasn't been re-entered in HA) can trip
  rate-limiting or smart-lockout policies on Gmail, Microsoft 365,
  and similar providers, temporarily locking you out of the account.
- **Credentials in HA storage.** Your IMAP username and password are
  stored in Home Assistant's config-entry store (encrypted at rest by
  HA, but accessible to anyone who can read your HA installation, run
  the diagnostics download, or access a backup). Treat the HA instance
  as a sensitive credential store.
- **Private data on disk and in backups.** Once data is ingested, your
  per-EIC electricity consumption / production / sharing values are
  stored in HA's recorder database and in any backups you take. EICs
  and consumption patterns are personally identifying information that
  can reveal occupancy, household size, and lifestyle patterns. Don't
  publish your HA backups or diagnostics dumps verbatim.
- **Long-term statistics corruption.** The integration writes hourly
  long-term statistics directly into HA's recorder using a
  running-cumulative-kWh counter. A bug in the running sum, a recorder
  purge while the integration is running, or an interrupted import can
  result in incorrect, non-monotonic, or duplicated statistics — which
  may pollute your Energy dashboard. Cleaning up requires manual
  intervention in the recorder.
- **Wrong numbers, real consequences.** Reconciliation invariants are
  checked but not enforced; if the parser or aggregation is wrong, the
  sensor values and Energy-dashboard bars will be wrong. **Do not use
  these values for billing disputes, tax filings, regulatory reporting,
  or any other purpose where accuracy matters** without independently
  reconciling against your supplier's invoice and OKTE's web portal.
- **OKTE format changes.** If OKTE changes the email subject prefix,
  filename format, MSCONS structure, or LIN codes, this integration may
  silently stop importing data or import it incorrectly until a fix is
  released.
- **Trusted-mailbox assumption.** The integration matches messages by
  subject substring and attachment filename, and applies a configurable
  **sender allowlist** (defaulting to `edc@okte.sk`) — it does *not*
  verify DKIM/SPF or sign-verify the payload. The allowlist alone
  doesn't prove the message actually came from OKTE; someone with the
  ability to spoof the `From` header and deliver into your mailbox
  could still inject fake data. Defense in depth: use a dedicated
  mailbox whose address is not public, keep the allowlist populated,
  and don't publish EICs you've enabled.

  If you rely on manual mail forwarding (the `Fwd:` kind that rewrites
  the From header to you) instead of automatic forwarding (which
  preserves the original sender), add your own forwarder address to
  the allowlist — or clear the allowlist entirely to disable sender
  filtering.
- **No support guarantee.** This is a hobbyist project. Issues may take
  time to be triaged or may not be fixed at all.

If any of these risks are unacceptable for your setup, don't install
this integration yet. Wait for a stabilized release or roll your own.

Before installing it's recommended to:

1. **Review the integration code yourself** (or have someone you trust
   do it). This integration was largely co-authored with an AI assistant,
   was written quickly to a single spec, and has had no external code
   review at the time of the first release. The codebase is small —
   roughly 1.5k lines of Python under `custom_components/okte_edc/` —
   and you'll be handing it your IMAP credentials and giving it write
   access to your mailbox, so giving it a read before installing is a
   sensible precaution.
2. Create a **dedicated email account** that only receives OKTE
   settlement emails (or forwards from another inbox), so the worst
   case is losing those emails only.
3. **Back up your Home Assistant configuration and recorder database**
   before first use and before any major version upgrade.
4. Start with cleanup set to `leave_in_place` (the default). Don't
   switch to `archive` or `delete_after_days` until you've watched the
   integration run for at least a few cycles without issues.

## What it does

OKTE delivers daily MSCONS XML files (in the `E4SK40` profile) to your
mailbox describing how much electricity was shared between members of an
SZE group during each 15-minute interval. This integration:

1. Polls a configured IMAP mailbox for OKTE settlement emails.
2. Parses the attached MSCONS files (handling `.xml` and `.xml.gz`,
   plus the basic-ISO and extended-ISO date formats and DST days).
3. Creates one HA device per metering point (EIC) with the appropriate
   energy + diagnostic sensors.
4. Imports hourly long-term statistics so the Energy dashboard shows
   accurate historical bars going back as far as your mailbox has data.

Off-take and producer metering points are auto-detected from the EIC
prefix (`24ZZSVYR…` → producer, anything else → off-take).

## Entities

For each enabled EIC the integration creates:

| Suffix                 | Source LIN | Role      | Energy dashboard slot |
| ---------------------- | ---------- | --------- | --------------------- |
| `grid_import`          | CPS15      | off-take  | Grid consumption      |
| `shared_in`            | SHA15      | off-take  | Solar production      |
| `total_consumption`    | PS15       | off-take  | (reference)           |
| `grid_return`          | CPM15      | producer  | Return to grid        |
| `shared_out`           | SHA15      | producer  | (informational)       |
| `total_export`         | PM15       | producer  | (reference)           |
| `last_import`          | —          | both      | diagnostic            |
| `file_version`         | —          | both      | diagnostic            |
| `reconciliation_delta` | —          | both      | diagnostic            |

Entity IDs follow `sensor.okte_<short_eic>_<suffix>`, where `<short_eic>`
is the lowercased last 8 alphanumeric characters of the EIC.

## Installation (HACS)

1. In HACS → **Custom repositories**, add this repository URL with
   category *Integration*.
2. Install **OKTE EDC** and restart Home Assistant.
3. **Settings → Devices & Services → Add Integration**, select **OKTE
   EDC**, and enter the IMAP credentials of the mailbox that receives the
   OKTE settlement emails.
4. On the next step, select which discovered EICs you want to import.

## Polling and email cleanup

The integration polls IMAP only within a configurable time window
(default: **09:00 – 13:00 Europe/Bratislava**) because OKTE publishes
once a day in this window. Outside the window the coordinator stays
quiet and serves cached data. Polling cadence within the window is also
configurable (default 30 minutes).

After a message has been processed it's marked with a `$OkteProcessed`
custom IMAP keyword so the integration never re-processes it. You can
choose what happens to processed messages:

- **Leave in place** (default).
- **Archive** to a folder of your choice.
- **Delete** after N days.

If the IMAP server doesn't support arbitrary keywords (rare) the
integration falls back to using `\Seen` for tracking and emits a startup
warning.

## Reconciliation and corrections

Every MSCONS file is checked for internal consistency:

- Off-take: `PS15[i] − SHA15[i] − CPS15[i] ≈ 0` for every interval.
- Producer: `PM15[i] − SHA15[i] − CPM15[i] ≈ 0` for every interval.

The largest per-interval deviation is exposed as the per-EIC
`reconciliation_delta` sensor (in kWh). A delta above 1e-3 kWh produces
a warning in the log but the data is still imported.

When OKTE publishes a corrected file (`_V2`, `_V3`, …) the integration
detects the higher version and overwrites the prior values for the same
intervals. The `file_version` sensor reflects the most recently
processed version.

## Configuration

All settings live in the integration's **Configure** screen:

- **Polling interval (minutes)** — default 30.
- **Polling window start / end** — default 09:00–13:00.
- **Polling timezone** — default `Europe/Bratislava`.
- **Email cleanup mode** — leave / archive / delete.
- **Archive folder** — when cleanup is `archive`.
- **Delete after N days** — when cleanup is `delete_after_days`.
- **Scan window (days)** — how far back to look on first install and on
  rescan; default 30.
- **Per-EIC enable toggles** — disable an EIC without removing it.
- **Rescan mailbox for new metering points** — picks up newly added
  EICs without re-installing the integration.

## What this integration does *not* do

- **No live "today" extrapolation.** OKTE publishes D+1; sensors stay at
  yesterday's final state until tomorrow's email arrives.
- **No PV gross-yield data.** OKTE files contain *export to grid*, not
  *generation*. Self-consumed PV is invisible to OKTE because it never
  crosses the meter.
- **No inverter or battery control.**
- **No schedule / optimization analysis.** A separate companion project
  is planned for that.

## Troubleshooting

- **No EICs discovered.** Confirm the mailbox actually contains OKTE
  emails (subject contains `[EDC_SZE_7/SZE]`) within the configured
  scan window. If you forward from another mailbox, the `Fwd:` prefix
  is fine — the integration does substring matching.
- **Reconciliation delta is high.** Open an issue with the file
  attached (after redacting your EIC) — this almost always indicates
  either a parser bug or unusual data from OKTE.
- **Statistics aren't appearing in the Energy dashboard.** Check that
  the sensor entity_id matches `sensor.okte_<8-chars>_<suffix>` and
  that the device class / state class are correctly recognised; the
  HA Developer Tools → Statistics page will say if a sensor is being
  picked up.

## Development

```bash
python3 -m pip install --user defusedxml pytest
python3 -m pytest tests/
```

Tests cover the MSCONS parser, hourly aggregation (including
spring-forward 23-hour and fall-back 25-hour days), reconciliation, the
V1→V2 correction flow, filename regex, and attachment extraction. The
test suite uses minimal stubs of `homeassistant.*` so it runs without a
full HA install.

The anonymized OKTE files under `tests/fixtures/` were produced from
real production files via `tests/_anonymize.py`. To regenerate or extend
them with your own samples:

```bash
python3 tests/_anonymize.py path/to/your_real_file.xml
```

The anonymizer replaces EICs, partner codes, and per-message reference
numbers with deterministic synthetic values; the per-quarter quantities
are kept so reconciliation invariants remain testable. Anything under
`tests/fixtures/real/` is `.gitignore`-d so unprocessed real files are
never committed by accident.

## License

MIT — see [LICENSE](./LICENSE).
