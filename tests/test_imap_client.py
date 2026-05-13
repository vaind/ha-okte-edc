"""Tests for IMAP client helpers that are pure (no live server)."""

from __future__ import annotations

import email
import email.message
import gzip

from okte_edc.const import FILENAME_RE
from okte_edc.imap_client import _extract_okte_attachments


def _make_email(filenames: list[tuple[str, bytes]]) -> email.message.Message:
    msg = email.message.EmailMessage()
    msg["Subject"] = "[EDC_SZE_7/SZE] foo bar"
    msg.set_content("hello")
    for name, payload in filenames:
        msg.add_attachment(
            payload,
            maintype="application",
            subtype="octet-stream",
            filename=name,
        )
    return msg


def test_filename_regex_basic():
    m = FILENAME_RE.match("24ZZS00000000001_20260503_D_V1.xml")
    assert m is not None
    assert m.group("eic") == "24ZZS00000000001"
    assert m.group("date") == "20260503"
    assert m.group("version") == "1"


def test_filename_regex_gzipped():
    m = FILENAME_RE.match("24ZZSVYR00000099_20260503_D_V2.xml.gz")
    assert m is not None
    assert m.group("eic") == "24ZZSVYR00000099"
    assert m.group("version") == "2"


def test_filename_regex_lowercase_extension():
    m = FILENAME_RE.match("24ZZS00000000001_20260503_d_v1.XML")
    assert m is not None


def test_filename_regex_rejects_wrong_pattern():
    assert FILENAME_RE.match("not_a_real_filename.xml") is None
    assert FILENAME_RE.match("24ZZS00000000001_2026_D_V1.xml") is None


def test_extract_attachments_decompresses_gz():
    payload = b"<MSCONSDOCUMENT/>"
    gz = gzip.compress(payload)
    msg = _make_email(
        [("24ZZS00000000001_20260503_D_V1.xml.gz", gz)]
    )
    atts = list(_extract_okte_attachments(msg))
    assert len(atts) == 1
    assert atts[0].payload == payload
    assert atts[0].eic == "24ZZS00000000001"
    assert atts[0].file_version == 1


def test_extract_attachments_ignores_non_matching_filenames():
    msg = _make_email([("random.txt", b"data"), ("readme.pdf", b"more")])
    assert list(_extract_okte_attachments(msg)) == []
