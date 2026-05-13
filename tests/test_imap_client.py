"""Tests for IMAP client helpers that are pure (no live server)."""

from __future__ import annotations

import email
import email.message
import gzip

from okte_edc.const import FILENAME_RE, parse_sender_allowlist
from okte_edc.imap_client import (
    _extract_okte_attachments,
    _extract_sender_address,
    _imap_quote,
)


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


def test_extract_attachments_rejects_oversized_raw_payload():
    """A multi-megabyte raw attachment is dropped without decompression."""
    from okte_edc.const import MAX_RAW_ATTACHMENT_BYTES

    huge = b"X" * (MAX_RAW_ATTACHMENT_BYTES + 1)
    msg = _make_email([("24ZZS00000000001_20260503_D_V1.xml", huge)])
    assert list(_extract_okte_attachments(msg)) == []


def test_extract_attachments_rejects_zip_bomb_like_gzip():
    """A gzip whose decompressed size exceeds the cap is dropped."""
    from okte_edc.const import MAX_DECOMPRESSED_XML_BYTES

    big = b"A" * (MAX_DECOMPRESSED_XML_BYTES + 1024)
    gz = gzip.compress(big)
    msg = _make_email([("24ZZS00000000001_20260503_D_V1.xml.gz", gz)])
    assert list(_extract_okte_attachments(msg)) == []


# Sender extraction & allowlist parsing


def test_extract_sender_plain_address():
    assert _extract_sender_address("edc@okte.sk") == "edc@okte.sk"


def test_extract_sender_with_display_name():
    assert (
        _extract_sender_address('"OKTE EDC" <edc@okte.sk>') == "edc@okte.sk"
    )


def test_extract_sender_lowercases():
    assert _extract_sender_address("EDC@OKTE.SK") == "edc@okte.sk"


def test_extract_sender_missing_header_returns_empty():
    assert _extract_sender_address("") == ""


def test_parse_sender_allowlist_basic():
    assert parse_sender_allowlist("edc@okte.sk") == ["edc@okte.sk"]
    assert parse_sender_allowlist(
        "edc@okte.sk, FORWARDER@example.com"
    ) == ["edc@okte.sk", "forwarder@example.com"]


def test_parse_sender_allowlist_empty():
    assert parse_sender_allowlist("") == []
    assert parse_sender_allowlist(None) == []
    assert parse_sender_allowlist("   ") == []
    assert parse_sender_allowlist(", ,,") == []


# IMAP quoting


def test_imap_quote_brackets():
    """The integration subject contains `[` and `]` — without quoting the IMAP
    server treats it as an invalid atom and silently returns nothing."""
    assert _imap_quote("[EDC_SZE_7/SZE]") == '"[EDC_SZE_7/SZE]"'


def test_imap_quote_escapes_quotes_and_backslash():
    assert _imap_quote('a"b\\c') == '"a\\"b\\\\c"'


def test_imap_quote_folder_with_slash():
    assert _imap_quote("Archive/OKTE") == '"Archive/OKTE"'


def test_subject_filter_variants_are_progressively_broader():
    """Three variants, narrow → broad: SUBJECT-full, TEXT-full, TEXT-token.

    The last variant uses a punctuation-free token so it still matches
    on servers whose fulltext implementation tokenizes the subject
    around ``[``/``]``/``/``.
    """
    from okte_edc.imap_client import ImapSession

    variants = ImapSession._subject_filter_variants()
    assert len(variants) == 3
    assert variants[0][0] == "SUBJECT"
    assert variants[1][0] == "TEXT"
    assert variants[2][0] == "TEXT"
    assert variants[0][1] == '"[EDC_SZE_7/SZE]"'
    assert variants[1][1] == '"[EDC_SZE_7/SZE]"'
    assert variants[2][1] == '"EDC_SZE_7"'
