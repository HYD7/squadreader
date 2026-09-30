"""Live stream hub: fan-out of the reader's ready-made tick lines to a FEW
token-protected Export API subscribers (``GET /export/v1/live/stream``).

Fork feature, OFF by default (config ``export_live_enabled``). There is still no
public ``/stream`` or ``/latest``: this hub is reachable only through the
Bearer-protected Export API (export_api.py), and only when it was built.

Cost model -- the tick thread must never pay for a subscriber
--------------------------------------------------------------
``publish()`` runs on the tick thread and is O(1): it stamps a sequence number,
appends the frame to a bounded ring (time AND byte capped), appends a reference
to each subscriber's bounded queue and notifies. No encoding, no socket write,
no serialization (the JSON line the reader already built is passed through
verbatim). With no subscriber it stops after the ring append. Encoding and
socket writes happen on the subscriber's own thread (the HTTP handler thread,
see httpsrv.py), which iterates ``Subscriber.events()``.

A slow subscriber never blocks ``publish()``: when its queue overflows the
queue is dropped, the subscriber is marked "resync" and, once it catches up, it
receives one ``gap`` event followed by the last complete frame (plus what came
after it, plus any ``match`` event it missed).

Wire format (SSE)::

    id: <seq>            (full / pos / match only)
    event: hello | full | pos | match | hb | gap
    data: <one line of JSON>

* ``hello``  first event: {schema, serverId, bootId, hz, twoTier, seq, resume, match}
* ``full``   the reader's own full-frame line, byte for byte
* ``pos``    the reader's own position-frame line (two-tier mode), byte for byte
* ``match``  {matchId, state: "started"|"ended"}; 3-tick debounced
* ``hb``     {seq, ts}; sent after ``hb_interval`` (5 s) without another event
* ``gap``    {fromSeq, toSeq, dropped}; events were lost, the next ``full`` is a
             fresh baseline

``seq`` restarts at 1 with every reader process; ``bootId`` (random per process)
tells a consumer that this happened.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections import deque
from typing import Any, Callable, Iterator, Optional

from .recording_lifecycle import (
    INACTIVE_MATCH_STATES,
    MATCH_TRANSITION_CONFIRM_TICKS,
)

SCHEMA_LIVE = "sqr-live-1"

#: Idle time after which a ``hb`` event is written.
HB_INTERVAL_SEC = 5.0
#: Frames one subscriber may have queued before it is declared slow (~16 s at
#: 4 frames/s). Also bounded in bytes by the ring's byte cap.
SUB_QUEUE_MAX = 64

_SPLIT_LINES = re.compile(rb"\r\n|\r|\n")
_SEQ_RE = re.compile(r"[0-9]{1,15}")


class _Frame:
    """One published event. Immutable except for the lazily cached wire bytes
    (shared by all subscribers; a benign race can only compute it twice)."""

    __slots__ = ("seq", "kind", "data", "t", "size", "_wire")

    def __init__(self, seq: int, kind: str, data: str, t: float) -> None:
        self.seq = seq
        self.kind = kind
        self.data = data
        self.t = t
        # x2: the encoded copy the first subscriber caches on the frame.
        self.size = 2 * len(data) + 128
        self._wire: Optional[bytes] = None

    def payload(self) -> bytes:
        data = self.data
        if data.endswith("\r\n"):
            data = data[:-2]
        elif data.endswith("\n"):
            data = data[:-1]
        return data.encode("utf-8")

    def wire(self) -> bytes:
        w = self._wire
        if w is None:
            body = self.payload()
            if b"\n" in body or b"\r" in body:      # never true for json.dumps
                body = b"\ndata: ".join(_SPLIT_LINES.split(body))
            w = (b"id: %d\nevent: %s\ndata: " % (self.seq, self.kind.encode("ascii"))
                 + body + b"\n\n")
            self._wire = w
        return w


def _event(name: str, obj: Any) -> bytes:
    """An id-less event (hello / hb / gap): must not move Last-Event-ID."""
    return (b"event: " + name.encode("ascii") + b"\ndata: "
            + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n\n")


class Subscriber:
    """One attached client. Not thread-safe by itself: ``events()`` is consumed
    by exactly one thread; the hub touches the queue under its lock."""

    def __init__(self, hub: "LiveHub", boot_frames: list[_Frame], hello: dict,
                 gap: Optional[dict]) -> None:
        self._hub = hub
        self._cond = threading.Condition(hub._lock)
        self._queue: deque[_Frame] = deque()
        self._qbytes = 0
        self._boot = boot_frames
        self._hello = hello
        self._boot_gap = gap
        self._resync = False
        self._gap_from = 0
        self._dropped = 0
        self._closed = False
        self.bytes_sent = 0
        self.gaps = 0

    # -- called by the hub, under its lock -----------------------------------

    def _offer(self, f: _Frame) -> None:
        if self._resync:
            self._dropped += 1
            return
        q = self._queue
        if len(q) >= SUB_QUEUE_MAX or self._qbytes + f.size > self._hub._byte_cap:
            self._gap_from = q[0].seq if q else f.seq
            self._dropped = len(q) + 1
            q.clear()
            self._qbytes = 0
            self._resync = True
            self.gaps += 1
            self._hub._gaps += 1
            return
        q.append(f)
        self._qbytes += f.size

    # -- called by the subscriber's own thread --------------------------------

    def sent(self, n: int) -> None:
        """Account bytes that were actually written to the socket."""
        self.bytes_sent += n

    def close(self) -> None:
        self._hub._detach(self)

    def __enter__(self) -> "Subscriber":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def events(self) -> Iterator[bytes]:
        """Encoded SSE events, blocking; ends when the hub or this subscriber is
        closed. The consumer writes each yielded chunk and calls ``sent()``."""
        yield _event("hello", self._hello)
        if self._boot_gap is not None:
            yield _event("gap", self._boot_gap)
        boot, self._boot = self._boot, []
        for f in boot:
            yield f.wire()
        hub = self._hub
        last_write = time.monotonic()
        while True:
            out: list[bytes] = []
            hb: Optional[dict] = None
            with hub._lock:
                while True:
                    if self._closed or hub._closed:
                        return
                    if self._resync or self._queue:
                        break
                    remaining = hub._hb_interval - (time.monotonic() - last_write)
                    if remaining <= 0:
                        hb = {"seq": hub._seq, "ts": int(time.time() * 1000)}
                        break
                    self._cond.wait(remaining)
                if hb is None:
                    if self._resync:
                        gap_from, dropped = self._gap_from, self._dropped
                        frames = hub._resync_frames(gap_from)
                        to_seq = hub._seq
                        self._resync = False
                        self._dropped = 0
                        out.append(_event("gap", {"fromSeq": gap_from,
                                                  "toSeq": to_seq,
                                                  "dropped": dropped}))
                        self._queue.clear()
                        self._qbytes = 0
                    else:
                        frames = list(self._queue)
                        self._queue.clear()
                        self._qbytes = 0
                    batch = frames
                else:
                    batch = []
            if hb is not None:
                out.append(_event("hb", hb))
            else:
                # Encoded here, on the subscriber's thread, outside the lock.
                out.extend(f.wire() for f in batch)
            yield from out
            last_write = time.monotonic()


class LiveHub:
    """See the module docstring. One instance per reader process."""

    def __init__(self, *, server_id: str, hz: float, two_tier: bool = False,
                 max_clients: int = 2, ring_seconds: float = 30.0,
                 ring_max_mb: float = 32.0, hb_interval: float = HB_INTERVAL_SEC,
                 boot_id: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.server_id = str(server_id)
        self.hz = hz
        self.two_tier = bool(two_tier)
        self.max_clients = int(max_clients)
        self.boot_id = boot_id or secrets.token_hex(8)
        self._ring_seconds = float(ring_seconds)
        self._byte_cap = int(float(ring_max_mb) * 1024 * 1024)
        self._hb_interval = float(hb_interval)
        self._clock = clock
        self._lock = threading.Lock()
        self._ring: deque[_Frame] = deque()
        self._ring_bytes = 0
        self._subs: list[Subscriber] = []
        self._seq = 0
        self._last_full: Optional[_Frame] = None
        self._closed = False
        # match lifecycle (3-tick debounce, same constants as the recorder)
        self._m_id: Optional[str] = None
        self._m_state: Optional[str] = None       # "started" | "ended" | None
        self._cand: Optional[tuple[str, str]] = None
        self._cand_n = 0
        # statistics
        self._published = 0
        self._pub_ns_sum = 0
        self._pub_ns_max = 0
        self._gaps = 0
        self._rejected = 0
        self._closed_bytes = 0
        self._closed_subs = 0

    # -- producer side (tick thread) ------------------------------------------

    def set_two_tier(self, value: bool) -> None:
        self.two_tier = bool(value)

    def publish(self, line: str, kind: str, match_id: Any = None,
                match_state: Any = None) -> None:
        """Hand one ready-made JSON line to the hub. O(1); never blocks on a
        subscriber; never raises for bad input (unknown kind is ignored)."""
        if kind != "full" and kind != "pos":
            return
        t0 = time.perf_counter_ns()
        now = self._clock()
        with self._lock:
            if self._closed:
                return
            self._seq += 1
            f = _Frame(self._seq, kind, line, now)
            self._admit(f, now)
            if kind == "full":
                self._last_full = f
                for mid, state in self._match_step(match_id, match_state):
                    self._seq += 1
                    self._admit(_Frame(
                        self._seq, "match",
                        json.dumps({"matchId": mid, "state": state},
                                   ensure_ascii=False, separators=(",", ":")),
                        now), now)
            self._published += 1
        dt = time.perf_counter_ns() - t0
        self._pub_ns_sum += dt
        if dt > self._pub_ns_max:
            self._pub_ns_max = dt

    def _admit(self, f: _Frame, now: float) -> None:
        """Ring append + per-subscriber enqueue. Caller holds the lock."""
        ring = self._ring
        ring.append(f)
        self._ring_bytes += f.size
        horizon = now - self._ring_seconds
        while len(ring) > 1 and (ring[0].t < horizon
                                 or self._ring_bytes > self._byte_cap):
            self._ring_bytes -= ring.popleft().size
        for sub in self._subs:
            sub._offer(f)
            sub._cond.notify()

    def _match_step(self, match_id: Any, match_state: Any
                    ) -> list[tuple[str, str]]:
        """Debounced match lifecycle from one FULL frame. Same rules as the
        recorder: a transition needs MATCH_TRANSITION_CONFIRM_TICKS consecutive
        agreeing ticks; an ambiguous tick (unknown/missing state) resets the
        count (fail closed). Returns the confirmed transitions."""
        mid = match_id if isinstance(match_id, str) and match_id else None
        state = match_state if isinstance(match_state, str) else None
        cand: Optional[tuple[str, str]] = None
        if mid is not None and state == "InProgress":
            if self._m_state == "started" and self._m_id == mid:
                self._cand = None
                self._cand_n = 0
                return []
            cand = ("started", mid)
        elif state in INACTIVE_MATCH_STATES and self._m_state == "started" \
                and self._m_id is not None:
            cand = ("ended", self._m_id)
        if cand is None:
            self._cand = None
            self._cand_n = 0
            return []
        if cand == self._cand:
            self._cand_n += 1
        else:
            self._cand = cand
            self._cand_n = 1
        if self._cand_n < MATCH_TRANSITION_CONFIRM_TICKS:
            return []
        self._cand = None
        self._cand_n = 0
        out: list[tuple[str, str]] = []
        kind, cid = cand
        if kind == "started":
            if self._m_state == "started" and self._m_id is not None:
                out.append((self._m_id, "ended"))     # id changed with no end seen
            out.append((cid, "started"))
            self._m_id, self._m_state = cid, "started"
        else:
            out.append((cid, "ended"))
            self._m_state = "ended"
        return out

    # -- subscriber side ---------------------------------------------------------

    def subscribe(self, last_event_id: Optional[str] = None) -> Optional[Subscriber]:
        """Attach a subscriber, or None when the hub is full (`live_busy`) or
        closed. `last_event_id` (the SSE header) resumes from the ring when it
        is still there; otherwise the subscriber starts from the last full frame."""
        lei: Optional[int] = None
        if isinstance(last_event_id, str) and _SEQ_RE.fullmatch(last_event_id.strip()):
            lei = int(last_event_id.strip())
        with self._lock:
            if self._closed:
                return None
            if len(self._subs) >= self.max_clients:
                self._rejected += 1
                return None
            resume = "none"
            gap: Optional[dict] = None
            frames: list[_Frame]
            if lei is not None and lei <= self._seq and self._ring \
                    and self._ring[0].seq <= lei + 1:
                resume = "ring"
                frames = [f for f in self._ring if f.seq > lei]
            else:
                frames = self._fresh_frames()
                if lei is not None and lei < self._seq:
                    resume = "gap"
                    gap = {"fromSeq": lei + 1, "toSeq": self._seq,
                           "dropped": self._seq - lei}
                    self._gaps += 1
            hello = {
                "schema": SCHEMA_LIVE, "serverId": self.server_id,
                "bootId": self.boot_id, "hz": self.hz, "twoTier": self.two_tier,
                "seq": self._seq, "resume": resume,
                "match": ({"matchId": self._m_id, "state": self._m_state}
                          if self._m_id is not None else None),
            }
            sub = Subscriber(self, frames, hello, gap)
            self._subs.append(sub)
            return sub

    def _fresh_frames(self) -> list[_Frame]:
        lf = self._last_full
        if lf is None:
            return []
        return [lf] + [f for f in self._ring if f.seq > lf.seq]

    def _resync_frames(self, gap_from: int) -> list[_Frame]:
        """Last full frame + everything after it + any `match` event the slow
        subscriber missed (they may precede the last full frame)."""
        lf = self._last_full
        if lf is None:
            return [f for f in self._ring if f.kind == "match" and f.seq >= gap_from]
        sel = {f.seq: f for f in self._ring
               if f.seq > lf.seq or (f.kind == "match" and f.seq >= gap_from)}
        sel[lf.seq] = lf
        return [sel[k] for k in sorted(sel)]

    def _detach(self, sub: Subscriber) -> None:
        with self._lock:
            if sub._closed:
                return
            sub._closed = True
            try:
                self._subs.remove(sub)
            except ValueError:
                pass
            self._closed_bytes += sub.bytes_sent
            self._closed_subs += 1
            sub._cond.notify_all()

    def close(self) -> None:
        """Stop the hub: every subscriber's ``events()`` ends."""
        with self._lock:
            self._closed = True
            for sub in self._subs:
                sub._cond.notify_all()

    # -- read side ---------------------------------------------------------------

    def snapshot_bytes(self) -> Optional[bytes]:
        """JSON body of ``GET /export/v1/live/snapshot`` (the last full frame
        embedded verbatim), or None before the first full frame."""
        with self._lock:
            lf = self._last_full
            mid, mstate = self._m_id, self._m_state
            seq = self._seq
            now = self._clock()
        if lf is None:
            return None
        head = {"schema": SCHEMA_LIVE, "serverId": self.server_id,
                "bootId": self.boot_id, "seq": seq, "frameSeq": lf.seq,
                "ageMs": max(0, int((now - lf.t) * 1000)),
                "match": ({"matchId": mid, "state": mstate}
                          if mid is not None else None)}
        pre = json.dumps(head, ensure_ascii=False, separators=(",", ":")
                         ).encode("utf-8")[:-1]
        return pre + b',"frame":' + lf.payload() + b"}"

    def info(self) -> dict:
        """The ``live`` object of the Export API ``info`` response."""
        with self._lock:
            clients = len(self._subs)
        return {"enabled": True, "hz": self.hz, "twoTier": self.two_tier,
                "clients": clients, "maxClients": self.max_clients}

    def stats(self) -> dict:
        with self._lock:
            n = self._published
            bytes_sent = self._closed_bytes + sum(s.bytes_sent for s in self._subs)
            last_full_age = (round(self._clock() - self._last_full.t, 3)
                             if self._last_full is not None else None)
            return {
                "enabled": True, "bootId": self.boot_id, "seq": self._seq,
                "subscribers": len(self._subs), "maxClients": self.max_clients,
                "published": n,
                "publishAvgNs": (self._pub_ns_sum // n) if n else 0,
                "publishMaxNs": self._pub_ns_max,
                "gaps": self._gaps, "rejected": self._rejected,
                "bytesSent": bytes_sent,
                "ringFrames": len(self._ring), "ringBytes": self._ring_bytes,
                "lastFullAgeSec": last_full_age,
                "match": ({"matchId": self._m_id, "state": self._m_state}
                          if self._m_id is not None else None),
            }


__all__ = ["LiveHub", "Subscriber", "SCHEMA_LIVE", "HB_INTERVAL_SEC",
           "SUB_QUEUE_MAX"]
