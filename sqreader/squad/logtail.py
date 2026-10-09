"""
Authoritative kill-feed events from the Squad server log.

The memory reader samples take-hit info at ~0.5 Hz off the (short-lived)
soldier actor, so it only ever catches a fraction of kills. The server's
own log records EVERY damage / wound / death with the exact attacker and
weapon — it is the game's ground truth. We tail it and correlate the
lines into clean events shaped like the frontend's DamageEvent, so the
event-first kill feed attributes ~every death exactly.

Log lines we use (verified live on Squad v10.x):

  LogSquad: Player: <victim> ActualDamage=<f> from <attacker>
      (Online IDs: EOS: <eos> steam: <id> | Player Controller ID: <ctrl>)caused by <weapon>
  LogSquadTrace: [DedicatedServer]Wound(): Player: <victim> KillingDamage=<f> from <ctrl> (...)
  LogSquadTrace: [DedicatedServer]Die():   Player:<victim> KillingDamage=<f> from <ctrl|nullptr> (...)

Correlation:
  - ActualDamage carries the attacker NAME + weapon; remember it per victim.
  - Wound() -> the victim is incapacitated. Emit a wounded event using the
    remembered attacker/weapon, and remember who downed them.
  - Die() from a real controller -> instant kill; attacker from the last
    ActualDamage. Die() from nullptr -> bleed-out; attacker is whoever the
    Wound() recorded. No record at all -> a true world death (no attacker).
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

# Victim names contain spaces and unicode; capture non-greedily up to the
# " ActualDamage=" / " KillingDamage=" delimiter. "Player:" may or may not
# be followed by a space.
_ACTUAL_RE = re.compile(
    r"LogSquad: Player:\s?(?P<victim>.+?) ActualDamage=[-\d.]+ from "
    r"(?P<attacker>.+?) \(Online IDs:\s*EOS:\s*(?P<eos>\w+).*?\)\s*caused by (?P<weapon>\S+)"
)
_WOUND_RE = re.compile(
    r"Wound\(\): Player:\s?(?P<victim>.+?) KillingDamage=[-\d.]+ from (?P<from>\S+)"
    r"(?:\s*\(Online IDs:\s*EOS:\s*(?P<from_eos>\w+))?"
    r"(?:.*?caused by (?P<causer>\S+))?"
)
_DIE_RE = re.compile(
    r"Die\(\): Player:\s?(?P<victim>.+?) KillingDamage=[-\d.]+ from (?P<from>\S+)"
    r"(?:\s*\(Online IDs:\s*EOS:\s*(?P<from_eos>\w+))?"
    r"(?:.*?caused by (?P<causer>\S+))?"
)
# "<reviver> (Online IDs: ...) has revived <victim> (Online IDs: ...)"
_REVIVE_RE = re.compile(r"has revived (?P<victim>.+?) \(Online IDs:")
_TS_RE = re.compile(r"^\[(?P<ts>[\d.]+-[\d.]+:[\d]+)\]")


_POSSESS_RE = re.compile(
    r"On(?:Un)?Possess\(\): PC=(?P<name>.+?)\s*\(Online IDs:\s*EOS:\s*(?P<eos>[0-9a-fA-F]{32})")
_LOGIN_EOS_RE = re.compile(r"PostLogin: NewPlayer:.*Online IDs:\s*EOS:\s*([0-9a-fA-F]{32})")


def resolve_event_names(events: list[dict], players: list[dict]) -> list[dict]:
    """Rewrite each event's attacker/victim to the matching snapshot player's
    base name.

    Squad log lines carry the full display name WITH the clan tag
    ("『GM』 I3lack"); the memory reader splits the tag off (name="I3lack",
    clanTag separate). The frontend keys the kill feed on the base name, so
    without this ~half the events (every tagged player) would fail to match.
    Exact match first, else the longest base name that is a suffix of the log
    name at a tag boundary (a non-alphanumeric separator precedes it).

    Also performs EOS-based attacker recovery: when the name-cache lost the
    attacker (a death long after the wound, past the correlation TTL), the Die
    line still carried the killer's controller EOS in `killerEos`. Resolve it to
    a name against the live roster here — or, if it is the victim's own EOS,
    flag a self death rather than crediting them their own kill. This is what
    converts the bare "?" rows into named kills."""
    bases = [p["name"] for p in players if p.get("name")]
    base_set = set(bases)
    eos_to_name = {p["eosId"]: p["name"]
                   for p in players if p.get("eosId") and p.get("name")}
    name_to_eos = {p["name"]: p["eosId"]
                   for p in players if p.get("name") and p.get("eosId")}

    def resolve(full: Optional[str]) -> Optional[str]:
        if not full or full in base_set:
            return full
        best = None
        for n in bases:
            if len(n) < 3 or not full.endswith(n):
                continue
            i = len(full) - len(n)
            if i > 0 and full[i - 1].isalnum():
                continue  # mid-word, not a clan-tag boundary
            if best is None or len(n) > len(best):
                best = n
        return best or full

    for e in events:
        e["victim"] = resolve(e.get("victim"))
        e["attacker"] = resolve(e.get("attacker"))
        keos = e.pop("killerEos", None)
        if not e.get("attacker") and not keos:
            # Wound/Die with a nullptr ActualDamage: only the event's own
            # controller EOS identifies the attacker.
            keos = e.get("attackerEosId")
        if not e.get("attacker") and keos:
            veos = e.get("victimEosId") or name_to_eos.get(e.get("victim"))
            if veos and keos == veos:
                e["selfInflicted"] = True   # died to their own pawn — self/give-up
            else:
                nm = eos_to_name.get(keos)
                if nm and nm != e.get("victim"):
                    e["attacker"] = nm
                    e["attackerEosId"] = keos
    return events


def log_from_pid(pid: int) -> Optional[str]:
    """The running server's own log, derived from the binary it is executing.

    Squad always lays itself out as
    ``<root>/SquadGame/Binaries/Linux/SquadGameServer`` and always writes
    ``<root>/SquadGame/Saved/Logs/SquadGame.log``, so the process points at its
    own log no matter where somebody installed it. That matters because the
    fallback glob below only looks under ``/home/*/serverfiles`` — LinuxGSM's
    layout — and a Docker image, an /opt install or a user outside /home would
    find nothing and lose the kill feed without ever saying why.
    """
    import os
    try:
        exe = os.path.realpath(f"/proc/{pid}/exe")
    except OSError:
        return None
    root = exe
    for _ in range(4):                       # .../SquadGame/Binaries/Linux/exe
        root = os.path.dirname(root)
    if not root or root == os.sep:
        return None
    cand = os.path.join(root, "SquadGame", "Saved", "Logs", "SquadGame.log")
    return cand if os.access(cand, os.R_OK) else None


def find_squad_log(pid: Optional[int] = None) -> Optional[str]:
    """Locate the active server's SquadGame.log.

    Order matters: the PROCESS is asked first, because it is the only source
    that is right regardless of how the server was installed. The glob is kept
    as a fallback for the case where the exe path is unreadable, and an
    operator's explicit `squad_log_glob` still wins over both by pointing the
    glob wherever they say.
    """
    import glob
    import os

    from ..config import get as _config_get
    if pid is not None:
        found = log_from_pid(pid)
        if found:
            return found
    cands = [c for c in glob.glob(_config_get("squad_log_glob"))
             if os.access(c, os.R_OK)]
    if not cands:
        return None
    return max(cands, key=lambda p: os.stat(p).st_mtime)


def _parse_ts(line: str) -> Optional[float]:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        # e.g. 2026.07.12-18.31.24:082  (last field is milliseconds)
        dt = datetime.strptime(m.group("ts"), "%Y.%m.%d-%H.%M.%S:%f")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


_CHAIN_RE = re.compile(r"^\[[^\]]*\]\[\s*(\d+)\]")


def _chain_id(line: str) -> Optional[str]:
    """Chain id from the `[timestamp][ chain]` line header, if present."""
    m = _CHAIN_RE.match(line)
    return m.group(1) if m else None


def _norm_weapon(w: str) -> str:
    # BP_M4_SimonOffense_T800_C_2007009837 -> BP_M4_SimonOffense_T800_C
    # (strip the trailing runtime instance id so it matches the catalog)
    return re.sub(r"_\d+$", "", w) if w and w != "nullptr" else w


class DamageLogParser:
    """Pure correlation state machine — feed it lines, drain events. No I/O,
    so it is directly unit-testable."""

    # Wound -> death (bleed-out / give-up) routinely takes 4-5 min, >10 min
    # happens, so the wound record must outlive that. The last-hit record is
    # only trusted while fresh (it is consulted for instant kills).
    HIT_FRESH_SEC = 180.0

    def __init__(self, *, ttl_sec: float = 1200.0, max_buffer: int = 2048):
        self.ttl = ttl_sec
        self._last_gc: float = 0.0
        # victim -> (attacker, eos, weapon, ts) from the most recent hit
        self._pending: dict[str, tuple] = {}
        # victim -> (attacker, eos, weapon, ts) recorded at incap, awaiting death
        self._wounded_by: dict[str, tuple] = {}
        self._events: deque = deque(maxlen=max_buffer)
        self._last_ts: float = 0.0
        # EOS -> player name learned from the log itself (SquadReader's
        # roster eosId is not the real EOS). Insertion-ordered; oldest
        # dropped past EOS_MAP_MAX. Deliberately NOT cleared on map load.
        self.eos_to_name: dict[str, str] = {}
        # PostLogin entries awaiting their "Join succeeded": (chain, eos, ts)
        self._pending_login: deque = deque(maxlen=16)

    EOS_MAP_MAX = 4096
    LOGIN_JOIN_SEC = 2.0

    def _learn(self, eos: Optional[str], name: Optional[str]) -> None:
        if not eos or not name or eos == "INVALID":
            return
        name = name.strip()
        if not name:
            return
        d = self.eos_to_name
        if eos in d:
            del d[eos]            # re-insert => most recently seen last
        d[eos] = name
        while len(d) > self.EOS_MAP_MAX:
            del d[next(iter(d))]

    def _gc(self, now: float) -> None:
        self._last_gc = now
        for d in (self._pending, self._wounded_by):
            stale = [k for k, v in d.items() if now - v[3] > self.ttl]
            for k in stale:
                del d[k]

    def feed(self, line: str) -> None:
        ts = _parse_ts(line) or self._last_ts
        self._last_ts = ts

        if "OnPossess(): PC=" in line or "OnUnPossess(): PC=" in line:
            m = _POSSESS_RE.search(line)
            if m:
                self._learn(m.group("eos"), m.group("name"))
            return
        if "PostLogin: NewPlayer" in line:
            m = _LOGIN_EOS_RE.search(line)
            if m:
                self._pending_login.append((_chain_id(line), m.group(1), ts))
            return
        if "Join succeeded: " in line:
            pl = self._pending_login
            while pl and ts - pl[0][2] > self.LOGIN_JOIN_SEC:
                pl.popleft()                      # stale: never got its Join
            chain = _chain_id(line)
            hit = None
            if chain is not None:
                hit = next((e for e in pl if e[0] == chain), None)
            if hit is None and pl:
                hit = pl[0]                       # no/unknown chain: FIFO
            if hit is not None:
                pl.remove(hit)
                self._learn(hit[1], line.split("Join succeeded: ", 1)[1])
            return

        if "LogWorld: Bringing World" in line:
            # New map/match: nothing remembered from the last one may leak in
            # (round-end deaths arrive AFTER WaitingPostMatch, so that state
            # change is deliberately not a reset point).
            self._pending.clear()
            self._wounded_by.clear()
            return

        m = _ACTUAL_RE.search(line)
        if m:
            victim = m.group("victim").strip()
            attacker = m.group("attacker").strip()
            if attacker != "nullptr":
                self._learn(m.group("eos"), attacker)
            self._pending[victim] = (
                attacker, m.group("eos"), _norm_weapon(m.group("weapon")), ts)
            if ts - self._last_gc > 60.0:   # bound memory on long kill-less spells
                self._gc(ts)
            return

        m = _REVIVE_RE.search(line)
        if m:
            # A revived player is back in the fight — forget who downed them
            # so their NEXT death (a fresh incap, or a later world cause) is
            # not mis-credited to the old wounder.
            victim = m.group("victim").strip()
            self._wounded_by.pop(victim, None)
            self._pending.pop(victim, None)
            return

        m = _WOUND_RE.search(line)
        if m:
            victim = m.group("victim").strip()
            src = self._fresh_hit(victim, ts)
            attacker = src[0] if src else None
            eos = src[1] if src else None
            weapon = src[2] if src else None
            # The Wound line carries the downing controller's own EOS and the
            # weapon even when the preceding ActualDamage was `from nullptr`
            # (vehicle weapon / collision). It is authoritative for the EOS.
            w_eos = self._valid_eos(m.group("from"), m.group("from_eos"))
            if w_eos and eos and w_eos != eos:
                # The remembered hit belongs to someone else; the Wound line
                # is authoritative. Names come from the roster via the EOS.
                eos = w_eos
                attacker = None
                weapon = self._line_weapon(m.group("causer"))
            elif w_eos and not eos:
                eos = w_eos
            if not weapon:
                weapon = self._line_weapon(m.group("causer"))
            self._wounded_by[victim] = (attacker, eos, weapon, ts)
            self._emit(victim, attacker, eos, weapon, ts,
                       wounded=True, killed=False,
                       killer_eos=eos if attacker is None else None)
            if ts - self._last_gc > 60.0:
                self._gc(ts)
            return

        m = _DIE_RE.search(line)
        if m:
            victim = m.group("victim").strip()
            frm = m.group("from")
            causer = m.group("causer")
            die_eos = m.group("from_eos")
            if frm and frm != "nullptr":
                # instant kill — attacker is the last hit's source
                src = self._fresh_hit(victim, ts)
            elif not causer or causer == "nullptr" or causer.startswith("BP_Soldier"):
                # bleed-out / give-up — attacker is whoever downed them
                src = self._wounded_by.get(victim)
                if src and ts - src[3] > self.ttl:
                    src = None
            else:
                src = None   # vehicle / world cause: a new death, not a bleed-out
            attacker = src[0] if src else None
            eos = src[1] if src else None
            weapon = src[2] if src else None
            if src and frm and frm != "nullptr" and die_eos and die_eos != "INVALID" \
                    and eos and die_eos != eos:
                # the Die line credits a different controller than the
                # remembered hit: the line is authoritative
                attacker, eos, weapon = None, die_eos, None
            if not weapon:
                weapon = self._line_weapon(causer)
            # The Die line names the CREDITED killer by controller EOS (the same
            # EOS the game scores the kill to). Keep it: when the name-cache has
            # expired — a death long after the wound, e.g. round-end deaths of
            # players downed minutes earlier, past the correlation TTL — this is
            # the ONLY thing that still identifies the killer. resolve_event_names
            # turns it into a name against the live roster (or a self death if it
            # is the victim's own EOS). This is what kills the bare "?" rows.
            killer_eos = die_eos if die_eos and die_eos != "INVALID" else None
            if killer_eos is None and attacker is None and eos:
                killer_eos = eos   # EOS remembered from the Wound line
            # No damage record attributes this death — classify it from the
            # log's own "from" / "caused by" instead of showing a bare "?":
            #   caused by <world class>    -> a world cause (fall, drown,
            #     vehicle, roadkill), NOT a soldier pawn; surface it as the
            #     damage type so the feed names the cause.
            #   from <own pawn> + nullptr  -> the player gave up / self-
            #     terminated while downed (no weapon). A self death, credit no one.
            #   caused by <soldier pawn>   -> a real kill whose name-cache
            #     expired; leave it for killer_eos to recover the name.
            #   from nullptr (no cause)    -> a true unnamed world/bleed death.
            self_inflicted = None
            damage_type = None
            if attacker is None:
                if causer and causer != "nullptr" \
                        and not causer.startswith("BP_Soldier"):
                    damage_type = _norm_weapon(causer)
                elif frm and frm != "nullptr" and (not causer or causer == "nullptr"):
                    self_inflicted = True
            self._emit(victim, attacker, eos, weapon, ts,
                       wounded=False, killed=True,
                       self_inflicted=self_inflicted, damage_type=damage_type,
                       killer_eos=killer_eos)
            self._wounded_by.pop(victim, None)
            self._pending.pop(victim, None)
            self._gc(ts)

    @staticmethod
    def _valid_eos(frm, eos):
        if not frm or frm == "nullptr" or not eos or eos == "INVALID":
            return None
        return eos

    @staticmethod
    def _line_weapon(causer):
        """`caused by` of a Wound/Die line as a weapon class. Soldier pawns
        (bleed-out, melee/give-up) are not weapons."""
        if not causer or causer == "nullptr" or causer.startswith("BP_Soldier"):
            return None
        return _norm_weapon(causer)

    def _fresh_hit(self, victim, ts):
        src = self._pending.get(victim)
        if src and ts - src[3] > self.HIT_FRESH_SEC:
            return None
        return src

    def _emit(self, victim, attacker, eos, weapon, ts, *, wounded, killed,
              self_inflicted=None, damage_type=None, killer_eos=None):
        if attacker is None and not self_inflicted:
            known = self.eos_to_name.get(eos or "") or self.eos_to_name.get(killer_eos or "")
            if known:
                attacker = known
        self._events.append({
            "victim": victim,
            "victimEosId": None,          # log gives the attacker's EOS, not victim's
            "victimTeam": None,           # frontend fills team from the snapshot
            "attacker": None if attacker == victim else attacker,
            "attackerEosId": eos,
            "killerEos": killer_eos,      # Die-line controller EOS — resolve_event_names
                                          # turns it into a name / self-death, then drops it
            "selfInflicted": (attacker == victim) if self_inflicted is None
                             else bool(self_inflicted),
            "causerWeapon": weapon,
            "causerClass": None,
            "damageType": damage_type,
            "hitDistance": None,
            "headshot": None,
            "wounded": wounded,
            "killed": killed,
            "ts": round(ts, 3),
            "source": "log",
        })

    def drain(self) -> list[dict]:
        out = list(self._events)
        self._events.clear()
        return out


class LogTailer:
    """Follows the Squad log from its end in a daemon thread, feeds each new
    line to a DamageLogParser, and hands recent events to the snapshot loop
    via drain(). Read-only and isolated: if the log path is unreadable or the
    thread dies, the reader keeps running with an empty event stream."""

    def __init__(self, path: str, *, poll_sec: float = 0.25,
                 catchup_bytes: int = 4_000_000):
        self.path = path
        self.poll_sec = poll_sec
        # On startup, replay this many bytes from the log tail to rebuild
        # correlation state (in-flight wounds) without emitting the stale
        # events, so a death just after a reader restart is still attributed.
        self.catchup_bytes = catchup_bytes
        self.parser = DamageLogParser()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.lines_read = 0
        self.last_error: Optional[str] = None
        self._open_ok = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="squad-logtail",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def healthy(self) -> bool:
        """Thread alive, log opened fine and still present. A quiet log (no
        new lines) is NOT unhealthy."""
        import os
        t = self._thread
        return bool(t is not None and t.is_alive() and self._open_ok
                    and os.path.exists(self.path))

    def drain(self) -> list[dict]:
        with self._lock:
            return self.parser.drain()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._follow()
            except Exception as e:  # never let the tailer crash the reader
                self._open_ok = False
                self.last_error = f"{type(e).__name__}: {e}"
                time.sleep(2.0)

    def _follow(self) -> None:
        import os
        f = open(self.path, encoding="utf-8", errors="replace")
        self._open_ok = True
        try:
            # Initial catch-up: replay the log's tail to rebuild the
            # correlation state (a player downed just before we started can
            # still be credited when they bleed out) but DISCARD those old
            # events — drain() clears the event buffer while keeping the
            # pending / wounded_by state. Without this, a reader restart
            # mid-match leaves in-flight deaths unattributed for ~a minute.
            size = os.fstat(f.fileno()).st_size
            if self.catchup_bytes > 0 and size > 0:
                f.seek(max(0, size - self.catchup_bytes))
                if size > self.catchup_bytes:
                    f.readline()  # drop the partial first line
                catch = f.read()  # up to the current end
                with self._lock:
                    for line in catch.splitlines():
                        if line:
                            self.parser.feed(line)
                    self.parser.drain()  # discard stale events, keep state
            else:
                f.seek(0, os.SEEK_END)
            cur_ino = os.fstat(f.fileno()).st_ino
            buf = ""
            while not self._stop.is_set():
                chunk = f.read(65536)
                if chunk:
                    buf += chunk
                    while "\n" in buf:
                        line, _, buf = buf.partition("\n")
                        if line:
                            with self._lock:
                                self.parser.feed(line)
                            self.lines_read += 1
                    continue
                # no new data — check for rotation (restart / new map log)
                try:
                    st = os.stat(self.path)
                    if st.st_ino != cur_ino or st.st_size < f.tell():
                        f.close()
                        f = open(self.path, encoding="utf-8", errors="replace")
                        self._open_ok = True
                        cur_ino = os.fstat(f.fileno()).st_ino
                        buf = ""
                        continue
                except OSError:
                    pass
                time.sleep(self.poll_sec)
        finally:
            f.close()
