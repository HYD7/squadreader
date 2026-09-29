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
    GET /export/v1/unverified?after=&limit=
                                          summary list of matches that are NOT
                                          exported (end never observed); no
                                          player data, no stats
    GET /export/v1/recordings/<recId>     NDJSON replay stream (gzip / zstd)
    POST /export/v1/control/recording     {"enabled", "reason", "actor"}: switch new
                                          recordings on/off (persistent, audited)
    POST /export/v1/ack                   {"recordings": [...]}: "I mirrored these"

The two POST endpoints are the ONLY writes. They change nothing but the recording
switch and the ack list (files in the data directory, see export_state.py); they
take no path, run no command and change no other setting. A POST needs the same
Bearer token, ``Content-Type: application/json`` and a body of at most 4 KB.

What is exported
----------------
Only matches with ``status='final'`` AND ``end_state='finalized'`` (the end of
the match was actually observed). ``unverified`` / ``legacy`` / ``open`` rows
never appear, and asking for one by id gives the same 404 as an unknown id.
Only recordings whose sidecar says ``recordingState='finalized'`` are served.
A recording belongs to a match through the sidecar's ``matchId``.

Making the matches that are NOT exported visible
------------------------------------------------
A match whose end the reader never saw stays ``unverified`` and is never
exported (see docs/zerozone-kacan-maclar.md for when that happens). Two
read-only views make that loss countable without exporting anything from
those matches: ``health.unverified`` (counts) and ``GET /export/v1/unverified``
(a per-match SUMMARY: ids, times, map, peak players, player COUNT, never a
player row or a statistic). They do not touch the finalized-only guarantee of
``matches`` / ``matches/<id>`` / ``recordings/<id>``.

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
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__, netpolicy
from .export_state import (
    MAX_ACTOR_LEN,
    REC_ID_RE,
    ExportState,
    ExportStateError,
    RetentionPolicy,
    clean_text,
    disk_status,
    free_bytes,
    read_retention_state,
    recording_status,
)
from .ingest_client import SCHEMA_MATCH, build_match_bundle
from .recording_lifecycle import RECORDING_STATE_FINALIZED

log = logging.getLogger("sqreader.export")

SCHEMA_EXPORT = "sqr-export-1"
MIN_TOKEN_LEN = 32
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
#: POST bodies are tiny JSON objects; anything bigger is refused unread (413).
MAX_BODY_BYTES = 4096
MAX_ACK_RECORDINGS = 100
_CONTROL_PATH = "/export/v1/control/recording"
_ACK_PATH = "/export/v1/ack"
_POST_PATHS = (_CONTROL_PATH, _ACK_PATH)
#: How long a retention run that had to delete un-acked recordings keeps the
#: reader reported as degraded.
_UNACKED_DEGRADED_SEC = 24 * 3600

# Same pattern Zero Zone's isTrainingLayer() uses (jensen|training, any case),
# applied to the three descriptive columns joined by spaces. LIKE is
# case-insensitive for ASCII, which is all this pattern contains.
_TRAINING_TERMS = ("jensen", "training")
_DAY_SEC = 86400

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


def _training_sql(alias: str = "") -> str:
    """SQL boolean: the match's map/layer/mode names look like a training range."""
    p = f"{alias}." if alias else ""
    hay = (f"(COALESCE({p}map_name,'') || ' ' || COALESCE({p}layer_name,'') "
           f"|| ' ' || COALESCE({p}game_mode,''))")
    return "(" + " OR ".join(f"{hay} LIKE '%{t}%'" for t in _TRAINING_TERMS) + ")"


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

    def finalized_ids(self) -> Optional[list[str]]:
        """Ids of every finalized recording of THIS server; None when the
        directory cannot be read."""
        try:
            names = sorted(e.name for e in os.scandir(self._dir)
                           if e.name.endswith(".sqrx"))
        except OSError:
            return None
        return [n[:-len(".sqrx")] for n in names
                if self.finalized(n[:-len(".sqrx")]) is not None]

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


def _positive(value: Any) -> Optional[float | int]:
    """A configured limit, or None when it is off (0 / unset)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value) if float(value).is_integer() else value


def _safe_ip(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return clean_text(value, 64) or None


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
                 reader_state: Optional[Callable[[], str]] = None,
                 state: Optional[ExportState] = None,
                 data_dir: Optional[Path] = None,
                 retention: Optional[RetentionPolicy] = None,
                 active_recording: Optional[Callable[[], Optional[dict]]] = None,
                 target_info: Optional[Callable[[], Optional[dict]]] = None,
                 degraded_reasons: Optional[Callable[[], list[str]]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
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
        # Control switch + ack store live in the data directory (by default the
        # recordings directory's parent): see export_state.py.
        if data_dir is not None:
            self._data_dir: Optional[Path] = Path(data_dir)
        elif self._recordings_dir is not None:
            self._data_dir = self._recordings_dir.parent
        else:
            self._data_dir = self._db.parent
        self.state = state if state is not None else ExportState(self._data_dir)
        self._retention = retention if retention is not None else RetentionPolicy()
        self._active_recording = active_recording
        self._target_info = target_info
        self._degraded_reasons = degraded_reasons
        self._clock = clock if clock is not None else time.time

    max_body_bytes = MAX_BODY_BYTES

    def __repr__(self) -> str:  # the token must not leak through a stray repr
        return f"ExportApi(server_id={self.server_id!r})"

    # -- entry point ---------------------------------------------------------

    def handle(self, method: str, target: str, authorization: Optional[str], *,
               body: Optional[bytes] = None, content_type: Optional[str] = None,
               content_length: Optional[int] = None,
               client_ip: Optional[str] = None) -> ExportResponse:
        """Answer one request. `target` is the raw request target
        (path plus query string); `authorization` the Authorization header.
        `body`, `content_type`, `content_length` and `client_ip` matter to POST
        only (`body` is None when the transport refused to read it)."""
        path, _, query = target.partition("?")
        if method == "POST" and path in _POST_PATHS:
            if not self._authorized(authorization):
                return _error(401, "unauthorized",
                              headers=[("WWW-Authenticate", "Bearer")])
            if query:
                return _error(400, "bad_query")
            try:
                if path == _CONTROL_PATH:
                    return self._control(body, content_type, content_length, client_ip)
                return self._ack(body, content_type, content_length, client_ip)
            except ExportStateError as exc:
                return _error(500, exc.kind)
        if method not in ("GET", "HEAD"):
            return _error(405, "method_not_allowed",
                          headers=[("Allow", "GET, HEAD")])
        if not self._authorized(authorization):
            return _error(401, "unauthorized",
                          headers=[("WWW-Authenticate", "Bearer")])
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
        if parts == ["unverified"]:
            return self._unverified(query)
        if len(parts) == 2 and parts[0] == "matches":
            return self._match(parts[1])
        if len(parts) == 2 and parts[0] == "recordings":
            return self._recording(parts[1])
        if parts in (["control", "recording"], ["ack"]):
            return _error(405, "method_not_allowed", headers=[("Allow", "POST")])
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
        min_free = self._retention.min_free_bytes
        enabled, why = recording_status(self.state, self._recordings_dir, min_free)
        ret_state = (read_retention_state(self._data_dir)
                     if self._data_dir is not None else None) or {}
        reasons = self._degraded(state, readable, rec_ok, min_free, ret_state)
        return _json(200, {
            "schema": SCHEMA_EXPORT,
            "ok": ok,
            "readerState": state,
            "lastFinalizedAt": last,
            "statsDbReadable": readable,
            "recordingsDirReadable": rec_ok,
            "recordingEnabled": enabled,
            "recordingEnabledReason": why,
            "recordingControl": self.state.recording_control(),
            "activeRecording": self._provided(
                self._active_recording, ("id", "startedAtUtc", "sizeBytes")),
            "disk": disk_status(self._recordings_dir, min_free),
            "retention": {
                "maxAgeDays": _positive(self._retention.max_age_days),
                "maxTotalBytes": _positive(self._retention.max_total_bytes),
                "lastRunAt": ret_state.get("lastRunAt")
                if isinstance(ret_state.get("lastRunAt"), str) else None,
                "lastDeleted": _count(ret_state.get("lastDeleted")),
            },
            "degradedReason": reasons[0] if reasons else None,
            "degradedReasons": reasons,
            "target": self._provided(
                self._target_info,
                ("gamePort", "queryPort", "matchedPortKind", "containerId")),
            "netPolicy": netpolicy.status(),
            "pendingAck": self._pending_ack(),
            "unverified": self._unverified_summary(),
        })

    def _unverified_summary(self) -> Optional[dict[str, Any]]:
        """How many matches of THIS server were never exported because their end
        was not observed, next to how many were. None when the stats DB cannot
        be read.

        Only ``status='final'`` rows count: a match that is still running (or
        was re-opened after a reader restart) is neither. Times are the
        observed last moment (``ended_at``) in epoch seconds. Training-range
        matches are counted apart (``training``): Zero Zone pays nothing for
        them, so they must not inflate the headline numbers.
        """
        now = int(self._clock())
        d1, d7, d30 = (now - n * _DAY_SEC for n in (1, 7, 30))
        sql = (
            "SELECT end_state AS es, " + _training_sql() + " AS tr, "
            "COUNT(*) AS total, "
            "SUM(ended_at >= ?) AS n1, SUM(ended_at >= ?) AS n7, "
            "SUM(ended_at >= ?) AS n30, MAX(ended_at) AS last "
            "FROM matches WHERE status='final' "
            "AND end_state IN ('unverified','finalized') "
            "AND server_id=? AND ended_at IS NOT NULL GROUP BY es, tr")
        try:
            conn = self._connect()
            try:
                rows = conn.execute(sql, (d1, d7, d30, self.server_id)).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            return None

        def agg(state: str, training: bool) -> dict[str, Any]:
            for r in rows:
                if r["es"] == state and bool(r["tr"]) == training:
                    return {"total": int(r["total"]), "n1": int(r["n1"] or 0),
                            "n7": int(r["n7"] or 0), "n30": int(r["n30"] or 0),
                            "last": r["last"]}
            return {"total": 0, "n1": 0, "n7": 0, "n30": 0, "last": None}

        u, f = agg("unverified", False), agg("finalized", False)
        ut, ft = agg("unverified", True), agg("finalized", True)
        denom = u["n30"] + f["n30"]
        return {
            "total": u["total"],
            "last24h": u["n1"],
            "last7d": u["n7"],
            "last30d": u["n30"],
            "lastAt": u["last"],
            "finalizedLast7d": f["n7"],
            "finalizedLast30d": f["n30"],
            "ratioLast30d": round(u["n30"] / denom, 4) if denom else None,
            "training": {
                "total": ut["total"],
                "last30d": ut["n30"],
                "lastAt": ut["last"],
                "finalizedLast30d": ft["n30"],
            },
            "asOf": now,
        }

    @staticmethod
    def _provided(provider: Optional[Callable[[], Optional[dict]]],
                  keys: tuple[str, ...]) -> Optional[dict[str, Any]]:
        """A dict from a status hook, cut to `keys`; None if unknown. A hook must
        never break the API."""
        if provider is None:
            return None
        try:
            got = provider()
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(got, dict):
            return None
        return {k: got.get(k) for k in keys}

    def _degraded(self, state: str, readable: bool, rec_ok: Optional[bool],
                  min_free: int, ret_state: dict) -> list[str]:
        """Machine-readable reasons the reader is degraded, most important
        first. Empty = nothing wrong that this API can see."""
        out: list[str] = []
        if state not in ("ok", "unknown"):
            out.append(f"reader_{state}")
        if self._degraded_reasons is not None:
            try:
                extra = self._degraded_reasons()
            except Exception:  # noqa: BLE001
                extra = []
            out += [r for r in extra if isinstance(r, str) and r and r not in out]
        if not readable:
            out.append("stats_db_unreadable")
        if rec_ok is False:
            out.append("recordings_dir_unreadable")
        if min_free > 0 and self._recordings_dir is not None:
            free = free_bytes(self._recordings_dir)
            if free is not None and free < min_free:
                out.append("disk_low")
        stamp = ret_state.get("lastUnackedDeletedAt")
        if isinstance(stamp, str):
            try:
                when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc)
                if ((datetime.now(timezone.utc) - when).total_seconds()
                        < _UNACKED_DEGRADED_SEC):
                    out.append("unacked_deleted")
            except ValueError:
                pass
        return out

    def _pending_ack(self) -> Optional[int]:
        if self._sidecars is None:
            return None
        ids = self._sidecars.finalized_ids()
        if ids is None:
            return None
        acked = self.state.acked_ids()
        return sum(1 for i in ids if i not in acked)

    # -- POST endpoints --------------------------------------------------------

    @staticmethod
    def _read_body(body: Optional[bytes], content_type: Optional[str],
                   content_length: Optional[int]
                   ) -> tuple[Optional[dict], Optional[ExportResponse]]:
        """The JSON object of a POST body, or the error response to send."""
        ctype = (content_type or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return None, _error(415, "unsupported_media_type")
        if content_length is None:
            return None, _error(411, "length_required")
        if content_length > MAX_BODY_BYTES:
            return None, _error(413, "payload_too_large")
        if body is None or len(body) != content_length:
            return None, _error(400, "bad_body")

        def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for k, v in pairs:
                if k in out:
                    raise ValueError("duplicate key")
                out[k] = v
            return out

        def no_constants(name: str) -> Any:
            raise ValueError(name)

        try:
            obj = json.loads(body.decode("utf-8"), object_pairs_hook=no_duplicates,
                             parse_constant=no_constants)
        except (ValueError, RecursionError):
            return None, _error(400, "bad_json")
        if not isinstance(obj, dict):
            return None, _error(400, "bad_json")
        return obj, None

    def _control(self, body: Optional[bytes], content_type: Optional[str],
                 content_length: Optional[int], client_ip: Optional[str]
                 ) -> ExportResponse:
        obj, err = self._read_body(body, content_type, content_length)
        if err is not None or obj is None:
            return err or _error(400, "bad_json")
        if not set(obj) <= {"enabled", "reason", "actor"}:
            return _error(400, "unknown_key")
        enabled = obj.get("enabled")
        if not isinstance(enabled, bool):
            return _error(400, "bad_enabled")
        reason = obj.get("reason", "")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            return _error(400, "bad_reason")
        actor = obj.get("actor")
        if not isinstance(actor, str) or not clean_text(actor, MAX_ACTOR_LEN):
            return _error(400, "bad_actor")
        self.state.set_recording(enabled, reason, actor, _safe_ip(client_ip))
        now_enabled, why = recording_status(
            self.state, self._recordings_dir, self._retention.min_free_bytes)
        return _json(200, {
            "schema": SCHEMA_EXPORT,
            "recordingEnabled": now_enabled,
            "recordingEnabledReason": why,
            "control": self.state.recording_control(),
        })

    def _ack(self, body: Optional[bytes], content_type: Optional[str],
             content_length: Optional[int], client_ip: Optional[str]
             ) -> ExportResponse:
        obj, err = self._read_body(body, content_type, content_length)
        if err is not None or obj is None:
            return err or _error(400, "bad_json")
        if "recordings" not in obj:
            return _error(400, "bad_recordings")
        if set(obj) != {"recordings"}:
            return _error(400, "unknown_key")
        ids = obj["recordings"]
        if not isinstance(ids, list) or not ids:
            return _error(400, "bad_recordings")
        if len(ids) > MAX_ACK_RECORDINGS:
            return _error(400, "too_many_recordings")
        if not all(isinstance(i, str) and 0 < len(i) <= 200 for i in ids):
            return _error(400, "bad_recordings")
        accepted: list[str] = []
        rejected: list[str] = []
        for rec_id in dict.fromkeys(ids):
            ok = bool(REC_ID_RE.fullmatch(rec_id)) and (
                self.state.is_acked(rec_id)
                or (self._sidecars is not None
                    and self._sidecars.finalized(rec_id) is not None))
            (accepted if ok else rejected).append(rec_id)
        self.state.ack(accepted, _safe_ip(client_ip))
        return _json(200, {"schema": SCHEMA_EXPORT, "accepted": accepted,
                           "rejected": rejected})

    @staticmethod
    def _paging(query: str) -> tuple[Optional[tuple[int, Optional[tuple[int, str]]]],
                                     Optional[ExportResponse]]:
        """((limit, after cursor), None), or (None, the 400 to send)."""
        try:
            qs = urllib.parse.parse_qs(query, keep_blank_values=True,
                                       strict_parsing=bool(query))
        except ValueError:
            return None, _error(400, "bad_query")
        if any(len(v) != 1 for v in qs.values()):
            return None, _error(400, "bad_query")

        limit = DEFAULT_LIMIT
        if "limit" in qs:
            raw = qs["limit"][0]
            if not _LIMIT_RE.fullmatch(raw) or int(raw) < 1:
                return None, _error(400, "bad_limit")
            limit = min(int(raw), MAX_LIMIT)

        after: Optional[tuple[int, str]] = None
        if "after" in qs:
            m = _CURSOR_RE.fullmatch(qs["after"][0])
            if not m:
                return None, _error(400, "bad_cursor")
            after = (int(m.group(1)), m.group(2))
        return (limit, after), None

    def _matches(self, query: str) -> ExportResponse:
        paging, err = self._paging(query)
        if err is not None or paging is None:
            return err or _error(400, "bad_query")
        limit, after = paging

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

    def _unverified(self, query: str) -> ExportResponse:
        """Summary of the matches that are NOT exported because their end was
        never observed (status final, end_state unverified), oldest first.

        Deliberately no player rows and no statistics: the counters of such a
        match are unconfirmed and must not become rewards. ``lastSeenAt`` is the
        last moment the reader saw the match, NOT a proven end time.
        """
        paging, err = self._paging(query)
        if err is not None or paging is None:
            return err or _error(400, "bad_query")
        limit, after = paging
        sql = (
            "SELECT m.match_id, m.server_id, m.started_at, m.ended_at, "
            "m.end_state, m.end_reason, m.map_name, m.layer_name, m.game_mode, "
            "m.peak_players, m.tick_count, "
            "(SELECT COUNT(*) FROM player_matches pm "
            " WHERE pm.match_id = m.match_id) AS player_count, "
            + _training_sql("m") + " AS is_training "
            "FROM matches m WHERE m.status='final' AND m.end_state='unverified' "
            "AND m.server_id=? AND m.ended_at IS NOT NULL")
        args: list[Any] = [self.server_id]
        if after is not None:
            sql += " AND (m.ended_at > ? OR (m.ended_at = ? AND m.match_id > ?))"
            args += [after[0], after[0], after[1]]
        sql += " ORDER BY m.ended_at ASC, m.match_id ASC LIMIT ?"
        args.append(limit)
        conn = self._connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
        items = [{
            "matchId": r["match_id"],
            "serverId": r["server_id"],
            "startedAt": r["started_at"],
            "lastSeenAt": r["ended_at"],
            "endedAt": r["ended_at"],
            "endState": r["end_state"],
            "endReason": r["end_reason"],
            "mapName": r["map_name"],
            "layerName": r["layer_name"],
            "gameMode": r["game_mode"],
            "peakPlayers": r["peak_players"],
            "tickCount": r["tick_count"],
            "playerCount": int(r["player_count"] or 0),
            "isTraining": bool(r["is_training"]),
        } for r in rows]
        next_cursor: Optional[str] = None
        if len(rows) == limit:
            next_cursor = f"{rows[-1]['ended_at']}:{rows[-1]['match_id']}"
        return _json(200, {
            "schema": SCHEMA_EXPORT,
            "serverId": self.server_id,
            "unverifiedMatches": items,
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
                     reader_state: Optional[Callable[[], str]] = None,
                     **extra: Any) -> Optional[ExportApi]:
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
                     container_id=container_id or None, reader_state=reader_state,
                     **extra)


__all__ = ["ExportApi", "ExportResponse", "RecordingStream", "configure_export",
           "canonical_json", "bundle_sha256", "SCHEMA_EXPORT"]
