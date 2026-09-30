"""Export API control plane: POST /export/v1/control/recording, POST
/export/v1/ack and the extended GET /export/v1/health.

Recording behaviour is driven through the reader's REAL recording path: the
synthetic snapshots of tests/zz_fixture.py go through `StatsStore.record_tick`
and `recorder._handle_snap` (with the gate the serve loop installs), exactly as
`cli._consume_full` feeds them. Nothing writes a `.sqrx` or a stats row by hand.

Status codes pinned here (also the contract for the Zero Zone panel):

  401 unauthorized             no / wrong Bearer token (before anything else)
  400 bad_query                a query string on a POST
  415 unsupported_media_type   Content-Type is not application/json
  411 length_required          no Content-Length (e.g. chunked)
  413 payload_too_large        Content-Length above the endpoint limit (the body is
                               not used): 4096 control, 24576 ack
  400 bad_json                 not UTF-8 JSON / not an object / duplicate key / NaN
  400 unknown_key              a key other than the documented ones
  400 bad_enabled | bad_actor | bad_reason | bad_recordings | too_many_recordings
  405 method_not_allowed       GET on a POST endpoint (Allow: POST)
  404 not_found                export switched off (no token file)
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import socket
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import zz_fixture
from zz_fixture import MATCH_IDS, SERVER_A_ID, SERVER_B_ID, build_zz_fixture

from sqreader import netpolicy
from sqreader import recorder as _recorder
from sqreader import sqrx as _sqrx
from sqreader.export_api import (
    ACK_MAX_BODY_BYTES,
    MAX_ACK_RECORDINGS,
    MAX_BODY_BYTES,
    MAX_REC_ID_LEN,
    SCHEMA_EXPORT,
    ExportApi,
)
from sqreader.export_state import (
    AUDIT_FILE,
    STATE_FILE,
    ExportState,
    RecordingGate,
    RetentionPolicy,
)
from sqreader.httpsrv import _TickBeat, serve_in_background
from sqreader.recording_lifecycle import MATCH_TRANSITION_CONFIRM_TICKS
from sqreader.sqrx import SqrxReader
from sqreader.stats import StatsStore

TOKEN = "zz-control-test-token-" + "0123456789abcdef" * 2
CONTROL = "/export/v1/control/recording"
ACK = "/export/v1/ack"
ID_A, ID_B, ID_C, ID_D = (MATCH_IDS[k] for k in "ABCD")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def base_fx(tmp_path_factory):
    return build_zz_fixture(tmp_path_factory.mktemp("zzc") / "fixture")


@pytest.fixture
def fx(base_fx, tmp_path):
    """A private copy of the fixture: POSTs write state files next to it."""
    root = tmp_path / "fixture"
    shutil.copytree(base_fx.root, root)
    return root


def make_api(root: Path, server_id=SERVER_A_ID, **kw) -> ExportApi:
    return ExportApi(token=TOKEN, server_id=server_id,
                     stats_db=root / server_id / "stats.db",
                     recordings_dir=root / server_id / "recordings",
                     squad_port=7787, **kw)


def start(root: Path, server_id=SERVER_A_ID, api=True, **kw):
    export = make_api(root, server_id, **kw) if api else None
    srv = serve_in_background(
        "127.0.0.1", 0, _TickBeat(),
        recordings_dir=root / server_id / "recordings",
        stats_db=root / server_id / "stats.db", export_api=export)
    return srv, srv.server_address[1], export


def stop(srv):
    srv.shutdown()
    srv.server_close()


def http(port, path, *, method="POST", body=None, token=TOKEN,
         ctype="application/json", headers=None):
    """(status, headers, body bytes). HTTP errors are returned, not raised."""
    hdrs = dict(headers or {})
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    data = body
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
    if data is not None and ctype is not None:
        hdrs["Content-Type"] = ctype
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers=hdrs, method=method)
    if data is not None and ctype is None:
        req.remove_header("Content-type")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers.items()), r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers.items()), e.read()
        finally:
            e.close()


def post_json(port, path, obj, **kw):
    status, hdrs, body = http(port, path, body=obj, **kw)
    return status, json.loads(body), hdrs


def health(port):
    status, _h, body = http(port, "/export/v1/health", method="GET")
    assert status == 200
    return json.loads(body)


def raw_post(port, head_lines: list[bytes], body: bytes = b"") -> int:
    """A POST with hand-made headers (urllib always adds Content-Length)."""
    return raw_post_to(port, CONTROL, head_lines, body)


def raw_post_to(port, path: str, head_lines: list[bytes], body: bytes = b"") -> int:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(b"POST " + path.encode() + b" HTTP/1.1\r\nHost: x\r\n"
                  b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
                  + b"".join(h + b"\r\n" for h in head_lines)
                  + b"Connection: close\r\n\r\n" + body)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return int(data.split(b" ", 2)[1])


def audit_lines(data_dir: Path) -> list[dict]:
    p = data_dir / AUDIT_FILE
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def db_rows(db: Path, sql: str, args=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# a rig that plays whole matches through the real recording + stats path
# ---------------------------------------------------------------------------

class Rig:
    """One reader data dir: `<root>/<server>/{stats.db,recordings/}`."""

    def __init__(self, root: Path, server_id="zz-ctl") -> None:
        self.server_id = server_id
        self.clock = zz_fixture._Clock()
        self.run = zz_fixture._ServerRun(root, server_id, self.clock)
        self.run.store = StatsStore(self.run.db_path, server_id=server_id)
        self.run.box = zz_fixture._new_state_box()
        self.data_dir = root / server_id
        self.rec_dir = self.run.rec_dir
        self.db = self.run.db_path

    def close(self) -> None:
        if self.run.store is not None:
            self.run.store.close()
            self.run.store = None

    def play(self, key: str, match_id: str, shift_s: int, *, gate=None,
             on_frame=None, finish=True) -> zz_fixture._Spec:
        """One match of match A's script. `on_frame(f)` runs before frame f is fed;
        with `finish` the match ends the way the game reports it."""
        from datetime import timedelta
        spec = dataclasses.replace(zz_fixture._SPEC_A, key=key, server_id=self.server_id)
        run = self.run
        run.box["record_gate"] = gate
        with pytest.MonkeyPatch.context() as mp, \
                mock.patch.object(_recorder, "_utc_now", self.clock.utcnow), \
                mock.patch.object(_sqrx, "time", SimpleNamespace(time=self.clock.time)):
            mp.setitem(zz_fixture.MATCH_IDS, key, match_id)
            mp.setitem(zz_fixture._START, key,
                       zz_fixture._START["A"] + timedelta(seconds=shift_s))
            sim = zz_fixture._Sim(spec)
            self.clock.now = spec.start
            for f in range(spec.n_full):
                if on_frame is not None:
                    on_frame(f)
                events = sim.advance(f)
                snap = sim.full_frame(f, run._next_tick(), "InProgress")
                snap["damageEvents"] = events
                run._feed_full(snap)
            if finish:
                for j in range(MATCH_TRANSITION_CONFIRM_TICKS):
                    run._feed_full(sim.full_frame(spec.n_full + j, run._next_tick(),
                                                  "WaitingPostMatch"))
        return spec

    def sidecars(self) -> dict[str, dict]:
        return {p.name[:-len(".meta.json")]: json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(self.rec_dir.glob("*.meta.json"))}

    def sqrx_files(self) -> list[Path]:
        return sorted(self.rec_dir.glob("*.sqrx"))


MID_1 = "5a7e0a00-7787-4a00-8000-00000000c101"
MID_2 = "5a7e0a00-7787-4a00-8000-00000000c102"
MID_3 = "5a7e0a00-7787-4a00-8000-00000000c103"


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path / "live")
    yield r
    r.close()


# ---------------------------------------------------------------------------
# 1. access control and input validation
# ---------------------------------------------------------------------------

def test_control_and_ack_need_the_token(fx):
    srv, port, _ = start(fx)
    try:
        for path, body in ((CONTROL, {"enabled": False, "actor": "a"}),
                           (ACK, {"recordings": ["x"]})):
            for token in (None, "wrong-" + "x" * 40, TOKEN[:-1]):
                status, obj, hdrs = post_json(port, path, body, token=token)
                assert status == 401, (path, token)
                assert obj == {"schema": SCHEMA_EXPORT, "error": "unauthorized"}
                assert hdrs["WWW-Authenticate"] == "Bearer"
        # nothing changed and nothing was audited
        data_dir = fx / SERVER_A_ID
        assert not (data_dir / STATE_FILE).exists()
        assert audit_lines(data_dir) == []
    finally:
        stop(srv)


def test_control_is_a_plain_404_when_export_is_off(fx):
    srv, port, _ = start(fx, api=False)
    try:
        for path in (CONTROL, ACK):
            status, _hdrs, body = http(port, path, body={"enabled": False, "actor": "a"})
            assert status == 404
            assert b"sqr-export" not in body
        assert not (fx / SERVER_A_ID / STATE_FILE).exists()
    finally:
        stop(srv)


def test_control_rejects_bad_requests(fx):
    srv, port, _ = start(fx)
    data_dir = fx / SERVER_A_ID
    good = {"enabled": False, "reason": "bakım", "actor": "zz-admin-1"}
    try:
        # Content-Type
        for ctype in ("text/plain", "application/x-www-form-urlencoded",
                      "application/jsonx", "text/json", None):
            status, _h, _b = http(port, CONTROL, body=good, ctype=ctype)
            assert status == 415, ctype
        assert http(port, CONTROL, body=good, ctype="Application/JSON; charset=utf-8"
                    )[0] == 200                                       # parameters are fine
        # size: above the limit -> 413, the body is not interpreted
        big = json.dumps({**good, "reason": "x" * (MAX_BODY_BYTES + 10)}).encode()
        assert len(big) > MAX_BODY_BYTES
        assert http(port, CONTROL, body=big)[0] == 413
        assert http(port, CONTROL, body=b"{" + b" " * 200_000 + b"}")[0] == 413
        # no Content-Length (chunked) -> 411
        assert raw_post(port, [b"Content-Type: application/json",
                               b"Transfer-Encoding: chunked"],
                        b"0\r\n\r\n") == 411
        assert raw_post(port, [b"Content-Type: application/json",
                               b"Content-Length: abc"], b"{}") == 411
        # a query string is refused
        assert http(port, CONTROL + "?x=1", body=good)[0] == 400
        # malformed JSON
        for body in (b"", b"{", b"[]", b'"x"', b"null", b"1", b"\xff\xfe",
                     b'{"enabled": true, "enabled": false, "actor": "a"}',   # duplicate
                     b'{"enabled": NaN, "actor": "a"}'):
            status, obj, _h = post_json(port, CONTROL, body)
            assert (status, obj["error"]) == (400, "bad_json"), body
        # keys / types
        cases = [
            ({"enabled": True, "actor": "a", "path": "/etc"}, "unknown_key"),
            ({"enabled": True, "actor": "a", "cmd": "rm -rf /"}, "unknown_key"),
            ({"enabled": True, "actor": "a", "min_free_gb": 0}, "unknown_key"),
            ({"actor": "a"}, "bad_enabled"),
            ({"enabled": "false", "actor": "a"}, "bad_enabled"),
            ({"enabled": 0, "actor": "a"}, "bad_enabled"),
            ({"enabled": None, "actor": "a"}, "bad_enabled"),
            ({"enabled": True}, "bad_actor"),
            ({"enabled": True, "actor": ""}, "bad_actor"),
            ({"enabled": True, "actor": " \n\t "}, "bad_actor"),
            ({"enabled": True, "actor": 5}, "bad_actor"),
            ({"enabled": True, "actor": "a", "reason": 5}, "bad_reason"),
            ({"enabled": True, "actor": "a", "reason": ["x"]}, "bad_reason"),
        ]
        for body, code in cases:
            status, obj, _h = post_json(port, CONTROL, body)
            assert (status, obj["error"]) == (400, code), body
        # GET on a POST endpoint
        for path in (CONTROL, ACK):
            status, hdrs, _b = http(port, path, method="GET")
            assert status == 405 and hdrs["Allow"] == "POST"
        # other methods on the POST endpoints stay 405 (GET, HEAD)
        assert http(port, CONTROL, method="PUT", body=good)[0] == 405
        # ...and none of the rejected requests changed or audited anything. The
        # only accepted one (the charset case above) is the single audit line.
        assert len(audit_lines(data_dir)) == 1
    finally:
        stop(srv)


def test_control_success_shape(fx):
    srv, port, _ = start(fx)
    try:
        status, obj, hdrs = post_json(
            port, CONTROL, {"enabled": False, "reason": "disk bakımı", "actor": "zz:42"})
        assert status == 200
        assert hdrs["Cache-Control"] == "no-store"
        assert set(obj) == {"schema", "recordingEnabled", "recordingEnabledReason",
                            "control"}
        assert obj["schema"] == SCHEMA_EXPORT
        assert obj["recordingEnabled"] is False
        assert obj["recordingEnabledReason"] == "disabled_by_admin"
        assert set(obj["control"]) == {"enabled", "reason", "actor", "changedAt"}
        assert obj["control"]["enabled"] is False
        assert obj["control"]["reason"] == "disk bakımı"
        assert obj["control"]["actor"] == "zz:42"
        assert obj["control"]["changedAt"].endswith("Z")
        # `reason` is optional, and control characters / length are cleaned
        status, obj, _h = post_json(port, CONTROL, {
            "enabled": True, "actor": "a\nb\x00c‮" + "z" * 300,
            "reason": "line1\r\nline2\x07" + "y" * 500})
        assert status == 200 and obj["recordingEnabled"] is True
        assert obj["recordingEnabledReason"] is None
        assert "\n" not in obj["control"]["actor"] and "\x00" not in obj["control"]["actor"]
        assert "‮" not in obj["control"]["actor"]
        assert len(obj["control"]["actor"]) <= 100 and len(obj["control"]["reason"]) <= 200
        status, obj, _h = post_json(port, CONTROL, {"enabled": True, "actor": "x"})
        assert status == 200 and obj["control"]["reason"] == ""
    finally:
        stop(srv)


# ---------------------------------------------------------------------------
# 2. what the switch does to the recorder (real recording path)
# ---------------------------------------------------------------------------

def test_disabled_before_the_match_records_nothing_but_stats_flow(rig):
    state = ExportState(rig.data_dir)
    state.set_recording(False, "bakım", "zz-admin", "127.0.0.1")
    gate = RecordingGate(state, rig.rec_dir, RetentionPolicy())
    rig.play("E", MID_1, 0, gate=gate)
    assert rig.sqrx_files() == [] and rig.sidecars() == {}
    rows = db_rows(rig.db, "SELECT match_id, status, end_state FROM matches")
    assert rows == [(MID_1, "final", "finalized")]
    assert db_rows(rig.db, "SELECT COUNT(*) FROM player_matches WHERE match_id=?",
                   (MID_1,))[0][0] > 0
    assert db_rows(rig.db, "SELECT COUNT(*) FROM kill_events WHERE match_id=?",
                   (MID_1,))[0][0] > 0


def test_disabling_mid_match_does_not_cut_the_open_recording(rig):
    state = ExportState(rig.data_dir)
    gate = RecordingGate(state, rig.rec_dir, RetentionPolicy())
    spec = None

    def switch_off(f):
        if f == 30:
            state.set_recording(False, "yarıda kapat", "zz-admin", "10.0.0.5")

    spec = rig.play("E", MID_1, 0, gate=gate, on_frame=switch_off)
    # The open recording ran to the end of its match and finalized normally.
    assert len(rig.sqrx_files()) == 1
    (rec_id, meta), = rig.sidecars().items()
    assert meta["recordingState"] == "finalized" and meta["inProgress"] is False
    assert meta["matchId"] == MID_1 and meta["ticks"] == spec.n_full
    with SqrxReader(rig.sqrx_files()[0]) as r:
        frames = [json.loads(line) for line in r]
    assert len(frames) == spec.n_full                         # every frame, none dropped
    assert frames[-1]["gameState"]["matchState"] == "InProgress"
    # The NEXT match (after the confirmed transition) is not recorded ...
    rig.play("F", MID_2, 7200, gate=gate)
    assert len(rig.sqrx_files()) == 1
    assert list(rig.sidecars()) == [rec_id]
    # ... but its stats are.
    rows = dict(db_rows(rig.db, "SELECT match_id, end_state FROM matches"))
    assert rows == {MID_1: "finalized", MID_2: "finalized"}
    # Switching it back on records the following match again.
    state.set_recording(True, "", "zz-admin", "10.0.0.5")
    rig.play("G", MID_3, 14400, gate=gate)
    assert len(rig.sqrx_files()) == 2
    assert {m["matchId"] for m in rig.sidecars().values()} == {MID_1, MID_3}


def test_enabling_without_a_gate_changes_nothing_for_the_recorder(rig):
    """The recorder without a gate (tests, `watch`) is byte-for-byte the old one."""
    rig.play("E", MID_1, 0, gate=None)
    assert len(rig.sqrx_files()) == 1


def test_state_survives_a_restart(rig, fx):
    state = ExportState(rig.data_dir)
    state.set_recording(False, "bakım", "zz-admin", "127.0.0.1")
    # "restart": a new process builds new objects from the files
    state2 = ExportState(rig.data_dir)
    assert state2.recording_control()["enabled"] is False
    assert state2.recording_control()["reason"] == "bakım"
    gate2 = RecordingGate(state2, rig.rec_dir, RetentionPolicy())
    assert gate2() == "disabled_by_admin"
    rig.play("E", MID_1, 0, gate=gate2)
    assert rig.sqrx_files() == []
    # the file is plain, atomic JSON and never leaves a temp file behind
    assert json.loads((rig.data_dir / STATE_FILE).read_text())["recording"]["enabled"] is False
    assert not list(rig.data_dir.glob("*.tmp"))
    # ... and the HTTP layer of a restarted reader reports it
    srv, port, _ = start(fx)
    try:
        post_json(port, CONTROL, {"enabled": False, "reason": "r", "actor": "zz"})
    finally:
        stop(srv)
    srv, port, _ = start(fx)                       # new server, new ExportApi
    try:
        h = health(port)
        assert h["recordingEnabled"] is False
        assert h["recordingEnabledReason"] == "disabled_by_admin"
        assert h["recordingControl"]["actor"] == "zz"
    finally:
        stop(srv)


def test_corrupt_state_file_falls_back_to_defaults(tmp_path):
    (tmp_path / STATE_FILE).write_text("{ not json", encoding="utf-8")
    state = ExportState(tmp_path)
    assert state.recording_control()["enabled"] is True
    assert state.acked_ids() == set()
    state.set_recording(False, "", "a", None)             # and it recovers
    assert ExportState(tmp_path).recording_control()["enabled"] is False


# ---------------------------------------------------------------------------
# 3. audit log
# ---------------------------------------------------------------------------

def test_every_change_is_one_audit_line_and_never_holds_the_token(fx):
    srv, port, _ = start(fx)
    data_dir = fx / SERVER_A_ID
    try:
        post_json(port, CONTROL, {"enabled": False, "reason": "bir", "actor": "zz-1"})
        post_json(port, CONTROL, {"enabled": True, "reason": "iki", "actor": "zz-2"})
        post_json(port, CONTROL, {"enabled": False, "reason": "üç", "actor": "zz-3"})
        post_json(port, CONTROL, {"enabled": False, "actor": "zz", "junk": 1})   # 400
        post_json(port, CONTROL, {"enabled": False, "actor": "zz"}, token="bad" * 20)
        lines = audit_lines(data_dir)
        assert len(lines) == 3
        assert [(x["action"], x["new"]["enabled"], x["actor"], x["reason"])
                for x in lines] == [
            ("recording_control", False, "zz-1", "bir"),
            ("recording_control", True, "zz-2", "iki"),
            ("recording_control", False, "zz-3", "üç"),
        ]
        assert [x["old"]["enabled"] for x in lines] == [True, False, True]
        assert all(x["clientIp"] == "127.0.0.1" for x in lines)
        assert all(x["ts"].endswith("Z") and "T" in x["ts"] for x in lines)
        text = (data_dir / AUDIT_FILE).read_text(encoding="utf-8")
        assert TOKEN not in text and "Bearer" not in text and "Authorization" not in text
        assert TOKEN not in (data_dir / STATE_FILE).read_text(encoding="utf-8")
    finally:
        stop(srv)


def test_audit_failure_rolls_the_change_back(fx):
    data_dir = fx / SERVER_A_ID
    (data_dir / AUDIT_FILE).mkdir()                       # cannot be appended to
    srv, port, _ = start(fx)
    try:
        status, obj, _h = post_json(port, CONTROL, {"enabled": False, "actor": "zz"})
        assert (status, obj["error"]) == (500, "audit_unavailable")
        assert health(port)["recordingEnabled"] is True   # the switch did not move
        assert ExportState(data_dir).recording_control()["enabled"] is True
    finally:
        stop(srv)


# ---------------------------------------------------------------------------
# 4. ack
# ---------------------------------------------------------------------------

def rec_ids(fx_base):
    a = fx_base.matches["A"].recording_id
    return {"A": a, "B": fx_base.matches["B"].recording_id,
            "C": fx_base.matches["C"].recording_id,
            "D": fx_base.matches["D"].recording_id}


def test_ack_accepts_finalized_and_rejects_everything_else(base_fx, fx):
    ids = rec_ids(base_fx)
    srv, port, _ = start(fx)                              # server zz-7787
    data_dir = fx / SERVER_A_ID
    try:
        assert health(port)["pendingAck"] == 1            # A only (C unverified, D active)
        ask = [ids["A"], ids["C"], ids["D"], ids["B"], "no-such-recording",
               "../evil", "a/b", "x" * 200, ids["A"]]
        status, obj, _h = post_json(port, ACK, {"recordings": ask})
        assert status == 200
        assert set(obj) == {"schema", "accepted", "rejected"}
        assert obj["schema"] == SCHEMA_EXPORT
        assert obj["accepted"] == [ids["A"]]
        # unverified / active / another server / unknown / malformed: ONE answer
        assert obj["rejected"] == [ids["C"], ids["D"], ids["B"], "no-such-recording",
                                   "../evil", "a/b", "x" * 200]
        assert health(port)["pendingAck"] == 0
        # idempotent: the same ack again is accepted again, nothing new is stored
        lines_before = audit_lines(data_dir)
        status, obj2, _h = post_json(port, ACK, {"recordings": [ids["A"]]})
        assert status == 200 and obj2["accepted"] == [ids["A"]] and obj2["rejected"] == []
        assert audit_lines(data_dir) == lines_before
        assert [x["action"] for x in lines_before] == ["ack"]
        assert lines_before[0]["recordings"] == [ids["A"]]
        # persisted (survives a restart) and readable by the retention command
        assert ExportState(data_dir, readonly=True).acked_ids() == {ids["A"]}
    finally:
        stop(srv)
    srv, port, _ = start(fx)
    try:
        assert health(port)["pendingAck"] == 0
    finally:
        stop(srv)


def test_ack_of_the_other_server_recording_is_rejected_on_this_server(base_fx, fx):
    ids = rec_ids(base_fx)
    srv, port, _ = start(fx, SERVER_B_ID)
    try:
        status, obj, _h = post_json(port, ACK, {"recordings": [ids["A"], ids["B"]]})
        assert (obj["accepted"], obj["rejected"]) == ([ids["B"]], [ids["A"]])
    finally:
        stop(srv)


def test_ack_validation(fx):
    srv, port, _ = start(fx)
    try:
        assert http(port, ACK, body={"recordings": ["a"]}, ctype="text/plain")[0] == 415
        assert http(port, ACK, body=b"x" * (ACK_MAX_BODY_BYTES + 1))[0] == 413
        for body, code in [
            ({}, "bad_recordings"),
            ({"recordings": []}, "bad_recordings"),
            ({"recordings": "abc"}, "bad_recordings"),
            ({"recordings": [1, 2]}, "bad_recordings"),
            ({"recordings": [["a"]]}, "bad_recordings"),
            ({"recordings": [""]}, "bad_recordings"),
            ({"recordings": ["x" * 201]}, "bad_recordings"),
            ({"recordings": ["a"], "extra": 1}, "unknown_key"),
            ({"recordings": ["a"] * 101}, "too_many_recordings"),
            ({"recordings": [f"r{i}" for i in range(101)]}, "too_many_recordings"),
        ]:
            status, obj, _h = post_json(port, ACK, body)
            assert (status, obj["error"]) == (400, code), body
        # exactly 100 is fine
        status, obj, _h = post_json(port, ACK,
                                    {"recordings": [f"r{i}" for i in range(100)]})
        assert status == 200 and len(obj["rejected"]) == 100 and obj["accepted"] == []
        assert audit_lines(fx / SERVER_A_ID) == []          # nothing acked, nothing logged
    finally:
        stop(srv)


REAL_ID = "2026-05-25_002432_Gorodok_RAAS_v1_40323435"       # 42 chars, a real shape


def _ack_body(ids, pad_to=None) -> bytes:
    raw = json.dumps({"recordings": ids}).encode()
    if pad_to is not None:
        assert len(raw) <= pad_to
        raw += b" " * (pad_to - len(raw))                   # trailing JSON whitespace
    return raw


def test_ack_body_limit_covers_a_full_batch_of_the_longest_ids():
    longest = "x" * MAX_REC_ID_LEN
    ids = [longest] * MAX_ACK_RECORDINGS
    for kwargs in ({}, {"separators": (",", ":")}, {"indent": 2}, {"indent": 4}):
        assert len(json.dumps({"recordings": ids}, **kwargs).encode()
                   ) <= ACK_MAX_BODY_BYTES, kwargs
    # the control endpoint's limit did not grow
    assert MAX_BODY_BYTES == 4096
    assert ExportApi.body_limit(ACK) == ACK_MAX_BODY_BYTES
    assert ExportApi.body_limit(CONTROL) == MAX_BODY_BYTES
    assert ExportApi.max_body_bytes == ACK_MAX_BODY_BYTES    # what the transport reads


def test_ack_takes_100_realistic_long_ids(fx):
    ids = [f"{REAL_ID[:-8]}{i:08d}" for i in range(MAX_ACK_RECORDINGS)]
    assert len(ids) == 100 and len(_ack_body(ids)) > MAX_BODY_BYTES     # the old bug
    srv, port, _ = start(fx)
    try:
        status, obj, _h = post_json(port, ACK, {"recordings": ids})
        assert status == 200 and len(obj["rejected"]) == 100 and obj["accepted"] == []
        # the longest permitted ids, 100 of them
        longest = [f"{i:03d}" + "y" * (MAX_REC_ID_LEN - 3) for i in range(100)]
        status, obj, _h = post_json(port, ACK, {"recordings": longest})
        assert status == 200 and len(obj["rejected"]) == 100
        # 101 ids are refused by the count, not by the size
        status, obj, _h = post_json(port, ACK, {"recordings": ids + [REAL_ID]})
        assert (status, obj["error"]) == (400, "too_many_recordings")
        status, obj, _h = post_json(port, ACK, {"recordings": longest + ["z" * 200]})
        assert (status, obj["error"]) == (400, "too_many_recordings")
    finally:
        stop(srv)


def test_ack_body_size_boundary_and_control_keeps_the_small_limit(fx):
    srv, port, _ = start(fx)
    try:
        at_limit = _ack_body([REAL_ID], pad_to=ACK_MAX_BODY_BYTES)
        assert len(at_limit) == ACK_MAX_BODY_BYTES
        assert http(port, ACK, body=at_limit)[0] == 200
        over = _ack_body([REAL_ID], pad_to=ACK_MAX_BODY_BYTES + 1)
        assert http(port, ACK, body=over)[0] == 413
        # the connection is answered (not reset) for a much bigger declared body too
        assert http(port, ACK, body=b" " * 200_000)[0] == 413
        # chunked / no length is still refused on ack
        assert raw_post_to(port, ACK, [b"Content-Type: application/json",
                                       b"Transfer-Encoding: chunked"],
                           b"0\r\n\r\n") == 411
        # control did NOT inherit the ack limit
        good = {"enabled": True, "reason": "", "actor": "zz-admin-1"}
        padded = json.dumps(good).encode()
        padded += b" " * (MAX_BODY_BYTES + 1 - len(padded))
        assert http(port, CONTROL, body=padded)[0] == 413
        padded = json.dumps(good).encode()
        padded += b" " * (MAX_BODY_BYTES - len(padded))
        assert http(port, CONTROL, body=padded)[0] == 200
    finally:
        stop(srv)


def _raw_headers_only(port, path, declared, *, token=TOKEN, extra=b"", method=b"POST"):
    """Send ONLY the headers of a request that declares `declared` body bytes and
    return (status, seconds until the answer). A server that tried to read the
    body would block until its read timeout instead of answering at once."""
    import time
    auth = b"Authorization: Bearer " + token.encode() + b"\r\n" if token else b""
    t0 = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=8) as s:
        s.sendall(method + b" " + path.encode() + b" HTTP/1.1\r\nHost: x\r\n" + auth
                  + b"Content-Type: application/json\r\n"
                  + b"Content-Length: " + str(declared).encode() + b"\r\n"
                  + extra + b"Connection: close\r\n\r\n")
        data = s.recv(65536)
    return int(data.split(b" ", 2)[1]), time.monotonic() - t0


def test_transport_body_limit_is_per_path_and_nothing_over_it_is_read(fx):
    srv, port, _ = start(fx)
    try:
        # control: 5000 declared bytes > 4096 -> 413 at once, body never read
        status, took = _raw_headers_only(port, CONTROL, 5000)
        assert status == 413 and took < 3.0
        # ... also without auth: 401 (auth still decides), still nothing read
        status, took = _raw_headers_only(port, CONTROL, 5000, token=None)
        assert status == 401 and took < 3.0
        # ack: the same 5000 (and up to 24576) is within ITS limit -> the
        # transport waits for the body, so it is NOT answered before it arrives
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            body = _ack_body([REAL_ID], pad_to=5000)
            s.sendall(b"POST " + ACK.encode() + b" HTTP/1.1\r\nHost: x\r\n"
                      b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
                      b"Content-Type: application/json\r\nContent-Length: 5000\r\n"
                      b"Connection: close\r\n\r\n")
            s.settimeout(0.5)
            with pytest.raises(socket.timeout):
                s.recv(1)                          # no answer before the body
            s.settimeout(10)
            s.sendall(body)
            data = b""
            while chunk := s.recv(65536):
                data += chunk
            assert int(data.split(b" ", 2)[1]) == 200
        assert http(port, ACK, body=_ack_body([REAL_ID], pad_to=ACK_MAX_BODY_BYTES)
                    )[0] == 200                      # 24576 accepted
        status, took = _raw_headers_only(port, ACK, ACK_MAX_BODY_BYTES + 1)
        assert status == 413 and took < 3.0
        # a path that takes no POST reads no body: 405 at once, even for 1 KiB
        for path in ("/export/v1/info", "/export/v1/matches", "/export/v1/ack/"):
            status, took = _raw_headers_only(port, path, 1024)
            assert status == 405 and took < 3.0, path
        status, took = _raw_headers_only(port, "/export/v1/info", 1024, method=b"PUT")
        assert status == 405 and took < 3.0
    finally:
        stop(srv)


def test_oversized_control_body_is_answered_not_reset(fx):
    """The refused body is drained AFTER the 413, so a real client (which sends
    headers and body together) reads the answer instead of a reset."""
    srv, port, _ = start(fx)
    try:
        for size in (MAX_BODY_BYTES + 1, 5000, 60_000):
            assert http(port, CONTROL, body=b" " * size)[0] == 413, size
    finally:
        stop(srv)


# ---------------------------------------------------------------------------
# 5. health
# ---------------------------------------------------------------------------

def test_health_has_the_new_fields(fx):
    started = "2026-09-01T20:00:00+00:00"
    srv, port, _ = start(
        fx, reader_state=lambda: "ok",
        retention=RetentionPolicy(max_age_days=90, max_total_bytes=10 * 1024 ** 3,
                                  min_free_bytes=1),
        active_recording=lambda: {"id": "rec-1", "startedAtUtc": started,
                                  "sizeBytes": 123, "secret": "not exported"},
        target_info=lambda: {"gamePort": 7787, "queryPort": 27165,
                             "matchedPortKind": "Port", "containerId": "c" * 12,
                             "pid": 4242})
    try:
        h = health(port)
        # the original fields are all still there
        assert h["schema"] == SCHEMA_EXPORT and h["ok"] is True
        assert h["readerState"] == "ok" and h["lastFinalizedAt"] == 1788286190
        assert h["statsDbReadable"] is True and h["recordingsDirReadable"] is True
        assert h["recordingEnabled"] is True and h["recordingEnabledReason"] is None
        assert h["activeRecording"] == {"id": "rec-1", "startedAtUtc": started,
                                        "sizeBytes": 123}
        disk = h["disk"]
        assert set(disk) == {"recordingsBytes", "recordingsCount", "freeBytes",
                             "totalBytes", "minFreeBytes"}
        assert disk["recordingsCount"] == 3                  # A, C, D of zz-7787
        rec_dir = fx / SERVER_A_ID / "recordings"
        assert disk["recordingsBytes"] == sum(
            p.stat().st_size for p in rec_dir.iterdir()
            if p.name.endswith((".sqrx", ".meta.json")))
        assert disk["freeBytes"] > 0 and disk["totalBytes"] >= disk["freeBytes"]
        assert disk["minFreeBytes"] == 1
        assert h["retention"] == {"maxAgeDays": 90, "maxTotalBytes": 10 * 1024 ** 3,
                                  "lastRunAt": None, "lastDeleted": None}
        assert h["degradedReason"] is None and h["degradedReasons"] == []
        assert h["target"] == {"gamePort": 7787, "queryPort": 27165,
                               "matchedPortKind": "Port", "containerId": "c" * 12}
        assert h["netPolicy"] == netpolicy.status()
        assert set(h["netPolicy"]) == {"policy", "allowHosts", "blockedAttempts",
                                       "blockedByPurpose"}
        assert h["pendingAck"] == 1
    finally:
        stop(srv)


def test_health_unknowns_are_null_not_invented(fx):
    srv, port, _ = start(fx)                       # no hooks, no limits configured
    try:
        h = health(port)
        assert h["activeRecording"] is None and h["target"] is None
        assert h["retention"]["maxAgeDays"] is None
        assert h["retention"]["maxTotalBytes"] is None
        assert h["disk"]["minFreeBytes"] is None
    finally:
        stop(srv)


def test_health_disk_is_null_when_unreadable(tmp_path, monkeypatch):
    api = ExportApi(token=TOKEN, server_id="s", stats_db=tmp_path / "missing.db",
                    recordings_dir=tmp_path / "nodir", reader_state=lambda: "ok")
    body = json.loads(api.handle("GET", "/export/v1/health", f"Bearer {TOKEN}").body)
    assert body["disk"] is None
    assert body["pendingAck"] is None
    assert "recordings_dir_unreadable" in body["degradedReasons"]
    assert "stats_db_unreadable" in body["degradedReasons"]
    assert body["degradedReason"] == body["degradedReasons"][0]

    # an existing directory whose filesystem cannot be queried
    rec = tmp_path / "rec"
    rec.mkdir()
    api = ExportApi(token=TOKEN, server_id="s", stats_db=tmp_path / "missing.db",
                    recordings_dir=rec)

    def boom(_p):
        raise OSError("no such device")

    monkeypatch.setattr(shutil, "disk_usage", boom)
    body = json.loads(api.handle("GET", "/export/v1/health", f"Bearer {TOKEN}").body)
    assert body["disk"] is None


def test_health_degraded_reasons_and_recording_reason(fx, monkeypatch):
    # a stale reader + a hook that reports a scanner suspicion
    srv, port, _ = start(fx, reader_state=lambda: "stale",
                         degraded_reasons=lambda: ["scanner_suspect"])
    try:
        h = health(port)
        assert h["ok"] is False
        assert h["degradedReasons"][:2] == ["reader_stale", "scanner_suspect"]
        assert h["degradedReason"] == "reader_stale"
    finally:
        stop(srv)
    # a broken hook must not break the endpoint
    srv, port, _ = start(fx, degraded_reasons=lambda: 1 / 0,
                         active_recording=lambda: 1 / 0, target_info=lambda: 1 / 0)
    try:
        h = health(port)
        assert h["degradedReason"] is None and h["activeRecording"] is None
    finally:
        stop(srv)
    # disk below the floor: the recorder would refuse, health says why
    huge = 10 ** 18
    srv, port, _ = start(fx, retention=RetentionPolicy(min_free_bytes=huge))
    try:
        h = health(port)
        assert h["recordingEnabled"] is False
        assert h["recordingEnabledReason"] == "disk_low"
        assert h["degradedReason"] == "disk_low"
        assert h["disk"]["minFreeBytes"] == huge
        # the operator switch outranks it in the reported reason
        post_json(port, CONTROL, {"enabled": False, "actor": "zz"})
        assert health(port)["recordingEnabledReason"] == "disabled_by_admin"
    finally:
        stop(srv)


def test_health_needs_the_token_and_does_not_leak_it(fx):
    srv, port, _ = start(fx)
    try:
        assert http(port, "/export/v1/health", method="GET", token=None)[0] == 401
        status, _h, body = http(port, "/export/v1/health", method="GET")
        assert status == 200 and TOKEN.encode() not in body
    finally:
        stop(srv)
