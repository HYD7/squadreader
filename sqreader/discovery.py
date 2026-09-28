"""Finding the game server process safely, and knowing it is still the same one.

A reader attaches to ONE game server. On a host that runs several (two
Pterodactyl/Docker servers on ports 7787 and 7788 were observed) picking "the
first SquadGameServer" is a coin toss, and a wrong pick is quiet and expensive:
every match it records lands under another server's name. So this module never
chooses between equally good candidates. Zero matches and several matches are
both errors, and the error says what was searched for and why each candidate
was dropped.

What identifies a process
-------------------------
``(pid, start_time_ticks)``. A PID is only a slot number; the kernel hands it to
the next process once the old one dies, so a PID alone cannot tell "the server I
was reading" from "something that inherited its number". Field 22 of
``/proc/<pid>/stat`` (boot-relative start time in clock ticks) is what makes the
pair unique for the lifetime of the boot. NEVER pin a PID in a unit file or a
config: it is transient by nature. Pin the port (``squad_port`` /
``squad_query_port``) and, if you want a second check, the container id.

How a candidate is recognised
-----------------------------
* ``/proc/<pid>/exe`` basename, or (when the exe link does not exist at all)
  ``cmdline[0]`` basename, equals a wanted binary name. An exe link that exists
  but points elsewhere is a launcher (``bash start.sh ... SquadGameServer``,
  ``tini``, ``wings``): it is dropped even when its argv[0] says otherwise,
  because ``exec -a`` can put any name there.
* An exe link that cannot be read (``PermissionError``) is NOT trusted and is
  reported: a reader that cannot identify its target has no business attaching
  to it, and the operator should be told it lacks the privilege, not that
  "no server is running".

Ports
-----
Squad's game port is ``-Port=`` and its query port ``-QueryPort=``; both also
appear inside UE-style URL arguments (``Map?Port=7787?QueryPort=27165``). They
are matched as WHOLE numbers per key, case-insensitively: ``Port=7787`` is not
``Port=77870`` and is not ``QueryPort=7787``. ``game_port`` filters on ``Port``
and ``query_port`` on ``QueryPort``; the result says which key matched
(``matched_port_kind``). It is not verified on the target hosts which key the
operator's 7787/7788 are, so both are supported and reported rather than assumed.

Containers
----------
The 64-hex container id is read from ``/proc/<pid>/cgroup`` (cgroup v1
``/docker/<id>``, v2 ``docker-<id>.scope``, containerd ``cri-containerd-<id>``,
libpod, kubepods...). An unrecognised layout gives ``None``, never a guess. When
the container is re-created its id changes: ``expected_container_id`` must then
be updated by the operator, and until then the new process is refused loudly.

Everything takes the ``/proc`` root as a parameter, so tests can run against a
fake tree; those tests do not prove anything about a real host's memory or
cgroups.
"""
from __future__ import annotations

import errno
import glob as _glob
import os
import posixpath
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

#: Binary names the dedicated server runs as. The first is the one Squad ships
#: (``SquadGame/Binaries/Linux/SquadGameServer``); the second is Unreal's
#: default name for a shipping build with modules and is only ever an
#: additional candidate (it can cause an ambiguity error, never a wrong pick).
DEFAULT_EXE_NAMES: tuple[str, ...] = ("SquadGameServer",
                                      "SquadGameServer-Linux-Shipping")

#: Where the game writes its log INSIDE a Pterodactyl container.
DEFAULT_LOG_PATH_IN_CONTAINER = "/home/container/SquadGame/Saved/Logs/SquadGame.log"

#: Below this many hex characters a container id prefix is too short to trust.
MIN_CONTAINER_PREFIX = 12

KIND_GAME = "Port"
KIND_QUERY = "QueryPort"


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class DiscoveryError(Exception):
    """Base class: every failure here is an operator-facing, Turkish message."""


class DiscoveryConfigError(DiscoveryError):
    """The criteria themselves are unusable (e.g. a 3-character container id)."""


class ProcUnavailable(DiscoveryError):
    """There is no /proc to look at (Windows, or a stripped container)."""


class GameProcessNotFound(DiscoveryError):
    """No process satisfied the criteria."""

    def __init__(self, message: str, report: "ScanReport | None" = None):
        super().__init__(message)
        self.report = report


class ContainerMismatch(GameProcessNotFound):
    """Game processes exist and match the ports, but not the expected container.

    Kept apart from a plain "not found" because the remedy differs: the
    container was re-created (its id changed) and the operator must update
    ``expected_container_id``. The reader waits and says so; it does not attach.
    """


class AmbiguousGameProcess(DiscoveryError):
    """Two or more processes satisfied the criteria. Nothing was chosen."""

    def __init__(self, message: str, candidates: "Sequence[GameProcess]",
                 report: "ScanReport | None" = None):
        super().__init__(message)
        self.candidates = tuple(candidates)
        self.report = report


class AmbiguousLogPath(DiscoveryError):
    """The log-file glob fallback matched more than one file."""

    def __init__(self, message: str, matches: Sequence[str]):
        super().__init__(message)
        self.matches = tuple(matches)


class ServerIdMismatch(DiscoveryError):
    """The data directory already belongs to a different server_id."""


class TargetLost(Exception):
    """Raised by a running reader session when its game process is gone or was
    replaced (different start time). Not a DiscoveryError: it is control flow."""


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GameProcess:
    """A discovered game server process. Identity is ``(pid, start_time_ticks)``."""
    pid: int
    start_time_ticks: int
    cmdline: tuple[str, ...]
    exe: str | None
    matched_port_kind: str | None      # "Port" | "QueryPort" | "Port+QueryPort" | None
    game_port: int | None
    query_port: int | None
    container_id: str | None
    root_path: str
    log_path: str | None
    server_uuid: str | None = None     # Pterodactyl server UUID, only if it is in the cgroup path
    proc_root: str = "/proc"

    @property
    def identity(self) -> tuple[int, int]:
        return (self.pid, self.start_time_ticks)

    def describe(self) -> str:
        ports = []
        if self.game_port is not None:
            ports.append(f"Port={self.game_port}")
        if self.query_port is not None:
            ports.append(f"QueryPort={self.query_port}")
        return (f"PID {self.pid}, portlar: {' '.join(ports) or 'yok'}, "
                f"konteyner: {self.container_id[:12] if self.container_id else 'bilinmiyor'}")


@dataclass
class ScanReport:
    """What a scan looked at. Carried by the errors and by the doctor output."""
    criteria: str = ""
    scanned: int = 0
    candidates: int = 0
    #: (pid, reason) for every candidate that was dropped.
    eliminated: list[tuple[int, str]] = field(default_factory=list)
    #: launcher / wrapper processes that name the binary but are not it.
    launchers: int = 0
    #: pids whose files could not be read for lack of privilege.
    permission_denied: list[int] = field(default_factory=list)
    #: processes that vanished between listing and reading (normal churn).
    vanished: int = 0
    #: pids that failed with some other OS error.
    read_errors: list[int] = field(default_factory=list)
    #: True when at least one candidate was dropped ONLY for its container id.
    container_mismatch: bool = False
    #: (pid, ports, container) of candidates dropped for the container id.
    container_mismatch_seen: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"ölçüt: {self.criteria}",
                 f"taranan süreç: {self.scanned}",
                 f"aday: {self.candidates}"]
        if self.eliminated:
            parts.append("elenen adaylar: " + "; ".join(
                f"PID {p}: {why}" for p, why in self.eliminated))
        if self.launchers:
            parts.append(f"{self.launchers} süreç başlatıcı/sarmalayıcı olarak "
                         f"elendi (komut satırında ikili dosya adı geçiyor ama "
                         f"kendisi ikili dosya değil)")
        if self.permission_denied:
            parts.append(f"{len(self.permission_denied)} süreç izin nedeniyle "
                         f"okunamadı (PID: {_pid_list(self.permission_denied)}); "
                         f"reader'ı bu süreçlere erişebilen bir kullanıcıyla "
                         f"(örn. root) çalıştırın")
        if self.read_errors:
            parts.append(f"{len(self.read_errors)} süreç okuma hatasıyla "
                         f"atlandı (PID: {_pid_list(self.read_errors)})")
        return ". ".join(parts)


def _pid_list(pids: Iterable[int], limit: int = 10) -> str:
    items = list(pids)
    text = ", ".join(str(p) for p in items[:limit])
    return text + (f", ... (+{len(items) - limit})" if len(items) > limit else "")


# --------------------------------------------------------------------------
# /proc access (injectable)
# --------------------------------------------------------------------------

class ProcFS:
    """Thin reader over a /proc-shaped tree. Subclass or replace in tests to
    simulate permission errors or an exe link without touching the real OS."""

    def __init__(self, root: "str | os.PathLike[str]" = "/proc") -> None:
        self.root = str(root)

    def exists(self) -> bool:
        return os.path.isdir(self.root)

    def list_pids(self) -> list[int]:
        try:
            names = os.listdir(self.root)
        except FileNotFoundError as e:
            raise ProcUnavailable(
                f"çalıştırılmadı: Linux host gerekli ({self.root} bulunamadı)") from e
        return sorted(int(n) for n in names if n.isdigit())

    def read_bytes(self, pid: int, name: str) -> bytes:
        with open(os.path.join(self.root, str(pid), name), "rb") as f:
            return f.read()

    def readlink(self, pid: int, name: str) -> str:
        return os.readlink(os.path.join(self.root, str(pid), name))

    def process_root(self, pid: int) -> str:
        return os.path.join(self.root, str(pid), "root")


def _is_vanished(exc: BaseException) -> bool:
    return isinstance(exc, (FileNotFoundError, ProcessLookupError,
                            NotADirectoryError))


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------

_PORT_RE = re.compile(r"^(port|queryport)=(\d{1,5})$", re.IGNORECASE)


def split_cmdline(raw: bytes | str) -> list[str]:
    """``/proc/<pid>/cmdline`` -> argv. NUL-separated with a trailing NUL."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    args = text.split("\0")
    if args and args[-1] == "":
        args.pop()
    return args


def parse_ports(args: Iterable[str]) -> dict[str, list[int]]:
    """Every ``Port=`` / ``QueryPort=`` value in a command line.

    Handles ``-Port=7787``, ``/Port=7787``, ``Port=7787`` and UE URL arguments
    that chain options with ``?``. The value must be a WHOLE argument piece: the
    match is anchored at both ends, so ``Port=77870`` is 77870 and
    ``BeaconPort=7787`` is not a port at all. Returns ``{"Port": [...],
    "QueryPort": [...]}`` in order of appearance.
    """
    out: dict[str, list[int]] = {KIND_GAME: [], KIND_QUERY: []}
    for arg in args:
        for piece in arg.split("?"):
            piece = piece.strip().strip("\"'").lstrip("-/")
            m = _PORT_RE.match(piece)
            if not m:
                continue
            kind = KIND_GAME if m.group(1).lower() == "port" else KIND_QUERY
            out[kind].append(int(m.group(2)))
    return out


def effective_port(values: Sequence[int]) -> tuple[int | None, bool]:
    """(port, conflicting). Several DIFFERENT values for one key is a conflict:
    which one the game honours is not something to guess."""
    distinct = sorted(set(values))
    if not distinct:
        return None, False
    if len(distinct) > 1:
        return None, True
    return distinct[0], False


def parse_stat_start_time(data: bytes | str) -> int:
    """Start time (clock ticks since boot) from ``/proc/<pid>/stat``.

    Field 2 is the command name in parentheses and may itself contain spaces
    and parentheses (``(Main (x) Thread)``), so the fields after it are counted
    from the LAST ``)``. ``starttime`` is field 22; after the comm the first
    remaining field is field 3 (state), which puts it at index 19.
    """
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    rp = text.rfind(")")
    if rp < 0:
        raise ValueError("stat: kapanış parantezi yok")
    rest = text[rp + 1:].split()
    if len(rest) < 20:
        raise ValueError("stat: alan sayısı yetersiz")
    return int(rest[19])


_HEX64_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{64})(?![0-9a-fA-F])")
_UUID_RE = re.compile(
    r"(?<![0-9a-fA-F])([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?![0-9a-fA-F])")
#: A 64-hex token is only a container id when the path says it is a container
#: runtime's; a bare hash in some other cgroup path is not.
_RUNTIME_MARKERS = ("docker", "containerd", "crio", "cri-o", "libpod",
                    "podman", "kubepods", "pterodactyl")


def _cgroup_paths(text: str) -> list[str]:
    paths = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":", 2)          # v1 "id:ctrl:path", v2 "0::path"
        paths.append(parts[2] if len(parts) == 3 else line)
    return paths


def parse_container_id(cgroup_text: str) -> str | None:
    """Docker/containerd container id (lower-case 64 hex) or None.

    Recognised: v1 ``/docker/<id>[/...]``, v2 ``0::/docker/<id>``,
    ``/system.slice/docker-<id>.scope``, ``cri-containerd-<id>.scope``,
    ``libpod-<id>.scope``, ``/kubepods/.../<id>``. Nested ids take the innermost
    (last). Lines that disagree, or a layout without a runtime marker, give
    None: an unrecognised format is reported as unknown, never guessed.
    """
    found: set[str] = set()
    for path in _cgroup_paths(cgroup_text):
        low = path.lower()
        if not any(m in low for m in _RUNTIME_MARKERS):
            continue
        ids = _HEX64_RE.findall(path)
        if ids:
            found.add(ids[-1].lower())
    return next(iter(found)) if len(found) == 1 else None


def parse_server_uuid(cgroup_text: str) -> str | None:
    """Pterodactyl server UUID, only when it literally appears in a container
    cgroup path (Docker names Pterodactyl containers by it). Never invented."""
    found: set[str] = set()
    for path in _cgroup_paths(cgroup_text):
        low = path.lower()
        if not any(m in low for m in _RUNTIME_MARKERS):
            continue
        found.update(u.lower() for u in _UUID_RE.findall(path))
    return next(iter(found)) if len(found) == 1 else None


def normalize_expected_container_id(value: Any) -> str | None:
    """Config value -> lower-case hex, 12..64 chars, or None when unset."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    for prefix in ("docker://", "containerd://", "cri-o://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if not re.fullmatch(r"[0-9a-f]+", text):
        raise DiscoveryConfigError(
            "expected_container_id yalnız onaltılık (hex) karakter içermeli")
    if not MIN_CONTAINER_PREFIX <= len(text) <= 64:
        raise DiscoveryConfigError(
            f"expected_container_id en az {MIN_CONTAINER_PREFIX}, en çok 64 "
            f"karakter olmalı (verilen: {len(text)})")
    return text


def container_id_matches(expected: str, actual: str | None) -> bool:
    """Full match, or ``expected`` (>= 12 chars) is a prefix of ``actual``."""
    if not actual:
        return False
    e = expected.strip().lower()
    a = actual.strip().lower()
    return a == e or (len(e) >= MIN_CONTAINER_PREFIX and a.startswith(e))


def _strip_deleted(path: str) -> str:
    return path[:-len(" (deleted)")] if path.endswith(" (deleted)") else path


def _basename(path: str) -> str:
    return posixpath.basename(path.replace("\\", "/"))


def build_log_path(root_path: str, log_path_in_container: str | None) -> str:
    """``<root_path>/<relative log path>`` for the process's own filesystem."""
    rel = (log_path_in_container or DEFAULT_LOG_PATH_IN_CONTAINER).lstrip("/\\")
    return str(Path(root_path) / Path(*rel.replace("\\", "/").split("/")))


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Criteria:
    exe_names: tuple[str, ...]
    game_port: int | None
    query_port: int | None
    expected_container_id: str | None
    expected_exe_path_suffix: str | None
    log_path_in_container: str | None

    def describe(self) -> str:
        bits = [f"ikili dosya: {'/'.join(self.exe_names)}"]
        if self.game_port is not None:
            bits.append(f"Port={self.game_port}")
        if self.query_port is not None:
            bits.append(f"QueryPort={self.query_port}")
        if self.expected_container_id:
            bits.append(f"konteyner={self.expected_container_id}")
        if self.expected_exe_path_suffix:
            bits.append(f"exe yolu sonu={self.expected_exe_path_suffix}")
        if self.game_port is None and self.query_port is None:
            bits.append("port ölçütü yok")
        return ", ".join(bits)


def _make_criteria(exe_names: Sequence[str], game_port: Any, query_port: Any,
                   expected_container_id: Any, expected_exe_path_suffix: Any,
                   log_path_in_container: Any) -> _Criteria:
    if isinstance(exe_names, str):
        exe_names = (exe_names,)
    names = tuple(n for n in (str(x).strip() for x in exe_names) if n)
    if not names:
        raise DiscoveryConfigError("en az bir ikili dosya adı gerekli")

    def _port(v: Any, label: str) -> int | None:
        if v in (None, ""):
            return None
        try:
            n = int(v)
        except (TypeError, ValueError) as e:
            raise DiscoveryConfigError(f"{label} bir tam sayı olmalı: {v!r}") from e
        if not 1 <= n <= 65535:
            raise DiscoveryConfigError(f"{label} 1-65535 aralığında olmalı: {n}")
        return n

    suffix = str(expected_exe_path_suffix).strip() if expected_exe_path_suffix else None
    return _Criteria(
        exe_names=names,
        game_port=_port(game_port, "squad_port"),
        query_port=_port(query_port, "squad_query_port"),
        expected_container_id=normalize_expected_container_id(expected_container_id),
        expected_exe_path_suffix=suffix or None,
        log_path_in_container=(str(log_path_in_container)
                               if log_path_in_container else None))


def _inspect(fs: ProcFS, pid: int, crit: _Criteria, report: ScanReport,
             matches: list[GameProcess]) -> None:
    report.scanned += 1
    # 1. cmdline -----------------------------------------------------------
    try:
        args = split_cmdline(fs.read_bytes(pid, "cmdline"))
    except PermissionError:
        report.permission_denied.append(pid)
        return
    except OSError as e:
        if _is_vanished(e):
            report.vanished += 1
        else:
            report.read_errors.append(pid)
        return
    if not args:                      # kernel thread / zombie: nothing to run
        return

    # 2. exe link ----------------------------------------------------------
    exe: str | None = None
    exe_denied = False
    exe_absent = False
    try:
        exe = _strip_deleted(fs.readlink(pid, "exe"))
    except PermissionError:
        exe_denied = True
        report.permission_denied.append(pid)
    except OSError as e:
        if _is_vanished(e) or getattr(e, "errno", None) == errno.EINVAL:
            exe_absent = True
        else:
            report.read_errors.append(pid)
            return

    argv0 = args[0]
    if exe is not None:
        is_binary = _basename(exe) in crit.exe_names
    elif exe_absent:
        is_binary = _basename(argv0) in crit.exe_names
    else:
        is_binary = False              # exe unreadable: not trusted (see module doc)

    mentions_binary = any(n in a for a in args for n in crit.exe_names)
    if not is_binary:
        if exe_denied:
            # Can't tell; only count it as a would-be candidate when it looks
            # like one, so the operator learns why nothing matched.
            if _basename(argv0) in crit.exe_names:
                report.candidates += 1
                report.eliminated.append(
                    (pid, "exe bağlantısı izin nedeniyle okunamadı, kimlik doğrulanamadı"))
        elif mentions_binary:
            report.launchers += 1
        return

    report.candidates += 1

    def drop(reason: str) -> None:
        report.eliminated.append((pid, reason))

    # 3. exe path suffix ---------------------------------------------------
    if crit.expected_exe_path_suffix:
        path = exe if exe is not None else argv0
        if not path.replace("\\", "/").endswith(crit.expected_exe_path_suffix):
            drop(f"exe yolu {path!r} beklenen sonla ({crit.expected_exe_path_suffix}) bitmiyor")
            return

    # 4. ports -------------------------------------------------------------
    ports = parse_ports(args[1:])
    game, game_conflict = effective_port(ports[KIND_GAME])
    query, query_conflict = effective_port(ports[KIND_QUERY])
    if game_conflict or query_conflict:
        drop("komut satırında çelişkili Port/QueryPort değerleri var "
             f"(Port={ports[KIND_GAME]}, QueryPort={ports[KIND_QUERY]})")
        return
    if crit.game_port is not None and game != crit.game_port:
        drop(f"Port={game if game is not None else 'yok'}"
             f" (aranan Port={crit.game_port})"
             + (f"; QueryPort={query}" if query is not None else ""))
        return
    if crit.query_port is not None and query != crit.query_port:
        drop(f"QueryPort={query if query is not None else 'yok'}"
             f" (aranan QueryPort={crit.query_port})"
             + (f"; Port={game}" if game is not None else ""))
        return
    if crit.game_port is not None and crit.query_port is not None:
        kind: str | None = f"{KIND_GAME}+{KIND_QUERY}"
    elif crit.game_port is not None:
        kind = KIND_GAME
    elif crit.query_port is not None:
        kind = KIND_QUERY
    else:
        kind = None

    # 5. start time --------------------------------------------------------
    try:
        start = parse_stat_start_time(fs.read_bytes(pid, "stat"))
    except PermissionError:
        report.permission_denied.append(pid)
        drop("/proc/<pid>/stat izin nedeniyle okunamadı")
        return
    except (OSError, ValueError) as e:
        if isinstance(e, OSError) and _is_vanished(e):
            report.vanished += 1
            report.candidates -= 1
            return
        drop(f"/proc/<pid>/stat ayrıştırılamadı ({e})")
        return

    # 6. container ---------------------------------------------------------
    container: str | None = None
    server_uuid: str | None = None
    cgroup_denied = False
    try:
        cg_text = fs.read_bytes(pid, "cgroup").decode("utf-8", "replace")
        container = parse_container_id(cg_text)
        server_uuid = parse_server_uuid(cg_text)
    except PermissionError:
        cgroup_denied = True
        report.permission_denied.append(pid)
    except OSError:
        pass                            # no cgroup file: unknown, not an error
    if crit.expected_container_id:
        if not container_id_matches(crit.expected_container_id, container):
            if cgroup_denied:
                drop("cgroup izin nedeniyle okunamadı, konteyner doğrulanamadı")
            elif container is None:
                drop("konteyner kimliği okunamadı/tanınmadı "
                     "(beklenen: " + crit.expected_container_id + ")")
            else:
                report.container_mismatch = True
                desc = (f"PID {pid} (Port={game}, QueryPort={query}, "
                        f"konteyner {container[:12]})")
                report.container_mismatch_seen.append(desc)
                drop(f"konteyner {container[:12]} beklenen "
                     f"{crit.expected_container_id[:12]} ile eşleşmiyor")
            return

    root = fs.process_root(pid)
    matches.append(GameProcess(
        pid=pid, start_time_ticks=start, cmdline=tuple(args), exe=exe,
        matched_port_kind=kind, game_port=game, query_port=query,
        container_id=container, root_path=root,
        log_path=build_log_path(root, crit.log_path_in_container),
        server_uuid=server_uuid, proc_root=fs.root))


def _scan(fs: ProcFS, pids: Iterable[int], crit: _Criteria
          ) -> tuple[list[GameProcess], ScanReport]:
    report = ScanReport(criteria=crit.describe())
    matches: list[GameProcess] = []
    for pid in pids:
        _inspect(fs, pid, crit, report, matches)
    return matches, report


def _not_found(report: ScanReport, crit: _Criteria, what: str) -> GameProcessNotFound:
    only_container = (report.container_mismatch
                      and len(report.eliminated) == len(report.container_mismatch_seen))
    if only_container:
        msg = (f"{what}: oyun süreçleri var ama beklenen konteynerde değil "
               f"(expected_container_id={crit.expected_container_id}). Görülen: "
               + "; ".join(report.container_mismatch_seen)
               + ". Konteyner yeniden oluşturulduysa kimliği DEĞİŞİR: yönetici "
                 "sqreader config'inde expected_container_id değerini güncellemeli; "
                 "o zamana kadar reader bu sürece BAĞLANMAZ. "
               + report.summary())
        return ContainerMismatch(msg, report)
    return GameProcessNotFound(f"{what}. {report.summary()}", report)


def _ambiguous(matches: Sequence[GameProcess], report: ScanReport,
               what: str) -> AmbiguousGameProcess:
    lines = "; ".join(m.describe() for m in matches)
    msg = (f"{what}: {len(matches)} süreç ölçütle eşleşti, hiçbiri seçilmedi "
           f"(ilk eşleşme seçilmez). Adaylar: {lines}. Hangi sunucunun "
           f"okunacağını squad_port / squad_query_port / expected_container_id "
           f"ile belirtin. {report.summary()}")
    return AmbiguousGameProcess(msg, matches, report)


def discover_game_process(*, proc_root: "str | os.PathLike[str]" = "/proc",
                          exe_names: Sequence[str] = DEFAULT_EXE_NAMES,
                          game_port: int | str | None = None,
                          query_port: int | str | None = None,
                          expected_container_id: str | None = None,
                          expected_exe_path_suffix: str | None = None,
                          log_path_in_container: str | None = None,
                          fs: ProcFS | None = None) -> GameProcess:
    """The ONE game process that satisfies the criteria, else an error.

    Raises ``GameProcessNotFound`` (also ``ContainerMismatch``),
    ``AmbiguousGameProcess`` or ``ProcUnavailable``. Never picks between
    several matches.
    """
    crit = _make_criteria(exe_names, game_port, query_port, expected_container_id,
                          expected_exe_path_suffix, log_path_in_container)
    fs = fs or ProcFS(proc_root)
    matches, report = _scan(fs, fs.list_pids(), crit)
    if not matches:
        raise _not_found(report, crit, "Oyun sunucusu süreci bulunamadı")
    if len(matches) > 1:
        raise _ambiguous(matches, report, "Belirsiz oyun sunucusu")
    return matches[0]


def verify_game_process(pid: int, *, proc_root: "str | os.PathLike[str]" = "/proc",
                        exe_names: Sequence[str] = DEFAULT_EXE_NAMES,
                        game_port: int | str | None = None,
                        query_port: int | str | None = None,
                        expected_container_id: str | None = None,
                        expected_exe_path_suffix: str | None = None,
                        log_path_in_container: str | None = None,
                        fs: ProcFS | None = None) -> GameProcess:
    """Check a caller-supplied PID (``--pid``) against the same criteria.

    A pinned PID is trusted no more than a discovered one: it must be the game
    binary (not a launcher) and carry the requested ports / container.
    """
    crit = _make_criteria(exe_names, game_port, query_port, expected_container_id,
                          expected_exe_path_suffix, log_path_in_container)
    fs = fs or ProcFS(proc_root)
    if not fs.exists():
        raise ProcUnavailable(
            f"çalıştırılmadı: Linux host gerekli ({fs.root} bulunamadı)")
    matches, report = _scan(fs, [int(pid)], crit)
    if matches:
        return matches[0]
    exc = _not_found(report, crit, f"--pid {pid} doğrulanamadı (oyun sunucusu "
                                   f"ikili dosyası değil ya da ölçütlere uymuyor)")
    raise exc


def is_same_process(proc: GameProcess, *,
                    proc_root: "str | os.PathLike[str] | None" = None,
                    fs: ProcFS | None = None) -> bool:
    """Whether ``proc.pid`` is STILL the same process: same PID AND same start
    time. False when it is gone or the PID now belongs to something else.
    A permission error is re-raised: "cannot tell" must not read as "gone".
    """
    fs = fs or ProcFS(proc_root if proc_root is not None else proc.proc_root)
    try:
        start = parse_stat_start_time(fs.read_bytes(proc.pid, "stat"))
    except OSError as e:
        if isinstance(e, PermissionError):
            raise
        return False
    except ValueError:
        return False
    return start == proc.start_time_ticks


# --------------------------------------------------------------------------
# waiting for the game / re-attaching
# --------------------------------------------------------------------------

def wait_for_game_process(discover: Callable[[], GameProcess], *,
                          sleep: Callable[[float], None] = time.sleep,
                          on_state: Callable[[str, Exception, int, float], None] | None = None,
                          min_delay: float = 5.0, max_delay: float = 120.0,
                          clock: Callable[[], float] = time.monotonic,
                          max_wait: float | None = None) -> GameProcess:
    """Call ``discover`` until it yields a process, backing off between tries.

    A missing game server is a normal, temporary condition (restart, crash,
    map-change reboot): it never crashes the reader. ``on_state(state, error,
    attempt, delay)`` is told ``"waiting_for_game"`` or ``"container_mismatch"``
    so health output can say which. Ambiguity and unusable criteria are NOT
    retried: they will not fix themselves and must be seen. ``max_wait`` (tests,
    one-shot tools) turns the wait into the last error.
    """
    delay = min_delay
    attempt = 0
    t0 = clock()
    while True:
        try:
            return discover()
        except GameProcessNotFound as e:
            attempt += 1
            state = ("container_mismatch" if isinstance(e, ContainerMismatch)
                     else "waiting_for_game")
            if max_wait is not None and clock() - t0 + delay > max_wait:
                raise
            if on_state is not None:
                on_state(state, e, attempt, delay)
            sleep(delay)
            delay = min(delay * 2, max_delay)


def reattach_loop(run_session: Callable[[], Any], *,
                  on_lost: Callable[[TargetLost, int, float], None] | None = None,
                  sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic,
                  min_delay: float = 5.0, max_delay: float = 120.0,
                  stable_after: float = 60.0) -> Any:
    """Run reader sessions until one ends WITHOUT losing its target.

    ``run_session`` attaches (waiting for the game if it must), reads until it
    is stopped, and returns; or raises ``TargetLost`` when its process is gone or
    replaced. The loop then starts a fresh session, which re-discovers with the
    same criteria: the old PID is never reused. A session that dies quickly and
    repeatedly gets a growing pause (bounded by ``max_delay``), so a flapping
    game cannot turn the reader into a busy loop; one that ran ``stable_after``
    seconds resets the pause.
    """
    delay = min_delay
    lost = 0
    while True:
        t0 = clock()
        try:
            return run_session()
        except TargetLost as e:
            lost += 1
            if clock() - t0 >= stable_after:
                delay = min_delay
            if on_lost is not None:
                on_lost(e, lost, delay)
            sleep(delay)
            delay = min(delay * 2, max_delay)


# --------------------------------------------------------------------------
# log path
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LogResolution:
    path: str | None
    source: str            # "explicit" | "container" | "exe" | "glob" | "none"
    tried: tuple[str, ...] = ()


def resolve_log_path(proc: GameProcess, *, explicit: str | None = None,
                     log_path_in_container: str | None = None,
                     glob_pattern: str | None = None,
                     is_readable: Callable[[str], bool] | None = None,
                     glob_fn: Callable[[str], list[str]] = _glob.glob
                     ) -> LogResolution:
    """Where the game's SquadGame.log is.

    1. An explicit path (CLI ``--squad-log`` / config ``log_path``) wins.
    2. ``<root_path>/<log_path_in_container>``: the process's own filesystem via
       ``/proc/<pid>/root``, which is how a host-side reader sees a container.
    3. The same, derived from the running exe's own location
       (``<root>/SquadGame/Binaries/Linux/<exe>``): for non-Pterodactyl layouts.
    4. Only for a process that is NOT known to be in a container: the glob
       fallback. More than one match is an error (``AmbiguousLogPath``); the
       first/newest is never taken.
    ``None`` path means nothing was found; ``tried`` lists what was looked at.
    """
    readable = is_readable or (lambda p: os.path.isfile(p) and os.access(p, os.R_OK))
    if explicit:
        return LogResolution(str(explicit), "explicit", (str(explicit),))
    tried: list[str] = []

    primary = build_log_path(proc.root_path, log_path_in_container)
    tried.append(primary)
    if readable(primary):
        return LogResolution(primary, "container", tuple(tried))

    if proc.exe:
        root = proc.exe.replace("\\", "/")
        for _ in range(4):               # .../SquadGame/Binaries/Linux/<exe>
            root = posixpath.dirname(root)
        if root and root != "/":
            derived = build_log_path(
                proc.root_path, posixpath.join(root, "SquadGame", "Saved",
                                               "Logs", "SquadGame.log"))
            if derived not in tried:
                tried.append(derived)
                if readable(derived):
                    return LogResolution(derived, "exe", tuple(tried))

    if proc.container_id is None and glob_pattern:
        found = sorted(g for g in glob_fn(glob_pattern) if readable(g))
        tried.append(glob_pattern)
        if len(found) > 1:
            raise AmbiguousLogPath(
                f"Log dosyası belirsiz: {glob_pattern} ifadesi {len(found)} "
                f"dosyayla eşleşti ({', '.join(found)}). İlk eşleşme seçilmez; "
                f"config'te log_path (ya da --squad-log) ile yolu belirtin.",
                found)
        if found:
            return LogResolution(found[0], "glob", tuple(tried))
    return LogResolution(None, "none", tuple(tried))


# --------------------------------------------------------------------------
# data-directory protection
# --------------------------------------------------------------------------

def check_stats_db_server_id(db_path: "str | os.PathLike[str]",
                             server_id: str) -> None:
    """Refuse to run against a stats DB that already holds ANOTHER server's rows.

    One reader = one game server = one data directory. Starting reader B on
    reader A's directory would mix 7787 and 7788 records irrecoverably, so this
    raises ``ServerIdMismatch`` before anything is opened for writing. A missing
    or empty DB (fresh directory) passes.
    """
    path = Path(db_path)
    if not path.is_file():
        return
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise ServerIdMismatch(
            f"Veri dizini doğrulanamadı: {path} salt-okunur açılamadı ({e}). "
            f"Başka bir sunucuya ait olmadığından emin olunamadığı için "
            f"başlatılmıyor.") from e
    try:
        try:
            rows = conn.execute(
                "SELECT server_id, COUNT(*) FROM matches GROUP BY server_id"
            ).fetchall()
        except sqlite3.OperationalError:
            return                       # no matches table yet: nothing to mix
    finally:
        conn.close()
    foreign = [(sid, n) for sid, n in rows if sid != server_id]
    if foreign:
        seen = ", ".join(f"{sid!r} ({n} maç)" for sid, n in foreign)
        raise ServerIdMismatch(
            f"Veri dizini başka bir sunucuya ait: {path} içinde server_id "
            f"{seen} kayıtları var, config'teki server_id ise {server_id!r}. "
            f"Bir reader örneği = bir oyun sunucusu = bir veri dizini. Her "
            f"sunucu için ayrı config ve ayrı veri dizini kullanın; kayıtlar "
            f"karışmasın diye başlatılmıyor.")


# --------------------------------------------------------------------------
# doctor report
# --------------------------------------------------------------------------

def discovery_report(*, pid: int | None = None,
                     proc_root: "str | os.PathLike[str]" = "/proc",
                     explicit_log: str | None = None,
                     glob_pattern: str | None = None,
                     exe_names: Sequence[str] = DEFAULT_EXE_NAMES,
                     game_port: Any = None, query_port: Any = None,
                     expected_container_id: Any = None,
                     expected_exe_path_suffix: Any = None,
                     log_path_in_container: Any = None,
                     fs: ProcFS | None = None) -> tuple[str, list[str]]:
    """(status, lines) for ``sqreader doctor``. status is ``"ok"``, ``"fail"``
    or ``"skipped"``; a skipped check must never read as a pass."""
    fs = fs or ProcFS(proc_root)
    if not fs.exists():
        return "skipped", ["ÇALIŞTIRILMADI: Linux host gerekli "
                           f"({fs.root} bulunamadı); süreç keşfi doğrulanmadı"]
    try:
        crit = _make_criteria(exe_names, game_port, query_port,
                              expected_container_id, expected_exe_path_suffix,
                              log_path_in_container)
        pids = [int(pid)] if pid is not None else fs.list_pids()
        matches, report = _scan(fs, pids, crit)
        if not matches:
            raise _not_found(report, crit, "Oyun sunucusu süreci bulunamadı"
                             if pid is None else f"--pid {pid} doğrulanamadı")
        if len(matches) > 1:
            raise _ambiguous(matches, report, "Belirsiz oyun sunucusu")
    except DiscoveryError as e:
        return "fail", [f"BAŞARISIZ: {e}"]
    proc = matches[0]
    lines = [
        f"eşleşen PID: {proc.pid} (start_time_ticks={proc.start_time_ticks})",
        f"portlar: Port={proc.game_port if proc.game_port is not None else '-'} "
        f"QueryPort={proc.query_port if proc.query_port is not None else '-'}",
        "eşleşen port anahtarı: "
        + (proc.matched_port_kind
           or "yok (port ölçütü verilmedi; tek aday olduğu için seçildi)"),
        f"konteyner kimliği: {proc.container_id or 'tanınmadı/yok'}",
    ]
    if proc.server_uuid:
        lines.append(f"Pterodactyl sunucu UUID: {proc.server_uuid}")
    try:
        res = resolve_log_path(proc, explicit=explicit_log,
                               log_path_in_container=log_path_in_container,
                               glob_pattern=glob_pattern)
        if res.path:
            lines.append(f"log yolu: {res.path} (kaynak: {res.source})")
        else:
            lines.append("log yolu: BULUNAMADI (bakılan: "
                         + ", ".join(res.tried) + ")")
    except AmbiguousLogPath as e:
        lines.append(f"log yolu: BAŞARISIZ: {e}")
    if report.permission_denied:
        lines.append(f"izin sorunu: {len(report.permission_denied)} süreç izin "
                     f"nedeniyle okunamadı (PID: {_pid_list(report.permission_denied)})")
    else:
        lines.append("izin sorunu: yok")
    if not proc.exe:
        lines.append("uyarı: exe bağlantısı okunamadı; kimlik cmdline[0]'a dayanıyor")
    return "ok", lines


def platform_supports_proc() -> bool:
    return not sys.platform.startswith("win")


__all__ = [
    "DEFAULT_EXE_NAMES", "DEFAULT_LOG_PATH_IN_CONTAINER", "KIND_GAME", "KIND_QUERY",
    "DiscoveryError", "DiscoveryConfigError", "ProcUnavailable",
    "GameProcessNotFound", "ContainerMismatch", "AmbiguousGameProcess",
    "AmbiguousLogPath", "ServerIdMismatch", "TargetLost",
    "GameProcess", "ScanReport", "ProcFS", "LogResolution",
    "parse_ports", "parse_stat_start_time", "parse_container_id",
    "parse_server_uuid", "split_cmdline", "container_id_matches",
    "normalize_expected_container_id", "build_log_path",
    "discover_game_process", "verify_game_process", "is_same_process",
    "wait_for_game_process", "reattach_loop", "resolve_log_path",
    "check_stats_db_server_id", "discovery_report",
]
