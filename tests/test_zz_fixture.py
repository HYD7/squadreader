"""The Zero Zone integration fixture (tests/zz_fixture.py).

Checks that the generated dataset really contains what the integration tests
will rely on, and that it got there through the reader's real code paths.

What is NOT asserted, and why:
* A dedicated "vehicle destroyed" / "FOB destroyed" event does not exist in the
  reader. Those two are asserted through what the reader does record: the
  per-player counters (`vehicle_kills`, `fobs_destroyed`) in stats.db and the
  entity's health reaching 0 and then disappearing in the replay frames.
* Team changes are not tracked by stats.db (only the last `team_id`); they are
  asserted on the replay frames, and the last-team behaviour is pinned below.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from zz_fixture import (
    MATCH_IDS,
    SERVER_A_ID,
    SERVER_B_ID,
    build_zz_fixture,
    eos_id,
)

from sqreader.ingest_client import build_match_bundle
from sqreader.recorder import extract_metadata
from sqreader.sqrx import SqrxReader

REPO = Path(__file__).resolve().parent.parent
HEX32 = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    return build_zz_fixture(tmp_path_factory.mktemp("zzfx") / "out")


def _rows(db: Path, sql: str, params=()):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _lines(path: Path) -> list[dict]:
    with SqrxReader(path) as r:
        return [json.loads(line) for line in r]


def _full(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("t") != "pos"]


def _pos(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("t") == "pos"]


def _player(frame: dict, name: str) -> dict:
    return next(p for p in frame["players"] if p["name"] == name)


# -- layout / isolation -------------------------------------------------------

def test_layout_two_separate_server_dirs(fx):
    assert set(fx.server_dirs) == {SERVER_A_ID, SERVER_B_ID}
    assert fx.server_dirs[SERVER_A_ID] != fx.server_dirs[SERVER_B_ID]
    for sid in (SERVER_A_ID, SERVER_B_ID):
        assert fx.stats_dbs[sid].is_file()
        assert fx.stats_dbs[sid].parent == fx.server_dirs[sid]
        assert fx.recording_dirs[sid].parent == fx.server_dirs[sid]
    assert fx.manifest_path.is_file()
    assert json.loads(fx.manifest_path.read_text(encoding="utf-8")) == fx.manifest
    # the sqlite side files of a cleanly closed WAL database are gone
    assert not list(fx.root.rglob("*-wal")) and not list(fx.root.rglob("*-shm"))


def test_servers_data_never_mixes(fx):
    db_a, db_b = fx.stats_dbs[SERVER_A_ID], fx.stats_dbs[SERVER_B_ID]
    m_a = _rows(db_a, "SELECT match_id, server_id FROM matches ORDER BY started_at")
    m_b = _rows(db_b, "SELECT match_id, server_id FROM matches")
    assert [r["match_id"] for r in m_a] == [MATCH_IDS["A"], MATCH_IDS["C"], MATCH_IDS["D"]]
    assert {r["server_id"] for r in m_a} == {SERVER_A_ID}
    assert [r["match_id"] for r in m_b] == [MATCH_IDS["B"]]
    assert {r["server_id"] for r in m_b} == {SERVER_B_ID}

    for db, own in ((db_a, MATCH_IDS.values()), (db_b, [MATCH_IDS["B"]])):
        for table in ("player_matches", "kill_events", "vehicle_session"):
            ids = {r["match_id"] for r in _rows(db, f"SELECT DISTINCT match_id FROM {table}")}
            assert ids <= set(own)
    # no player is shared between the servers
    eos_a = {r["eos_id"] for r in _rows(db_a, "SELECT eos_id FROM player_matches")}
    eos_b = {r["eos_id"] for r in _rows(db_b, "SELECT eos_id FROM player_matches")}
    assert eos_a and eos_b and not (eos_a & eos_b)

    for sid, dirpath in fx.recording_dirs.items():
        sqrx = sorted(dirpath.glob("*.sqrx"))
        assert sqrx, sid
        for path in sqrx:
            with SqrxReader(path) as r:
                assert r.server_id == sid
            meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            assert meta["serverId"] == sid
    assert len(list(fx.recording_dirs[SERVER_A_ID].glob("*.sqrx"))) == 3
    assert len(list(fx.recording_dirs[SERVER_B_ID].glob("*.sqrx"))) == 1


def test_eos_ids_are_fixed_32_hex(fx):
    all_eos = set()
    for sid in (SERVER_A_ID, SERVER_B_ID):
        all_eos |= {r["eos_id"] for r in _rows(fx.stats_dbs[sid], "SELECT eos_id FROM players")}
    assert all_eos and all(HEX32.match(e) for e in all_eos)
    assert eos_id("kartal") == "bff991fa2e29062630f5cfec11158269"      # pinned: stable across runs
    for m in fx.manifest["matches"].values():
        for p in m["players"]:
            assert HEX32.match(p["eosId"]) and p["eosId"] == eos_id(p["key"])


# -- end states ---------------------------------------------------------------

def _match_row(fx, key: str) -> dict:
    m = fx.matches[key]
    return _rows(fx.stats_dbs[m.server_id], "SELECT * FROM matches WHERE match_id=?",
                 (m.match_id,))[0]


def _meta(fx, key: str) -> dict:
    return json.loads(fx.matches[key].meta_path.read_text(encoding="utf-8"))


def test_a_and_b_finalized_with_confirmed_end(fx):
    for key, winner in (("A", 1), ("B", 2)):
        row = _match_row(fx, key)
        assert row["status"] == "final" and row["end_state"] == "finalized"
        assert row["winner_team"] == winner
        meta = _meta(fx, key)
        assert meta["recordingState"] == "finalized" and meta["inProgress"] is False
        assert meta["matchId"] == fx.matches[key].match_id


def test_c_unverified(fx):
    row = _match_row(fx, "C")
    assert row["status"] == "final" and row["end_state"] == "unverified"
    assert row["winner_team"] is None            # never publish an unobserved result
    meta = _meta(fx, "C")
    assert meta["recordingState"] == "unverified" and meta["inProgress"] is True


def test_d_still_active(fx):
    row = _match_row(fx, "D")
    assert row["status"] == "open" and row["end_state"] is None
    assert row["winner_team"] is None
    meta = _meta(fx, "D")
    assert meta["recordingState"] == "active" and meta["inProgress"] is True
    assert meta["endedAtUtc"] is None
    # the writer was closed but the recording still holds frames of a live match
    frames = _full(_lines(fx.matches["D"].sqrx_path))
    assert len(frames) == 12
    assert all(f["gameState"]["matchState"] == "InProgress" for f in frames)


def test_a_and_b_share_a_time_window_on_different_servers(fx):
    a, b = _match_row(fx, "A"), _match_row(fx, "B")
    assert a["server_id"] != b["server_id"]
    assert (a["started_at"], a["ended_at"]) == (b["started_at"], b["ended_at"])
    ma, mb = _meta(fx, "A"), _meta(fx, "B")
    assert (ma["startedAtUtc"], ma["endedAtUtc"]) == (mb["startedAtUtc"], mb["endedAtUtc"])
    assert ma["startedAtUtc"].startswith("2026-09-01T18:00:00")
    assert a["map_name"] != b["map_name"]


# -- match A content ----------------------------------------------------------

def _pm(fx, name: str) -> dict:
    return _rows(fx.stats_dbs[SERVER_A_ID],
                 "SELECT * FROM player_matches WHERE match_id=? AND name=?",
                 (MATCH_IDS["A"], name))[0]


def test_a_roster_two_teams_at_least_six_players(fx):
    rows = _rows(fx.stats_dbs[SERVER_A_ID],
                 "SELECT team_id, name FROM player_matches WHERE match_id=?", (MATCH_IDS["A"],))
    assert len(rows) >= 6
    assert {r["team_id"] for r in rows} == {1, 2}
    assert _match_row(fx, "A")["peak_players"] == 8


def test_a_team_change_is_visible_in_frames(fx):
    frames = _full(_lines(fx.matches["A"].sqrx_path))
    teams = [_player(f, "Deniz")["teamId"] for f in frames]
    assert teams[0] == 1 and teams[-1] == 2
    assert teams.count(1) > 5 and teams.count(2) > 5
    first_t2 = teams.index(2)
    assert all(t == 2 for t in teams[first_t2:])              # switches once, stays
    squads = [(_player(f, "Deniz")["squadTeamId"], _player(f, "Deniz")["squadName"])
              for f in (frames[0], frames[-1])]
    assert squads == [(1, "BRAVO"), (2, "CHARLIE")]
    # stats.db keeps only the last observed team (no per-segment tracking)
    assert _pm(fx, "Deniz")["team_id"] == 2


def test_a_teamkill(fx):
    row = _rows(fx.stats_dbs[SERVER_A_ID],
                "SELECT * FROM kill_events WHERE match_id=? AND team_kill=1", (MATCH_IDS["A"],))
    assert len(row) == 1
    ev = row[0]
    assert (ev["attacker_name"], ev["victim_name"]) == ("Bora", "Yıldız")
    assert ev["attacker_team"] == ev["victim_team"] == 1
    assert ev["killed"] == 1 and ev["self_inflicted"] == 0
    assert ev["attacker_eos"] == eos_id("bora") and ev["victim_eos"] == eos_id("yildiz")
    assert _pm(fx, "Bora")["team_kills"] == 1
    assert _pm(fx, "Yıldız")["deaths"] == 2


def test_a_suicide(fx):
    rows = _rows(fx.stats_dbs[SERVER_A_ID],
                 "SELECT * FROM kill_events WHERE match_id=? AND self_inflicted=1",
                 (MATCH_IDS["A"],))
    assert len(rows) == 1
    ev = rows[0]
    assert ev["victim_name"] == "Şevval" and ev["victim_eos"] == eos_id("sevval")
    assert ev["attacker_eos"] is None and ev["team_kill"] == 0 and ev["killed"] == 1
    assert _pm(fx, "Şevval")["deaths"] == 1


def test_a_revive(fx):
    assert _pm(fx, "Yıldız")["revived_points"] == 1.0
    wounds = _rows(fx.stats_dbs[SERVER_A_ID],
                   "SELECT * FROM kill_events WHERE match_id=? AND wounded=1 AND victim_name='Bora'",
                   (MATCH_IDS["A"],))
    assert len(wounds) == 1 and wounds[0]["attacker_name"] == "Volkan"
    assert _pm(fx, "Volkan")["woundeds"] == 1 and _pm(fx, "Bora")["wounds"] == 1
    # frames: Bora goes down, then is up again after the revive, and never died in between
    states = [_player(f, "Bora")["soldier"]["lifeState"]
              if _player(f, "Bora")["soldier"] else None for f in _full(_lines(fx.matches["A"].sqrx_path))]
    down = [i for i, s in enumerate(states) if s == "incapacitated"]
    assert down and states[down[0] - 1] == "healthy" and states[down[-1] + 1] == "healthy"
    assert None not in states[down[0]:down[-1] + 2]


def test_a_vehicle_destroyed(fx):
    assert _pm(fx, "Kartal")["vehicle_kills"] == 1
    frames = _full(_lines(fx.matches["A"].sqrx_path))
    hp = [next((v["health"] for v in f["vehicles"] if v["classShort"] == "BP_BTR-D_Kord_C"), None)
          for f in frames]
    assert hp[0] == 1000.0
    dead_at = hp.index(0.0)
    assert 0 < hp[dead_at - 1] < 1000.0            # damaged, then destroyed
    assert hp[-1] is None                          # wreck removed later
    wreck = next(v for v in frames[dead_at]["vehicles"] if v["classShort"] == "BP_BTR-D_Kord_C")
    assert wreck["seats"] == [] and wreck["lastDamager"]["name"] == "Kartal"
    sessions = _rows(fx.stats_dbs[SERVER_A_ID],
                     "SELECT * FROM vehicle_session WHERE match_id=?", (MATCH_IDS["A"],))
    assert {s["vehicle_class"] for s in sessions} == {"BP_BTR-D_Kord_C", "BP_BFV_C"}


def test_a_fob_destroyed(fx):
    assert _pm(fx, "Tarkan")["fobs_destroyed"] == 1
    assert _pm(fx, "Yıldız")["fobs_built"] == 1
    frames = _full(_lines(fx.matches["A"].sqrx_path))
    fob = [next((d["health"] for d in f["deployables"] if d.get("isFob")), None) for f in frames]
    built = next(i for i, h in enumerate(fob) if h is not None)
    dead_at = fob.index(0.0)
    assert built < dead_at and fob[built] == 1000.0
    assert 0 < fob[dead_at - 1] < 1000.0
    assert fob[dead_at + 1] is None                # gone after the last frame it was listed
    assert next(d for d in frames[dead_at]["deployables"])["team"] == 1


def test_a_has_full_and_position_frames_and_they_move(fx):
    frames = _lines(fx.matches["A"].sqrx_path)
    full, pos = _full(frames), _pos(frames)
    # 3 position frames follow each full frame, except that the recorder only opens
    # the file on the 3rd full frame, so the 6 sampled before that are dropped
    # (write_position_frame returns 0 with no recording open).
    assert len(full) == 60 and len(pos) == 3 * (59 - 2)
    assert all(f["gameState"]["matchState"] == "InProgress" for f in full)
    # the ~1 Hz : 4 Hz interleave (three position frames after each full frame)
    kinds = "".join("P" if f.get("t") == "pos" else "F" for f in frames)
    assert kinds.startswith("FFF") and "FPPPF" in kinds
    ids = {p["eosId"] for p in full[0]["players"]}
    for p in pos:
        assert p["t"] == "pos" and p["fullTick"] >= 1
        assert {q["id"] for q in p["players"]} <= ids
        assert p["vehicles"]
    # players and vehicles change position over time (full and position frames)
    first, last = _player(full[0], "Kartal")["soldier"]["position"], \
        _player(full[-1], "Kartal")["soldier"]["position"]
    assert (first["x"], first["y"]) != (last["x"], last["y"])
    kartal = eos_id("kartal")
    trail = [next(q for q in p["players"] if q["id"] == kartal) for p in pos[:12]]
    assert len({(q["x"], q["y"]) for q in trail}) > 6
    bfv = [next(v for v in p["vehicles"] if v["id"] == "0x7ff600010000") for p in pos[:30]]
    assert len({(v["x"], v["y"]) for v in bfv}) > 10
    # tick ids are consecutive on full frames (the recorder's sequence check)
    assert [f["tick"] for f in full] == list(range(full[0]["tick"], full[0]["tick"] + 60))


def test_a_privacy_relevant_fields_are_present_in_the_raw_recording(fx):
    """The redaction step (later task) needs something to redact."""
    full = _full(_lines(fx.matches["A"].sqrx_path))
    p = _player(full[0], "Kartal")
    assert HEX32.match(p["eosId"]) and p["_addr"].startswith("0x")
    assert p["stats"]["isAdmin"] is True
    assert full[0]["squads"][0]["creatorOnlineId"] and full[0]["squads"][0]["leaderStateAddr"]
    dmg = [e for f in full for e in f["damageEvents"]]
    assert dmg and all("attackerEosId" in e for e in dmg)


# -- recordings are real .sqrx --------------------------------------------------

def test_recordings_readable_and_match_their_sidecars(fx):
    for key in ("A", "B", "C"):
        m = fx.matches[key]
        with SqrxReader(m.sqrx_path) as r:
            assert r.format_version == 1 and r.server_id == m.server_id
            assert r.created_ms > 0
        frames = _lines(m.sqrx_path)
        meta = _meta(fx, key)
        assert meta["ticks"] == len(_full(frames))
        assert meta["positionFrames"] == len(_pos(frames))
        assert meta["totalFrames"] == len(frames)
        assert meta["sizeBytes"] == m.sqrx_path.stat().st_size
        assert extract_metadata(m.sqrx_path)["ticks"] == meta["ticks"]
        assert extract_metadata(m.sqrx_path)["positionFrames"] == meta["positionFrames"]
        assert m.sqrx_path.stem == meta["id"] == m.recording_id
    # A/B never carry a post-match frame: the recorder stops writing on confirmed end
    assert _full(_lines(fx.matches["A"].sqrx_path))[-1]["gameState"]["matchState"] == "InProgress"


def test_stats_bundles_are_buildable(fx):
    for key, m in fx.matches.items():
        b = build_match_bundle(fx.stats_dbs[m.server_id], m.match_id)
        assert b is not None and b["schema"] == "sqr-match-1"
        assert b["server_id"] == m.server_id and b["match"]["match_id"] == m.match_id
        assert b["players"], key
    assert len(build_match_bundle(fx.stats_dbs[SERVER_A_ID], MATCH_IDS["A"])["kills"]) == 8       # 6 kill/tk + 1 suicide + 1 wound
    assert build_match_bundle(fx.stats_dbs[SERVER_B_ID], MATCH_IDS["A"]) is None


def test_manifest_matches_the_written_files(fx):
    for key, m in fx.manifest["matches"].items():
        assert (fx.root / m["recordingFile"]).is_file()
        assert (fx.root / m["metaFile"]).is_file()
        assert m["recordingFile"].startswith(m["serverId"] + "/recordings/")
        assert m["expectedRecordingState"] == _meta(fx, key)["recordingState"]
    kinds = {k: m["kind"] for k, m in fx.manifest["matches"].items()}
    assert kinds == {"A": "finalized", "B": "finalized", "C": "unverified", "D": "active"}
    a_kinds = {e["kind"] for e in fx.manifest["matches"]["A"]["events"]}
    assert {"tk", "suicide", "revive", "vehicle_destroyed", "fob_destroyed",
            "kill", "wound"} <= a_kinds
    assert str(fx.root) not in json.dumps(fx.manifest)          # relative paths only


# -- determinism / guard rails --------------------------------------------------

def _dump_db(db: Path) -> dict:
    out = {}
    for table in ("matches", "player_matches", "kill_events", "vehicle_session",
                  "players", "player_elo"):
        out[table] = _rows(db, f"SELECT * FROM {table} ORDER BY 1, 2, 3")
    return out


def test_generation_is_deterministic(fx, tmp_path):
    other = build_zz_fixture(tmp_path / "again")
    def files(root: Path) -> list[Path]:
        # sqlite side files appear whenever a reader opens the WAL database
        return sorted(p.relative_to(root) for p in root.rglob("*")
                      if p.is_file() and not p.name.endswith(("-wal", "-shm")))

    mine, theirs = files(fx.root), files(other.root)
    assert mine == theirs
    for rel in mine:
        if rel.suffix == ".db":
            continue                    # SQLite file bytes are compared as table contents below
        assert (fx.root / rel).read_bytes() == (other.root / rel).read_bytes(), rel
    for sid in (SERVER_A_ID, SERVER_B_ID):
        assert _dump_db(fx.stats_dbs[sid]) == _dump_db(other.stats_dbs[sid])


def test_refuses_a_non_empty_directory(tmp_path):
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        build_zz_fixture(tmp_path)
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "x"


def test_cli_writes_the_fixture(tmp_path):
    out = tmp_path / "cli-out"
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "make_zz_fixture.py"), "--out", str(out)],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((out / "fixture-manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["matches"]) == {"A", "B", "C", "D"}
    again = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "make_zz_fixture.py"), "--out", str(out)],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert again.returncode == 2
