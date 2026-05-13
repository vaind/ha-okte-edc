"""One-off anonymizer for real OKTE fixtures.

Real MSCONS files contain identifiers that pin a payload to a specific
sharing group and account: the EIC of each metering point, the sharing
group recipient code, and the per-message reference / access / document
numbers. None of these affect parsing behavior — they're just opaque
identifiers — so we substitute deterministic synthetic values before
committing the files as test fixtures. The 15-minute quantity values
are not personally identifying and are kept as-is so reconciliation
invariants remain testable.

The anonymizer is **pattern-driven**, not table-driven: real identifiers
do not appear anywhere in this file's source. Each real value the script
sees in an input file is mapped to a stable synthetic counterpart for
that run, so cross-file references (e.g. the same recipient code across
three files in the same SZE group) stay consistent.

Usage::

    python3 tests/_anonymize.py ~/Downloads/<EIC>_<YYYYMMDD>_D_V1.xml ...

Writes anonymized copies into ``tests/fixtures/`` with the synthetic
EICs embedded in their filenames.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Public synthetic placeholders. Producers keep the `VYR` infix so the
# parser's role-detection regex still resolves them as producers.
_OFFTAKE_EIC_TEMPLATE = "24ZZS{:011d}"
_PRODUCER_EIC_TEMPLATE = "24ZZSVYR{:08d}"
_X_PARTNER_TEMPLATE = "24X-EXAMPLE-{:04d}"
_Y_PARTNER_TEMPLATE = "24Y-EXAMPLE-{:04d}"
_REFERENCE_TEMPLATE = "FIXTURE{:08d}A"

# Patterns for the identifier shapes OKTE emits. Each is matched against
# the file text and replaced with a deterministic synthetic counterpart;
# the same input always produces the same output within one invocation.
#
# We can't use ``\b`` boundaries because Python's regex treats ``_`` as a
# word character, and OKTE filenames embed the EIC between underscores
# (``<EIC>_<DATE>_D_V1.xml``). The lookaround pair excludes only further
# alphanumerics on either side.
_NO_ALNUM_BEFORE = r"(?<![A-Z0-9])"
_NO_ALNUM_AFTER = r"(?![A-Z0-9])"

_EIC_PRODUCER_RE = re.compile(
    _NO_ALNUM_BEFORE + r"24ZZSVYR[A-Z0-9]{8}" + _NO_ALNUM_AFTER
)
_EIC_OFFTAKE_RE = re.compile(
    _NO_ALNUM_BEFORE + r"24ZZS(?!VYR)[A-Z0-9]{11}" + _NO_ALNUM_AFTER
)
_PARTNER_X_RE = re.compile(
    _NO_ALNUM_BEFORE + r"24X-[A-Z0-9-]{12}" + _NO_ALNUM_AFTER
)
_PARTNER_Y_RE = re.compile(
    _NO_ALNUM_BEFORE + r"24Y-[A-Z0-9-]{12}" + _NO_ALNUM_AFTER
)
# OKTE's reference / access / document tail: a base-36 token, 13 chars,
# starting with a digit. Matched after the partner / EIC patterns so it
# does not overlap with them.
_REFERENCE_RE = re.compile(
    _NO_ALNUM_BEFORE + r"[0-9][0-9A-Z]{12}" + _NO_ALNUM_AFTER
)


class _Mapper:
    """Hand out stable synthetic values keyed by the originals seen."""

    def __init__(self, template: str, start: int = 1) -> None:
        self._template = template
        self._next = start
        self._seen: dict[str, str] = {}

    def map(self, original: str) -> str:
        if original not in self._seen:
            self._seen[original] = self._template.format(self._next)
            self._next += 1
        return self._seen[original]


def anonymize(text: str, mappers: dict[str, _Mapper]) -> str:
    """Apply pattern-based substitutions, sharing mapper state across files."""

    def _sub(pattern: re.Pattern[str], mapper: _Mapper) -> None:
        nonlocal text
        text = pattern.sub(lambda m: mapper.map(m.group(0)), text)

    # Order matters: EIC patterns first, then partner codes, then
    # the more permissive reference token so it doesn't swallow them.
    _sub(_EIC_PRODUCER_RE, mappers["producer_eic"])
    _sub(_EIC_OFFTAKE_RE, mappers["offtake_eic"])
    _sub(_PARTNER_X_RE, mappers["x_partner"])
    _sub(_PARTNER_Y_RE, mappers["y_partner"])
    _sub(_REFERENCE_RE, mappers["reference"])
    return text


def _build_mappers() -> dict[str, _Mapper]:
    return {
        "offtake_eic": _Mapper(_OFFTAKE_EIC_TEMPLATE),
        "producer_eic": _Mapper(_PRODUCER_EIC_TEMPLATE, start=99),
        "x_partner": _Mapper(_X_PARTNER_TEMPLATE),
        "y_partner": _Mapper(_Y_PARTNER_TEMPLATE),
        "reference": _Mapper(_REFERENCE_TEMPLATE),
    }


def fixture_filename(original_path: Path, mappers: dict[str, _Mapper]) -> str:
    """Translate a real filename to its anonymized counterpart.

    The filename starts with the EIC; we re-apply the same mapper used
    inside the file so the renamed fixture matches its contents.
    """
    name = original_path.name
    eic_match = _EIC_PRODUCER_RE.match(name) or _EIC_OFFTAKE_RE.match(name)
    if not eic_match:
        raise ValueError(f"Filename does not start with an EIC: {name}")
    original_eic = eic_match.group(0)
    mapper = (
        mappers["producer_eic"]
        if _EIC_PRODUCER_RE.match(original_eic)
        else mappers["offtake_eic"]
    )
    return name.replace(original_eic, mapper.map(original_eic), 1)


def main(paths: list[str]) -> int:
    if not paths:
        print(__doc__)
        return 2
    target_dir = Path(__file__).parent / "fixtures"
    target_dir.mkdir(parents=True, exist_ok=True)
    mappers = _build_mappers()
    for raw_path in paths:
        source = Path(raw_path).expanduser().resolve()
        text = source.read_text(encoding="utf-8")
        anon = anonymize(text, mappers)
        dest = target_dir / fixture_filename(source, mappers)
        dest.write_text(anon, encoding="utf-8")
        print(f"  {source.name} -> {dest.relative_to(target_dir.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
