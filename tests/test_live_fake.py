"""`python -m sqreader.live_fake`: a recording played as a fake live Export API.

ZZ unit tests and Playwright use it, so its contract is pinned here: the printed
port line, real httpsrv + Export API + token on 127.0.0.1, the events of a whole
recording (hello -> full/pos -> match started -> match ended), `--loop` with a new
matchId, and the `.ndjson.gz` / `.sqrx` inputs.
"""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sqreader import live_fake
from sqreader.sqrx import SqrxWriter
from test_live_stream import TOKEN, Sse, status_of, wait_for

REPO = Path(__file__).resolve().parent.parent
GOLDEN = sorted((REPO / "tests" / "fixtures" / "zz_golden" / "zz-7787")
                .glob("recording-*.ndjson.gz"))


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / "token"
    p.write_text(TOKEN + "\n", encoding="utf-8")
    return p


def _tiny_lines(mid: str = "tiny-match", n_full: int = 4) -> list[str]:
    t0 = datetime(2026, 9, 1, 18, 0, 0, tzinfo=timezone.utc)
    out = []
    for i in range(n_full):
        ts = (t0 + timedelta(seconds=i)).isoformat()
        out.append(json.dumps({
            "timestamp": ts, "tick": i + 1,
            "gameState": {"matchId": mid, "matchState": "InProgress"},
            "players": []}))
        out.append(json.dumps({"t": "pos", "tick": 100 + i, "timestamp":
                               (t0 + timedelta(seconds=i + 0.5)).isoformat(),
                               "players": []}))
    return out


def _write_gz(path: Path, lines: list[str]) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _collect(port: int, stop_after: str, count: int = 1, *, limit: int = 2000,
             headers=None) -> list[dict]:
    c = Sse(port, headers=headers, timeout=15)
    seen: list[dict] = []
    try:
        n = 0
        for _ in range(limit):
            ev = c.event()
            seen.append(ev)
            if ev["event"] == "match" and json.loads(ev["data"])["state"] == stop_after:
                n += 1
                if n >= count:
                    return seen
        raise AssertionError(f"no {count}x match {stop_after}: {seen[-4:]}")
    finally:
        c.close()


@pytest.mark.skipif(not GOLDEN, reason="golden recording fixture missing")
def test_cli_plays_the_golden_recording_within_10_seconds(token_file):
    # NOTE: the golden recording spans 590 s of recorded time; --speed 100 makes
    # that ~6 s of wall clock (--speed 10 would need ~59 s).
    proc = subprocess.Popen(
        [sys.executable, "-m", "sqreader.live_fake", "--recording", str(GOLDEN[0]),
         "--export-token-file", str(token_file), "--port", "0", "--speed", "100",
         "--server-id", "zz-7787", "--wait-client"],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        port = json.loads(line)["port"]
        assert isinstance(port, int) and port > 0 and list(json.loads(line)) == ["port"]
        code, _, _ = status_of(port, "/export/v1/info", token=None)
        assert code == 401
        code, _, body = status_of(port, "/export/v1/info")
        assert code == 200 and json.loads(body)["serverId"] == "zz-7787"
        t0 = time.monotonic()
        evs = _collect(port, "ended")
        elapsed = time.monotonic() - t0
        assert elapsed < 10.0, elapsed
        names = [e["event"] for e in evs]
        assert names[0] == "hello"
        hello = json.loads(evs[0]["data"])
        assert hello["serverId"] == "zz-7787" and hello["twoTier"] is True
        assert "full" in names and "pos" in names
        matches = [json.loads(e["data"]) for e in evs if e["event"] == "match"]
        assert [m["state"] for m in matches] == ["started", "ended"]
        assert matches[0]["matchId"] == matches[1]["matchId"] == \
            "5a7e0a00-7787-4a00-8000-00000000a001"
        assert names.index("match") > names.index("full")
        # frames are the recording's own lines
        recorded = gzip.open(GOLDEN[0], "rt", encoding="utf-8").read().splitlines()
        first_full = next(e for e in evs if e["event"] == "full")
        assert first_full["data"] == recorded[0]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc.stdout.close()
        proc.stderr.close()


def test_bad_token_file_exits_2(tmp_path):
    rec = _write_gz(tmp_path / "r.ndjson.gz", _tiny_lines())
    bad = tmp_path / "short"
    bad.write_text("short", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-m", "sqreader.live_fake", "--recording", str(rec),
         "--export-token-file", str(bad)],
        cwd=str(REPO), capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "unusable" in r.stderr


def test_binds_loopback_only_and_serves_token_protected_export(tmp_path, token_file):
    rec = _write_gz(tmp_path / "r.ndjson.gz", _tiny_lines())
    fake = live_fake.LiveFake(rec, token_file, server_id="fk-1", speed=50)
    port = fake.start()
    try:
        assert fake._srv.server_address[0] == "127.0.0.1"
        assert status_of(port, "/export/v1/live/snapshot", token=None)[0] == 401
        assert wait_for(lambda: status_of(port, "/export/v1/live/snapshot")[0] == 200)
        # no public live surface even here
        for path in ("/stream", "/latest"):
            assert status_of(port, path, token=None)[0] == 404
        info = json.loads(status_of(port, "/export/v1/info")[2])
        assert info["live"]["enabled"] is True and info["live"]["twoTier"] is True
    finally:
        fake.close()


def test_match_started_then_ended_at_the_end_of_the_recording(tmp_path, token_file):
    rec = _write_gz(tmp_path / "r.ndjson.gz", _tiny_lines("only-once", 5))
    fake = live_fake.LiveFake(rec, token_file, server_id="fk-1", speed=50,
                              wait_client=True, hb_interval=0.3)
    port = fake.start()
    try:
        evs = _collect(port, "ended")
        m = [json.loads(e["data"]) for e in evs if e["event"] == "match"]
        assert m == [{"matchId": "only-once", "state": "started"},
                     {"matchId": "only-once", "state": "ended"}]
        # afterwards it idles and heartbeats (no loop)
        c = Sse(port, timeout=15)
        try:
            c.event()
            assert c.until("hb")[-1]["event"] == "hb"
        finally:
            c.close()
    finally:
        fake.close()


def test_loop_restarts_with_a_new_match_id(tmp_path, token_file):
    rec = _write_gz(tmp_path / "r.ndjson.gz", _tiny_lines("loopy", 4))
    fake = live_fake.LiveFake(rec, token_file, server_id="fk-1", speed=200,
                              loop=True, wait_client=True, end_linger=0.05)
    port = fake.start()
    try:
        c = Sse(port, timeout=15)
        try:
            states = []
            for _ in range(600):
                ev = c.event()
                if ev["event"] == "match":
                    states.append(json.loads(ev["data"]))
                    if len(states) == 4:
                        break
        finally:
            c.close()
        assert [s["state"] for s in states] == ["started", "ended", "started", "ended"]
        assert states[0]["matchId"] == states[1]["matchId"] == "loopy"
        assert states[2]["matchId"] == states[3]["matchId"] != "loopy"
    finally:
        fake.close()


def test_sqrx_recordings_are_played_too(tmp_path, token_file):
    p = tmp_path / "r.sqrx"
    with SqrxWriter(p, "srv-x") as w:
        for line in _tiny_lines("from-sqrx", 4):
            w.write_line(line)
    fake = live_fake.LiveFake(p, token_file, server_id="fk-1", speed=200,
                              wait_client=True)
    port = fake.start()
    try:
        evs = _collect(port, "ended")
        assert any(json.loads(e["data"]).get("matchId") == "from-sqrx"
                   for e in evs if e["event"] == "match")
    finally:
        fake.close()


def test_recording_without_a_full_frame_is_refused(tmp_path, token_file):
    rec = _write_gz(tmp_path / "r.ndjson.gz",
                    [json.dumps({"t": "pos", "timestamp": "2026-09-01T18:00:00+00:00"})])
    with pytest.raises(ValueError):
        live_fake.LiveFake(rec, token_file, server_id="fk-1")
    with pytest.raises(ValueError):
        live_fake.LiveFake(rec, token_file, server_id="fk-1", speed=0)
