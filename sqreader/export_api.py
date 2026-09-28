"""Export API v1: a token-protected, read-only pull interface for finished matches.

Zero Zone (or any other consumer that holds the token) pulls FINALIZED matches
and their replay recordings from here. It is independent of the central
push/enroll machinery (ingest_client push, fleet, updater): nothing in this
module talks to the network, and it changes nothing on disk.

All endpoints are GET (HEAD is answered too), JSON except the recording
download, and all need ``Authorization: Bearer <token>``::

    GET /export/v1/info                   reader / server identity
    GET /export/v1/health                 what the reader can actually vouch for
    GET /export/v1/matches?after=&limit=  finalized matches, cursor-paged
    GET /export/v1/matches/<matchId>      one match bundle (sqr-match-1)
    GET /export/v1/recordings/<recId>     NDJSON replay stream (gzip / zstd)

What is exported
----------------
Only matches with ``status='final'`` AND ``end_state='finalized'`` (the end of
the match was actually observed). ``unverified`` / ``legacy`` / ``open`` rows
never appear, and asking for one by id gives the same 404 as an unknown id.
Only recordings whose sidecar says ``recordingState='finalized'`` are served.
A recording belongs to a match through the sidecar's ``matchId``.

Off unless configured
---------------------
The token lives in a file (``--export-token-file`` / config
``export_token_file``). No file, an empty file, an unreadable file or a token
shorter than 32 characters leaves the API OFF: every ``/export/v1/*`` path is
then a plain 404 (its existence is not revealed) and one line is logged at
start-up. The token is never logged and never appears in a response body.

The handler wiring (routing, streaming) lives in httpsrv.py; this module owns
the policy: authentication, what may be exported, and the response shapes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from .ingest_client import SCHEMA_MATCH, build_match_bundle
from .recording_lifecycle import RECORDING_STATE_FINALIZED

log = logging.getLogger("sqreader.export")

SCHEMA_EXPORT = "sqr-export-1"
MIN_TOKEN_LEN = 32
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

# Match ids are OWI MatchIDs (UUIDs) or synthetic uuid5 strings. ':' is
# excluded so "<endedAt>:<matchId>" splits unambiguously. fullmatch, not
# match(): `$` would let a trailing newline through.
_MATCH_ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,128}")
# Recording id = the .sqrx file's stem (same charset as /api/recording/<id>).
_REC_ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,200}")
_CURSOR_RE = re.compile(r"([0-9]{1,15}):([A-Za-z0-9_\-]{1,128})")
_LIMIT_RE = re.compile(r"[0-9]{1,6}")
# A header-safe token: printable ASCII, no spaces.
_TOKEN_RE = re.compile(r"[\x21-\x7e]+")


# --------------------------------------------------------------------------
# response objects (httpsrv.py turns these into bytes on the wire)
# --------------------------------------------------------------------------

@dataclass
class RecordingStream:
    """A recording the handler must stream (path is server-built, never echoed)."""
    path: Path
    rec_id: str
    headers: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ExportResponse:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: list[tuple[str, str]] = field(default_factory=list)
    recording: Optional[RecordingStream] = None


def _json(status: int, obj: Any, *, headers: Optional[list[tuple[str, str]]] = None
          ) -> ExportResponse:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    hdrs = [("Cache-Control", "no-store")]
    hdrs.extend(headers or [])
    return ExportResponse(status, body, "application/json", hdrs)


def _error(status: int, code: str, *, headers: Optional[list[tuple[str, str]]] = None
           ) -> ExportResponse:
    # Fixed text per code: nothing request-derived (and never the token) is echoed.
    return _json(status, {"schema": SCHEMA_EXPORT, "error": code}, headers=headers)


def _not_found() -> ExportResponse:
    # One body for "no such match", "not finalized" and "bad id" alike.
    return _error(404, "not_found")


def canonical_json(obj: Any) -> bytes:
    """The bytes a bundle hash is taken over: sorted keys, fixed separators,
    UTF-8, no ASCII escaping."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def bundle_sha256(bundle: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(bundle)).hexdigest()


# --------------------------------------------------------------------------
# sidecar (recording metadata) reading — read-only, never self-heals
# --------------------------------------------------------------------------

class _SidecarIndex:
    """Finalized recordings of one directory, grouped by match id.

    Reads ``<id>.meta.json`` sidecars only. It deliberately does not reuse
    httpsrv._MetaCache: that one repairs a missing sidecar by writing a new one,
    and an export must not modify the recordings directory.
    """

    def __init__(self, directory: Path, server_id: str) -> None:
        self._dir = directory
        self._server_id = server_id
        self._cache: dict[str, tuple[tuple[int, int, int], Optional[dict]]] = {}

    def _sidecar(self, sidecar: Path) -> Optional[dict]:
        try:
            st = sidecar.stat()
        except OSError:
            return None
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        hit = self._cache.get(sidecar.name)
        if hit is not None and hit[0] == sig:
            return hit[1]
        meta: Optional[dict] = None
        try:
            loaded = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, ValueError):
            meta = None
        self._cache[sidecar.name] = (sig, meta)
        return meta

    def finalized(self, rec_id: str) -> Optional[tuple[Path, dict]]:
        """(sqrx path, sidecar) of a finalized recording of THIS server, else
        None. Fails closed on anything unexpected."""
        if not _REC_ID_RE.fullmatch(rec_id):
            return None
        sqrx = self._dir / f"{rec_id}.sqrx"
        if not sqrx.is_file():
            return None
        meta = self._sidecar(sqrx.with_suffix(".meta.json"))
        if (meta is None
                or meta.get("recordingState") != RECORDING_STATE_FINALIZED
                or meta.get("inProgress") is not False
                or meta.get("serverId") != self._server_id
                or not isinstance(meta.get("matchId"), str)
                # the id ends up in a response header: same strict charset
                or not _MATCH_ID_RE.fullmatch(meta["matchId"])):
            return None
        return sqrx, meta

    def by_match(self) -> dict[str, list[dict[str, Any]]]:
        """match id -> export items of its finalized recordings."""
        out: dict[str, list[dict[str, Any]]] = {}
        try:
            names = sorted(e.name for e in os.scandir(self._dir)
                           if e.name.endswith(".sqrx"))
        except OSError:
            return out
        for name in names:
            rec_id = name[:-len(".sqrx")]
            got = self.finalized(rec_id)
            if got is None:
                continue
            sqrx, meta = got
            try:
                size = sqrx.stat().st_size
            except OSError:
                continue
            out.setdefault(meta["matchId"], []).append(
                recording_item(rec_id, meta, size))
        for items in out.values():
            items.sort(key=lambda r: (r["startedAtUtc"] or "", r["id"]))
        return out


def _count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0 else None


def recording_item(rec_id: str, meta: dict, size_bytes: int) -> dict[str, Any]:
    return {
        "id": rec_id,
        "state": RECORDING_STATE_FINALIZED,
        "totalFrames": _count(meta.get("totalFrames")),
        "sizeBytes": size_bytes,
        "startedAtUtc": meta.get("startedAtUtc"),
        "endedAtUtc": meta.get("endedAtUtc"),
    }


# --------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------

class ExportApi:
    """Request policy for /export/v1/*. One instance serves one reader/server."""

    def __init__(self, *, token: str, server_id: str, stats_db: Path,
                 recordings_dir: Optional[Path] = None,
                 squad_port: Optional[int] = None,
                 container_id: Optional[str] = None,
                 reader_state: Optional[Callable[[], str]] = None) -> None:
        if len(token) < MIN_TOKEN_LEN:
            raise ValueError(f"export token must be at least {MIN_TOKEN_LEN} characters")
        self._token = token.encode("utf-8")
        self.server_id = server_id
        self._db = Path(stats_db)
        self._recordings_dir = Path(recordings_dir) if recordings_dir else None
        self._sidecars = (_SidecarIndex(self._recordings_dir, server_id)
                          if self._recordings_dir else None)
        self._squad_port = squad_port
        self._container_id = container_id
        self._reader_state = reader_state

    def __repr__(self) -> str:  # the token must not leak through a stray repr
        return f"ExportApi(server_id={self.server_id!r})"

    # -- entry point ---------------------------------------------------------

    def handle(self, method: str, target: str,
               authorization: Optional[str]) -> ExportResponse:
        """Answer one request. `target` is the raw request target
        (path plus query string); `authorization` the Authorization header."""
        if method not in ("GET", "HEAD"):
            return _error(405, "method_not_allowed",
                          headers=[("Allow", "GET, HEAD")])
        if not self._authorized(authorization):
            return _error(401, "unauthorized",
                          headers=[("WWW-Authenticate", "Bearer")])
        path, _, query = target.partition("?")
        try:
            return self._route(path, query)
        except sqlite3.Error:
            return _error(503, "stats_unavailable")

    def _authorized(self, header: Optional[str]) -> bool:
        if not header:
            return False
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(value.strip().encode("utf-8"), self._token)

    def _route(self, path: str, query: str) -> ExportResponse:
        prefix = "/export/v1/"
        if not path.startswith(prefix):
            return _not_found()
        parts = path[len(prefix):].split("/")
        if parts == ["info"]:
            return self._info()
        if parts == ["health"]:
            return self._health()
        if parts == ["matches"]:
            return self._matches(query)
        if len(parts) == 2 and parts[0] == "matches":
            return self._match(parts[1])
        if len(parts) == 2 and parts[0] == "recordings":
            return self._recording(parts[1])
        return _not_found()

    # -- helpers -------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # as_uri() percent-encodes the path; mode=ro cannot write even by mistake.
        conn = sqlite3.connect(f"{self._db.resolve().as_uri()}?mode=ro", uri=True,
                               timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _state(self) -> str:
        if self._reader_state is None:
            return "unknown"
        try:
            state = self._reader_state()
        except Exception:  # noqa: BLE001 - a status hook must never break the API
            return "unknown"
        return state if isinstance(state, str) and state else "unknown"

    def _stats_schema_version(self) -> Optional[int]:
        try:
            conn = self._connect()
            try:
                return int(conn.execute("PRAGMA user_version").fetchone()[0])
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            return None

    def _bundle(self, match_id: str) -> Optional[dict[str, Any]]:
        """The bundle of a finalized match of this server, else None.

        Built with ingest_client.build_match_bundle so the columns are SR's raw
        ones. `elo` is dropped: it is the CURRENT rating of each player across
        all matches, so it changes whenever any other match is rated and would
        make the hash of an old match drift. Rows are sorted so the same data
        always serialises (and hashes) the same way.
        """
        bundle = build_match_bundle(self._db, match_id)
        if bundle is None:
            return None
        m = bundle.get("match") or {}
        if (m.get("status") != "final" or m.get("end_state") != "finalized"
                or m.get("server_id") != self.server_id
                or m.get("ended_at") is None):
            return None
        bundle.pop("elo", None)
        bundle["players"].sort(key=lambda r: str(r.get("eos_id")))
        bundle["kills"].sort(key=lambda r: (r.get("id") is None, r.get("id")))
        bundle["vehicles"].sort(key=lambda r: (r.get("id") is None, r.get("id")))
        return bundle

    def _recordings_of(self, match_id: str) -> list[dict[str, Any]]:
        if self._sidecars is None:
            return []
        return self._sidecars.by_match().get(match_id, [])

    # -- endpoints -----------------------------------------------------------

    def _info(self) -> ExportResponse:
        return _json(200, {
            "schema": SCHEMA_EXPORT,
            "readerVersion": __version__,
            "statsSchemaVersion": self._stats_schema_version(),
            "serverId": self.server_id,
            "squadPort": self._squad_port,
            "containerId": self._container_id,
            "readerState": self._state(),
        })

    def _health(self) -> ExportResponse:
        readable = False
        last: Optional[int] = None
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT MAX(ended_at) FROM matches WHERE status='final' "
                    "AND end_state='finalized' AND server_id=?",
                    (self.server_id,)).fetchone()
                last = row[0] if row and row[0] is not None else None
                readable = True
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            readable = False
        rec_ok: Optional[bool] = None
        if self._recordings_dir is not None:
            try:
                with os.scandir(self._recordings_dir):
                    rec_ok = True
            except OSError:
                rec_ok = False
        state = self._state()
        ok = readable and rec_ok is not False and state != "stale"
        return _json(200, {
            "schema": SCHEMA_EXPORT,
            "ok": ok,
            "readerState": state,
            "lastFinalizedAt": last,
            "statsDbReadable": readable,
            "recordingsDirReadable": rec_ok,
        })

    def _matches(self, query: str) -> ExportResponse:
        try:
            qs = urllib.parse.parse_qs(query, keep_blank_values=True,
                                       strict_parsing=bool(query))
        except ValueError:
            return _error(400, "bad_query")
        if any(len(v) != 1 for v in qs.values()):
            return _error(400, "bad_query")

        limit = DEFAULT_LIMIT
        if "limit" in qs:
            raw = qs["limit"][0]
            if not _LIMIT_RE.fullmatch(raw) or int(raw) < 1:
                return _error(400, "bad_limit")
            limit = min(int(raw), MAX_LIMIT)

        after: Optional[tuple[int, str]] = None
        if "after" in qs:
            m = _CURSOR_RE.fullmatch(qs["after"][0])
            if not m:
                return _error(400, "bad_cursor")
            after = (int(m.group(1)), m.group(2))

        sql = ("SELECT match_id, ended_at FROM matches WHERE status='final' "
               "AND end_state='finalized' AND server_id=? AND ended_at IS NOT NULL")
        args: list[Any] = [self.server_id]
        if after is not None:
            sql += " AND (ended_at > ? OR (ended_at = ? AND match_id > ?))"
            args += [after[0], after[0], after[1]]
        sql += " ORDER BY ended_at ASC, match_id ASC LIMIT ?"
        args.append(limit)
        conn = self._connect()
        try:
            rows = [(str(r["match_id"]), r["ended_at"])
                    for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

        recordings = self._sidecars.by_match() if self._sidecars else {}
        items: list[dict[str, Any]] = []
        for match_id, _ended in rows:
            bundle = self._bundle(match_id)
            if bundle is None:      # re-opened or changed since the listing query
                continue
            m = bundle["match"]
            items.append({
                "matchId": m.get("match_id"),
                "serverId": m.get("server_id"),
                "startedAt": m.get("started_at"),
                "endedAt": m.get("ended_at"),
                "endState": m.get("end_state"),
                "mapName": m.get("map_name"),
                "layerName": m.get("layer_name"),
                "gameMode": m.get("game_mode"),
                "bundleSha256": bundle_sha256(bundle),
                "recordings": recordings.get(match_id, []),
            })
        next_cursor: Optional[str] = None
        if len(rows) == limit:
            last_id, last_ended = rows[-1]
            next_cursor = f"{last_ended}:{last_id}"
        return _json(200, {
            "schema": SCHEMA_EXPORT,
            "serverId": self.server_id,
            "matches": items,
            "nextCursor": next_cursor,
        })

    def _match(self, match_id: str) -> ExportResponse:
        if not _MATCH_ID_RE.fullmatch(match_id):
            return _not_found()
        bundle = self._bundle(match_id)
        if bundle is None:
            return _not_found()
        return _json(200, {
            "schema": SCHEMA_MATCH,
            "readerVersion": __version__,
            "statsSchemaVersion": self._stats_schema_version(),
            "serverId": self.server_id,
            "endState": bundle["match"].get("end_state"),
            "bundleSha256": bundle_sha256(bundle),
            "bundle": bundle,
            "recordings": self._recordings_of(match_id),
        })

    def _recording(self, rec_id: str) -> ExportResponse:
        if self._sidecars is None:
            return _not_found()
        got = self._sidecars.finalized(rec_id)
        if got is None:
            return _not_found()
        sqrx, meta = got
        total = _count(meta.get("totalFrames"))
        if total is None:
            total = _count_frames(sqrx)
            if total is None:
                return _not_found()
        return ExportResponse(200, recording=RecordingStream(
            sqrx, rec_id,
            [("X-Sqr-Total-Frames", str(total)),
             ("X-Sqr-Match-Id", meta["matchId"])]))


def _count_frames(sqrx: Path) -> Optional[int]:
    """NDJSON line count of a recording whose sidecar predates `totalFrames`.
    Streams the file; never holds it in memory."""
    from .sqrx import SqrxReader
    try:
        with SqrxReader(sqrx) as r:
            return sum(1 for _ in r)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _read_token(path: Path) -> Optional[str]:
    """The token in `path`, or None (after logging why). Never logs the token."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning("export API disabled: token file %s does not exist", path)
        return None
    except (OSError, UnicodeDecodeError):
        log.warning("export API disabled: token file %s is not readable", path)
        return None
    token = raw.strip()
    if not token:
        log.warning("export API disabled: token file %s is empty", path)
        return None
    if len(token) < MIN_TOKEN_LEN:
        log.error("export API disabled: the token in %s is shorter than %d "
                  "characters", path, MIN_TOKEN_LEN)
        return None
    if not _TOKEN_RE.fullmatch(token):
        log.error("export API disabled: the token in %s must be printable ASCII "
                  "without whitespace", path)
        return None
    if os.name == "posix":
        try:
            if path.stat().st_mode & 0o077:
                log.warning("export token file %s is readable by group/others; "
                            "run: chmod 600 %s", path, path)
        except OSError:
            pass
    return token


def configure_export(token_file: Any, *, server_id: str,
                     stats_db: Optional[Path],
                     recordings_dir: Optional[Path] = None,
                     squad_port: Any = None,
                     container_id: Optional[str] = None,
                     reader_state: Optional[Callable[[], str]] = None
                     ) -> Optional[ExportApi]:
    """Build the Export API from settings, or None when it is (or must stay) off.

    No token file configured is the normal, silent default. A configured file
    that cannot be used logs one line and leaves the API off.
    """
    if not token_file:
        return None
    if stats_db is None:
        log.warning("export API disabled: it needs --stats-db")
        return None
    token = _read_token(Path(str(token_file)).expanduser())
    if token is None:
        return None
    port: Optional[int] = None
    try:
        port = int(squad_port) if squad_port not in (None, "") else None
    except (TypeError, ValueError):
        port = None
    return ExportApi(token=token, server_id=server_id, stats_db=Path(stats_db),
                     recordings_dir=recordings_dir, squad_port=port,
                     container_id=container_id or None, reader_state=reader_state)


__all__ = ["ExportApi", "ExportResponse", "RecordingStream", "configure_export",
           "canonical_json", "bundle_sha256", "SCHEMA_EXPORT"]
