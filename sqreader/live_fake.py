"""Fake live producer: plays a RECORDING into a real `LiveHub` and serves it with
the real `httpsrv` + Export API on 127.0.0.1. For tests of consumers of the live
stream (Zero Zone unit / Playwright tests); it never touches a game, discovery,
a PID file or process memory.

    python -m sqreader.live_fake --recording rec.ndjson.gz \\
        --export-token-file token.txt --port 0 --speed 10 --loop --server-id zz-7787

* ``--recording``  ``.ndjson.gz`` / ``.ndjson`` or ``.sqrx``; one JSON object per
  line. A line with ``"t": "pos"`` is a position frame, anything else a full frame.
* ``--speed N``    recording time runs N times faster than the wall clock (the
  ``timestamp`` deltas of consecutive frames are the schedule). Default 1.
* ``--loop``       start over after the end with a NEW matchId (uuid5 of the
  original id and the loop number; every occurrence in a full frame is replaced).
* ``--port 0``     pick a free port; the real one is printed to stdout as one JSON
  line ``{"port": N}`` once the server accepts connections.
* ``--wait-client``  start playing only when the first subscriber has attached.
* ``--end-linger S``  wall seconds to idle after the end before a loop restarts.

The lines are published byte for byte (only the matchId of a looped frame is
substituted). At the end of the recording three copies of the last full frame with
``matchState: "EndState"`` are published (unless it already ended), so the hub's
own 3-tick debounce emits ``match ended``. IPv4 loopback only; the token is read
from the file, never taken from the command line.
"""
from __future__ import annotations

import argparse
import gzip
import json
import signal
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .export_api import ExportApi, configure_export
from .httpsrv import _TickBeat, serve_in_background
from .livehub import LiveHub
from .recording_lifecycle import (
    INACTIVE_MATCH_STATES,
    MATCH_TRANSITION_CONFIRM_TICKS,
)

HOST = "127.0.0.1"


class _Item:
    __slots__ = ("t", "kind", "line", "match_id", "match_state")

    def __init__(self, t: float, kind: str, line: str,
                 match_id: Optional[str], match_state: Optional[str]) -> None:
        self.t = t
        self.kind = kind
        self.line = line
        self.match_id = match_id
        self.match_state = match_state


def _iter_lines(path: Path) -> Iterator[str]:
    name = path.name.lower()
    if name.endswith(".sqrx"):
        from .sqrx import SqrxReader
        with SqrxReader(path) as r:
            yield from r
    elif name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            yield from f
    else:
        with open(path, encoding="utf-8") as f:
            yield from f


def _parse_ts(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_recording(path: Path) -> list[_Item]:
    """Every line of a recording with its schedule time (seconds since the first
    frame). Lines that are not JSON objects are skipped."""
    items: list[_Item] = []
    t0: Optional[float] = None
    last = 0.0
    for raw in _iter_lines(path):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        ts = _parse_ts(d.get("timestamp"))
        if ts is not None:
            if t0 is None:
                t0 = ts
            last = max(last, ts - t0)
        raw_gs = d.get("gameState")
        gs: dict = raw_gs if isinstance(raw_gs, dict) else {}
        kind = "pos" if d.get("t") == "pos" else "full"
        items.append(_Item(last, kind, raw + "\n",
                           gs.get("matchId") if kind == "full" else None,
                           gs.get("matchState") if kind == "full" else None))
    if not any(i.kind == "full" for i in items):
        raise ValueError(f"no full frame in {path}")
    return items


class LiveFake:
    """The producer thread + the real HTTP server. ``start()`` returns the port."""

    def __init__(self, recording: Path, token_file: Path, *, server_id: str,
                 speed: float = 1.0, loop: bool = False, port: int = 0,
                 hz: float = 1.0, wait_client: bool = False,
                 end_linger: float = 1.0, max_clients: int = 2,
                 hb_interval: float = 5.0) -> None:
        if speed <= 0:
            raise ValueError("--speed must be > 0")
        self.items = load_recording(Path(recording))
        self.token_file = Path(token_file)
        self.server_id = server_id
        self.speed = float(speed)
        self.loop = loop
        self.wait_client = wait_client
        self.end_linger = max(0.0, float(end_linger))
        self.two_tier = any(i.kind == "pos" for i in self.items)
        self.hub = LiveHub(server_id=server_id, hz=hz, two_tier=self.two_tier,
                           max_clients=max_clients, hb_interval=hb_interval)
        self._port_req = port
        self._tmp = tempfile.TemporaryDirectory(prefix="sqr-live-fake-")
        self._stop = threading.Event()
        self._srv: Any = None
        self._thread: Optional[threading.Thread] = None
        self.port = 0
        self.loops_done = 0
        self.api: Optional[ExportApi] = None
        self._beat = _TickBeat()

    def start(self) -> int:
        tmp = Path(self._tmp.name)
        beat = self._beat
        self.api = configure_export(
            self.token_file, server_id=self.server_id,
            stats_db=tmp / "stats.db", recordings_dir=None,
            squad_port=None, reader_state=lambda: "ok",
            data_dir=tmp, live=self.hub)
        if self.api is None:
            raise ValueError(
                f"--export-token-file {self.token_file} is unusable "
                f"(missing, empty, < 32 characters or not printable ASCII)")
        self._srv = serve_in_background(
            HOST, self._port_req, beat, export_api=self.api,
            health_provider=lambda: {"fake": True, "live": self.hub.stats()},
            stale_after_sec=3600.0)
        self.port = int(self._srv.server_address[1])
        self._thread = threading.Thread(target=self._play, daemon=True,
                                        name="sqreader-live-fake")
        self._thread.start()
        return self.port

    # -- playback ----------------------------------------------------------

    def _sleep_until(self, due: float) -> bool:
        """Wait for monotonic time `due`; False when asked to stop."""
        while True:
            left = due - time.monotonic()
            if left <= 0:
                return not self._stop.is_set()
            if self._stop.wait(min(left, 0.25)):
                return False

    def _play(self) -> None:
        if self.wait_client:
            while not self._stop.is_set() and self.hub.stats()["subscribers"] == 0:
                self._stop.wait(0.02)
        orig_id = next((i.match_id for i in self.items
                        if i.kind == "full" and i.match_id), None)
        n = 0
        while not self._stop.is_set():
            new_id = orig_id
            if n > 0 and orig_id:
                new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{orig_id}:loop{n}"))
            start = time.monotonic()
            last_full: Optional[_Item] = None
            last_t = 0.0
            for it in self.items:
                if not self._sleep_until(start + it.t / self.speed):
                    return
                line, mid = it.line, it.match_id
                if n > 0 and orig_id and new_id and it.kind == "full":
                    line = line.replace(orig_id, new_id)
                    mid = new_id if mid == orig_id else mid
                self.hub.publish(line, it.kind, mid, it.match_state)
                self._beat.mark()
                if it.kind == "full":
                    last_full = _Item(it.t, it.kind, line, mid, it.match_state)
                last_t = it.t
            if last_full is not None and last_full.match_state not in \
                    INACTIVE_MATCH_STATES:
                self._publish_end(last_full, start, last_t)
            n += 1
            self.loops_done = n
            if not self.loop:
                return
            if self.end_linger and self._stop.wait(self.end_linger):
                return

    def _publish_end(self, last: _Item, start: float, last_t: float) -> None:
        """Three EndState copies of the last full frame -> the hub emits `match
        ended` through its normal 3-tick debounce."""
        try:
            d = json.loads(last.line)
            gs = d.setdefault("gameState", {})
            gs["matchState"] = "EndState"
        except (ValueError, AttributeError):
            return
        for k in range(1, MATCH_TRANSITION_CONFIRM_TICKS + 1):
            t = last_t + float(k)
            d["tick"] = int(d.get("tick") or 0) + k
            if not self._sleep_until(start + t / self.speed):
                return
            self.hub.publish(json.dumps(d, ensure_ascii=False) + "\n", "full",
                             last.match_id, "EndState")
            self._beat.mark()

    def close(self) -> None:
        self._stop.set()
        self.hub.close()
        if self._srv is not None:
            try:
                self._srv.shutdown()
                self._srv.server_close()
            except Exception:                                # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._tmp.cleanup()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m sqreader.live_fake",
        description="Play a recording as a fake live SquadReader Export API "
                    "(127.0.0.1 only). Test tool; touches no game.")
    ap.add_argument("--recording", type=Path, required=True,
                    help=".ndjson.gz / .ndjson / .sqrx recording to play")
    ap.add_argument("--export-token-file", type=Path, required=True,
                    help="file holding the Bearer token (>= 32 chars)")
    ap.add_argument("--port", type=int, default=0,
                    help="listen port (0 = free port, printed as {\"port\": N})")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="recording time / wall time (default 1)")
    ap.add_argument("--loop", action="store_true",
                    help="restart after the end with a new matchId")
    ap.add_argument("--server-id", default="zz-live")
    ap.add_argument("--hz", type=float, default=1.0,
                    help="value reported as `hz` in hello / info (default 1)")
    ap.add_argument("--wait-client", action="store_true",
                    help="start playing when the first subscriber attaches")
    ap.add_argument("--end-linger", type=float, default=1.0,
                    help="wall seconds to idle after the end before a --loop restart")
    ap.add_argument("--max-clients", type=int, default=2)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        fake = LiveFake(args.recording, args.export_token_file,
                        server_id=args.server_id, speed=args.speed,
                        loop=args.loop, port=args.port, hz=args.hz,
                        wait_client=args.wait_client,
                        end_linger=args.end_linger,
                        max_clients=args.max_clients)
        port = fake.start()
    except (OSError, ValueError) as e:
        print(f"live_fake: {e}", file=sys.stderr)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    print(json.dumps({"port": port}), flush=True)
    try:
        while not stop.wait(0.2):
            pass
    finally:
        fake.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
