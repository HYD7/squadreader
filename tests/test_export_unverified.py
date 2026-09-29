"""Making the matches that are NOT exported visible: `health.unverified` and
`GET /export/v1/unverified` (sqreader/export_api.py).

Data comes from the synthetic Zero Zone fixture (tests/zz_fixture.py): zz-7787
holds A (finalized), C (unverified) and D (still open); zz-7788 holds B
(finalized). Extra matches are written by a real StatsStore fed real snapshots.

The last tests pin down two behaviours of the stats writer that the doc
docs/zerozone-kacan-maclar.md describes (MAX-upsert counters, unreadable value
-> 0). They CHARACTERISE the current behaviour; they do not endorse it.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import pytest
import zz_fixture
from zz_fixture import MATCH_IDS, SERVER_A_ID, SERVER_B_ID, build_zz_fixture

from sqreader.export_api import MIN_TOKEN_LEN, SCHEMA_EXPORT, ExportApi
from sqreader.httpsrv import _TickBeat, serve_in_background
from sqreader.recording_lifecycle import MATCH_TRANSITION_CONFIRM_TICKS
from sqreader.stats import StatsStore

TOKEN = "zz-unverified-test-token-" + "0123456789abcdef" * 2
assert len(TOKEN) >= MIN_TOKEN_LEN
AUTH = f"Bearer {TOKEN}"

ID_A, ID_B, ID_C, ID_D = (MATCH_IDS[k] for k in "ABCD")
DAY = 86400

ITEM_KEYS = {
    "matchId", "serverId", "startedAt", "lastSeenAt", "endedAt", "endState",
    "endReason", "mapName", "layerName", "gameMode", "peakPlayers", "tickCount",
    "playerCount", "isTraining"}
SUMMARY_KEYS = {
    "total", "last24h", "last7d", "last30d", "lastAt", "finalizedLast7d",
    "finalizedLast30d", "ratioLast30d", "training", "asOf"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def call(api: ExportApi, target: str, *, method: str = "GET",
         auth: Optional[str] = AUTH) -> tuple[int, Any]:
    res = api.handle(method, target, auth)
    return res.status, json.loads(res.body)


def make_api(fx, server_id: str, *, now: float, with_recordings: bool = True) -> ExportApi:
    return ExportApi(token=TOKEN, server_id=server_id,
                     stats_db=fx.stats_dbs[server_id],
                     recordings_dir=fx.recording_dirs[server_id] if with_recordings else None,
                     clock=lambda: now)


def ended_at(db: Path, match_id: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT ended_at FROM matches WHERE match_id=?",
                            (match_id,)).fetchone()[0]
    finally:
        conn.close()


def http(port: int, path: str, *, token: Optional[str] = TOKEN, method: str = "GET"):
    hdrs = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers.items()), r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers.items()), e.read()
        finally:
            e.close()


def play(db: Path, server_id: str, key: str, match_id: str, *, shift_s: int = 0,
         frames: Optional[int] = None, end: str = "finalized",
         layer: Optional[tuple[str, str, str]] = None) -> None:
    """One more match (match A's script, optionally cut short) into `db` through
    the real StatsStore. `end`: finalized (the game reports the end), unverified
    (reader stops, as cli.py's shutdown path) or open (the store is just closed,
    the row stays 'open')."""
    spec = dataclasses.replace(zz_fixture._SPEC_A, key=key, server_id=server_id)
    if layer is not None:
        spec = dataclasses.replace(spec, map_name=layer[0], layer=layer[1], mode=layer[2])
    n = frames if frames is not None else spec.n_full
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(zz_fixture.MATCH_IDS, key, match_id)
        mp.setitem(zz_fixture._START, key,
                   zz_fixture._START["A"] + timedelta(seconds=shift_s))
        sim = zz_fixture._Sim(spec)
        store = StatsStore(db, server_id=server_id)
        try:
            tick = 20_000
            for f in range(n):
                events = sim.advance(f)
                tick += 1
                snap = sim.full_frame(f, tick, "InProgress")
                snap["damageEvents"] = events
                store.record_tick(snap)
            if end == "finalized":
                for j in range(MATCH_TRANSITION_CONFIRM_TICKS):
                    tick += 1
                    store.record_tick(sim.full_frame(n + j, tick, "WaitingPostMatch"))
            elif end == "unverified":
                store.finalize_open_match(reason="serve shutdown")
            elif end != "open":
                raise ValueError(end)
        finally:
            store.close()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    return build_zz_fixture(tmp_path_factory.mktemp("zzu") / "fixture")


@pytest.fixture(scope="module")
def times(fx):
    db = fx.stats_dbs[SERVER_A_ID]
    return {"A": ended_at(db, ID_A), "C": ended_at(db, ID_C), "D": ended_at(db, ID_D),
            "B": ended_at(fx.stats_dbs[SERVER_B_ID], ID_B)}


@pytest.fixture(scope="module")
def srv_a(fx, times):
    api = make_api(fx, SERVER_A_ID, now=times["C"] + 3600)
    srv = serve_in_background("127.0.0.1", 0, _TickBeat(),
                              recordings_dir=fx.recording_dirs[SERVER_A_ID],
                              stats_db=fx.stats_dbs[SERVER_A_ID], export_api=api)
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------------------
# 1. health.unverified
# ---------------------------------------------------------------------------

def _health(api: ExportApi) -> dict:
    status, body = call(api, "/export/v1/health")
    assert status == 200
    return body["unverified"]


def test_health_counts_c_not_d_and_lists_a_as_finalized(fx, times):
    # C (unverified) ended an hour ago; A (finalized) is one hour older or so
    api = make_api(fx, SERVER_A_ID, now=times["C"] + 3600)
    u = _health(api)
    assert set(u) == SUMMARY_KEYS
    assert u["total"] == 1                      # C only: D is still open
    assert u["last24h"] == u["last7d"] == u["last30d"] == 1
    assert u["lastAt"] == times["C"]
    assert u["finalizedLast7d"] == 1            # A
    assert u["finalizedLast30d"] == 1
    assert u["ratioLast30d"] == 0.5             # 1 / (1 + 1)
    assert u["asOf"] == times["C"] + 3600
    assert u["training"] == {"total": 0, "last30d": 0, "lastAt": None,
                             "finalizedLast30d": 0}


def test_health_windows(fx, times):
    c = times["C"]
    # 10 days later: out of 24h and 7d, still in 30d
    u = _health(make_api(fx, SERVER_A_ID, now=c + 10 * DAY))
    assert (u["total"], u["last24h"], u["last7d"], u["last30d"]) == (1, 0, 0, 1)
    assert (u["finalizedLast7d"], u["finalizedLast30d"]) == (0, 1)
    assert u["ratioLast30d"] == 0.5
    # 40 days later: only the all-time total and lastAt are left; ratio has no base
    u = _health(make_api(fx, SERVER_A_ID, now=c + 40 * DAY))
    assert (u["total"], u["last30d"], u["finalizedLast30d"]) == (1, 0, 0)
    assert u["ratioLast30d"] is None
    assert u["lastAt"] == c
    # exactly on the 24h edge counts (>=), one second past does not
    assert _health(make_api(fx, SERVER_A_ID, now=c + DAY))["last24h"] == 1
    assert _health(make_api(fx, SERVER_A_ID, now=c + DAY + 1))["last24h"] == 0


def test_health_server_b_has_none(fx, times):
    u = _health(make_api(fx, SERVER_B_ID, now=times["B"] + 60))
    assert u["total"] == u["last24h"] == u["last7d"] == u["last30d"] == 0
    assert u["lastAt"] is None
    assert u["finalizedLast30d"] == 1
    assert u["ratioLast30d"] == 0.0             # a base, and nothing lost


def test_health_unverified_is_null_when_db_unreadable(tmp_path):
    api = ExportApi(token=TOKEN, server_id="s", stats_db=tmp_path / "missing.db")
    status, body = call(api, "/export/v1/health")
    assert status == 200 and body["unverified"] is None
    assert body["statsDbReadable"] is False


@pytest.fixture(scope="module")
def mixed(tmp_path_factory):
    """One DB, two server ids, every kind of row (through the real StatsStore).
    The last store is `srv-1`'s so its open match is not swept away."""
    db = tmp_path_factory.mktemp("mixed") / "stats.db"
    ids = {k: f"5a7e0f00-0000-4000-8000-00000000{n:04d}" for n, k in enumerate(
        ("s2_unv", "s2_fin", "s1_fin", "s1_unv", "s1_train_unv", "s1_train_fin",
         "s1_open", "s1_short_unv"), start=1)}
    jensen = ("Jensen's Training Range", "JensensRange_Training_v1", "Training")
    play(db, "srv-2", "E", ids["s2_unv"], shift_s=0, frames=15, end="unverified")
    play(db, "srv-2", "F", ids["s2_fin"], shift_s=3600, end="finalized")
    play(db, "srv-1", "G", ids["s1_fin"], shift_s=7200, end="finalized")
    play(db, "srv-1", "H", ids["s1_unv"], shift_s=10800, frames=20, end="unverified")
    play(db, "srv-1", "I", ids["s1_train_unv"], shift_s=14400, frames=10,
         end="unverified", layer=jensen)
    play(db, "srv-1", "J", ids["s1_train_fin"], shift_s=18000, end="finalized",
         layer=jensen)
    play(db, "srv-1", "K", ids["s1_short_unv"], shift_s=21600, frames=5, end="unverified")
    play(db, "srv-1", "L", ids["s1_open"], shift_s=25200, frames=12, end="open")
    return db, ids


def _api_for(db: Path, server_id: str, now: float) -> ExportApi:
    return ExportApi(token=TOKEN, server_id=server_id, stats_db=db, clock=lambda: now)


def test_health_two_servers_in_one_db_do_not_mix(mixed):
    db, ids = mixed
    now = ended_at(db, ids["s1_open"]) + 60
    u1 = _health(_api_for(db, "srv-1", now))
    u2 = _health(_api_for(db, "srv-2", now))
    # srv-1: unverified s1_unv + s1_short_unv (training one is apart, open one is not counted)
    assert (u1["total"], u1["last30d"]) == (2, 2)
    assert (u1["finalizedLast30d"], u1["finalizedLast7d"]) == (1, 1)
    assert u1["ratioLast30d"] == round(2 / 3, 4)
    assert u1["lastAt"] == ended_at(db, ids["s1_short_unv"])
    assert u1["training"] == {"total": 1, "last30d": 1,
                              "lastAt": ended_at(db, ids["s1_train_unv"]),
                              "finalizedLast30d": 1}
    # srv-2: one of each
    assert (u2["total"], u2["finalizedLast30d"], u2["ratioLast30d"]) == (1, 1, 0.5)
    assert u2["training"]["total"] == 0


# ---------------------------------------------------------------------------
# 2. /export/v1/unverified
# ---------------------------------------------------------------------------

def test_unverified_lists_c_only(fx, times, srv_a):
    status, _, body = http(srv_a, "/export/v1/unverified")
    assert status == 200
    doc = json.loads(body)
    assert doc["schema"] == SCHEMA_EXPORT and doc["serverId"] == SERVER_A_ID
    assert set(doc) == {"schema", "serverId", "unverifiedMatches", "nextCursor"}
    assert [m["matchId"] for m in doc["unverifiedMatches"]] == [ID_C]   # not A, not D
    assert doc["nextCursor"] is None
    item = doc["unverifiedMatches"][0]
    assert set(item) == ITEM_KEYS
    assert item["serverId"] == SERVER_A_ID
    assert item["endState"] == "unverified"
    assert item["endReason"] == "serve shutdown"
    assert item["endedAt"] == item["lastSeenAt"] == times["C"]
    assert item["mapName"] == "Yehorivka" and item["layerName"] == "Yehorivka_AAS_v1"
    assert item["gameMode"] == "AAS"
    assert item["playerCount"] == 6 and item["peakPlayers"] == 6
    assert item["tickCount"] == 20
    assert item["isTraining"] is False
    assert isinstance(item["startedAt"], int) and item["startedAt"] < item["lastSeenAt"]


def test_unverified_has_no_player_or_stat_data(fx, srv_a):
    _, _, body = http(srv_a, "/export/v1/unverified")
    text = body.decode("utf-8")
    doc = json.loads(text)
    forbidden = {"players", "player", "kills", "deaths", "bundle", "bundleSha256",
                 "recordings", "score", "eos_id", "eosId", "winner_team", "winnerTeam",
                 "team1_tickets", "team1Tickets", "tickets", "kill_events"}

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in forbidden, k
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(doc)
    # not even by value: no player name / EOS id of match C
    for needle in ("Kartal", "Volkan", zz_fixture.eos_id("kartal"), zz_fixture.eos_id("volkan")):
        assert needle not in text


def test_matches_still_only_a_and_c_is_404(fx, srv_a):
    status, _, body = http(srv_a, "/export/v1/matches")
    assert status == 200
    assert [m["matchId"] for m in json.loads(body)["matches"]] == [ID_A]
    bodies = set()
    for mid in (ID_C, ID_D):
        status, _, body = http(srv_a, f"/export/v1/matches/{mid}")
        assert status == 404, mid
        bodies.add(body)
    assert len(bodies) == 1
    assert http(srv_a, f"/export/v1/matches/{ID_A}")[0] == 200
    # C's recording is still not served either
    assert http(srv_a, f"/export/v1/recordings/{fx.matches['C'].recording_id}")[0] == 404
    # and the new path does not alias into the matches namespace
    assert http(srv_a, f"/export/v1/unverified/{ID_C}")[0] == 404
    assert http(srv_a, "/export/v1/unverified/")[0] == 404


def test_unverified_server_b_is_empty(fx, times):
    api = make_api(fx, SERVER_B_ID, now=times["B"])
    status, doc = call(api, "/export/v1/unverified")
    assert status == 200
    assert doc["serverId"] == SERVER_B_ID and doc["unverifiedMatches"] == []
    assert doc["nextCursor"] is None


def test_unverified_two_servers_in_one_db_and_training_flag(mixed):
    db, ids = mixed
    _, d1 = call(_api_for(db, "srv-1", 0), "/export/v1/unverified?limit=100")
    _, d2 = call(_api_for(db, "srv-2", 0), "/export/v1/unverified?limit=100")
    got1 = [m["matchId"] for m in d1["unverifiedMatches"]]
    # oldest first by last-seen; the open match and every finalized one are absent
    assert got1 == [ids["s1_unv"], ids["s1_train_unv"], ids["s1_short_unv"]]
    assert [m["matchId"] for m in d2["unverifiedMatches"]] == [ids["s2_unv"]]
    assert {m["serverId"] for m in d1["unverifiedMatches"]} == {"srv-1"}
    flags = {m["matchId"]: m["isTraining"] for m in d1["unverifiedMatches"]}
    assert flags == {ids["s1_unv"]: False, ids["s1_train_unv"]: True,
                     ids["s1_short_unv"]: False}
    assert ids["s1_open"] not in got1


def test_unverified_paging_matches_the_matches_endpoint_rules(mixed):
    db, ids = mixed
    api = _api_for(db, "srv-1", 0)
    _, full = call(api, "/export/v1/unverified?limit=100")
    order = [m["matchId"] for m in full["unverifiedMatches"]]
    ended = [m["endedAt"] for m in full["unverifiedMatches"]]
    assert full["nextCursor"] is None
    # walk with every page size: each match exactly once, in order
    for limit in (1, 2, 3, 4):
        seen, after, pages = [], None, 0
        while True:
            q = f"/export/v1/unverified?limit={limit}" + (f"&after={after}" if after else "")
            _, page = call(api, q)
            pages += 1
            seen += [m["matchId"] for m in page["unverifiedMatches"]]
            after = page["nextCursor"]
            if after is None:
                break
        assert seen == order, limit
        if limit == 1:
            assert pages == 4                    # 3 full pages + the empty last
    # nextCursor is "<endedAt>:<matchId>" of the last item of a full page
    _, first = call(api, "/export/v1/unverified?limit=1")
    assert first["nextCursor"] == f"{ended[0]}:{order[0]}"
    _, rest = call(api, f"/export/v1/unverified?after={ended[0]}:{order[0]}&limit=100")
    assert [m["matchId"] for m in rest["unverifiedMatches"]] == order[1:]
    # defaults and clamping
    _, dflt = call(api, "/export/v1/unverified")
    assert len(dflt["unverifiedMatches"]) == 3
    _, big = call(api, "/export/v1/unverified?limit=100000")
    assert len(big["unverifiedMatches"]) == 3
    _, two = call(api, "/export/v1/unverified?limit=2")
    assert len(two["unverifiedMatches"]) == 2
    # past the end
    _, end = call(api, f"/export/v1/unverified?after={ended[-1]}:{order[-1]}")
    assert end["unverifiedMatches"] == [] and end["nextCursor"] is None
    # unknown parameters are ignored, as on /matches
    assert call(api, "/export/v1/unverified?unknown=1")[0] == 200


def test_unverified_bad_limit_and_cursor_are_400(srv_a):
    bad = ["limit=0", "limit=-1", "limit=abc", "limit=", "limit=1.5", "limit=1&limit=2",
           "limit=%20", "limit=1e3", "limit=99999999999999",
           "after=", "after=garbage", "after=1", "after=:x", "after=abc:def",
           "after=1788286190:", "after=1788286190:../x", "after=1788286190:a:b",
           "after=-5:abc", "after=1788286190:" + "a" * 200, "after=%00",
           "after=1&after=2", "limit"]
    for q in bad:
        status, _, body = http(srv_a, f"/export/v1/unverified?{q}")
        assert status == 400, q
        assert json.loads(body)["schema"] == SCHEMA_EXPORT
        assert TOKEN not in body.decode()
    # the same query is refused / accepted by /matches: one rule set for both
    for q in bad:
        assert http(srv_a, f"/export/v1/matches?{q}")[0] == 400, q
    assert http(srv_a, "/export/v1/unverified?limit=5&after=1788286190:abc")[0] == 200


def test_unverified_token_and_methods(srv_a):
    for hdr_token in (None, "wrong-token-" + "x" * 30, TOKEN + "x", TOKEN[:-1]):
        status, hdrs, body = http(srv_a, "/export/v1/unverified", token=hdr_token)
        assert status == 401
        assert hdrs["WWW-Authenticate"] == "Bearer"
        assert json.loads(body) == {"schema": SCHEMA_EXPORT, "error": "unauthorized"}
    status, hdrs, _ = http(srv_a, "/export/v1/unverified")
    assert status == 200
    assert hdrs["Content-Type"] == "application/json"
    assert hdrs["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in hdrs
    assert http(srv_a, "/export/v1/unverified", method="HEAD")[0] == 200
    for method in ("POST", "PUT", "DELETE"):
        status, hdrs, _ = http(srv_a, "/export/v1/unverified", method=method)
        assert status == 405, method
        assert hdrs["Allow"] == "GET, HEAD"


def test_export_off_makes_the_new_paths_404(fx, times):
    srv = serve_in_background("127.0.0.1", 0, _TickBeat(),
                              recordings_dir=fx.recording_dirs[SERVER_A_ID],
                              stats_db=fx.stats_dbs[SERVER_A_ID], export_api=None)
    port = srv.server_address[1]
    try:
        for path in ("/export/v1/unverified", "/export/v1/unverified?limit=1",
                     "/export/v1/health"):
            for token in (None, TOKEN):
                status, hdrs, body = http(port, path, token=token)
                assert status == 404, path
                assert "WWW-Authenticate" not in hdrs
                assert "sqr-export" not in body.decode("utf-8", "replace")
        assert http(port, "/health", token=None)[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_unverified_db_unreadable_is_503(tmp_path):
    api = ExportApi(token=TOKEN, server_id="s", stats_db=tmp_path / "missing.db")
    status, doc = call(api, "/export/v1/unverified")
    assert status == 503 and doc == {"schema": SCHEMA_EXPORT, "error": "stats_unavailable"}


# ---------------------------------------------------------------------------
# reader restart mid-match: unverified for a while, finalized once it really ends
# ---------------------------------------------------------------------------

def test_restart_mid_match_reopens_and_can_finalize(tmp_path):
    """stats.py: the start-up sweep turns a left-open row into final/unverified;
    the next InProgress tick of the SAME match id re-opens it (started_at kept);
    the observed end then makes it finalized and exportable."""
    db = tmp_path / "stats.db"
    match_id = "5a7e0e00-0000-4000-8000-000000000001"
    spec = dataclasses.replace(zz_fixture._SPEC_A, key="R", server_id="srv-r")
    now = 10**10
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(zz_fixture.MATCH_IDS, "R", match_id)
        mp.setitem(zz_fixture._START, "R", zz_fixture._START["A"])
        sim = zz_fixture._Sim(spec)
        tick = 30_000

        def feed(store, f, state="InProgress"):
            nonlocal tick
            tick += 1
            snap = sim.full_frame(f, tick, state)
            if state == "InProgress":
                snap["damageEvents"] = sim.advance(f)
            store.record_tick(snap)

        store = StatsStore(db, server_id="srv-r")
        for f in range(20):
            feed(store, f)
        store.close()                                     # process ends; row stays open
        api = _api_for(db, "srv-r", now)

        # boot 2: swept to final/unverified BEFORE the first tick of the new process
        store = StatsStore(db, server_id="srv-r")
        _, doc = call(api, "/export/v1/unverified")
        assert [m["matchId"] for m in doc["unverifiedMatches"]] == [match_id]
        assert doc["unverifiedMatches"][0]["endReason"].startswith("startup sweep")
        started = doc["unverifiedMatches"][0]["startedAt"]
        assert call(api, "/export/v1/matches")[1]["matches"] == []

        # first InProgress tick of the same match re-opens it: no longer listed
        feed(store, 20)
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT status, end_state FROM matches").fetchone() == (
            "open", "unverified")                          # end_state is stale while open
        conn.close()
        assert call(api, "/export/v1/unverified")[1]["unverifiedMatches"] == []
        assert _health(api)["total"] == 0

        # the end is observed: finalized, exported, still not unverified
        for f in range(21, spec.n_full):
            feed(store, f)
        for j in range(MATCH_TRANSITION_CONFIRM_TICKS):
            feed(store, spec.n_full + j, "WaitingPostMatch")
        store.close()

    assert [m["matchId"] for m in call(api, "/export/v1/matches")[1]["matches"]] == [match_id]
    assert call(api, "/export/v1/unverified")[1]["unverifiedMatches"] == []
    assert _health(api)["total"] == 0
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT started_at, end_state, tick_count FROM matches").fetchone()
    finally:
        conn.close()
    assert row[0] == started and row[1] == "finalized"
    assert row[2] == spec.n_full                          # ticks kept counting across the restart


# ---------------------------------------------------------------------------
# characterisation of the stats writer (documented, NOT changed here)
# ---------------------------------------------------------------------------

_EOS = "ab" * 16


def _snap(t: int, kills: Any, score: Any = None) -> dict:
    return {"timestamp": f"2026-09-01T18:00:{t:02d}Z", "tick": t,
            "gameState": {"matchState": "InProgress", "matchId": "m-cnt",
                          "mapName": "X_RAAS_v1"},
            "teams": [],
            "players": [{"eosId": _EOS, "name": "P", "teamId": 1,
                         "stats": {"kills": kills}, "score": score}]}


def _row(db: Path) -> tuple:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT kills, score FROM player_matches").fetchone()
    finally:
        conn.close()


def test_counters_are_max_upserts_a_reset_is_not_added_up(tmp_path):
    """A player whose in-game counter restarts at 0 (reconnect) is NOT summed:
    5 kills, reset, 3 more kills -> the game shows 3, the match total stays 5
    (it would be 8 if the counter were accumulated across the reset)."""
    db = tmp_path / "s.db"
    store = StatsStore(db, server_id="s")
    for t, k in enumerate([5, 0, 3], start=1):
        store.record_tick(_snap(t, k))
    store.close()
    assert _row(db)[0] == 5
    # and it only rises once it passes the old maximum
    store = StatsStore(db, server_id="s")
    store.record_tick(_snap(4, 7))
    store.close()
    assert _row(db)[0] == 7


def test_unreadable_counter_becomes_zero_not_null(tmp_path):
    db = tmp_path / "s.db"
    store = StatsStore(db, server_id="s")
    store.record_tick(_snap(1, None))                     # unreadable on the first tick
    assert _row(db)[0] == 0                                # stored as 0, not NULL
    store.record_tick(_snap(2, 4))
    store.record_tick(_snap(3, None))                     # unreadable later: max keeps 4
    store.close()
    assert _row(db)[0] == 4
