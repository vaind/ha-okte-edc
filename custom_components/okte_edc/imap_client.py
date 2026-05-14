"""Synchronous IMAP client used by the OKTE EDC integration.

The integration polls a mailbox on a configurable cadence and never holds a
long-lived connection, so a tiny sync wrapper around ``imaplib`` is plenty.
The coordinator runs all client methods through the HA executor.

Tracking of processed messages uses the ``$OkteProcessed`` custom IMAP
keyword, falling back to ``\\Seen`` if the server doesn't advertise
``\\*`` in PERMANENTFLAGS. See spec §10.3.
"""

from __future__ import annotations

import email
import gzip
import imaplib
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import Message
from email.utils import parseaddr
from typing import Iterable, Iterator

from .const import (
    FILENAME_RE,
    MAX_DECOMPRESSED_XML_BYTES,
    MAX_RAW_ATTACHMENT_BYTES,
    PROCESSED_KEYWORD,
    SUBJECT_SUBSTRING,
)

_LOGGER = logging.getLogger(__name__)


class ImapAuthError(Exception):
    """IMAP server refused the credentials."""


class ImapConnectionError(Exception):
    """Network / TLS / connect-time failure."""


class ImapFolderError(Exception):
    """Selected folder does not exist."""


@dataclass(frozen=True)
class Attachment:
    """An attachment extracted from an OKTE email."""

    filename: str
    eic: str
    file_date: str  # YYYYMMDD as in the filename
    file_version: int
    payload: bytes  # already decompressed (if .gz) so callers don't care
    raw_payload: bytes  # original bytes, useful for debugging


@dataclass(frozen=True)
class FetchedMessage:
    """A single fetched OKTE message and its OKTE attachments."""

    uid: bytes
    subject: str
    sender: str  # lowercased email address parsed from the From header
    attachments: list[Attachment]


class ImapSession:
    """Lightweight wrapper around an authenticated, folder-selected IMAP4."""

    def __init__(
        self,
        connection: imaplib.IMAP4,
        folder: str,
        *,
        keyword_supported: bool,
    ) -> None:
        self._conn = connection
        self._folder = folder
        self.keyword_supported = keyword_supported

    # ----- search / fetch ------------------------------------------------

    def search_unprocessed_uids(
        self, since: datetime | None = None
    ) -> list[bytes]:
        """Return UIDs of messages matching the OKTE subject that are not yet processed.

        ``since`` bounds the search to recent messages — important for
        keyword-fallback servers where there's no IMAP-side processed
        filter and we re-fetch every matching message in the window
        each cycle (the coordinator's in-memory state dedups, but the
        bandwidth is bounded by the time window).
        """
        criteria: list[str] = list(self._unprocessed_state_criteria())
        if since is not None:
            criteria += ["SINCE", since.strftime("%d-%b-%Y")]
        return self._search_with_subject_filter(*criteria)

    def search_recent_subject(self, since: datetime) -> list[bytes]:
        """Return UIDs of messages matching the OKTE subject delivered on/after ``since``.

        Used by the discovery flow to enumerate EICs without consulting
        any processed-state flag.
        """
        since_str = since.strftime("%d-%b-%Y")
        return self._search_with_subject_filter("SINCE", since_str)

    def _search_with_subject_filter(self, *extra: str) -> list[bytes]:
        """Run UID SEARCH across multiple subject-filter variants and union the results.

        Real-world IMAP servers vary in how they handle SUBJECT and TEXT
        searches against tokens with punctuation like ``[EDC_SZE_7/SZE]``:

        - Most RFC-3501-compliant servers accept ``SUBJECT "[…]"`` and
          do case-insensitive substring matching.
        - Some servers (observed in the wild) reject ``SUBJECT`` entirely
          with ``Only TEXT keyword is currently supported``.
        - Some servers' fulltext implementation tokenizes on punctuation,
          so ``TEXT "[EDC_SZE_7/SZE]"`` matches nothing even though the
          string is present in the message.

        We try three variants — narrow ``SUBJECT`` first, then ``TEXT``
        full pattern, then ``TEXT`` on the longest punctuation-free
        token (``EDC_SZE_7``) — and union the UIDs from every variant
        the server accepts. False positives are caught downstream by
        the attachment filename regex and the EIC cross-check, so a
        slightly wider net is harmless.
        """
        uids: set[bytes] = set()
        last_error: object = None
        for criteria in self._subject_filter_variants():
            full_criteria = [*criteria, *extra]
            _LOGGER.debug("IMAP SEARCH: %s", full_criteria)
            try:
                typ, data = self._conn.uid(
                    "SEARCH", None, *full_criteria
                )
            except imaplib.IMAP4.error as exc:
                last_error = exc
                continue
            if typ != "OK":
                last_error = data
                continue
            if data and data[0]:
                uids.update(data[0].split())
        if not uids and last_error is not None:
            _LOGGER.debug(
                "No matches across search variants; last error: %r",
                last_error,
            )
        return sorted(uids)

    @staticmethod
    def _subject_filter_variants() -> list[list[str]]:
        full_pattern = _imap_quote(SUBJECT_SUBSTRING)
        narrow_token = _imap_quote("EDC_SZE_7")
        return [
            ["SUBJECT", full_pattern],
            ["TEXT", full_pattern],
            ["TEXT", narrow_token],
        ]

    def search_processed_before(self, before: datetime) -> list[bytes]:
        """Return UIDs of already-processed messages older than ``before``."""
        before_str = before.strftime("%d-%b-%Y")
        if self.keyword_supported:
            criteria = ["KEYWORD", PROCESSED_KEYWORD, "BEFORE", before_str]
        else:
            # Without keyword support we can't reliably distinguish processed
            # from any other read message; conservatively no-op.
            return []
        typ, data = self._conn.uid("SEARCH", None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def fetch_message(self, uid: bytes) -> FetchedMessage | None:
        """Fetch a full message by UID and extract OKTE attachments.

        Uses ``BODY.PEEK[]`` instead of the legacy ``RFC822`` form.
        Per RFC 3501 §6.4.5, ``RFC822`` sets the ``\\Seen`` flag as a
        side effect of fetching — which silently turns into a bug on
        servers where we use ``\\Seen`` as the processed marker (every
        first-time fetch would mark the message as already processed
        before we'd actually done anything with it).
        """
        typ, data = self._conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK":
            raise ImapConnectionError(f"UID FETCH {uid!r} failed: {typ}")
        if not data or not data[0]:
            return None
        raw = data[0][1] if isinstance(data[0], tuple) else data[0]
        if isinstance(raw, str):
            raw = raw.encode()
        msg = email.message_from_bytes(raw)
        subject = _decode_header(msg.get("Subject", ""))
        sender = _extract_sender_address(msg.get("From", ""))
        attachments = list(_extract_okte_attachments(msg))
        return FetchedMessage(
            uid=uid,
            subject=subject,
            sender=sender,
            attachments=attachments,
        )

    # ----- state mutations ---------------------------------------------

    def mark_processed(self, uid: bytes) -> None:
        flag = PROCESSED_KEYWORD if self.keyword_supported else "\\Seen"
        typ, _ = self._conn.uid("STORE", uid, "+FLAGS", flag)
        if typ != "OK":
            _LOGGER.warning("Failed to set %s on UID %s", flag, uid)

    def archive(self, uid: bytes, archive_folder: str) -> bool:
        """Copy a UID to ``archive_folder`` then mark for deletion.

        Returns True on success, False if the folder doesn't exist (caller
        falls back to leave-in-place for this run).
        """
        typ, _ = self._conn.uid("COPY", uid, _imap_quote(archive_folder))
        if typ != "OK":
            _LOGGER.warning(
                "Archive copy to %s failed for UID %s", archive_folder, uid
            )
            return False
        typ, _ = self._conn.uid("STORE", uid, "+FLAGS", "\\Deleted")
        return typ == "OK"

    def mark_for_delete(self, uids: Iterable[bytes]) -> None:
        for uid in uids:
            typ, _ = self._conn.uid("STORE", uid, "+FLAGS", "\\Deleted")
            if typ != "OK":
                _LOGGER.warning("Mark-delete failed for UID %s", uid)

    def expunge(self) -> None:
        self._conn.expunge()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        try:
            self._conn.logout()
        except Exception:  # noqa: BLE001
            pass

    # ----- internals ----------------------------------------------------

    def _unprocessed_state_criteria(self) -> list[str]:
        """Return just the processed-state half of the search criteria.

        Combined with the (potentially multi-variant) subject filter by
        :meth:`_search_with_subject_filter`.

        For keyword-supported servers we rely on the ``$OkteProcessed``
        keyword we set ourselves, which is reliable.

        For keyword-fallback servers we deliberately do **not** filter
        on ``\\Seen`` here. Earlier versions did, but ``\\Seen`` can be
        toggled by the user's mail client (or any other process that
        opens the message), and we now rely on the coordinator's
        in-memory ``_last_processed_versions`` mapping to skip already
        imported (eic, date) pairs before doing real work — so an
        accidentally re-fetched message is cheap, not duplicated.
        """
        if self.keyword_supported:
            return ["NOT", "KEYWORD", PROCESSED_KEYWORD]
        return []


class ImapClient:
    """Connection factory; creates a session per poll cycle."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        folder: str,
        use_ssl: bool,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._folder = folder
        self._use_ssl = use_ssl

    def open_session(self) -> ImapSession:
        """Connect, login, select. Raises typed exceptions on failure."""
        conn = self._connect_and_login()
        typ, data = conn.select(self._folder)
        if typ != "OK":
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
            raise ImapFolderError(
                f"SELECT {self._folder!r} failed: {data!r}"
            )

        keyword_supported = _detect_keyword_support(conn, data)
        return ImapSession(conn, self._folder, keyword_supported=keyword_supported)

    def verify_credentials(self) -> None:
        """Open a connection, log in, and disconnect.

        Cheaper than ``open_session`` for paths (reauth, options validation)
        that just need to confirm the credentials work.
        """
        conn = self._connect_and_login()
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass

    def list_folders(self) -> list[str]:
        """Connect, log in, and return the list of folder names on the server.

        Used by the config flow to present a folder dropdown instead of
        asking the user to type a folder name. Returns folders in the
        order the server reported them. The connection is closed before
        returning.
        """
        conn = self._connect_and_login()
        try:
            typ, data = conn.list()
            if typ != "OK" or not data:
                return []
            return _parse_folder_list(data)
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

    def _connect_and_login(self) -> imaplib.IMAP4:
        try:
            if self._use_ssl:
                conn = imaplib.IMAP4_SSL(self._host, self._port)
            else:
                conn = imaplib.IMAP4(self._host, self._port)
        except (OSError, socket.gaierror, imaplib.IMAP4.error) as exc:
            raise ImapConnectionError(str(exc)) from exc

        try:
            conn.login(self._username, self._password)
        except imaplib.IMAP4.error as exc:
            err = str(exc).lower()
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
            if "auth" in err or "credential" in err or "login" in err:
                raise ImapAuthError(str(exc)) from exc
            raise ImapConnectionError(str(exc)) from exc
        return conn


# ---------------------------------------------------------------------------
# Helpers


def _detect_keyword_support(conn: imaplib.IMAP4, select_data: list[bytes]) -> bool:
    """Return True if the server allows arbitrary user keywords.

    Detected via PERMANENTFLAGS containing ``\\*`` in the SELECT response.
    """
    for line in select_data:
        if isinstance(line, bytes):
            line = line.decode(errors="replace")
        if "PERMANENTFLAGS" in line and "\\*" in line:
            return True
    # Some libraries return PERMANENTFLAGS only via untagged responses.
    untagged = conn.response("PERMANENTFLAGS")
    if untagged and untagged[1]:
        for line in untagged[1]:
            if isinstance(line, bytes):
                line = line.decode(errors="replace")
            if "\\*" in line:
                return True
    _LOGGER.warning(
        "IMAP server does not advertise arbitrary keywords; "
        "falling back to \\Seen for processed-tracking."
    )
    return False


def _imap_quote(value: str) -> str:
    """Wrap ``value`` in an IMAP quoted-string (RFC 3501 §4.3).

    Quoted strings escape ``"`` and ``\\`` with a leading backslash. Bare
    IMAP atoms can't contain ``[``, ``]``, ``{``, ``}``, ``(``, ``)``,
    spaces, or many other punctuation characters, so any user-controlled
    or punctuation-bearing argument we pass to ``UID SEARCH`` / ``UID
    COPY`` / similar must use this helper.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _decode_header(raw: str) -> str:
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001
        return raw


def _extract_sender_address(raw: str) -> str:
    """Return the lowercased email address from a possibly RFC2047-encoded From header.

    Strips display name (``"OKTE <edc@okte.sk>"`` → ``edc@okte.sk``). Returns
    ``""`` when the header is missing or unparseable; callers should treat
    that as "unknown sender" and apply their own policy.
    """
    if not raw:
        return ""
    decoded = _decode_header(raw)
    _, address = parseaddr(decoded)
    return address.strip().lower()


def _extract_okte_attachments(msg: Message) -> Iterator[Attachment]:
    """Yield Attachment for each MSCONS file matching the filename pattern."""
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        # email may MIME-encode the filename
        filename = _decode_header(filename)
        match = FILENAME_RE.match(filename)
        if not match:
            continue
        try:
            raw = part.get_payload(decode=True)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to decode attachment %s: %s", filename, exc
            )
            continue
        if raw is None:
            continue
        if len(raw) > MAX_RAW_ATTACHMENT_BYTES:
            _LOGGER.warning(
                "Skipping attachment %s: raw size %d exceeds cap %d",
                filename,
                len(raw),
                MAX_RAW_ATTACHMENT_BYTES,
            )
            continue
        if filename.lower().endswith(".gz"):
            try:
                payload = _safe_gunzip(raw)
            except OSError as exc:
                _LOGGER.warning(
                    "Failed to gunzip %s: %s", filename, exc
                )
                continue
        else:
            payload = raw
        if len(payload) > MAX_DECOMPRESSED_XML_BYTES:
            _LOGGER.warning(
                "Skipping attachment %s: decompressed size %d exceeds cap %d",
                filename,
                len(payload),
                MAX_DECOMPRESSED_XML_BYTES,
            )
            continue
        yield Attachment(
            filename=filename,
            eic=match.group("eic").upper(),
            file_date=match.group("date"),
            file_version=int(match.group("version")),
            payload=payload,
            raw_payload=raw,
        )


def _safe_gunzip(raw: bytes) -> bytes:
    """Decompress gzip data, bailing out at ``MAX_DECOMPRESSED_XML_BYTES``.

    Streaming-decompress so we can stop reading early when a malformed
    or maliciously crafted .gz expands far beyond a real OKTE file's
    size (which is on the order of 100 KB).
    """
    import io

    out = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
        while True:
            chunk = gz.read(64 * 1024)
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > MAX_DECOMPRESSED_XML_BYTES:
                raise OSError(
                    f"gzip output exceeds cap ({MAX_DECOMPRESSED_XML_BYTES} bytes)"
                )
    return bytes(out)


def file_date_to_iso(file_date: str) -> str:
    """Convert YYYYMMDD to YYYY-MM-DD for logging/state purposes."""
    return f"{file_date[0:4]}-{file_date[4:6]}-{file_date[6:8]}"


# RFC 3501 §7.2.2: each LIST response is
# ``(flags) "delimiter" "mailbox-name"``. We don't care about flags or
# delimiter; we just want the mailbox name. Mailbox names may be quoted
# (most common) or unquoted; if quoted, they may contain spaces.
_FOLDER_LINE_RE = re.compile(
    rb'^\([^)]*\)\s+(?:"[^"]*"|NIL)\s+(?:"(?P<quoted>[^"]*)"|(?P<unquoted>\S+))\s*$'
)


def _parse_folder_list(data: list[bytes]) -> list[str]:
    folders: list[str] = []
    for line in data:
        if line is None:
            continue
        if isinstance(line, tuple):
            # Long mailbox names come back as (header, content) tuples.
            line = b"".join(part for part in line if isinstance(part, bytes))
        match = _FOLDER_LINE_RE.match(line)
        if not match:
            continue
        name_bytes = match.group("quoted") or match.group("unquoted")
        if name_bytes is None:
            continue
        try:
            # imaplib returns IMAP UTF-7 for non-ASCII names.
            from imaplib import IMAP4

            name = imaputf7_decode(name_bytes)
        except Exception:  # noqa: BLE001
            name = name_bytes.decode("utf-8", errors="replace")
        folders.append(name)
    return folders


def imaputf7_decode(raw: bytes) -> str:
    """Decode IMAP modified UTF-7 (RFC 3501 §5.1.3) to a Python string.

    IMAP servers encode non-ASCII mailbox names in modified UTF-7. Python's
    stdlib ``imap_utf7`` codec is registered only inside the ``imaplib``
    module; we replicate the minimal decode behavior here so folder names
    show correctly in the config-flow dropdown.
    """
    text = raw.decode("ascii", errors="replace")
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c != "&":
            out.append(c)
            i += 1
            continue
        # & followed by - is a literal '&'
        end = text.find("-", i + 1)
        if end == -1:
            out.append(text[i:])
            break
        chunk = text[i + 1 : end]
        if chunk == "":
            out.append("&")
        else:
            # IMAP UTF-7 uses ',' instead of '/' as the 64th base64 char.
            import base64

            try:
                decoded = base64.b64decode(
                    chunk.replace(",", "/") + "=" * (-len(chunk) % 4)
                )
                out.append(decoded.decode("utf-16-be"))
            except Exception:  # noqa: BLE001
                out.append(text[i : end + 1])
        i = end + 1
    return "".join(out)
