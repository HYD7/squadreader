"""Export API v1 (sqreader/export_api.py) over a real HTTP server.

Data comes from the synthetic Zero Zone fixture (tests/zz_fixture.py), whose
stats.db / .sqrx / .meta.json were written by the reader's real code paths. The
extra matches used for the cursor tests are written the same way (a real
StatsStore fed real snapshots), not inserted as rows.
"""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import importlib.util
import io
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import zstandard
import zz_fixture
from zz_fixture import (
    MATCH_IDS,
    SERVER_A_ID,
    SERVER_B_ID,
    build_zz_fixture,
)

from sqreader import config
from sqreader.export_api import (
    MIN_TOKEN_LEN,
    SCHEMA_EXPORT,
    ExportApi,
    canonical_json,
    configure_export,
)
from sqreader.httpsrv import _TickBeat, serve_in_background
from sqreader.recording_lifecycle import MATCH_TRANSITION_CONFIRM_TICKS
from sqreader.stats import SCHEMA_VERSION, StatsStore

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "fixtures" / "zz_golden"
TOKEN = "zz-export-test-token-" + "0123456789abcdef" * 2
assert len(TOKEN) >= MIN_TOKEN_LEN

EXPORT_PATHS = ["/export/v1/info", "/export/v1/health", "/export/v1/matches",
                "/export/v1/unverified",
                f"/export/v1/matches/{MATCH_IDS['A']}",
                "/export/v1/recordings/whatever"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def request(port, path, *, token=TOKEN, method="GET", headers=None, data=None):
    """(status, headers, body) — HTTP errors are returned, not raised."""
    hdrs = dict(headers or {})
    if token is not None:
        hdrs.setdefault("Authorization", f"Bearer {token}")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=hdrs,
                                 method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers.items()), r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers.items()), e.read()
        finally:
            e.close()


def get_json(port, path, **kw):
    status, hdrs, body = request(port, path, **kw)
    assert status == 200, (path, status, body[:200])
    return json.loads(body)


def raw_request(port, target: bytes, extra: bytes = b"") -> int:
    """Send a request line verbatim (urllib refuses control characters)."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(b"GET " + target + b" HTTP/1.1\r\nHost: x\r\n"
                  b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
                  + extra + b"Connection: close\r\n\r\n")
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return int(data.split(b" ", 2)[1])


def zstd_decode(body: bytes) -> bytes:
    return zstandard.ZstdDecompressor().stream_reader(
        io.BytesIO(body), read_across_frames=True).read()


def start(fx, server_id, *, api=True, token=TOKEN, reader_state=None):
    rec_dir = fx.recording_dirs[server_id]
    db = fx.stats_dbs[server_id]
    export = (ExportApi(token=token, server_id=server_id, stats_db=db,
                        recordings_dir=rec_dir, squad_port=7787,
                        reader_state=reader_state) if api else None)
    srv = serve_in_background("127.0.0.1", 0, _TickBeat(), recordings_dir=rec_dir,
                              stats_db=db, export_api=export)
    return srv, srv.server_address[1]


def stop(srv):
    srv.shutdown()
    srv.server_close()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    return build_zz_fixture(tmp_path_factory.mktemp("zz") / "fixture")


@pytest.fixture(scope="module")
def srv_a(fx):
    srv, port = start(fx, SERVER_A_ID)
    yield port
    stop(srv)


@pytest.fixture(scope="module")
def srv_b(fx):
    srv, port = start(fx, SERVER_B_ID)
    yield port
    stop(srv)


ID_A = MATCH_IDS["A"]
ID_B = MATCH_IDS["B"]
ID_C = MATCH_IDS["C"]
ID_D = MATCH_IDS["D"]
# Ids of the extra matches of the multi-match environment. 'a002'/'a003' sort
# right after match A's '...a001'.
EXTRA_SAME_1 = "5a7e0a00-7787-4a00-8000-00000000a002"
EXTRA_SAME_2 = "5a7e0a00-7787-4a00-8000-00000000a003"
EXTRA_LATER = "5a7e0a00-7787-4a00-8000-00000000a004"
EXTRA_LEGACY = "5a7e0a00-7787-4a00-8000-00000000a005"


def _play_extra(db: Path, server_id: str, key: str, match_id: str, shift_s: int):
    """One more full match (match A's script) into `db` through the real
    StatsStore, `shift_s` seconds later than A."""
    from datetime import timedelta
    spec = dataclasses.replace(zz_fixture._SPEC_A, key=key, server_id=server_id)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(zz_fixture.MATCH_IDS, key, match_id)
        mp.setitem(zz_fixture._START, key,
                   zz_fixture._START["A"] + timedelta(seconds=shift_s))
        sim = zz_fixture._Sim(spec)
        store = StatsStore(db, server_id=server_id)
        try:
            tick = 10_000
            for f in range(spec.n_full):
                events = sim.advance(f)
                tick += 1
                snap = sim.full_frame(f, tick, "InProgress")
                snap["damageEvents"] = events
                store.record_tick(snap)
            for j in range(MATCH_TRANSITION_CONFIRM_TICKS):
                tick += 1
                store.record_tick(sim.full_frame(spec.n_full + j, tick, "WaitingPostMatch"))
        finally:
            store.close()


@pytest.fixture(scope="module")
def multi(tmp_path_factory):
    """zz-7787 with three extra FINALIZED matches, two of them ending at exactly
    the same second as A, and one more that was later downgraded to `legacy`."""
    fx2 = build_zz_fixture(tmp_path_factory.mktemp("zzm") / "fixture")
    db = fx2.stats_dbs[SERVER_A_ID]
    _play_extra(db, SERVER_A_ID, "E", EXTRA_SAME_2, 0)      # inserted out of id order
    _play_extra(db, SERVER_A_ID, "F", EXTRA_SAME_1, 0)
    _play_extra(db, SERVER_A_ID, "G", EXTRA_LATER, 7200)
    _play_extra(db, SERVER_A_ID, "H", EXTRA_LEGACY, 3600)
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE matches SET end_state='legacy' WHERE match_id=?",
                     (EXTRA_LEGACY,))
    conn.close()
    srv, port = start(fx2, SERVER_A_ID)
    yield fx2, port
    stop(srv)


# ---------------------------------------------------------------------------
# 1. access control
# ---------------------------------------------------------------------------

def test_export_off_when_not_configured(fx):
    srv, port = start(fx, SERVER_A_ID, api=False)
    try:
        for path in EXPORT_PATHS + ["/export/v1", "/export/v1/", "/export/v1/nope"]:
            for token in (None, TOKEN):
                status, hdrs, body = request(port, path, token=token)
                assert status == 404, path
                assert "WWW-Authenticate" not in hdrs
                assert "sqr-export" not in body.decode("utf-8", "replace")
        assert request(port, "/export/v1/info", method="POST", data=b"x")[0] == 404
        # the ordinary endpoints are untouched
        assert request(port, "/health", token=None)[0] == 200
        assert request(port, "/api/recordings", token=None)[0] == 200
    finally:
        stop(srv)


def test_missing_or_wrong_token_is_401(srv_a):
    bad_auth = [
        {},                                             # no header
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer wrong-token-" + "x" * 30},
        {"Authorization": "Basic " + TOKEN},
        {"Authorization": TOKEN},
        {"Authorization": "Bearer " + TOKEN + "x"},
        {"Authorization": "Bearer " + TOKEN[:-1]},
    ]
    for path in EXPORT_PATHS + ["/export/v1/unknown"]:
        for hdrs in bad_auth:
            status, resp_hdrs, body = request(srv_a, path, token=None, headers=hdrs)
            assert status == 401, (path, hdrs)
            assert resp_hdrs["WWW-Authenticate"] == "Bearer"
            assert json.loads(body) == {"schema": SCHEMA_EXPORT, "error": "unauthorized"}


def test_correct_token_is_200(srv_a):
    for path in ("/export/v1/info", "/export/v1/health", "/export/v1/matches",
                 f"/export/v1/matches/{ID_A}"):
        status, hdrs, _ = request(srv_a, path)
        assert status == 200, path
        assert hdrs["Content-Type"] == "application/json"
        assert hdrs["Cache-Control"] == "no-store"
        assert "Access-Control-Allow-Origin" not in hdrs        # no CORS
    # scheme is case-insensitive
    assert request(srv_a, "/export/v1/info", token=None,
                   headers={"Authorization": f"bearer {TOKEN}"})[0] == 200
    # unknown paths under /export/v1 are 404 once authenticated
    assert request(srv_a, "/export/v1/nope")[0] == 404
    assert request(srv_a, "/export/v1/matches/")[0] == 404


def test_token_too_short_is_refused():
    with pytest.raises(ValueError):
        ExportApi(token="short", server_id="x", stats_db=Path("x.db"))


# ---------------------------------------------------------------------------
# info / health
# ---------------------------------------------------------------------------

def test_info(fx, srv_a, srv_b):
    from sqreader import __version__
    info = get_json(srv_a, "/export/v1/info")
    assert info == {
        "schema": "sqr-export-1", "readerVersion": __version__,
        "statsSchemaVersion": SCHEMA_VERSION, "serverId": SERVER_A_ID,
        "squadPort": 7787, "containerId": None, "readerState": "unknown"}
    assert get_json(srv_b, "/export/v1/info")["serverId"] == SERVER_B_ID


def test_info_reader_state_and_unknowns(fx):
    srv, port = start(fx, SERVER_B_ID, reader_state=lambda: "ok")
    try:
        assert get_json(port, "/export/v1/info")["readerState"] == "ok"
    finally:
        stop(srv)

    def boom():
        raise RuntimeError("x")
    srv, port = start(fx, SERVER_B_ID, reader_state=boom)
    try:
        assert get_json(port, "/export/v1/info")["readerState"] == "unknown"
    finally:
        stop(srv)


def test_health(fx, srv_a, srv_b):
    a = get_json(srv_a, "/export/v1/health")
    # the original fields keep their values; the control / disk / retention /
    # target fields added later are covered by tests/test_export_control.py
    assert {k: a[k] for k in ("schema", "ok", "readerState", "lastFinalizedAt",
                              "statsDbReadable", "recordingsDirReadable")} == {
        "schema": "sqr-export-1", "ok": True, "readerState": "unknown",
        "lastFinalizedAt": 1788286190, "statsDbReadable": True,
        "recordingsDirReadable": True}
    assert get_json(srv_b, "/export/v1/health")["lastFinalizedAt"] == 1788286190


def test_health_unreadable(tmp_path):
    api = ExportApi(token=TOKEN, server_id="s", stats_db=tmp_path / "missing.db",
                    recordings_dir=tmp_path / "nodir", reader_state=lambda: "stale")
    res = api.handle("GET", "/export/v1/health", f"Bearer {TOKEN}")
    body = json.loads(res.body)
    assert body["ok"] is False
    assert body["statsDbReadable"] is False
    assert body["recordingsDirReadable"] is False
    assert body["lastFinalizedAt"] is None
    # an unreadable stats DB is a 503 on the data endpoints, not a crash
    res = api.handle("GET", "/export/v1/matches", f"Bearer {TOKEN}")
    assert res.status == 503
    assert json.loads(api.handle("GET", "/export/v1/info",
                                 f"Bearer {TOKEN}").body)["statsSchemaVersion"] is None


# ---------------------------------------------------------------------------
# 2. what is listed, servers stay apart
# ---------------------------------------------------------------------------

def test_matches_only_finalized_and_per_server(fx, srv_a, srv_b):
    a = get_json(srv_a, "/export/v1/matches")
    b = get_json(srv_b, "/export/v1/matches")
    assert a["schema"] == "sqr-export-1" and a["serverId"] == SERVER_A_ID
    assert [m["matchId"] for m in a["matches"]] == [ID_A]      # not C (unverified), not D (active)
    assert [m["matchId"] for m in b["matches"]] == [ID_B]
    assert a["nextCursor"] is None and b["nextCursor"] is None
    ma = a["matches"][0]
    assert ma["serverId"] == SERVER_A_ID and b["matches"][0]["serverId"] == SERVER_B_ID
    assert set(ma) == {"matchId", "serverId", "startedAt", "endedAt", "endState",
                       "mapName", "layerName", "gameMode", "bundleSha256", "recordings"}
    assert (ma["mapName"], ma["layerName"], ma["gameMode"]) == (
        "Fallujah", "Fallujah_RAAS_v1", "RAAS")
    assert ma["endState"] == "finalized"
    assert isinstance(ma["startedAt"], int) and isinstance(ma["endedAt"], int)
    assert ma["startedAt"] == 1788285600 and ma["endedAt"] == 1788286190   # epoch seconds
    assert b["matches"][0]["mapName"] == "Narva"
    # recordings: only this match's finalized one
    assert [r["id"] for r in ma["recordings"]] == [fx.matches["A"].recording_id]
    assert [r["id"] for r in b["matches"][0]["recordings"]] == [fx.matches["B"].recording_id]
    rec = ma["recordings"][0]
    assert set(rec) == {"id", "state", "totalFrames", "sizeBytes", "startedAtUtc",
                        "endedAtUtc"}
    assert rec["state"] == "finalized"
    assert rec["totalFrames"] == 60 + 171
    assert rec["sizeBytes"] == fx.matches["A"].sqrx_path.stat().st_size
    assert rec["startedAtUtc"].startswith("2026-09-01T18:00:00")
    assert rec["endedAtUtc"].startswith("2026-09-01T18:09:50")


def test_no_server_paths_in_responses(fx, srv_a):
    blob = b"".join(request(srv_a, p)[2] for p in (
        "/export/v1/info", "/export/v1/health", "/export/v1/matches",
        f"/export/v1/matches/{ID_A}")).decode("utf-8")
    for fragment in (str(fx.root), fx.root.as_posix(), "stats.db", ".sqrx", "/recordings/",
                     "\\\\", "AppData", "pytest-of"):
        assert fragment not in blob, fragment


# ---------------------------------------------------------------------------
# match detail
# ---------------------------------------------------------------------------

def test_match_detail_shape(fx, srv_a):
    d = get_json(srv_a, f"/export/v1/matches/{ID_A}")
    from sqreader import __version__
    assert list(d) == ["schema", "readerVersion", "statsSchemaVersion", "serverId",
                       "endState", "bundleSha256", "bundle", "recordings"]
    assert d["schema"] == "sqr-match-1"
    assert d["readerVersion"] == __version__
    assert d["statsSchemaVersion"] == SCHEMA_VERSION
    assert d["serverId"] == SERVER_A_ID and d["endState"] == "finalized"
    b = d["bundle"]
    assert set(b) == {"schema", "server_id", "match", "players", "kills", "vehicles"}
    assert "elo" not in b
    assert b["match"]["match_id"] == ID_A and b["match"]["winner_team"] == 1
    assert b["match"]["team1_tickets"] == 143 and b["match"]["team2_tickets"] == 98
    assert len(b["players"]) == 8
    # raw SR column names, NULL kept as null
    assert {"eos_id", "name", "kills", "deaths", "woundeds", "wounds", "team_kills",
            "revived_points", "objective_score", "vehicle_kills", "fobs_destroyed",
            "team_id", "playtime_sec", "elo_change"} <= set(b["players"][0])
    assert any(p["lives"] is None for p in b["players"])
    assert {"victim_eos", "attacker_eos", "team_kill", "killed", "wounded", "ts"} \
        <= set(b["kills"][0])
    assert {"vehicle_class", "seat", "entered_at", "exited_at", "distance_m"} \
        <= set(b["vehicles"][0])
    assert [r["id"] for r in d["recordings"]] == [fx.matches["A"].recording_id]
    # players are sorted by eos id: the same data always serialises the same way
    eos = [p["eos_id"] for p in b["players"]]
    assert eos == sorted(eos)


def test_nonfinal_and_unknown_matches_are_the_same_404(fx, srv_a):
    bodies = set()
    for mid in (ID_C, ID_D, ID_B, "5a7e0a00-7787-4a00-8000-0000000000ff", "x"):
        status, hdrs, body = request(srv_a, f"/export/v1/matches/{mid}")
        assert status == 404, mid
        bodies.add(body)
    assert len(bodies) == 1                    # unverified / active / other server / unknown


def test_nonfinal_recordings_are_404(fx, srv_a):
    for key in ("C", "D"):
        rid = fx.matches[key].recording_id
        status, _, body = request(srv_a, f"/export/v1/recordings/{rid}")
        assert status == 404, key
    # B's finalized recording is not served by A's server (different server id)
    assert request(srv_a, f"/export/v1/recordings/{fx.matches['B'].recording_id}")[0] == 404
    assert request(srv_a, f"/export/v1/recordings/{fx.matches['A'].recording_id}")[0] == 200


def test_finalized_state_needs_the_sidecar(tmp_path, fx):
    """A recording is served on its sidecar alone: no sidecar / not finalized /
    other server / no match id -> 404, and nothing is written to the directory."""
    src = fx.matches["A"]
    d = tmp_path / "rec"
    d.mkdir()
    meta = json.loads(src.meta_path.read_text(encoding="utf-8"))

    def put(name, **over):
        (d / f"{name}.sqrx").write_bytes(src.sqrx_path.read_bytes())
        m = dict(meta, id=name, filename=f"{name}.sqrx", **over)
        (d / f"{name}.meta.json").write_text(json.dumps(m), encoding="utf-8")

    put("ok")
    put("active", recordingState="active", inProgress=True)
    put("unver", recordingState="unverified", inProgress=True)
    put("stillrunning", inProgress=True)
    put("otherserver", serverId="someone-else")
    put("nomatch", matchId=None)
    put("badmatch", matchId="a\r\nX-Evil: 1")
    (d / "nosidecar.sqrx").write_bytes(src.sqrx_path.read_bytes())
    api = ExportApi(token=TOKEN, server_id=SERVER_A_ID, stats_db=fx.stats_dbs[SERVER_A_ID],
                    recordings_dir=d)
    before = sorted(p.name for p in d.iterdir())
    got = {n: api.handle("GET", f"/export/v1/recordings/{n}", f"Bearer {TOKEN}").status
           for n in ("ok", "active", "unver", "stillrunning", "otherserver", "nomatch",
                     "badmatch", "nosidecar", "absent")}
    assert got == {"ok": 200, "active": 404, "unver": 404, "stillrunning": 404,
                   "otherserver": 404, "nomatch": 404, "badmatch": 404,
                   "nosidecar": 404, "absent": 404}
    assert sorted(p.name for p in d.iterdir()) == before       # read-only


def test_total_frames_falls_back_to_counting(tmp_path, fx):
    src = fx.matches["A"]
    meta = json.loads(src.meta_path.read_text(encoding="utf-8"))
    meta.pop("totalFrames")
    (tmp_path / "r.sqrx").write_bytes(src.sqrx_path.read_bytes())
    (tmp_path / "r.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    api = ExportApi(token=TOKEN, server_id=SERVER_A_ID, stats_db=fx.stats_dbs[SERVER_A_ID],
                    recordings_dir=tmp_path)
    res = api.handle("GET", "/export/v1/recordings/r", f"Bearer {TOKEN}")
    assert dict(res.recording.headers)["X-Sqr-Total-Frames"] == "231"


# ---------------------------------------------------------------------------
# 4. cursor paging
# ---------------------------------------------------------------------------

def _walk(port, limit):
    seen, cursor, pages = [], None, 0
    while True:
        path = f"/export/v1/matches?limit={limit}"
        if cursor:
            path += f"&after={cursor}"
        page = get_json(port, path)
        pages += 1
        assert len(page["matches"]) <= limit
        seen += [m["matchId"] for m in page["matches"]]
        cursor = page["nextCursor"]
        if cursor is None:
            return seen, pages
        assert pages < 50, "cursor does not advance"


def test_cursor_paging_with_equal_ended_at(multi):
    fx2, port = multi
    everything = get_json(port, "/export/v1/matches?limit=100")
    ids = [m["matchId"] for m in everything["matches"]]
    ended = [m["endedAt"] for m in everything["matches"]]
    # A and both extras end in the very same second; the legacy one is not listed
    assert ended[:3] == [ended[0]] * 3
    assert ids == [ID_A, EXTRA_SAME_1, EXTRA_SAME_2, EXTRA_LATER]
    assert EXTRA_LEGACY not in ids and ID_C not in ids and ID_D not in ids
    assert everything["nextCursor"] is None
    for limit in (1, 2, 3, 4, 5):
        seen, pages = _walk(port, limit)
        assert seen == ids, limit                    # each exactly once, in order
        if limit == 1:
            assert pages == 5                        # 4 full pages + the empty last
    # nextCursor is "<endedAt>:<matchId>" of the last item of a full page
    first = get_json(port, "/export/v1/matches?limit=1")
    assert first["nextCursor"] == f"{ended[0]}:{ID_A}"
    # `after` is strictly after: same second, larger id
    nxt = get_json(port, f"/export/v1/matches?after={ended[0]}:{ID_A}&limit=100")
    assert [m["matchId"] for m in nxt["matches"]] == ids[1:]
    assert nxt["nextCursor"] is None
    # a cursor that no longer names a row still works (only its position counts)
    gone = get_json(port, f"/export/v1/matches?after={ended[0]}:5a7e0a00-7787-4a00-8000-00000000a0ff")
    assert [m["matchId"] for m in gone["matches"]] == [EXTRA_LATER]
    end = get_json(port, f"/export/v1/matches?after={ended[3]}:{EXTRA_LATER}")
    assert end["matches"] == [] and end["nextCursor"] is None


def test_paging_defaults_and_limits(multi):
    fx2, port = multi
    assert len(get_json(port, "/export/v1/matches")["matches"]) == 4       # default 50
    assert len(get_json(port, "/export/v1/matches?limit=100")["matches"]) == 4
    assert len(get_json(port, "/export/v1/matches?limit=100000")["matches"]) == 4  # clamped
    assert len(get_json(port, "/export/v1/matches?limit=2")["matches"]) == 2


def test_bad_limit_and_cursor_are_400(srv_a):
    bad = ["limit=0", "limit=-1", "limit=abc", "limit=", "limit=1.5", "limit=1&limit=2",
           "limit=%20", "limit=1e3", "limit=99999999999999",
           "after=", "after=garbage", "after=1", "after=:x", "after=abc:def",
           "after=1788286190:", "after=1788286190:../x", "after=1788286190:a:b",
           "after=-5:abc", "after=1788286190:" + "a" * 200, "after=%00",
           "after=1&after=2", "limit"]
    for q in bad:
        status, _, body = request(srv_a, f"/export/v1/matches?{q}")
        assert status == 400, q
        assert TOKEN not in body.decode()
    assert request(srv_a, "/export/v1/matches?limit=5&after=1788286190:abc")[0] == 200
    assert request(srv_a, "/export/v1/matches?unknown=1")[0] == 200         # ignored


# ---------------------------------------------------------------------------
# 5. recording stream
# ---------------------------------------------------------------------------

def _ndjson_lines(data: bytes) -> list[str]:
    assert data.endswith(b"\n")
    return data.decode("utf-8").split("\n")[:-1]


def test_recording_gzip_zstd_identity(fx, srv_a):
    m = fx.matches["A"]
    path = f"/export/v1/recordings/{m.recording_id}"
    status, hdrs, body = request(srv_a, path, headers={"Accept-Encoding": "gzip"})
    assert status == 200
    assert hdrs["Content-Encoding"] == "gzip"
    assert hdrs["Content-Type"] == "application/x-ndjson"
    assert hdrs["Cache-Control"] == "no-store"
    assert hdrs["X-Sqr-Match-Id"] == ID_A
    assert hdrs["Transfer-Encoding"] == "chunked"       # streamed, no Content-Length
    assert "Content-Length" not in hdrs
    assert "ETag" not in hdrs
    gz_lines = _ndjson_lines(gzip.decompress(body))
    assert len(gz_lines) == int(hdrs["X-Sqr-Total-Frames"]) == 231
    frames = [json.loads(x) for x in gz_lines]
    assert sum(1 for f in frames if f.get("t") == "pos") == 171
    assert sum(1 for f in frames if f.get("t") != "pos") == 60

    status, hdrs2, zbody = request(srv_a, path, headers={"Accept-Encoding": "zstd, gzip"})
    assert status == 200 and hdrs2["Content-Encoding"] == "zstd"
    assert hdrs2["X-Sqr-Total-Frames"] == "231" and hdrs2["X-Sqr-Match-Id"] == ID_A
    assert _ndjson_lines(zstd_decode(zbody)) == gz_lines
    # zstd is the on-disk body verbatim (passthrough), not recompressed
    assert zbody == m.sqrx_path.read_bytes()[len(zbody) * -1:]

    status, hdrs3, plain = request(srv_a, path)
    assert status == 200 and "Content-Encoding" not in hdrs3
    assert _ndjson_lines(plain) == gz_lines
    # q=0 means "not acceptable"
    status, hdrs4, _ = request(srv_a, path, headers={"Accept-Encoding": "zstd;q=0, gzip"})
    assert hdrs4["Content-Encoding"] == "gzip"


def test_recording_of_b_on_b_server(fx, srv_b):
    m = fx.matches["B"]
    status, hdrs, body = request(srv_b, f"/export/v1/recordings/{m.recording_id}",
                                 headers={"Accept-Encoding": "gzip"})
    assert status == 200 and hdrs["X-Sqr-Match-Id"] == ID_B
    assert len(_ndjson_lines(gzip.decompress(body))) == int(hdrs["X-Sqr-Total-Frames"])


def test_head_requests(fx, srv_a):
    rid = fx.matches["A"].recording_id
    status, hdrs, body = request(srv_a, "/export/v1/info", method="HEAD")
    assert status == 200 and body == b"" and int(hdrs["Content-Length"]) > 0
    status, hdrs, body = request(srv_a, f"/export/v1/recordings/{rid}", method="HEAD",
                                 headers={"Accept-Encoding": "gzip"})
    assert status == 200 and body == b""
    assert hdrs["X-Sqr-Total-Frames"] == "231"
    assert request(srv_a, "/export/v1/info", method="HEAD", token=None)[0] == 401
    assert request(srv_a, f"/export/v1/recordings/{fx.matches['C'].recording_id}",
                   method="HEAD")[0] == 404


def test_old_recording_endpoint_still_cacheable(fx, srv_a):
    """The shared streaming code keeps its old headers on /api/recording/<id>."""
    rid = fx.matches["A"].recording_id
    status, hdrs, _ = request(srv_a, f"/api/recording/{rid}", token=None,
                              headers={"Accept-Encoding": "gzip"})
    assert status == 200
    assert hdrs["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "ETag" in hdrs and "X-Sqr-Total-Frames" not in hdrs


# ---------------------------------------------------------------------------
# 6. hash
# ---------------------------------------------------------------------------

def test_bundle_sha256_is_stable_and_canonical(srv_a, srv_b):
    for port, mid in ((srv_a, ID_A), (srv_b, ID_B)):
        listed = get_json(port, "/export/v1/matches")["matches"][0]["bundleSha256"]
        d1 = get_json(port, f"/export/v1/matches/{mid}")
        d2 = get_json(port, f"/export/v1/matches/{mid}")
        assert d1 == d2
        assert d1["bundleSha256"] == d2["bundleSha256"] == listed
        assert get_json(port, "/export/v1/matches")["matches"][0]["bundleSha256"] == listed
        want = hashlib.sha256(json.dumps(
            d1["bundle"], sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        assert d1["bundleSha256"] == want
        assert len(want) == 64
    assert get_json(srv_a, f"/export/v1/matches/{ID_A}")["bundleSha256"] != \
        get_json(srv_b, f"/export/v1/matches/{ID_B}")["bundleSha256"]


def test_canonical_json_definition():
    assert canonical_json({"b": [1, {"z": None, "a": "ş"}], "a": 1.5}) == \
        '{"a":1.5,"b":[1,{"a":"ş","z":null}]}'.encode()


# ---------------------------------------------------------------------------
# 7. hostile ids
# ---------------------------------------------------------------------------

def test_path_traversal_and_bad_ids(fx, srv_a):
    # a finalized-looking recording just OUTSIDE the recordings dir
    outside = fx.recording_dirs[SERVER_A_ID].parent / "secret.sqrx"
    meta = json.loads(fx.matches["A"].meta_path.read_text(encoding="utf-8"))
    outside.write_bytes(fx.matches["A"].sqrx_path.read_bytes())
    outside.with_suffix(".meta.json").write_text(json.dumps(meta), encoding="utf-8")
    try:
        bad = [
            "..", "../secret", "..%2fsecret", "%2e%2e%2fsecret", "%2e%2e/secret",
            "..%5csecret", "..\\secret", "%5c..%5csecret", "/etc/passwd",
            "%2fetc%2fpasswd", "..%00", "a%00b", "%00", "a/b", "a//b", "./x",
            "secret.sqrx", "x.meta.json", "a.b", "a%20b", "%252e%252e",
            "a" * 300, "%C3%A9", "a;b", "a:b", "*", "?", "x?y=1",
        ]
        for bid in bad:
            for kind in ("recordings", "matches"):
                status, _, body = request(srv_a, f"/export/v1/{kind}/{bid}")
                assert status in (400, 404), (kind, bid, status)
                assert b"secret" not in body
        for target in (b"/export/v1/recordings/a\x00b", b"/export/v1/recordings/..\\secret",
                       b"/export/v1/matches/a\x00b", b"/export/v1/recordings/\x00",
                       b"/export/v1/recordings/../secret",
                       b"/export/v1/recordings/%2e%2e/secret",
                       b"/export/v1/recordings/a b", "/export/v1/recordings/é".encode()):
            assert raw_request(srv_a, target) in (400, 404), target
        # trailing newline must not slip through a `$` anchor
        assert raw_request(srv_a, b"/export/v1/recordings/x%0a") in (400, 404)
        # sanity: the same file addressed legitimately by name is not served either
        assert request(srv_a, "/export/v1/recordings/secret")[0] == 404
    finally:
        outside.unlink()
        outside.with_suffix(".meta.json").unlink()


# ---------------------------------------------------------------------------
# 8. methods
# ---------------------------------------------------------------------------

def test_other_methods_are_405(srv_a):
    for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
        for path in ("/export/v1/info", "/export/v1/matches", "/export/v1/nope"):
            for token in (TOKEN, None):
                data = b"{}" if method in ("POST", "PUT", "PATCH") else None
                status, hdrs, body = request(srv_a, path, method=method, data=data,
                                             token=token)
                assert status == 405, (method, path)
                assert hdrs["Allow"] == "GET, HEAD"
                assert "Access-Control-Allow-Origin" not in hdrs
    # outside /export/v1 the old behaviour stays (no handler -> 501)
    assert request(srv_a, "/health", method="POST", data=b"x", token=None)[0] == 501


# ---------------------------------------------------------------------------
# 9. the token never leaks
# ---------------------------------------------------------------------------

def test_token_never_in_bodies_or_logs(fx, tmp_path, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    token_file = tmp_path / "export.token"
    secret = "S3cr3t-" + "k" * 40
    token_file.write_text(secret + "\n", encoding="utf-8")
    if os.name == "posix":
        token_file.chmod(0o600)
    api = configure_export(token_file, server_id=SERVER_A_ID,
                           stats_db=fx.stats_dbs[SERVER_A_ID],
                           recordings_dir=fx.recording_dirs[SERVER_A_ID])
    assert api is not None and secret not in repr(api) and secret not in str(api)
    srv = serve_in_background("127.0.0.1", 0, _TickBeat(),
                              recordings_dir=fx.recording_dirs[SERVER_A_ID],
                              stats_db=fx.stats_dbs[SERVER_A_ID], export_api=api)
    port = srv.server_address[1]
    blobs = []
    try:
        wrong = "W" * 20 + "-wrong-token-" + "z" * 20
        for token in (secret, wrong, None):
            for path in EXPORT_PATHS + [f"/export/v1/recordings/{fx.matches['A'].recording_id}",
                                        "/export/v1/matches?limit=x"]:
                status, hdrs, body = request(port, path, token=token)
                blobs.append(body)
                blobs.append("\n".join(f"{k}: {v}" for k, v in hdrs.items()).encode())
                assert wrong.encode() not in body
        blob = b"\n".join(blobs)
        assert secret.encode() not in blob
        assert wrong.encode() not in blob
    finally:
        stop(srv)
    out = capsys.readouterr()
    assert secret not in out.out + out.err
    assert wrong not in out.out + out.err
    assert secret not in caplog.text and wrong not in caplog.text


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def _cfg(tmp_path, caplog, content, **kw):
    caplog.set_level(logging.DEBUG, logger="sqreader.export")
    f = tmp_path / "t.token"
    if content is not None:
        f.write_bytes(content)
        if os.name == "posix":
            f.chmod(0o600)
    api = configure_export(f, server_id="s", stats_db=tmp_path / "s.db", **kw)
    return api, [r for r in caplog.records if r.name == "sqreader.export"]


def test_config_not_set_is_silent_and_off(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="sqreader.export")
    assert configure_export(None, server_id="s", stats_db=tmp_path / "s.db") is None
    assert configure_export("", server_id="s", stats_db=tmp_path / "s.db") is None
    assert caplog.records == []


@pytest.mark.parametrize("content, level, phrase", [
    (None, logging.WARNING, "does not exist"),
    (b"", logging.WARNING, "empty"),
    (b"  \n\t\n", logging.WARNING, "empty"),
    (b"a" * (MIN_TOKEN_LEN - 1), logging.ERROR, "shorter than"),
    (b"a" * 20 + b" " + b"b" * 20, logging.ERROR, "printable ASCII"),
    ("ü".encode() * 40, logging.ERROR, "printable ASCII"),
    (b"\xff\xfe\xfa", logging.WARNING, "not readable"),
])
def test_config_unusable_token_file_leaves_api_off(tmp_path, caplog, content, level, phrase):
    api, records = _cfg(tmp_path, caplog, content)
    assert api is None
    assert len(records) == 1                                  # one line
    assert records[0].levelno == level and phrase in records[0].getMessage()


def test_config_short_token_error_does_not_show_token(tmp_path, caplog):
    api, records = _cfg(tmp_path, caplog, b"tiny-secret-value")
    assert api is None and "tiny-secret-value" not in caplog.text


def test_config_good_file(tmp_path, caplog):
    api, records = _cfg(tmp_path, caplog, (TOKEN + "\r\n").encode())
    assert api is not None and records == []
    assert api.handle("GET", "/export/v1/info", f"Bearer {TOKEN}").status in (200, 503)
    assert api.handle("GET", "/export/v1/info", "Bearer nope").status == 401


def test_config_needs_stats_db(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="sqreader.export")
    f = tmp_path / "t"
    f.write_text(TOKEN, encoding="utf-8")
    assert configure_export(f, server_id="s", stats_db=None) is None
    assert len(caplog.records) == 1 and "--stats-db" in caplog.records[0].getMessage()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_config_warns_when_token_file_is_group_readable(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="sqreader.export")
    f = tmp_path / "t"
    f.write_text(TOKEN, encoding="utf-8")
    f.chmod(0o640)
    assert configure_export(f, server_id="s", stats_db=tmp_path / "s.db") is not None
    assert any("chmod 600" in r.getMessage() for r in caplog.records)
    caplog.clear()
    f.chmod(0o600)
    assert configure_export(f, server_id="s", stats_db=tmp_path / "s.db") is not None
    assert caplog.records == []


def test_config_default_and_cli_option():
    assert "export_token_file" in config.DEFAULTS
    assert config.DEFAULTS["export_token_file"] is None
    cp = subprocess.run([sys.executable, "-m", "sqreader", "serve", "--help"], cwd=REPO,
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0 and "--export-token-file" in cp.stdout


def test_example_config_has_only_a_placeholder():
    ex = json.loads((REPO / "sqreader.config.example.json").read_text(encoding="utf-8"))
    assert ex["export_token_file"] is None


# ---------------------------------------------------------------------------
# 10. golden files
# ---------------------------------------------------------------------------

def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and p.name != "README.md"}


def test_golden_files_match_regeneration(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "dump_zz_golden", REPO / "scripts" / "dump_zz_golden.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fresh = tmp_path / "fresh"
    names = mod.generate(fresh)
    assert names == sorted(p for p in _tree(fresh))
    committed, regenerated = _tree(GOLDEN), _tree(fresh)
    assert sorted(committed) == sorted(regenerated)
    for name in committed:
        assert committed[name] == regenerated[name], name
    # a second run into the same place changes nothing
    mod.generate(fresh)
    assert _tree(fresh) == regenerated
    # the promised file set
    for sid, mid, rec in (
            (SERVER_A_ID, ID_A, "2026-09-01_180020_Fallujah_RAAS_v1_0000a001"),
            (SERVER_B_ID, ID_B, "2026-09-01_180020_Narva_RAAS_v1_0000b001")):
        for f in ("info.json", "matches.json", f"match-{mid}.json",
                  f"recording-{rec}.ndjson.gz", f"recording-{rec}.headers.json"):
            assert f"{sid}/{f}" in committed, f
    assert (GOLDEN / "README.md").is_file()
    # the stored recordings are valid gzip with the advertised frame count
    for sid, rec in ((SERVER_A_ID, "2026-09-01_180020_Fallujah_RAAS_v1_0000a001"),
                     (SERVER_B_ID, "2026-09-01_180020_Narva_RAAS_v1_0000b001")):
        hdrs = json.loads(committed[f"{sid}/recording-{rec}.headers.json"])
        lines = gzip.decompress(committed[f"{sid}/recording-{rec}.ndjson.gz"]).count(b"\n")
        assert str(lines) == hdrs["X-Sqr-Total-Frames"]
        assert committed[f"{sid}/recording-{rec}.ndjson.gz"][4:8] == b"\0\0\0\0"   # mtime 0
