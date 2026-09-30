"""Live stream (fork feature): `LiveHub` + `GET /export/v1/live/stream|snapshot`.

Covers the contract of the token-protected live surface: 401 without a token, 404
when the feature is off, 503 `live_busy` for the extra client, the event order
hello -> full/pos -> match -> hb, `Last-Event-ID` resume, the snapshot, a slow
client that gets a `gap` without ever blocking `publish()`, and the cost of
`publish()` itself (p99 <= 50 microseconds, with and without subscribers).
The public no-live contract is tests/test_public_no_live.py (unchanged).
"""
from __future__ import annotations

import gc
import http.client
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from sqreader.export_api import ExportApi
from sqreader.httpsrv import _LIVE_WRITE_TIMEOUT_SEC, _TickBeat, serve_in_background
from sqreader.livehub import LiveHub, SCHEMA_LIVE, SUB_QUEUE_MAX

TOKEN = "t" * 40
LIVE = "/export/v1/live/stream"
SNAP = "/export/v1/live/snapshot"


def full_line(tick: int, mid: str = "m-1", state: str = "InProgress",
              pad: int = 0) -> str:
    # deliberately NOT json.dumps-canonical (double spaces, non-ASCII): the hub
    # must pass the reader's line through byte for byte.
    padding = "x" * pad
    return (f'{{"tick": {tick},  "gameState": {{"matchId": "{mid}", '
            f'"matchState": "{state}"}}, "players": [{{"name": "Zoë"}}], '
            f'"pad": "{padding}"}}\n')


def pos_line(tick: int) -> str:
    return f'{{"t": "pos", "tick": {tick}, "players": []}}\n'


class Stack:
    def __init__(self, tmp_path: Path, live: bool = True, **hub_kw: Any) -> None:
        hub_kw.setdefault("hb_interval", 0.2)
        self.hub: Optional[LiveHub] = (
            LiveHub(server_id="srv-1", hz=1.0, **hub_kw) if live else None)
        self.api = ExportApi(token=TOKEN, server_id="srv-1",
                             stats_db=tmp_path / "stats.db", data_dir=tmp_path,
                             live=self.hub)
        self.srv = serve_in_background("127.0.0.1", 0, _TickBeat(),
                                       export_api=self.api)
        self.port = self.srv.server_address[1]

    def close(self) -> None:
        if self.hub is not None:
            self.hub.close()
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    yield s
    s.close()


def status_of(port: int, path: str, token: Optional[str] = TOKEN,
              method: str = "GET") -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        hdrs = {"Authorization": f"Bearer {token}"} if token is not None else {}
        conn.request(method, path, headers=hdrs)
        r = conn.getresponse()
        body = r.read()
        return r.status, dict(r.getheaders()), body
    finally:
        conn.close()


class Sse:
    """Minimal SSE client over http.client (decodes chunked framing)."""

    def __init__(self, port: int, headers: Optional[dict] = None,
                 token: Optional[str] = TOKEN, timeout: float = 5.0) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        self.conn.request("GET", LIVE, headers=h)
        self.resp = self.conn.getresponse()

    def event(self) -> dict:
        ev: dict = {}
        data: list[str] = []
        while True:
            raw = self.resp.readline()
            if not raw:
                raise EOFError("stream ended")
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "":
                if ev or data:
                    ev["data"] = "\n".join(data)
                    return ev
                continue
            if line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "data":
                data.append(value)
            else:
                ev[name] = value

    def until(self, event: str, limit: int = 400) -> list[dict]:
        seen = []
        for _ in range(limit):
            ev = self.event()
            seen.append(ev)
            if ev["event"] == event:
                return seen
        raise AssertionError(f"no {event!r} in {limit} events: {seen[-3:]}")

    def close(self) -> None:
        try:
            self.resp.close()
            self.conn.close()
        except OSError:
            pass


def wait_for(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# -- access control ------------------------------------------------------------

def test_stream_and_snapshot_need_the_token(stack):
    for path in (LIVE, SNAP):
        for tok in (None, "wrong" * 8):
            code, hdrs, body = status_of(stack.port, path, token=tok)
            assert code == 401, path
            assert json.loads(body)["error"] == "unauthorized"
            assert hdrs.get("WWW-Authenticate") == "Bearer"


def test_disabled_live_is_404_for_both_endpoints(tmp_path):
    s = Stack(tmp_path, live=False)
    try:
        for path in (LIVE, SNAP):
            code, _, body = status_of(s.port, path)
            assert code == 404
            assert json.loads(body)["error"] == "not_found"
            # identical to any unknown Export path
            assert status_of(s.port, "/export/v1/nope")[0] == 404
            # no token: the general Export behaviour, 401 (auth before routing)
            assert status_of(s.port, path, token=None)[0] == 401
    finally:
        s.close()


def test_info_carries_live_only_when_enabled(tmp_path):
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    on = Stack(tmp_path / "on", live=True, max_clients=2)
    off = Stack(tmp_path / "off", live=False)
    try:
        _, _, b_on = status_of(on.port, "/export/v1/info")
        _, _, b_off = status_of(off.port, "/export/v1/info")
        assert json.loads(b_on)["live"] == {
            "enabled": True, "hz": 1.0, "twoTier": False,
            "clients": 0, "maxClients": 2}
        assert "live" not in json.loads(b_off)
    finally:
        on.close()
        off.close()


def test_info_live_clients_counts_subscribers(stack):
    c = Sse(stack.port)
    try:
        c.event()  # hello
        _, _, b = status_of(stack.port, "/export/v1/info")
        assert json.loads(b)["live"]["clients"] == 1
    finally:
        c.close()


def test_wrong_method_and_query(stack):
    assert status_of(stack.port, LIVE, method="POST")[0] == 405
    assert status_of(stack.port, LIVE, method="HEAD")[0] == 405
    assert status_of(stack.port, LIVE + "?x=1")[0] == 400
    assert status_of(stack.port, SNAP + "?x=1")[0] == 400
    # HEAD on the snapshot is the ordinary Export HEAD behaviour
    stack.hub.publish(full_line(1), "full", "m-1", "InProgress")
    assert status_of(stack.port, SNAP, method="HEAD")[0] == 200


def test_stream_headers(stack):
    c = Sse(stack.port)
    try:
        r = c.resp
        assert r.status == 200
        assert r.getheader("Content-Type") == "text/event-stream"
        assert r.getheader("Cache-Control") == "no-store"
        assert r.getheader("X-Accel-Buffering") == "no"
        assert r.getheader("Transfer-Encoding") == "chunked"
        assert _LIVE_WRITE_TIMEOUT_SEC == 10.0
    finally:
        c.close()


# -- 503 live_busy --------------------------------------------------------------

def test_third_client_gets_503_live_busy(stack):
    a, b = Sse(stack.port), Sse(stack.port)
    try:
        a.event()
        b.event()
        code, hdrs, body = status_of(stack.port, LIVE)
        assert code == 503
        assert json.loads(body) == {"schema": "sqr-export-1", "error": "live_busy"}
        assert hdrs.get("Retry-After") == "5"
        assert stack.hub.stats()["rejected"] == 1
        # a freed slot is reusable
        a.close()
        assert wait_for(lambda: (stack.hub.publish(pos_line(0), "pos") or True)
                        and stack.hub.stats()["subscribers"] == 1)
        c = Sse(stack.port)
        try:
            assert c.event()["event"] == "hello"
        finally:
            c.close()
    finally:
        a.close()
        b.close()


# -- event order and content ------------------------------------------------------

def test_event_order_hello_frames_match_hb(stack):
    hub = stack.hub
    c = Sse(stack.port)
    try:
        hello = c.event()
        assert hello["event"] == "hello" and "id" not in hello
        hd = json.loads(hello["data"])
        assert hd["schema"] == SCHEMA_LIVE == "sqr-live-1"
        assert hd["serverId"] == "srv-1" and hd["hz"] == 1.0
        assert hd["twoTier"] is False and hd["bootId"] == hub.boot_id
        assert hd["seq"] == 0 and hd["resume"] == "none" and hd["match"] is None

        sent = []
        for i in range(1, 4):
            sent.append(full_line(i))
            hub.publish(sent[-1], "full", "m-1", "InProgress")
            sent.append(pos_line(i))
            hub.publish(sent[-1], "pos")
        for i in range(4, 7):
            sent.append(full_line(i, state="EndState"))
            hub.publish(sent[-1], "full", "m-1", "EndState")

        evs = c.until("hb")
        names = [e["event"] for e in evs]
        assert names[-1] == "hb"
        # frames first, then match(started) after the 3rd InProgress full,
        # match(ended) after the 3rd EndState full, then hb
        assert names.count("match") == 2
        assert names.index("match") > names.index("full")
        matches = [json.loads(e["data"]) for e in evs if e["event"] == "match"]
        assert matches == [{"matchId": "m-1", "state": "started"},
                           {"matchId": "m-1", "state": "ended"}]
        assert names.index("hb") > len(names) - 2
        # ids are the strictly increasing seq; hb has none
        ids = [int(e["id"]) for e in evs if "id" in e]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        assert "id" not in evs[-1]
        # data is the reader's line, untouched (no re-serialization)
        got = [e["data"] for e in evs if e["event"] in ("full", "pos")]
        assert got == [ln.rstrip("\n") for ln in sent]
        hb = json.loads(evs[-1]["data"])
        assert hb["seq"] == ids[-1] and isinstance(hb["ts"], int)
    finally:
        c.close()


def test_two_tier_flag_in_hello(tmp_path):
    s = Stack(tmp_path, two_tier=True)
    try:
        c = Sse(s.port)
        try:
            assert json.loads(c.event()["data"])["twoTier"] is True
        finally:
            c.close()
    finally:
        s.close()


def test_new_joiner_gets_last_full_first_and_match_in_hello(stack):
    hub = stack.hub
    for i in range(1, 5):
        hub.publish(full_line(i, "m-9"), "full", "m-9", "InProgress")
    hub.publish(pos_line(5), "pos")
    c = Sse(stack.port)
    try:
        hello = json.loads(c.event()["data"])
        assert hello["match"] == {"matchId": "m-9", "state": "started"}
        e1, e2 = c.event(), c.event()
        assert e1["event"] == "full" and e1["data"] == full_line(4, "m-9").rstrip()
        assert e2["event"] == "pos"
    finally:
        c.close()


# -- Last-Event-ID -----------------------------------------------------------------

def test_last_event_id_resumes_from_ring(stack):
    hub = stack.hub
    for i in range(1, 6):
        hub.publish(full_line(i, mid="m-2"), "full", "m-2", "InProgress")
    # seq 1..3 = full 1-3, 4 = match started, 5..6 = full 4-5
    c0 = Sse(stack.port, headers={"Last-Event-ID": "0"})
    try:
        assert json.loads(c0.event()["data"])["resume"] == "ring"
        evs = [c0.event() for _ in range(6)]
        assert [int(e["id"]) for e in evs] == [1, 2, 3, 4, 5, 6]
        assert [e["event"] for e in evs] == ["full"] * 3 + ["match", "full", "full"]
    finally:
        c0.close()
    c = Sse(stack.port, headers={"Last-Event-ID": "3"})
    try:
        hello = json.loads(c.event()["data"])
        assert hello["resume"] == "ring" and hello["seq"] == 6
        rest = [c.event() for _ in range(3)]
        assert [int(e["id"]) for e in rest] == [4, 5, 6]
        hub.publish(full_line(6, mid="m-2"), "full", "m-2", "InProgress")
        assert int(c.event()["id"]) == 7          # then straight on with live frames
    finally:
        c.close()


def test_last_event_id_stale_or_bogus_starts_fresh(tmp_path):
    now = [0.0]
    hub = LiveHub(server_id="s", hz=1.0, ring_seconds=5, clock=lambda: now[0])
    for i in range(1, 30):
        now[0] += 1.0
        hub.publish(full_line(i), "full", "m", "InProgress")
    # ring only holds the last ~5 s: seq 3 is gone
    with hub.subscribe("3") as sub:
        it = sub.events()
        hello = json.loads(next(it).split(b"data: ", 1)[1])
        assert hello["resume"] == "gap"
        gap = next(it)
        assert gap.startswith(b"event: gap\n")
        gd = json.loads(gap.split(b"data: ", 1)[1])
        assert gd["fromSeq"] == 4 and gd["toSeq"] == hub._seq
        first = next(it)
        assert first.startswith(b"id: ") and b"event: full" in first
    for bogus in ("abc", "-1", "", None, str(10 ** 6)):
        with hub.subscribe(bogus) as sub:
            hello = json.loads(next(sub.events()).split(b"data: ", 1)[1])
            assert hello["resume"] == "none", bogus
    hub.close()


def test_last_event_id_current_seq_gets_only_new_frames():
    hub = LiveHub(server_id="s", hz=1.0)
    hub.publish(full_line(1), "full", "m", "InProgress")
    hub.publish(full_line(2), "full", "m", "InProgress")
    with hub.subscribe(str(hub._seq)) as sub:
        it = sub.events()
        hello = json.loads(next(it).split(b"data: ", 1)[1])
        assert hello["resume"] == "ring"
        hub.publish(full_line(3), "full", "m", "InProgress")
        nxt = next(it)
        assert nxt.startswith(b"id: 3\n")
    hub.close()


# -- snapshot ----------------------------------------------------------------------

def test_snapshot(stack):
    code, hdrs, body = status_of(stack.port, SNAP)
    assert code == 503 and json.loads(body)["error"] == "live_no_frame"
    line = full_line(7, mid="m-7", pad=50)
    stack.hub.publish(pos_line(1), "pos")
    stack.hub.publish(line, "full", "m-7", "InProgress")
    stack.hub.publish(pos_line(2), "pos")
    code, hdrs, body = status_of(stack.port, SNAP)
    assert code == 200
    assert hdrs["Content-Type"] == "application/json"
    assert hdrs["Cache-Control"] == "no-store"
    assert line.rstrip("\n").encode("utf-8") in body     # embedded verbatim
    doc = json.loads(body)
    assert doc["schema"] == "sqr-live-1" and doc["serverId"] == "srv-1"
    assert doc["bootId"] == stack.hub.boot_id
    assert doc["seq"] == 3 and doc["frameSeq"] == 2 and doc["ageMs"] >= 0
    assert doc["frame"]["gameState"]["matchId"] == "m-7"
    assert doc["match"] is None                         # not yet debounced


# -- match debounce ----------------------------------------------------------------

def _matches(hub: LiveHub) -> list[dict]:
    return [json.loads(f.data) for f in hub._ring if f.kind == "match"]


def test_match_events_are_debounced_three_ticks():
    hub = LiveHub(server_id="s", hz=1.0)
    for i in range(2):
        hub.publish(full_line(i), "full", "a", "InProgress")
    assert _matches(hub) == []
    # an ambiguous tick resets the count
    hub.publish(full_line(3), "full", "a", "Weird")
    hub.publish(full_line(4), "full", "a", "InProgress")
    hub.publish(full_line(5), "full", "a", "InProgress")
    assert _matches(hub) == []
    hub.publish(full_line(6), "full", "a", "InProgress")
    assert _matches(hub) == [{"matchId": "a", "state": "started"}]
    # two inactive ticks are not an end, a running tick cancels them
    hub.publish(full_line(7), "full", "a", "EndState")
    hub.publish(full_line(8), "full", "a", "EndState")
    hub.publish(full_line(9), "full", "a", "InProgress")
    hub.publish(full_line(10), "full", "a", "EndState")
    hub.publish(full_line(11), "full", "a", "EndState")
    assert len(_matches(hub)) == 1
    hub.publish(full_line(12), "full", "a", "WaitingPostMatch")
    assert _matches(hub)[-1] == {"matchId": "a", "state": "ended"}
    # position frames never move the state machine
    for i in range(10):
        hub.publish(pos_line(i), "pos", "zzz", "EndState")
    assert len(_matches(hub)) == 2
    # a new id running for three ticks starts the next match
    for i in range(3):
        hub.publish(full_line(20 + i, mid="b"), "full", "b", "InProgress")
    assert _matches(hub)[-1] == {"matchId": "b", "state": "started"}
    # id change without an observed end: ended(old) then started(new)
    for i in range(3):
        hub.publish(full_line(30 + i, mid="c"), "full", "c", "InProgress")
    assert _matches(hub)[-2:] == [{"matchId": "b", "state": "ended"},
                                  {"matchId": "c", "state": "started"}]
    hub.close()


def test_bad_input_never_raises():
    hub = LiveHub(server_id="s", hz=1.0)
    hub.publish("x", "weird")          # ignored
    hub.publish(full_line(1), "full", None, None)
    hub.publish(full_line(1), "full", 12, ["x"])
    assert hub.stats()["published"] == 2
    hub.close()
    hub.publish(full_line(2), "full", "a", "InProgress")   # closed: no-op


def test_boot_id_is_per_process_random_and_stats_shape():
    a, b = LiveHub(server_id="s", hz=1.0), LiveHub(server_id="s", hz=1.0)
    assert a.boot_id != b.boot_id and len(a.boot_id) >= 16
    a.publish(full_line(1), "full", "m", "InProgress")
    st = a.stats()
    for k in ("bootId", "seq", "subscribers", "maxClients", "published",
              "publishAvgNs", "publishMaxNs", "gaps", "rejected", "bytesSent",
              "ringFrames", "ringBytes", "lastFullAgeSec", "match"):
        assert k in st, k
    assert st["published"] == 1 and st["publishMaxNs"] >= st["publishAvgNs"] > 0


def test_ring_is_bounded_by_time_and_bytes():
    now = [0.0]
    hub = LiveHub(server_id="s", hz=1.0, ring_seconds=10, ring_max_mb=1,
                  clock=lambda: now[0])
    for i in range(100):
        now[0] += 1.0
        hub.publish(full_line(i), "full", "m", "InProgress")
    assert len(hub._ring) <= 12          # 10 s of 1 Hz frames (+ match event)
    big = full_line(0, pad=300_000)
    for _ in range(50):
        hub.publish(big, "full", "m", "InProgress")
    assert hub.stats()["ringBytes"] <= 1024 * 1024 + 2 * len(big) + 200
    assert hub._last_full is not None      # the newest full frame always survives
    hub.close()


# -- slow client -------------------------------------------------------------------

def test_slow_subscriber_gets_gap_and_last_full_and_publish_never_blocks():
    hub = LiveHub(server_id="s", hz=1.0)
    sub = hub.subscribe()
    it = sub.events()
    next(it)                                  # hello; then nobody reads
    worst = 0
    n = SUB_QUEUE_MAX * 3
    for i in range(1, n + 1):
        t0 = time.perf_counter_ns()
        hub.publish(full_line(i), "full", "m", "InProgress")
        worst = max(worst, time.perf_counter_ns() - t0)
    assert worst < 20_000_000                 # never anywhere near a stall
    assert hub.stats()["gaps"] >= 1
    # now the slow client wakes up: gap first, then the newest full frame
    ev = next(it)
    assert ev.startswith(b"event: gap\n")
    gd = json.loads(ev.split(b"data: ", 1)[1])
    assert gd["dropped"] > 0 and gd["toSeq"] == hub._seq and gd["fromSeq"] >= 1
    nxt = next(it)
    while b"event: match" in nxt:       # a match event of the lost range comes first
        nxt = next(it)
    assert b"event: full" in nxt and (b'"tick": %d,' % n) in nxt
    # and it keeps receiving live frames after the resync
    hub.publish(full_line(n + 1), "full", "m", "InProgress")
    last = next(it)
    assert (b'"tick": %d,' % (n + 1)) in last
    sub.close()
    hub.close()


def test_slow_subscriber_does_not_lose_a_match_event():
    hub = LiveHub(server_id="s", hz=1.0)
    sub = hub.subscribe()
    it = sub.events()
    next(it)
    for i in range(3):
        hub.publish(full_line(i, mid="m"), "full", "m", "InProgress")   # started
    for i in range(3):
        hub.publish(full_line(10 + i, mid="m", state="EndState"), "full", "m",
                    "EndState")                                          # ended
    for i in range(SUB_QUEUE_MAX * 2):
        hub.publish(pos_line(i), "pos")       # overflow, the last full is old
    out = []
    hub.publish(pos_line(999), "pos")
    for _ in range(12):
        out.append(next(it))
        if b'"tick": 999' in out[-1]:
            break
    blob = b"".join(out)
    assert b'"state":"started"' in blob and b'"state":"ended"' in blob
    assert blob.index(b"event: gap") < blob.index(b'"state":"ended"')
    sub.close()
    hub.close()


def test_slow_http_client_gets_gap_over_the_wire(tmp_path):
    s = Stack(tmp_path, ring_max_mb=64)
    try:
        sock = socket.create_connection(("127.0.0.1", s.port), timeout=10)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.sendall(f"GET {LIVE} HTTP/1.1\r\nHost: x\r\n"
                     f"Authorization: Bearer {TOKEN}\r\n\r\n".encode())
        assert wait_for(lambda: s.hub.stats()["subscribers"] == 1)
        big = full_line(0, pad=200_000)
        worst = 0
        t_all = time.perf_counter()
        for _ in range(200):                  # ~40 MB into a non-reading peer
            t0 = time.perf_counter_ns()
            s.hub.publish(big, "full", "m", "InProgress")
            worst = max(worst, time.perf_counter_ns() - t0)
        total = time.perf_counter() - t_all
        assert worst < 50_000_000 and total < 3.0
        assert s.hub.stats()["gaps"] >= 1
        # drain: the stream must contain a gap event (the writer thread is
        # unblocked as we read)
        sock.settimeout(10)
        seen = b""
        end = time.monotonic() + 8
        while b"event: gap" not in seen and time.monotonic() < end:
            chunk = sock.recv(1 << 20)
            if not chunk:
                break
            seen += chunk
        assert b"event: gap" in seen
        sock.close()
    finally:
        s.close()


def test_dropped_client_frees_the_slot(stack):
    c = Sse(stack.port)
    c.event()
    assert stack.hub.stats()["subscribers"] == 1
    c.close()

    def gone() -> bool:
        stack.hub.publish(pos_line(1), "pos")
        return stack.hub.stats()["subscribers"] == 0
    assert wait_for(gone, timeout=6.0)


def test_hub_close_ends_the_stream(stack):
    c = Sse(stack.port)
    c.event()
    stack.hub.close()
    with pytest.raises((EOFError, http.client.HTTPException, OSError)):
        for _ in range(50):
            c.event()
    c.close()


def test_bytes_sent_is_accounted(stack):
    c = Sse(stack.port)
    try:
        c.event()
        stack.hub.publish(full_line(1, pad=1000), "full", "m", "InProgress")
        c.event()
        assert wait_for(lambda: stack.hub.stats()["bytesSent"] > 1000)
    finally:
        c.close()


# -- cost of publish() -----------------------------------------------------------

def _p99_us(samples: list[int]) -> float:
    s = sorted(samples)
    return s[int(len(s) * 0.99)] / 1000.0


def _measure(hub: LiveHub, n: int = 10_000) -> list[int]:
    full = full_line(1, pad=13_000)
    pos = pos_line(1)
    out = []
    gc.collect()
    gc.disable()
    try:
        for i in range(n):
            line, kind = (full, "full") if i % 4 == 0 else (pos, "pos")
            t0 = time.perf_counter_ns()
            hub.publish(line, kind, "m", "InProgress")
            out.append(time.perf_counter_ns() - t0)
    finally:
        gc.enable()
    return out


def test_publish_p99_without_subscribers():
    hub = LiveHub(server_id="s", hz=1.0)
    p99 = _p99_us(_measure(hub))
    print(f"publish p99 (no subscriber): {p99:.1f} us")
    assert p99 <= 50.0
    hub.close()


def test_publish_p99_with_subscribers():
    hub = LiveHub(server_id="s", hz=1.0, max_clients=2)
    stop = threading.Event()
    counts = [0, 0]

    def consume(idx: int) -> None:
        with hub.subscribe() as sub:
            for chunk in sub.events():
                counts[idx] += len(chunk)
                if stop.is_set():
                    return

    ts = [threading.Thread(target=consume, args=(i,), daemon=True) for i in range(2)]
    for t in ts:
        t.start()
    assert wait_for(lambda: hub.stats()["subscribers"] == 2)
    p99 = _p99_us(_measure(hub))
    print(f"publish p99 (2 subscribers): {p99:.1f} us")
    stop.set()
    hub.close()
    for t in ts:
        t.join(timeout=3)
    assert p99 <= 50.0
    assert min(counts) > 0            # they really were consuming
