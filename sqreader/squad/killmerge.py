"""Pure merger of game-log damage events with memory-reader damage events.

The log gives authoritative attacker/weapon/kill flags and a UTC timestamp;
memory gives the rich fields (EOS, team, position, damage, bone, ...). Memory
timestamps are match world-time and are never compared with log timestamps:
matching uses only the victim key, event kind and a short tick/time window.
No I/O; the clock is supplied by the caller."""
import copy
from typing import Optional

_COPY_FIELDS = ("victimEosId", "victimTeam", "victimSoldier", "victimPos",
                "causerClass", "damage", "hitDistance", "headshot", "bone",
                "attackerCtrlAddr")


def _kind(ev: dict) -> Optional[str]:
    if ev.get("killed"):
        return "killed"
    if ev.get("wounded"):
        return "wounded"
    return None


def _norm(name) -> str:
    return str(name).strip().lower() if name else ""


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
        log_events = log_events or []
        mem_events = mem_events or []
        self._buf = [b for b in self._buf
                     if tick - b["tick"] <= self.window_ticks
                     and now - b["now"] <= self.window_sec]
        name_to_eos = {_norm(p.get("name")): p["eosId"]
                       for p in (players or [])
                       if p.get("name") and p.get("eosId")}

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
                for b in reversed(self._buf):
                    if b["kind"] == kind and self._same_victim(b, eos, name):
                        hit = b
                        break
            if hit is None:
                ev["source"] = "log"
                out.append(ev)
                continue
            self._buf.remove(hit)
            used.add(id(hit))
            m = hit["ev"]
            for f in _COPY_FIELDS:
                ev[f] = copy.deepcopy(m.get(f))
            if not ev.get("attackerEosId"):
                ev["attackerEosId"] = m.get("attackerEosId")
            if not ev.get("causerWeapon"):
                ev["causerWeapon"] = m.get("causerWeapon")
            if not ev.get("attacker") and not ev.get("selfInflicted"):
                ev["attacker"] = m.get("attacker")
            ev["source"] = "log+memory"
            out.append(ev)

        if not log_healthy:
            for b in fresh:
                if id(b) not in used:
                    ev = copy.deepcopy(b["ev"])
                    ev["source"] = "memory"
                    out.append(ev)
        return out
