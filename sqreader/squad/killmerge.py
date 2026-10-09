"""Pure merger of game-log damage events with memory-reader damage events.

The log gives authoritative attacker/weapon/kill flags and a UTC timestamp;
memory gives the rich fields (EOS, team, position, damage, bone, ...). Memory
timestamps are match world-time and are never compared with log timestamps:
matching uses only the victim key, event kind and a short tick/time window.
No I/O; the clock is supplied by the caller."""
import copy
import math
from typing import Optional

_COPY_FIELDS = ("victimEosId", "victimTeam", "victimSoldier", "victimPos",
                "causerClass", "damage", "hitDistance", "headshot", "bone")


def _kind(ev: dict) -> Optional[str]:
    if ev.get("killed"):
        return "killed"
    if ev.get("wounded"):
        return "wounded"
    return None


def _norm(name) -> str:
    return str(name).strip().lower() if name else ""


def _xyz(pos):
    """(x, y, z|None) from a {"x","y","z"} dict of finite numbers, else None."""
    if not isinstance(pos, dict):
        return None
    try:
        x, y = float(pos["x"]), float(pos["y"])
        z = float(pos["z"]) if pos.get("z") is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y)) or (
            z is not None and not math.isfinite(z)):
        return None
    return x, y, z


def _find_player(players, eos, name):
    """Roster player by EOS id, else by a unique normalised name."""
    if eos:
        for p in players:
            if p.get("eosId") == eos:
                return p
    if name:
        cands = [p for p in players if _norm(p.get("name")) == name]
        if len(cands) == 1:
            return cands[0]
    return None


def _fill_distance(ev: dict, players: list) -> None:
    """Fill hitDistance (m) from roster positions: the memory FHitResult
    distance is a short trace length, not shooter-victim distance.

    Only a value with hitDistanceSource == "positions" is kept; anything
    else is recomputed, or reset to None if it cannot be computed.
    Note: for log+memory events victimPos comes from the memory event's
    tick (hit moment) and the attacker position from the current tick, so
    there is ~1-2 s skew and the result is approximate."""
    if ev.get("hitDistanceSource") == "positions":
        return
    if ev.get("selfInflicted"):
        return
    ev["hitDistance"] = None
    _compute_distance(ev, players)


def _compute_distance(ev: dict, players: list) -> None:
    att = _norm(ev.get("attacker"))
    if not att:
        return
    ap = _find_player(players, ev.get("attackerEosId"), att)
    if not ap:
        return
    a = _xyz((ap.get("soldier") or {}).get("position"))
    v = _xyz(ev.get("victimPos"))
    if v is None:
        vp = _find_player(players, ev.get("victimEosId"), _norm(ev.get("victim")))
        if vp:
            v = _xyz((vp.get("soldier") or {}).get("position"))
    if a is None or v is None:
        return
    d2 = (a[0] - v[0]) ** 2 + (a[1] - v[1]) ** 2
    if a[2] is not None and v[2] is not None:
        d2 += (a[2] - v[2]) ** 2
    d = round(math.sqrt(d2) / 100.0, 1)
    if 1.0 <= d < 10000.0:
        ev["hitDistance"] = d
        ev["hitDistanceSource"] = "positions"


class DamageEventMerger:
    def __init__(self, window_ticks: int = 3, window_sec: float = 6.0):
        self.window_ticks = window_ticks
        self.window_sec = window_sec
        self._buf: list[dict] = []   # {ev, kind, eos, name, tick, now}

    def _same_victim(self, entry: dict, eos: str, name: str) -> bool:
        if entry["eos"] and eos:
            return entry["eos"] == eos
        return bool(name) and entry["name"] == name

    def merge(self, log_events, mem_events, players, *, tick, now, log_healthy):
        out = self._merge(log_events, mem_events, players, tick=tick, now=now,
                          log_healthy=log_healthy)
        roster = [p for p in (players or []) if isinstance(p, dict)]
        for ev in out:
            _fill_distance(ev, roster)
        return out

    def _merge(self, log_events, mem_events, players, *, tick, now, log_healthy):
        log_events = log_events or []
        mem_events = mem_events or []
        self._buf = [b for b in self._buf
                     if tick - b["tick"] <= self.window_ticks
                     and now - b["now"] <= self.window_sec]
        seen: dict[str, set] = {}
        for p in (players or []):
            if p.get("name") and p.get("eosId"):
                seen.setdefault(_norm(p["name"]), set()).add(p["eosId"])
        # a name shared by several players is ambiguous: leave it to the
        # name-based match instead of guessing one EOS
        name_to_eos = {n: next(iter(e)) for n, e in seen.items()
                       if len(e) == 1}

        fresh = []
        for m in mem_events:
            entry = {"ev": m, "kind": _kind(m),
                     "eos": m.get("victimEosId") or "",
                     "name": _norm(m.get("victim")),
                     "tick": tick, "now": now}
            fresh.append(entry)
            if entry["kind"]:
                self._buf.append(entry)

        out = []
        used = set()
        for le in log_events:
            ev = copy.deepcopy(le)
            kind = _kind(ev)
            name = _norm(ev.get("victim"))
            eos = ev.get("victimEosId") or name_to_eos.get(name, "")
            hit = None
            if kind:
                att = _norm(ev.get("attacker"))
                for b in self._buf:     # oldest first
                    if b["kind"] != kind or not self._same_victim(b, eos, name):
                        continue
                    if b.get("emitted"):
                        # an already-published memory event only stands for
                        # this log event if the attacker is the same
                        # (or unknown in memory)
                        m_att = _norm(b["ev"].get("attacker"))
                        if m_att and m_att != att:
                            continue
                        hit = b         # emitted + attacker fits: best
                        break
                    if hit is None:
                        hit = b         # else the oldest candidate
            if hit is None:
                ev["source"] = "log"
                out.append(ev)
                continue
            self._buf.remove(hit)
            used.add(id(hit))
            if hit.get("emitted"):
                # same death already published as "memory" while the log was
                # unhealthy: do not count it a second time
                continue
            m = hit["ev"]
            for f in _COPY_FIELDS:
                ev[f] = copy.deepcopy(m.get(f))
            # the log is authoritative for the attacker: never take it from
            # memory. Suicides / world deaths (no attacker) stay untouched.
            if ev.get("attacker") and not ev.get("selfInflicted"):
                ev["attackerCtrlAddr"] = copy.deepcopy(m.get("attackerCtrlAddr"))
                if (not ev.get("attackerEosId")
                        and _norm(m.get("attacker")) == _norm(ev["attacker"])):
                    ev["attackerEosId"] = m.get("attackerEosId")
                if not ev.get("causerWeapon"):
                    ev["causerWeapon"] = m.get("causerWeapon")
            ev["source"] = "log+memory"
            out.append(ev)

        if not log_healthy:
            for b in fresh:
                if id(b) not in used:
                    b["emitted"] = True
                    ev = copy.deepcopy(b["ev"])
                    ev["source"] = "memory"
                    out.append(ev)
        return out
