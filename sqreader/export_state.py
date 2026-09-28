"""Export control state, ack store, audit log and the retention engine.

Everything here works on plain files in the reader's *data directory* (by default
the parent of the recordings directory; config ``export_state_dir`` overrides):

``export_state.json``
    Written ONLY by the reader process (atomic replace). Holds the persistent
    recording switch that ``POST /export/v1/control/recording`` flips
    (``{"recording": {"enabled", "reason", "actor", "changedAt"}}``) and the acks
    of ``POST /export/v1/ack`` (``{"acked": {"<recId>": "<UTC time>"}}``). The
    retention command only READS it.
``export_audit.log``
    Append-only, one JSON object per line: every control change, every ack, and
    every retention deletion of a recording that had NOT been acked. Never holds
    the token.
``retention_state.json``
    Written ONLY by the retention engine: when it last ran and what it removed.
    ``GET /export/v1/health`` reports it.

Nothing here can run a command, take a path from a caller, or change any other
setting: the control endpoint flips one boolean and two labels, and the retention
engine only ever touches ``<id>.sqrx`` / ``<id>.meta.json`` pairs that sit directly
in the recordings directory (never a symlink, never a path taken from a sidecar).

Retention rules (see :func:`plan_retention`)
--------------------------------------------
* NEVER touched: the active recording, anything whose sidecar is not readable,
  anything written to in the last ``in_progress_min`` minutes, and every recording
  that is not ``finalized`` (unverified / legacy ones are removed only when
  ``include_unverified`` is set AND they are older than the separate, longer
  ``unverified_max_age_days``).
* Order: finalized + old enough + acked; then, if the size quota is still exceeded,
  the oldest acked ones; then, only when free space is below the floor (an
  EMERGENCY), the oldest remaining ones -- acked first, un-acked last. Every
  un-acked deletion is logged at WARNING and written to the audit log first; if it
  cannot be audited it is not done.
* When ``require_ack`` is off (the export API is not in use, nobody will ever ack),
  every finalized recording counts as deletable.
* ``.sqrx`` and sidecar go together, crash-safe: the sidecar is first rewritten as
  ``recordingState="deleting"``, then the ``.sqrx`` goes, then the sidecar. A
  half-done pair is finished on the next run.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .recording_lifecycle import (
    RECORDING_STATE_ACTIVE,
    RECORDING_STATE_FINALIZED,
    RECORDING_STATE_UNVERIFIED,
)

log = logging.getLogger("sqreader.retention")

STATE_FILE = "export_state.json"
AUDIT_FILE = "export_audit.log"
RETENTION_STATE_FILE = "retention_state.json"

#: Sidecar state a recording carries while retention is removing it.
RECORDING_STATE_DELETING = "deleting"

MAX_REASON_LEN = 200
MAX_ACTOR_LEN = 100
#: Ids the recorder produces: the .sqrx file stem (same charset as the HTTP API).
REC_ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,200}")
_MAX_SIDECAR_BYTES = 1 << 20
_STATE_VERSION = 1


def utc_iso(when: Optional[datetime] = None) -> str:
    return (when or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def clean_text(value: str, max_len: int) -> str:
    """Control / format characters (newlines, NUL, bidi marks ...) become spaces,
    whitespace is collapsed, the result is cut to ``max_len``."""
    out = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in value)
    return " ".join(out.split())[:max_len]


class ExportStateError(Exception):
    """A state or audit file could not be written. ``kind`` is a stable code."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


# --------------------------------------------------------------------------
# small file helpers
# --------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _read_json_file(path: Path) -> Optional[dict]:
    try:
        if path.stat().st_size > _MAX_SIDECAR_BYTES:
            return None
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def append_audit(data_dir: Path, entry: dict[str, Any],
                 now: Optional[Callable[[], datetime]] = None) -> None:
    """Append one JSON line to ``export_audit.log``. Raises OSError on failure."""
    rec = {"ts": utc_iso(now() if now else None), **entry}
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(data_dir / AUDIT_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# control switch + ack store
# --------------------------------------------------------------------------

class ExportState:
    """The persistent recording switch and the acked recording ids.

    One instance per reader process (thread-safe). Every read of another
    process' data (the retention run) uses ``readonly=True``.
    """

    def __init__(self, data_dir: Path | str, *, now: Optional[Callable[[], datetime]] = None,
                 readonly: bool = False) -> None:
        self.data_dir = Path(data_dir)
        self._path = self.data_dir / STATE_FILE
        self._now = now
        self._lock = threading.RLock()
        self._control: dict[str, Any] = {"enabled": True, "reason": "", "actor": "",
                                         "changedAt": None}
        self._acked: dict[str, str] = {}
        self._load(readonly)

    def _load(self, readonly: bool) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError):
            log.error("export_state.json okunamadı; varsayılan durum kullanılıyor")
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            rec = data.get("recording")
            if isinstance(rec, dict) and isinstance(rec.get("enabled"), bool):
                self._control = {
                    "enabled": rec["enabled"],
                    "reason": str(rec.get("reason") or "")[:MAX_REASON_LEN],
                    "actor": str(rec.get("actor") or "")[:MAX_ACTOR_LEN],
                    "changedAt": rec.get("changedAt") if isinstance(
                        rec.get("changedAt"), str) else None,
                }
            acked = data.get("acked")
            if isinstance(acked, dict):
                self._acked = {k: str(v) for k, v in acked.items()
                               if isinstance(k, str) and REC_ID_RE.fullmatch(k)}
        except ValueError:
            log.error("export_state.json bozuk; varsayılan durum kullanılıyor")
            if not readonly:
                try:
                    os.replace(self._path, self._path.with_name(STATE_FILE + ".bad"))
                except OSError:
                    pass

    def _write(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self._path, {
            "version": _STATE_VERSION,
            "recording": self._control,
            "acked": self._acked,
        })

    # -- recording switch ------------------------------------------------------

    def recording_control(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._control)

    def set_recording(self, enabled: bool, reason: str, actor: str,
                      client_ip: Optional[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Flip the switch; returns ``(old, new)``. Persisted AND audited, or
        neither (a failure restores the previous state and raises)."""
        with self._lock:
            old = dict(self._control)
            new = {"enabled": bool(enabled),
                   "reason": clean_text(reason, MAX_REASON_LEN),
                   "actor": clean_text(actor, MAX_ACTOR_LEN),
                   "changedAt": utc_iso(self._now() if self._now else None)}
            self._control = new
            try:
                self._write()
            except OSError as exc:
                self._control = old
                raise ExportStateError("state_unavailable") from exc
            try:
                append_audit(self.data_dir, {
                    "action": "recording_control",
                    "old": {"enabled": old["enabled"], "reason": old["reason"],
                            "actor": old["actor"]},
                    "new": {"enabled": new["enabled"]},
                    "actor": new["actor"], "reason": new["reason"],
                    "clientIp": client_ip}, self._now)
            except OSError as exc:
                self._control = old
                try:
                    self._write()
                except OSError:
                    pass
                raise ExportStateError("audit_unavailable") from exc
            return old, dict(new)

    # -- acks ------------------------------------------------------------------

    def acked_ids(self) -> set[str]:
        with self._lock:
            return set(self._acked)

    def is_acked(self, rec_id: str) -> bool:
        with self._lock:
            return rec_id in self._acked

    def ack(self, rec_ids: Iterable[str], client_ip: Optional[str]) -> list[str]:
        """Record acks (idempotent). Returns the ids that were NEW."""
        with self._lock:
            stamp = utc_iso(self._now() if self._now else None)
            new = [r for r in dict.fromkeys(rec_ids) if r not in self._acked]
            if not new:
                return []
            before = dict(self._acked)
            for r in new:
                self._acked[r] = stamp
            try:
                self._write()
            except OSError as exc:
                self._acked = before
                raise ExportStateError("state_unavailable") from exc
            try:
                append_audit(self.data_dir, {"action": "ack", "recordings": new,
                                             "clientIp": client_ip}, self._now)
            except OSError as exc:
                self._acked = before
                try:
                    self._write()
                except OSError:
                    pass
                raise ExportStateError("audit_unavailable") from exc
            return new


# --------------------------------------------------------------------------
# retention state file
# --------------------------------------------------------------------------

def read_retention_state(data_dir: Path | str) -> Optional[dict]:
    return _read_json_file(Path(data_dir) / RETENTION_STATE_FILE)


def _write_retention_state(data_dir: Path, result: "RetentionResult",
                           now: datetime) -> None:
    prev = read_retention_state(data_dir) or {}
    state = {
        "lastRunAt": utc_iso(now),
        "lastDeleted": len(result.deleted),
        "lastFreedBytes": result.freed_bytes,
        "lastEmergency": result.emergency,
        "lastUnackedDeleted": result.unacked_deleted,
        "lastUnackedDeletedAt": (utc_iso(now) if result.unacked_deleted
                                 else prev.get("lastUnackedDeletedAt")),
    }
    _atomic_write_json(Path(data_dir) / RETENTION_STATE_FILE, state)


# --------------------------------------------------------------------------
# disk helpers
# --------------------------------------------------------------------------

def free_bytes(path: Path) -> Optional[int]:
    try:
        return int(shutil.disk_usage(path).free)
    except (OSError, ValueError):
        return None


def disk_status(rec_dir: Optional[Path], min_free_bytes: int) -> Optional[dict[str, Any]]:
    """``{recordingsBytes, recordingsCount, freeBytes, totalBytes, minFreeBytes}``
    or None when the recordings directory / the filesystem cannot be read."""
    if rec_dir is None:
        return None
    total_bytes = 0
    count = 0
    try:
        with os.scandir(rec_dir) as it:
            for e in it:
                is_rec = e.name.endswith(".sqrx")
                if not (is_rec or e.name.endswith(".meta.json")):
                    continue
                try:
                    if e.is_symlink() or not e.is_file(follow_symlinks=False):
                        continue
                    total_bytes += e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                if is_rec:
                    count += 1
        usage = shutil.disk_usage(rec_dir)
    except (OSError, ValueError):
        return None
    return {"recordingsBytes": total_bytes, "recordingsCount": count,
            "freeBytes": int(usage.free), "totalBytes": int(usage.total),
            "minFreeBytes": min_free_bytes if min_free_bytes > 0 else None}


def recording_status(state: Optional[ExportState], rec_dir: Optional[Path],
                     min_free_bytes: int) -> tuple[bool, Optional[str]]:
    """``(recordingEnabled, reason)``; reason is None when enabled, else
    ``no_recordings_dir`` | ``disabled_by_admin`` | ``disk_low``."""
    if rec_dir is None:
        return False, "no_recordings_dir"
    if state is not None and not state.recording_control()["enabled"]:
        return False, "disabled_by_admin"
    if min_free_bytes > 0:
        free = free_bytes(rec_dir)
        if free is not None and free < min_free_bytes:
            return False, "disk_low"
    return True, None


# --------------------------------------------------------------------------
# retention engine
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RetentionPolicy:
    max_age_days: float = 0.0            # 0 = off
    max_total_bytes: int = 0             # 0 = off
    min_free_bytes: int = 0              # 0 = off; below this = EMERGENCY
    in_progress_min: float = 60.0
    min_keep: int = 0                    # newest N finalized are safe from the free-space policy
    require_ack: bool = False
    include_unverified: bool = False
    unverified_max_age_days: float = 0.0


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value) if value > 0 else 0.0


def policy_from_config(get: Callable[[str], Any]) -> RetentionPolicy:
    """The retention settings of the config (defaults live in config.DEFAULTS).
    ``retention_require_ack`` unset = on exactly when the export API is."""
    require = get("retention_require_ack")
    if not isinstance(require, bool):
        require = bool(get("export_token_file"))
    gib = 1024 ** 3
    keep = get("retention_min_keep")
    return RetentionPolicy(
        max_age_days=_num(get("retention_max_age_days")),
        max_total_bytes=int(_num(get("retention_max_total_gb")) * gib),
        min_free_bytes=int(_num(get("retention_min_free_gb")) * gib),
        in_progress_min=_num(get("retention_in_progress_min"), 60.0),
        min_keep=int(keep) if isinstance(keep, int) and not isinstance(keep, bool)
        and keep > 0 else 0,
        require_ack=require,
        include_unverified=bool(get("retention_include_unverified")),
        unverified_max_age_days=_num(get("retention_unverified_max_age_days")),
    )


def resolve_state_dir(explicit: Any, recordings_dir: Optional[Path],
                      stats_db: Optional[Path]) -> Optional[Path]:
    if explicit:
        return Path(str(explicit)).expanduser()
    if recordings_dir is not None:
        return Path(recordings_dir).parent
    if stats_db is not None:
        return Path(stats_db).parent
    return None


@dataclass
class _Rec:
    rec_id: str
    sqrx: Path
    sidecar: Path
    size: int
    mtime: float
    klass: str                   # finalized | active | unverified | legacy | deleting | unreadable
    meta: Optional[dict] = None


@dataclass
class Victim:
    rec_id: str
    size: int
    reason: str                  # age | unverified_age | quota | free_space | repair
    detail: str                  # Turkish, for humans
    acked: bool
    unacked_emergency: bool = False


@dataclass
class RetentionPlan:
    victims: list[Victim] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    emergency: bool = False
    _recs: dict[str, _Rec] = field(default_factory=dict, repr=False)


@dataclass
class RetentionResult:
    dry_run: bool
    deleted: list[Victim] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    unacked_deleted: int = 0
    emergency: bool = False
    errors: list[str] = field(default_factory=list)


def _state_of(meta: dict) -> str:
    state = meta.get("recordingState")
    if state == RECORDING_STATE_FINALIZED:
        return "finalized" if meta.get("inProgress") is False else "unverified"
    if state == RECORDING_STATE_ACTIVE:
        return "active"
    if state == RECORDING_STATE_UNVERIFIED:
        return "unverified"
    if state == RECORDING_STATE_DELETING:
        return "deleting"
    return "legacy"


def _scan(rec_dir: Path) -> tuple[list[_Rec], list[str]]:
    """Recordings that sit DIRECTLY in ``rec_dir`` (regular files only, no
    symlinks, ids of the recorder's charset), plus leftover sidecar ids of a
    half-finished deletion. Raises OSError when the directory is unreadable."""
    root = rec_dir.resolve()
    recs: list[_Rec] = []
    sqrx_ids: set[str] = set()
    sidecar_names: list[str] = []
    with os.scandir(rec_dir) as it:
        entries = sorted(it, key=lambda e: e.name)
    for e in entries:
        if e.name.endswith(".meta.json"):
            sidecar_names.append(e.name)
            continue
        if not e.name.endswith(".sqrx"):
            continue
        stem = e.name[:-len(".sqrx")]
        if not REC_ID_RE.fullmatch(stem):
            continue
        try:
            if e.is_symlink() or not e.is_file(follow_symlinks=False):
                continue
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        path = root / e.name
        if path.resolve().parent != root:          # junction / odd mount: skip
            continue
        sqrx_ids.add(stem)
        sidecar = root / f"{stem}.meta.json"
        meta: Optional[dict] = None
        try:
            if not sidecar.is_symlink() and sidecar.is_file():
                meta = _read_json_file(sidecar)
        except OSError:
            meta = None
        klass = "unreadable" if meta is None else _state_of(meta)
        recs.append(_Rec(stem, path, sidecar, st.st_size, st.st_mtime, klass, meta))
    repairs: list[str] = []
    for name in sidecar_names:
        stem = name[:-len(".meta.json")]
        if not REC_ID_RE.fullmatch(stem) or stem in sqrx_ids:
            continue
        side = root / name
        try:
            if side.is_symlink() or not side.is_file():
                continue
        except OSError:
            continue
        meta = _read_json_file(side)
        if meta is not None and meta.get("recordingState") == RECORDING_STATE_DELETING:
            repairs.append(stem)               # .sqrx already gone; sidecar is left
    for r in recs:
        if r.klass == "deleting":              # sidecar says deleting, .sqrx still here
            repairs.append(r.rec_id)
    return recs, sorted(set(repairs))


def _gb(n: int) -> str:
    return f"{n / 1024 ** 3:.1f}"


def plan_retention(rec_dir: Path, policy: RetentionPolicy, *, acked: set[str],
                   now: float, free: Optional[int]) -> RetentionPlan:
    """Decide what would be removed and why. Touches nothing."""
    recs, repairs = _scan(rec_dir)
    plan = RetentionPlan(repairs=repairs, _recs={r.rec_id: r for r in recs})
    guard_cut = now - policy.in_progress_min * 60
    finals = sorted((r for r in recs if r.klass == "finalized"),
                    key=lambda r: (r.mtime, r.rec_id))
    elig = [r for r in finals if r.mtime <= guard_cut]
    chosen: dict[str, Victim] = {}

    def normally_deletable(r: _Rec) -> bool:
        return (not policy.require_ack) or r.rec_id in acked

    def mark(r: _Rec, reason: str, detail: str, *, unacked: bool = False) -> None:
        if r.rec_id not in chosen and r.rec_id not in repairs:
            chosen[r.rec_id] = Victim(r.rec_id, r.size, reason, detail,
                                      r.rec_id in acked, unacked)

    # 1. too old (finalized, and acked when acks are in use)
    if policy.max_age_days > 0:
        cut = now - policy.max_age_days * 86400
        for r in elig:
            if r.mtime < cut and normally_deletable(r):
                age = (now - r.mtime) / 86400
                mark(r, "age", f"yaş sınırı aşıldı ({age:.0f} gün > "
                     f"{policy.max_age_days:g})" + (", onaylı" if policy.require_ack else ""))
    # 1b. unverified / legacy: only on explicit request, own (longer) age limit
    if policy.include_unverified and policy.unverified_max_age_days > 0:
        cut_u = now - policy.unverified_max_age_days * 86400
        for r in recs:
            if r.klass in ("unverified", "legacy") and r.mtime < cut_u \
                    and r.mtime <= guard_cut:
                age = (now - r.mtime) / 86400
                mark(r, "unverified_age",
                     f"doğrulanmamış kayıt yaş sınırını aştı ({age:.0f} gün > "
                     f"{policy.unverified_max_age_days:g})")
    # 2. size quota, oldest first
    if policy.max_total_bytes > 0:
        total = sum(r.size for r in recs if r.rec_id not in chosen)
        for r in elig:
            if total <= policy.max_total_bytes:
                break
            if r.rec_id not in chosen and normally_deletable(r):
                mark(r, "quota", f"toplam boyut kotası aşıldı (kota "
                     f"{_gb(policy.max_total_bytes)} GB)"
                     + (", onaylı" if policy.require_ack else ""))
                total -= r.size
        if total > policy.max_total_bytes:
            plan.notes.append(
                "kota hâlâ aşık ama silinebilir onaylı kayıt kalmadı; onaysız kayıtlara "
                "dokunulmadı")
    # 3. free space floor (emergency): oldest first, acked before un-acked
    if policy.min_free_bytes > 0 and free is not None:
        plan.emergency = free < policy.min_free_bytes
        freed = sum(v.size for v in chosen.values())
        if free + freed < policy.min_free_bytes:
            keepers = ({r.rec_id for r in finals[-policy.min_keep:]}
                       if policy.min_keep else set())
            pool = [r for r in elig if r.rec_id not in chosen and r.rec_id not in keepers]
            pool_ok = [r for r in pool if normally_deletable(r)]
            pool_un = [r for r in pool if not normally_deletable(r)]
            deletable = sum(r.size for r in pool_ok + pool_un)
            if free + freed + deletable < policy.min_free_bytes:
                plan.notes.append(
                    f"cannot reach {_gb(policy.min_free_bytes)} GB free by pruning "
                    f"recordings (only {_gb(deletable)} GB here, {_gb(free)} GB free) "
                    f"— something else is filling the disk; not deleting on this policy")
            else:
                for r in pool_ok + pool_un:
                    if free + freed >= policy.min_free_bytes:
                        break
                    un = r.rec_id not in acked and policy.require_ack
                    mark(r, "free_space",
                         "disk ACİL durumu: boş alan alt sınırın altında"
                         + (" (onaysız kayıt silinir)" if un else ""), unacked=un)
                    freed += r.size
    plan.victims = list(chosen.values())
    return plan


def _atomic_sidecar(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _unlink_quiet(path: Path) -> None:
    try:
        if path.is_symlink():
            return
        path.unlink()
    except FileNotFoundError:
        pass


def _remove_pair(rec: _Rec, reason: str) -> None:
    """Crash-safe removal of one recording (see the module docstring)."""
    meta = dict(rec.meta or {})
    meta["recordingState"] = RECORDING_STATE_DELETING
    meta["deleteReason"] = reason
    _atomic_sidecar(rec.sidecar, meta)
    _unlink_quiet(rec.sqrx)
    _unlink_quiet(rec.sidecar)


def _finish_repair(root: Path, rec_id: str) -> None:
    sqrx, side = root / f"{rec_id}.sqrx", root / f"{rec_id}.meta.json"
    meta = _read_json_file(side) if side.is_file() and not side.is_symlink() else None
    if meta is None or meta.get("recordingState") != RECORDING_STATE_DELETING:
        return
    _unlink_quiet(sqrx)
    _unlink_quiet(side)


def run_retention(rec_dir: Path | str, policy: RetentionPolicy, *,
                  data_dir: Optional[Path | str] = None, dry_run: bool = False,
                  acked: Optional[set[str]] = None,
                  now: Optional[float] = None,
                  free: Optional[int] = None) -> RetentionResult:
    """Plan and (unless ``dry_run``) execute one retention pass.

    ``data_dir`` holds the state / audit files (None = recordings dir's parent);
    ``acked`` defaults to the ack store in it. Raises OSError when the recordings
    directory cannot be read.
    """
    rec_dir = Path(rec_dir).expanduser()
    root = rec_dir.resolve()
    state_dir = Path(data_dir) if data_dir is not None else root.parent
    if acked is None:
        acked = ExportState(state_dir, readonly=True).acked_ids()
    clock = now if now is not None else time.time()
    if free is None and policy.min_free_bytes > 0:
        free = free_bytes(rec_dir)
    plan = plan_retention(rec_dir, policy, acked=acked, now=clock, free=free)
    result = RetentionResult(dry_run=dry_run, notes=list(plan.notes),
                             emergency=plan.emergency)

    for rec_id in plan.repairs:
        if not dry_run:
            try:
                _finish_repair(root, rec_id)
            except OSError as exc:
                result.errors.append(f"{rec_id}: onarılamadı: {exc}")
                continue
        result.repaired.append(rec_id)

    for v in plan.victims:
        rec = plan._recs[v.rec_id]
        if dry_run:
            result.deleted.append(v)
            result.freed_bytes += v.size
            if v.unacked_emergency:
                result.unacked_deleted += 1
            continue
        # Re-read right before acting: the sidecar must still say what the plan
        # assumed, and the file must still be a plain file in this directory.
        try:
            fresh = (_read_json_file(rec.sidecar)
                     if rec.sidecar.is_file() and not rec.sidecar.is_symlink() else None)
            expected = (("unverified", "legacy") if v.reason == "unverified_age"
                        else ("finalized",))
            if (fresh is None or rec.sqrx.is_symlink() or not rec.sqrx.is_file()
                    or rec.sqrx.resolve().parent != root
                    or _state_of(fresh) not in expected):
                result.skipped.append(v.rec_id)
                continue
            rec.meta = fresh
        except OSError:
            result.skipped.append(v.rec_id)
            continue
        if v.unacked_emergency:
            msg = (f"disk acil durumu: onaysız kayıt siliniyor: {v.rec_id} "
                   f"({v.size} bayt, boş alan {free})")
            log.warning(msg)
            try:
                append_audit(state_dir, {
                    "action": "retention_delete_unacked", "recording": v.rec_id,
                    "sizeBytes": v.size, "freeBytes": free,
                    "minFreeBytes": policy.min_free_bytes, "reason": v.reason})
            except OSError as exc:
                log.error("denetim günlüğüne yazılamadı; %s silinmedi: %r", v.rec_id, exc)
                result.errors.append(f"{v.rec_id}: denetim günlüğü yazılamadı, silinmedi")
                continue
        try:
            _remove_pair(rec, v.reason)
        except OSError as exc:
            result.errors.append(f"{v.rec_id}: silinemedi: {exc}")
            continue
        result.deleted.append(v)
        result.freed_bytes += v.size
        if v.unacked_emergency:
            result.unacked_deleted += 1

    if not dry_run:
        try:
            _write_retention_state(state_dir, result, datetime.now(timezone.utc))
        except OSError as exc:
            result.errors.append(f"retention_state.json yazılamadı: {exc}")
        if result.deleted:
            try:
                append_audit(state_dir, {
                    "action": "retention_run", "deleted": len(result.deleted),
                    "freedBytes": result.freed_bytes,
                    "unackedDeleted": result.unacked_deleted,
                    "emergency": result.emergency})
            except OSError:
                pass
    return result


# --------------------------------------------------------------------------
# the recorder's gate
# --------------------------------------------------------------------------

class RecordingGate:
    """Called by the recorder right before it would OPEN a new ``.sqrx``.

    Returns None (go ahead) or a short reason (``disabled_by_admin`` /
    ``disk_low``). It is never consulted for a recording that is already open, so
    switching recording off cannot cut a match in half, and it never affects the
    stats database. When free space is below the floor it first runs one
    retention pass (at most once per ``min_interval`` seconds); only if that
    frees nothing does it refuse.
    """

    def __init__(self, state: Optional[ExportState], recordings_dir: Path,
                 policy: RetentionPolicy, *, data_dir: Optional[Path] = None,
                 min_interval: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._state = state
        self._dir = Path(recordings_dir)
        self._policy = policy
        self._data_dir = data_dir
        self._min_interval = min_interval
        self._clock = clock
        self._last_pass: Optional[float] = None

    def __call__(self) -> Optional[str]:
        if self._state is not None and not self._state.recording_control()["enabled"]:
            return "disabled_by_admin"
        need = self._policy.min_free_bytes
        if need <= 0:
            return None
        free = free_bytes(self._dir)
        if free is None or free >= need:
            return None
        now = self._clock()
        if self._last_pass is None or now - self._last_pass >= self._min_interval:
            self._last_pass = now
            try:
                run_retention(
                    self._dir, self._policy, data_dir=self._data_dir,
                    acked=self._state.acked_ids() if self._state else None)
            except Exception as exc:  # noqa: BLE001 - the gate must never raise
                log.warning("acil retention geçişi başarısız: %r", exc)
            free = free_bytes(self._dir)
            if free is None or free >= need:
                return None
        return "disk_low"


__all__ = [
    "ExportState", "ExportStateError", "RetentionPolicy", "RetentionPlan",
    "RetentionResult", "RecordingGate", "append_audit", "clean_text", "disk_status",
    "free_bytes", "plan_retention", "policy_from_config", "read_retention_state",
    "recording_status", "resolve_state_dir", "run_retention", "REC_ID_RE",
    "STATE_FILE", "AUDIT_FILE", "RETENTION_STATE_FILE", "MAX_REASON_LEN",
    "MAX_ACTOR_LEN",
]
