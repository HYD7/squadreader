"""Deterministic SquadReader dataset for the Zero Zone integration tests.

`build_zz_fixture(out_dir)` writes two independent server data directories and
one manifest::

    <out>/
      fixture-manifest.json            # what was written, expected values, event log
      zz-7787/
        stats.db                       # StatsStore SQLite (server_id = zz-7787)
        recordings/
          <A>.sqrx  <A>.meta.json      # match A: finalized, recordingState=finalized
          <C>.sqrx  <C>.meta.json      # match C: unverified (close not confirmed)
          <D>.sqrx  <D>.meta.json      # recording D: still ACTIVE (in progress)
      zz-7788/
        stats.db                       # server_id = zz-7788
        recordings/
          <B>.sqrx  <B>.meta.json      # match B: finalized, same window as A

Nothing here writes SQLite rows or `.sqrx` bytes by hand. Every synthetic
snapshot goes through the reader's real code paths, in the order the serve loop
uses them (sqreader/cli.py):

* `StatsStore.record_tick(snap)`         -> `stats.db` (matches, player_matches,
  kill_events, vehicle_session), including end-of-match confirmation;
* `recorder._handle_snap(...)`           -> `.sqrx` open / write / finalize and the
  `.meta.json` sidecar (state machine, sequence checks, active -> finalized);
* `recorder.write_position_frame(...)`   -> the `{"t":"pos"}` frames;
* `SqrxWriter` (via the recorder)        -> the container itself.

Determinism. Snapshot timestamps are fixed (2026-09-01, UTC) and there is no
randomness. The only wall-clock reads in those code paths are `recorder._utc_now`
(recording file name / `startedAtUtc` of the active sidecar) and the
`created_ms` header of `SqrxWriter`; both are replaced for the duration of the
build by a clock that follows the snapshot time. That patch is process-global,
so do not call `build_zz_fixture` from several threads at once.

What is real and what is authored. The snapshot SHAPE follows
`frontend/src/state/types.ts` (the reader's contract). The VALUES (names, EOS
ids, positions, counters, scores) are authored by this file: EOS ids are
`md5("zz-fixture:<key>")` hex digests, names are fictional. Counters follow the
reader's fields (`NumKills`, `NumTeamkills`, ...), but this fixture does NOT
prove how the live game increments them (e.g. whether a teamkill also counts as
a kill; here it does not). It is a fixture, not a real-memory test.

Time is compressed: one full frame every `FULL_FRAME_SEC` (10 s) and three
position frames between consecutive full frames (2.5 s apart), i.e. the live
1 Hz : 4 Hz ratio at one tenth of the wall-clock rate.

Known gaps (see the report of the task that added this file): the reader has no
dedicated "vehicle destroyed" or "FOB destroyed" event. Both are observable as
the per-player counters `vehicle_kills` / `fobs_destroyed` in `stats.db`, and in
the replay frames as the vehicle's / deployable's health going to 0 and the
entity then disappearing. Team changes are not tracked by `stats.db` (only the
last `team_id` is kept); they are observable in the replay frames.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest import mock

from sqreader import recorder as _recorder
from sqreader import sqrx as _sqrx
from sqreader.recording_lifecycle import MATCH_TRANSITION_CONFIRM_TICKS
from sqreader.stats import StatsStore

FIXTURE_SCHEMA = "zz-sr-fixture-1"
MANIFEST_NAME = "fixture-manifest.json"

SERVER_A_ID = "zz-7787"   # matches A, C and recording D
SERVER_B_ID = "zz-7788"   # match B

FULL_FRAME_SEC = 10
POS_FRAMES_PER_FULL = 3   # position frames between two consecutive full frames

# ---------------------------------------------------------------------------
# Fixed identities
# ---------------------------------------------------------------------------

MATCH_IDS = {
    "A": "5a7e0a00-7787-4a00-8000-00000000a001",
    "B": "5a7e0b00-7788-4a00-8000-00000000b001",
    "C": "5a7e0c00-7787-4a00-8000-00000000c001",
    "D": "5a7e0d00-7787-4a00-8000-00000000d001",
}

_START = {
    "A": datetime(2026, 9, 1, 18, 0, 0, tzinfo=timezone.utc),
    "B": datetime(2026, 9, 1, 18, 0, 0, tzinfo=timezone.utc),   # same window as A
    "C": datetime(2026, 9, 1, 19, 0, 0, tzinfo=timezone.utc),
    "D": datetime(2026, 9, 1, 20, 0, 0, tzinfo=timezone.utc),
}
_SERVER_START_TS = int(datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())


def eos_id(key: str) -> str:
    """Stable 32-hex EOS-style id for a fictional player key."""
    return hashlib.md5(f"zz-fixture:{key}".encode(), usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# Scenario description
# ---------------------------------------------------------------------------

Waypoints = tuple[tuple[int, float, float], ...]   # (frame, x_cm, y_cm)


@dataclass(frozen=True)
class _Player:
    key: str
    name: str
    clan: Optional[str]
    role: str
    # (from_frame, team, squad_id, squad_name); a later entry is a team/squad change.
    sched: tuple[tuple[int, int, int, str], ...]
    path: Waypoints
    is_admin: bool = False
    is_commander: bool = False


@dataclass(frozen=True)
class _Ev:
    """A combat / scoring event at full-frame `frame`.

    kind: kill | tk | wound | revive | suicide | objective
    """
    frame: int
    kind: str
    actor: str
    target: Optional[str] = None
    weapon: Optional[str] = None
    points: float = 0.0


@dataclass(frozen=True)
class _Veh:
    vid: str
    cls: str
    kind: str
    team: int
    max_hp: float
    path: Waypoints
    # (seat_idx, player_key, from_frame, to_frame_exclusive)
    crew: tuple[tuple[int, str, int, int], ...] = ()
    destroyed_frame: Optional[int] = None
    destroyed_by: Optional[str] = None
    gone_frame: Optional[int] = None     # first frame the wreck is no longer listed


@dataclass(frozen=True)
class _Fob:
    fid: str
    cls: str
    team: int
    x: float
    y: float
    builder: str
    build_frame: int
    destroy_frame: Optional[int] = None
    destroyed_by: Optional[str] = None


@dataclass(frozen=True)
class _Spec:
    key: str                    # "A" | "B" | "C" | "D"
    server_id: str
    kind: str                   # finalized | unverified | active
    map_name: str
    layer: str
    mode: str
    factions: tuple[str, str]
    n_full: int                 # InProgress full frames
    tickets_start: tuple[int, int]
    tickets_end: tuple[int, int]
    players: tuple[_Player, ...]
    events: tuple[_Ev, ...] = ()
    vehicles: tuple[_Veh, ...] = ()
    fobs: tuple[_Fob, ...] = ()

    @property
    def match_id(self) -> str:
        return MATCH_IDS[self.key]

    @property
    def start(self) -> datetime:
        return _START[self.key]


def _team1(key: str, name: str, clan: Optional[str], role: str, squad: tuple[int, str],
           path: Waypoints, **kw: Any) -> _Player:
    return _Player(key, name, clan, role, ((0, 1, squad[0], squad[1]),), path, **kw)


def _team2(key: str, name: str, clan: Optional[str], role: str, squad: tuple[int, str],
           path: Waypoints, **kw: Any) -> _Player:
    return _Player(key, name, clan, role, ((0, 2, squad[0], squad[1]),), path, **kw)


# Match A: 8 players, the full event set. Coordinates are UE centimetres,
# roughly inside +-300 m so the replay shows everyone in one screenful.
_A_PLAYERS = (
    _team1("kartal", "Kartal", "ZZ", "USA_SquadLeader_01", (1, "ALPHA"),
           ((0, -28000, -6000), (30, -8000, -2000), (59, -4000, 5000)), is_admin=True),
    _team1("yildiz", "Yıldız", "ZZ", "USA_Medic_01", (1, "ALPHA"),
           ((0, -27000, -4000), (30, -14000, 1000), (59, -9000, 3000))),
    _team1("bora", "Bora", None, "USA_Rifleman_01", (2, "BRAVO"),
           ((0, -26000, 3000), (30, -15000, 4000), (59, -7000, 8000))),
    # The team changer: BRAVO (team 1) until frame 30, then CHARLIE (team 2).
    # He drives the BFV (same waypoints) until frame 28, then walks across.
    _Player("deniz", "Deniz", "ZZ", "USA_Crewman_01",
            ((0, 1, 2, "BRAVO"), (30, 2, 3, "CHARLIE")),
            ((0, -24000, 7000), (29, -9000, 3000), (59, 12000, -4000))),
    _team2("volkan", "Volkan", None, "RGF_SquadLeader_01", (1, "ALPHA"),
           ((0, 26000, -6000), (30, 16000, -2000), (59, 8000, 4000)), is_commander=True),
    _team2("melis", "Melis", None, "RGF_Crewman_01", (1, "ALPHA"),
           ((0, 12000, -5000), (25, -2000, -2000), (59, 6000, 6000))),
    _team2("tarkan", "Tarkan", "TR", "RGF_HAT_01", (2, "BRAVO"),
           ((0, 12000, -5000), (25, -2000, -2000), (34, -6000, -500), (59, -6000, -500))),
    _team2("sevval", "Şevval", None, "RGF_Rifleman_01", (2, "BRAVO"),
           ((0, 24000, 4000), (30, 14000, 3000), (59, 6000, -2000))),
)

_A_EVENTS = (
    _Ev(6, "kill", "kartal", "volkan", "BP_M4A1_C"),
    _Ev(12, "tk", "bora", "yildiz", "BP_M4A1_C"),                  # teamkill
    _Ev(16, "wound", "volkan", "bora", "BP_AK74_C"),
    _Ev(18, "revive", "yildiz", "bora"),                            # revive
    _Ev(20, "objective", "kartal", points=25.5),
    _Ev(22, "suicide", "sevval", "sevval", "BP_M67_Frag_C"),        # suicide
    _Ev(40, "kill", "deniz", "kartal", "BP_M4A1_C"),                # after the team change
    _Ev(44, "kill", "kartal", "tarkan", "BP_M4A1_C"),
    _Ev(46, "objective", "melis", points=15.0),
    _Ev(50, "kill", "volkan", "bora", "BP_AK74_C"),
    _Ev(54, "kill", "melis", "yildiz", "BP_AK74_C"),
)

_A_VEHICLES = (
    _Veh("0x7ff600010000", "BP_BFV_C", "IFV", 1, 1000.0,
         ((0, -24000, 7000), (29, -9000, 3000), (59, -9000, 3000)),
         crew=((0, "deniz", 2, 29),)),
    # Destroyed by Kartal at frame 26 (health 1000 -> 350 -> 0), wreck listed
    # until frame 30, then gone.
    _Veh("0x7ff600020000", "BP_BTR-D_Kord_C", "APC", 2, 1000.0,
         ((0, 12000, -5000), (25, -2000, -2000), (59, -2000, -2000)),
         crew=((0, "melis", 0, 26), (1, "tarkan", 0, 26)),
         destroyed_frame=26, destroyed_by="kartal", gone_frame=30),
)

# Yıldız builds the team-1 FOB at frame 5; Tarkan destroys it at frame 34
# (health 600 -> 200 -> 0, listed until frame 35).
_A_FOBS = (
    _Fob("0x7ff610010000", "BP_FOBRadio_USA_C", 1, -7500.0, -500.0, "yildiz", 5,
         destroy_frame=34, destroyed_by="tarkan"),
)

_SPEC_A = _Spec(
    "A", SERVER_A_ID, "finalized", "Fallujah", "Fallujah_RAAS_v1", "RAAS",
    ("USA", "RGF"), 60, (300, 290), (143, 98),
    _A_PLAYERS, _A_EVENTS, _A_VEHICLES, _A_FOBS)

# Match B: another server, SAME window as A (18:00:00 - 18:09:50 UTC), disjoint
# players, other map, opposite winner (team 2).
_SPEC_B = _Spec(
    "B", SERVER_B_ID, "finalized", "Narva", "Narva_RAAS_v1", "RAAS",
    ("ADF", "INS"), 60, (300, 300), (60, 180),
    (
        _team1("b_aslan", "Aslan", "NV", "ADF_SquadLeader_01", (1, "ALPHA"),
               ((0, -18000, -3000), (30, -8000, 0), (59, -2000, 4000))),
        _team1("b_ceren", "Ceren", "NV", "ADF_Medic_01", (1, "ALPHA"),
               ((0, -17000, 0), (30, -9000, 2000), (59, -3000, 6000))),
        _team1("b_efe", "Efe", None, "ADF_Rifleman_01", (2, "BRAVO"),
               ((0, -16000, 4000), (30, -7000, 5000), (59, -1000, 9000))),
        _team2("b_gizem", "Gizem", None, "INS_SquadLeader_01", (1, "ALPHA"),
               ((0, 18000, -3000), (30, 9000, 0), (59, 2000, 3000))),
        _team2("b_hakan", "Hakan", "NV", "INS_Rifleman_01", (1, "ALPHA"),
               ((0, 17000, 0), (30, 8000, 2000), (59, 1000, 6000))),
        _team2("b_ipek", "İpek", None, "INS_Medic_01", (2, "BRAVO"),
               ((0, 16000, 4000), (30, 7000, 5000), (59, 2000, 8000))),
    ),
    (
        _Ev(8, "kill", "b_gizem", "b_efe", "BP_AK74_C"),
        _Ev(20, "kill", "b_aslan", "b_hakan", "BP_M4A1_C"),
        _Ev(33, "wound", "b_hakan", "b_ceren", "BP_AK74_C"),
        _Ev(36, "revive", "b_ipek", "b_hakan"),
        _Ev(45, "kill", "b_gizem", "b_aslan", "BP_AK74_C"),
    ))

# Match C: same server as A, a later match that is never confirmed to have
# ended (reader stopped mid-match) -> unverified.
_SPEC_C = _Spec(
    "C", SERVER_A_ID, "unverified", "Yehorivka", "Yehorivka_AAS_v1", "AAS",
    ("USA", "RGF"), 20, (250, 250), (210, 190),
    (
        _team1("kartal", "Kartal", "ZZ", "USA_SquadLeader_01", (1, "ALPHA"),
               ((0, -20000, -2000), (19, -10000, 1000)), is_admin=True),
        _team1("yildiz", "Yıldız", "ZZ", "USA_Medic_01", (1, "ALPHA"),
               ((0, -19000, 0), (19, -11000, 2000))),
        _team1("bora", "Bora", None, "USA_Rifleman_01", (2, "BRAVO"),
               ((0, -18000, 3000), (19, -9000, 4000))),
        _team2("volkan", "Volkan", None, "RGF_SquadLeader_01", (1, "ALPHA"),
               ((0, 20000, -2000), (19, 10000, 0)), is_commander=True),
        _team2("melis", "Melis", None, "RGF_Crewman_01", (1, "ALPHA"),
               ((0, 19000, 1000), (19, 9000, 3000))),
        _team2("tarkan", "Tarkan", "TR", "RGF_HAT_01", (2, "BRAVO"),
               ((0, 18000, 4000), (19, 8000, 5000))),
    ),
    (
        _Ev(5, "kill", "kartal", "melis", "BP_M4A1_C"),
        _Ev(9, "wound", "tarkan", "bora", "BP_AK74_C"),
        _Ev(11, "revive", "yildiz", "bora"),
        _Ev(15, "kill", "volkan", "yildiz", "BP_AK74_C"),
    ))

# Recording D: same server, a later match that is STILL running (the recorder
# holds its writer open, the stats row is 'open').
_SPEC_D = _Spec(
    "D", SERVER_A_ID, "active", "Mutrah", "Mutrah_RAAS_v1", "RAAS",
    ("USA", "RGF"), 12, (300, 300), (290, 285),
    (
        _team1("kartal", "Kartal", "ZZ", "USA_SquadLeader_01", (1, "ALPHA"),
               ((0, -22000, -1000), (11, -16000, 500)), is_admin=True),
        _team1("bora", "Bora", None, "USA_Rifleman_01", (2, "BRAVO"),
               ((0, -21000, 2000), (11, -15000, 3000))),
        _team1("yildiz", "Yıldız", "ZZ", "USA_Medic_01", (1, "ALPHA"),
               ((0, -20000, 0), (11, -14000, 1500))),
        _team2("volkan", "Volkan", None, "RGF_SquadLeader_01", (1, "ALPHA"),
               ((0, 22000, -1000), (11, 16000, 500)), is_commander=True),
        _team2("sevval", "Şevval", None, "RGF_Rifleman_01", (2, "BRAVO"),
               ((0, 21000, 2000), (11, 15000, 3000))),
        _team2("tarkan", "Tarkan", "TR", "RGF_HAT_01", (2, "BRAVO"),
               ((0, 20000, 0), (11, 14000, 1500))),
    ),
    (
        _Ev(4, "kill", "bora", "sevval", "BP_M4A1_C"),
        _Ev(8, "kill", "volkan", "yildiz", "BP_AK74_C"),
    ))

_SPECS = {"A": _SPEC_A, "B": _SPEC_B, "C": _SPEC_C, "D": _SPEC_D}


# ---------------------------------------------------------------------------
# Simulation: turns a spec into full / position frames
# ---------------------------------------------------------------------------

def _lerp_path(wp: Waypoints, t: float) -> tuple[float, float]:
    if t <= wp[0][0]:
        return (float(wp[0][1]), float(wp[0][2]))
    for (f0, x0, y0), (f1, x1, y1) in zip(wp, wp[1:], strict=False):
        if t <= f1:
            a = (t - f0) / (f1 - f0)
            return (round(x0 + (x1 - x0) * a, 2), round(y0 + (y1 - y0) * a, 2))
    return (float(wp[-1][1]), float(wp[-1][2]))


def _yaw_path(wp: Waypoints, t: float) -> float:
    """Heading (degrees) of the last non-degenerate segment that started at or before t."""
    yaw = 0.0
    for (f0, x0, y0), (_f1, x1, y1) in zip(wp, wp[1:], strict=False):
        if f0 > t:
            break
        if (x1, y1) != (x0, y0):
            yaw = round(math.degrees(math.atan2(y1 - y0, x1 - x0)) % 360.0, 1)
    return yaw


def _counters() -> dict[str, Any]:
    return {"kills": 0, "deaths": 0, "woundeds": 0, "wounds": 0, "teamKills": 0,
            "vehicleKills": 0, "revivedPoints": 0.0, "objectiveScore": 0.0,
            "fobsBuilt": 0, "fobsDestroyed": 0}


class _Sim:
    def __init__(self, spec: _Spec) -> None:
        self.spec = spec
        self.by_key = {p.key: p for p in spec.players}
        self.index = {p.key: i for i, p in enumerate(spec.players)}
        self.c = {p.key: _counters() for p in spec.players}
        self.applied = -1
        self.log: list[dict[str, Any]] = []     # observable event log (for the manifest)

    # -- time -----------------------------------------------------------------
    def ts(self, idx: float) -> datetime:
        return self.spec.start + timedelta(seconds=idx * FULL_FRAME_SEC)

    # -- team / squad ---------------------------------------------------------
    def membership(self, key: str, f: int) -> tuple[int, int, str]:
        cur = self.by_key[key].sched[0]
        for entry in self.by_key[key].sched:
            if entry[0] <= f:
                cur = entry
        return cur[1], cur[2], cur[3]

    def team_of(self, key: str, f: int) -> int:
        return self.membership(key, f)[0]

    # -- life state -----------------------------------------------------------
    def life(self, key: str, f: int) -> str:
        """alive | down | dead | switching  (for full frame `f`)."""
        for start, *_ in self.by_key[key].sched[1:]:
            if start <= f < start + 2:
                return "switching"
        for ev in self.spec.events:
            dies = (ev.kind in ("kill", "tk") and ev.target == key) or (
                ev.kind == "suicide" and ev.actor == key)
            if dies and ev.frame + 1 <= f <= ev.frame + 3:
                return "dead"
            if ev.kind == "wound" and ev.target == key and f >= ev.frame:
                rev = [r.frame for r in self.spec.events
                       if r.kind == "revive" and r.target == key and r.frame > ev.frame]
                if not rev or f < min(rev):
                    return "down"
        return "alive"

    def in_vehicle(self, key: str, f: int) -> Optional[tuple[_Veh, int]]:
        for veh in self.spec.vehicles:
            for seat, pk, f0, f1 in veh.crew:
                if pk == key and f0 <= f < f1:
                    return veh, seat
        return None

    # -- events ---------------------------------------------------------------
    def advance(self, f: int) -> list[dict[str, Any]]:
        """Apply everything scheduled at frame f (once) and return its damageEvents."""
        assert f == self.applied + 1, "frames must be applied in order"
        self.applied = f
        ts_epoch = round(self.ts(f).timestamp() + 0.5, 3)
        events: list[dict[str, Any]] = []

        for fob in self.spec.fobs:
            if fob.build_frame == f:
                self.c[fob.builder]["fobsBuilt"] += 1
                self._note(f, "fob_built", fob.builder, None, fob.cls)
            if fob.destroy_frame == f and fob.destroyed_by:
                self.c[fob.destroyed_by]["fobsDestroyed"] += 1
                self._note(f, "fob_destroyed", fob.destroyed_by, None, fob.cls)
        for veh in self.spec.vehicles:
            if veh.destroyed_frame == f and veh.destroyed_by:
                self.c[veh.destroyed_by]["vehicleKills"] += 1
                self._note(f, "vehicle_destroyed", veh.destroyed_by, None, veh.cls)

        for ev in self.spec.events:
            if ev.frame != f:
                continue
            a_team = self.team_of(ev.actor, f)
            if ev.kind == "kill":
                if self.team_of(ev.target, f) == a_team:
                    raise ValueError(f"kill {ev} is between teammates; use 'tk'")
                self.c[ev.actor]["kills"] += 1
                self.c[ev.target]["deaths"] += 1
                events.append(self._dmg(ev, ts_epoch, killed=True, wounded=False))
            elif ev.kind == "tk":
                if self.team_of(ev.target, f) != a_team:
                    raise ValueError(f"tk {ev} is between enemies")
                self.c[ev.actor]["teamKills"] += 1
                self.c[ev.target]["deaths"] += 1
                events.append(self._dmg(ev, ts_epoch, killed=True, wounded=False))
            elif ev.kind == "wound":
                self.c[ev.actor]["woundeds"] += 1
                self.c[ev.target]["wounds"] += 1
                events.append(self._dmg(ev, ts_epoch, killed=False, wounded=True))
            elif ev.kind == "revive":
                self.c[ev.actor]["revivedPoints"] += 1.0
            elif ev.kind == "suicide":
                self.c[ev.actor]["deaths"] += 1
                events.append({
                    "victim": self.by_key[ev.actor].name, "victimEosId": None,
                    "victimTeam": None, "victimSoldier": None, "victimPos": None,
                    "attacker": None, "attackerEosId": None, "attackerCtrlAddr": None,
                    "selfInflicted": True, "causerWeapon": ev.weapon, "causerClass": None,
                    "damageType": None, "hitDistance": None, "headshot": None,
                    "wounded": False, "killed": True, "ts": ts_epoch})
            elif ev.kind == "objective":
                self.c[ev.actor]["objectiveScore"] += ev.points
            else:
                raise ValueError(f"unknown event kind {ev.kind!r}")
            self._note(f, ev.kind, ev.actor, ev.target, ev.weapon)
        return events

    def _dmg(self, ev: _Ev, ts_epoch: float, *, killed: bool, wounded: bool) -> dict[str, Any]:
        actor = self.by_key[ev.actor]
        target = self.by_key[ev.target]
        x, y = _lerp_path(target.path, ev.frame)
        return {
            "victim": target.name, "victimEosId": None, "victimTeam": None,
            "victimSoldier": self._addr("soldier", target.key),
            "victimPos": {"x": x, "y": y, "z": self._z(y)},
            "attacker": actor.name, "attackerEosId": eos_id(actor.key),
            "attackerCtrlAddr": self._addr("ctrl", actor.key),
            "selfInflicted": False, "causerWeapon": ev.weapon, "causerClass": None,
            "damageType": None, "hitDistance": None, "headshot": None,
            "wounded": wounded, "killed": killed, "ts": ts_epoch}

    def _note(self, f: int, kind: str, actor: str, target: Optional[str],
              detail: Optional[str]) -> None:
        self.log.append({
            "frame": f, "timestampUtc": self.ts(f).isoformat(), "kind": kind,
            "actor": actor, "actorName": self.by_key[actor].name,
            "actorEosId": eos_id(actor),
            "target": target,
            "targetName": self.by_key[target].name if target else None,
            "targetEosId": eos_id(target) if target else None,
            "detail": detail})

    # -- addresses / misc -------------------------------------------------------
    def _addr(self, what: str, key: str) -> str:
        base = {"ps": 0x1F0000000000, "soldier": 0x2F0000000000, "ctrl": 0x3F0000000000,
                "squad": 0x4F0000000000, "team": 0x5F0000000000}[what]
        per_match = (ord(self.spec.key) - ord("A")) * 0x100000     # keep addresses distinct
        return f"0x{base + per_match + self.index[key] * 0x1000:x}"

    @staticmethod
    def _z(y: float) -> float:
        return round(100.0 + y / 1000.0, 2)

    # -- frame builders --------------------------------------------------------
    def _stat_block(self, key: str) -> tuple[dict[str, Any], float]:
        c = self.c[key]
        p = self.by_key[key]
        combat = (50.0 * c["kills"] + 20.0 * c["woundeds"] + 100.0 * c["vehicleKills"]
                  + 150.0 * c["fobsDestroyed"] - 25.0 * c["teamKills"])
        teamwork = 40.0 * c["revivedPoints"] + 30.0 * c["fobsBuilt"]
        return {
            "lives": None, "kills": c["kills"], "vehicleKills": c["vehicleKills"],
            "deaths": c["deaths"], "woundeds": c["woundeds"], "wounds": c["wounds"],
            "teamKills": c["teamKills"], "healPoints": 0.0,
            "revivedPoints": c["revivedPoints"], "teamWorkScore": teamwork,
            "objectiveScore": c["objectiveScore"], "combatScore": combat,
            "isAdmin": p.is_admin, "isCommander": p.is_commander,
            "fireTeamIndex": 0, "fireTeamPosition": 0,
            "suppliesDelivered": 0, "fobsBuilt": c["fobsBuilt"],
            "fobsDestroyed": c["fobsDestroyed"], "vehicleDamage": 1000.0 * c["vehicleKills"],
            "captures": 0, "defenses": 0,
        }, combat + teamwork + c["objectiveScore"]

    def _veh_state(self, veh: _Veh, f: int) -> Optional[tuple[float, bool]]:
        """(health, is_wreck) at full frame f, or None when the wreck is gone."""
        if veh.destroyed_frame is not None:
            if veh.gone_frame is not None and f >= veh.gone_frame:
                return None
            if f >= veh.destroyed_frame:
                return 0.0, True
            if f == veh.destroyed_frame - 1:
                return round(veh.max_hp * 0.35, 1), False
        return veh.max_hp, False

    def full_frame(self, idx: int, tick: int, state: str) -> dict[str, Any]:
        spec = self.spec
        f = min(idx, spec.n_full - 1)          # post-match frames repeat the last content
        players: list[dict[str, Any]] = []
        squads: dict[tuple[int, int], dict[str, Any]] = {}
        for i, p in enumerate(spec.players):
            team, squad_id, squad_name = self.membership(p.key, f)
            life = self.life(p.key, f)
            stats, score = self._stat_block(p.key)
            soldier: Optional[dict[str, Any]] = None
            if life in ("alive", "down"):
                x, y = _lerp_path(p.path, f)
                seat = self.in_vehicle(p.key, f)
                down = life == "down"
                soldier = {
                    "addr": self._addr("soldier", p.key),
                    "classShort": "BP_Soldier_" + p.role.split("_")[0] + "_C",
                    "health": 15.0 if down else 100.0,
                    "breathHoldStamina": 100.0, "stamina": 100.0, "staminaMax": 100.0,
                    "stance": "prone" if down else "standing",
                    "position": {"x": x, "y": y, "z": self._z(y)},
                    "yaw": _yaw_path(p.path, f), "attached": seat is not None,
                    "isWounded": down, "isBleeding": down, "isDying": False,
                    "lifeState": "incapacitated" if down else "healthy",
                    "weapon": {"className": "BP_M4A1_C", "name": "BP_M4A1_C_2147480001"},
                }
            players.append({
                "_addr": self._addr("ps", p.key), "name": p.name, "eosId": eos_id(p.key),
                "playerId": 100 + i, "teamId": team, "roleId": p.role, "score": score,
                "ping": 35 + i, "isBot": False, "clanTag": p.clan,
                "squadStateAddr": self._addr("squad", p.key),
                "teamStateAddr": self._addr("team", p.key),
                "squadId": squad_id, "squadName": squad_name, "squadTeamId": team,
                "soldier": soldier, "stats": stats, "voiceChannel": "none",
            })
            sq = squads.setdefault((team, squad_id), {
                "_addr": self._addr("squad", p.key), "id": squad_id, "teamId": team,
                "maxSize": 9, "name": squad_name, "kills": 0, "deaths": 0, "woundeds": 0,
                "score": 0.0, "teamWorkScore": 0.0, "objectiveScore": 0.0,
                "combatScore": 0.0, "revivedPoints": 0.0, "creationTime": 0.0,
                "playerCount": 0, "leaderStateAddr": self._addr("ps", p.key),
                "isLocked": False, "creatorName": p.name,
                "creatorOnlineId": eos_id(p.key)})
            sq["playerCount"] += 1
            sq["kills"] += stats["kills"]
            sq["deaths"] += stats["deaths"]
            sq["woundeds"] += stats["woundeds"]
            sq["revivedPoints"] += stats["revivedPoints"]

        # Linear ticket bleed over the InProgress frames.
        span = max(1, spec.n_full - 1)
        tickets = [round(a + (b - a) * f / span)
                   for a, b in zip(spec.tickets_start, spec.tickets_end, strict=True)]
        teams = []
        for tid in (1, 2):
            members = [pl for pl in players if pl["teamId"] == tid]
            teams.append({
                "_addr": f"0x6f0000000{tid}00", "id": tid, "tickets": tickets[tid - 1],
                "score": None, "objectiveScore": None,
                "kills": sum(m["stats"]["kills"] for m in members),
                "deaths": sum(m["stats"]["deaths"] for m in members),
                "woundeds": sum(m["stats"]["woundeds"] for m in members),
                "factionId": spec.factions[tid - 1], "playerCount": len(members),
                "squadCount": len([s for s in squads.values() if s["teamId"] == tid]),
                "vehicleSlotCount": None, "deployableSlotCount": None, "roleSlotCount": None})

        vehicles = []
        for veh in spec.vehicles:
            vs = self._veh_state(veh, f)
            if vs is None:
                continue
            hp, wreck = vs
            x, y = _lerp_path(veh.path, f)
            seats = []
            for seat, pk, f0, f1 in veh.crew:
                if f0 <= f < f1 and self.life(pk, f) in ("alive", "down"):
                    seats.append({
                        "idx": seat, "addr": f"0x7ff6{seat:04x}0000",
                        "occupantName": self.by_key[pk].name,
                        "occupantEosId": eos_id(pk),
                        "occupantPlayerId": 100 + self.index[pk],
                        "occupantTeamId": veh.team,
                        "playerStateAddr": self._addr("ps", pk)})
            entry: dict[str, Any] = {
                "id": veh.vid, "classShort": veh.cls, "team": veh.team, "health": hp,
                "maxHealth": veh.max_hp, "position": {"x": x, "y": y, "z": self._z(y)},
                "yaw": _yaw_path(veh.path, f), "attached": False, "kind": veh.kind,
                "seats": seats}
            if wreck and veh.destroyed_by:
                entry["lastDamager"] = {"addr": self._addr("ctrl", veh.destroyed_by),
                                        "name": self.by_key[veh.destroyed_by].name,
                                        "class": "BP_PlayerController_C"}
            vehicles.append(entry)

        deployables = []
        for fob in spec.fobs:
            if f < fob.build_frame:
                continue
            hp = 1000.0
            if fob.destroy_frame is not None:
                if f > fob.destroy_frame:
                    continue
                hp = {fob.destroy_frame: 0.0, fob.destroy_frame - 1: 200.0,
                      fob.destroy_frame - 2: 600.0}.get(f, 1000.0)
            builder = self.by_key[fob.builder]
            deployables.append({
                "id": fob.fid, "classShort": fob.cls, "actorName": fob.cls + "_2147480001",
                "team": fob.team, "initialTeam": fob.team, "isFob": True, "isPlaced": True,
                "buildState": 2, "cost": None, "health": hp, "maxHealth": 1000.0,
                "initialHealth": 1000.0, "owningFobAddr": None,
                "position": {"x": fob.x, "y": fob.y, "z": self._z(fob.y)}, "yaw": 0.0,
                "placer": builder.name, "placerEosId": eos_id(builder.key)})

        ts = self.ts(idx)
        elapsed = idx * FULL_FRAME_SEC
        return {
            "timestamp": ts.isoformat(),
            "server": spec.server_id,
            "schemaVersion": "phase3-draft",
            "tick": tick,
            "counts": {"playerStatesNonCDO": len(players),
                       "soldiersLive": sum(1 for p in players if p["soldier"]),
                       "vehicleSeatsLive": sum(len(v["seats"]) for v in vehicles),
                       "totalUObjects": 400000},
            "gameState": {
                "_addr": "0x7f0000001000", "serverName": f"ZZ Fixture {spec.server_id}",
                "maxPlayers": 100, "tickRate": 30.0, "matchId": spec.match_id,
                "matchState": state, "elapsedSec": elapsed, "isTicketBased": True,
                "gameModeId": spec.mode, "numTeams": 2, "timeOfCompletion": None,
                "serverStartTimestamp": _SERVER_START_TS,
                "worldTimeSec": 5000.0 + elapsed, "mapName": spec.map_name,
                "gameModeName": spec.mode, "layer": {"name": spec.layer}},
            "teams": teams,
            "squads": list(squads.values()),
            "players": players,
            "vehicles": vehicles,
            "captureZones": [], "markers": [], "deployables": deployables,
            "vehicleSpawners": [], "rallyPoints": [], "projectiles": [],
            "damageEvents": [],
        }

    def pos_frame(self, full_idx: int, k: int, tick: int, full_tick: int) -> dict[str, Any]:
        """The `k`-th (1-based) position frame after full frame `full_idx`."""
        t = full_idx + k / (POS_FRAMES_PER_FULL + 1)
        f = full_idx
        ts = self.ts(t)
        players = []
        for p in self.spec.players:
            if self.life(p.key, f) not in ("alive", "down"):
                continue            # dead / switching: no soldier to sample
            x, y = _lerp_path(p.path, t)
            players.append({"id": eos_id(p.key), "x": x, "y": y, "z": self._z(y),
                            "h": 15.0 if self.life(p.key, f) == "down" else 100.0,
                            "yaw": _yaw_path(p.path, t)})
        vehicles = []
        for veh in self.spec.vehicles:
            vs = self._veh_state(veh, f)
            if vs is None:
                continue
            x, y = _lerp_path(veh.path, t)
            vehicles.append({"id": veh.vid, "x": x, "y": y, "z": self._z(y), "h": vs[0],
                             "yaw": _yaw_path(veh.path, t), "team": veh.team})
        return {"t": "pos", "tick": tick, "timestamp": ts.isoformat(),
                "fullTick": full_tick, "players": players, "vehicles": vehicles}


# ---------------------------------------------------------------------------
# Driving the reader's real code paths
# ---------------------------------------------------------------------------

class _Clock:
    """Stands in for wall-clock reads inside recorder / sqrx while building."""

    def __init__(self) -> None:
        self.now = _START["A"]

    def utcnow(self) -> datetime:
        return self.now

    def time(self) -> float:
        return self.now.timestamp()


def _new_state_box() -> dict[str, Any]:
    return {"current": None, "last_state": None, "inactive_ticks": 0,
            "pending_match_id": None, "pending_match_buffer": [], "last_tick": None,
            "tick_sequence_required": False, "missing_tick_warned": False}


class _ServerRun:
    def __init__(self, root: Path, server_id: str, clock: _Clock) -> None:
        self.server_id = server_id
        self.clock = clock
        self.dir = root / server_id
        self.rec_dir = self.dir / "recordings"
        self.rec_dir.mkdir(parents=True)
        self.db_path = self.dir / "stats.db"
        self.tick = 0
        self.store: Optional[StatsStore] = None
        self.box = _new_state_box()
        self.filename_buffer: list = []

    def _next_tick(self) -> int:
        self.tick += 1
        return self.tick

    def _feed_full(self, snap: dict[str, Any]) -> None:
        assert self.store is not None
        self.clock.now = datetime.fromisoformat(snap["timestamp"])
        self.store.record_tick(snap)
        _recorder._handle_snap(
            snap=snap, raw_line=json.dumps(snap, ensure_ascii=False),
            state_box=self.box, out_dir=self.rec_dir, server_id=self.server_id,
            min_ticks=0, filename_buffer=self.filename_buffer)

    def play(self, spec: _Spec) -> dict[str, Any]:
        sim = _Sim(spec)
        self.store = StatsStore(self.db_path, server_id=self.server_id)
        self.box = _new_state_box()
        self.filename_buffer = []
        pos_tick = 0
        for f in range(spec.n_full):
            events = sim.advance(f)
            full_tick = self._next_tick()
            snap = sim.full_frame(f, full_tick, "InProgress")
            snap["damageEvents"] = events
            self._feed_full(snap)
            if f < spec.n_full - 1:
                for k in range(1, POS_FRAMES_PER_FULL + 1):
                    pos_tick += 1
                    pos = sim.pos_frame(f, k, pos_tick, full_tick)
                    self.clock.now = datetime.fromisoformat(pos["timestamp"])
                    _recorder.write_position_frame(
                        self.box, json.dumps(pos, ensure_ascii=False) + "\n")

        current = self.box["current"]
        assert current is not None, "recorder never opened a recording"
        recording_id = current.path.stem

        if spec.kind == "finalized":
            # The end the game reports: MATCH_TRANSITION_CONFIRM_TICKS consecutive
            # WaitingPostMatch frames -> both writers finalize on their own.
            for j in range(MATCH_TRANSITION_CONFIRM_TICKS):
                self._feed_full(sim.full_frame(spec.n_full + j, self._next_tick(),
                                               "WaitingPostMatch"))
            assert self.box["current"] is None
        elif spec.kind == "unverified":
            # cli.py's stop path: nothing proved the match ended.
            _recorder.finalize_recording(current, reason="serve shutdown")
            self.store.finalize_open_match(reason="serve shutdown")
            self.box["current"] = None
        elif spec.kind == "active":
            # The process goes away while the match is running: the writer is
            # closed by the OS, the sidecar still says "active", the stats row
            # stays 'open'. (Closing the store connection does not touch rows.)
            current.writer.close()
            self.box["current"] = None
        else:
            raise ValueError(spec.kind)
        self.store.close()
        self.store = None
        return {"recording_id": recording_id, "events": sim.log,
                "final_counters": {eos_id(k): dict(v) for k, v in sim.c.items()}}


def _describe(spec: _Spec, run: _ServerRun, info: dict[str, Any], root: Path) -> dict[str, Any]:
    rec = info["recording_id"]
    sqrx_path = run.rec_dir / f"{rec}.sqrx"
    meta_path = run.rec_dir / f"{rec}.meta.json"
    end = spec.start + timedelta(seconds=(spec.n_full - 1) * FULL_FRAME_SEC)
    return {
        "key": spec.key,
        "serverId": spec.server_id,
        "matchId": spec.match_id,
        "kind": spec.kind,
        "expectedEndState": {"finalized": "finalized", "unverified": "unverified",
                             "active": None}[spec.kind],
        "expectedStatus": "open" if spec.kind == "active" else "final",
        "expectedRecordingState": {"finalized": "finalized", "unverified": "unverified",
                                   "active": "active"}[spec.kind],
        "mapName": spec.map_name, "layerName": spec.layer, "gameMode": spec.mode,
        "factions": list(spec.factions),
        "startedAtUtc": spec.start.isoformat(),
        "lastInProgressFrameUtc": end.isoformat(),
        "fullFrames": spec.n_full,
        "ticketsEnd": list(spec.tickets_end),
        "expectedWinnerTeam": (
            (1 if spec.tickets_end[0] > spec.tickets_end[1] else 2)
            if spec.kind == "finalized" else None),
        "recordingId": rec,
        "recordingFile": sqrx_path.relative_to(root).as_posix(),
        "metaFile": meta_path.relative_to(root).as_posix(),
        "players": [{"key": p.key, "name": p.name, "eosId": eos_id(p.key),
                     "clanTag": p.clan, "initialTeam": p.sched[0][1]}
                    for p in spec.players],
        "events": info["events"],
        "finalCounters": info["final_counters"],
    }


@dataclass
class FixtureMatch:
    key: str
    server_id: str
    match_id: str
    kind: str                 # finalized | unverified | active
    recording_id: str
    sqrx_path: Path
    meta_path: Path
    info: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class FixtureResult:
    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    server_dirs: dict[str, Path]       # server_id -> data dir
    stats_dbs: dict[str, Path]         # server_id -> stats.db
    recording_dirs: dict[str, Path]    # server_id -> recordings dir
    matches: dict[str, FixtureMatch]   # "A" | "B" | "C" | "D"


def build_zz_fixture(out_dir: str | Path) -> FixtureResult:
    """Write the fixture under `out_dir` (must not exist or must be empty).

    Returns paths and the manifest (also written as `fixture-manifest.json`).
    Two calls with the same arguments produce identical `.sqrx`, `.meta.json`
    and manifest bytes, and identical `stats.db` table contents.
    """
    root = Path(out_dir)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"{root} is not empty; refusing to mix fixture data")
    root.mkdir(parents=True, exist_ok=True)

    clock = _Clock()
    described: dict[str, dict[str, Any]] = {}
    runs: dict[str, _ServerRun] = {}
    order = ("A", "C", "D", "B")           # per server: A, C, D in time order; B on its own
    with mock.patch.object(_recorder, "_utc_now", clock.utcnow), \
            mock.patch.object(_sqrx, "time", SimpleNamespace(time=clock.time)):
        for key in order:
            spec = _SPECS[key]
            run = runs.get(spec.server_id)
            if run is None:
                run = runs[spec.server_id] = _ServerRun(root, spec.server_id, clock)
            clock.now = spec.start
            described[key] = _describe(spec, run, run.play(spec), root)

    manifest = {
        "schema": FIXTURE_SCHEMA,
        "fullFrameSec": FULL_FRAME_SEC,
        "positionFramesPerFullFrame": POS_FRAMES_PER_FULL,
        "servers": {sid: {"dataDir": sid, "statsDb": f"{sid}/stats.db",
                          "recordingsDir": f"{sid}/recordings"} for sid in runs},
        "matches": {k: described[k] for k in sorted(described)},
    }
    manifest_path = root / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")

    matches = {}
    for key, d in described.items():
        run = runs[d["serverId"]]
        matches[key] = FixtureMatch(
            key=key, server_id=d["serverId"], match_id=d["matchId"], kind=d["kind"],
            recording_id=d["recordingId"],
            sqrx_path=run.rec_dir / f"{d['recordingId']}.sqrx",
            meta_path=run.rec_dir / f"{d['recordingId']}.meta.json", info=d)
    return FixtureResult(
        root=root, manifest_path=manifest_path, manifest=manifest,
        server_dirs={sid: r.dir for sid, r in runs.items()},
        stats_dbs={sid: r.db_path for sid, r in runs.items()},
        recording_dirs={sid: r.rec_dir for sid, r in runs.items()},
        matches=matches)
