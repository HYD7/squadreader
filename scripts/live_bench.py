#!/usr/bin/env python3
"""Local benchmark of the live stream hub (fork feature, `sqreader/livehub.py`).

    python scripts/live_bench.py [--seconds 20] [--rate 5] [--subs 0,1,2]
                                 [--pad-kb 0] [--json] [--out FILE]

What it measures, for 0, 1 and 2 subscribers (one FRESH worker process per
subscriber count, so RSS and CPU do not carry over between scenarios):

* publish time of `LiveHub.publish()` in nanoseconds (p50 / p99 / max), measured
  around every call on the producer thread (the "tick thread" of the reader);
* CPU% of the reader-side process (producer + real HTTP server + the per-subscriber
  writer threads; 100 = one full core) and CPU microseconds per published line;
* RSS of that process (MB) after the run and its growth over the scenario;
* gaps a subscriber saw and bytes it received.

The subscribers are REAL SSE clients (`GET /export/v1/live/stream`, Bearer token)
running in separate processes, so their own CPU is not counted as reader cost.
The producer is the real `LiveHub` behind the real `httpsrv` + Export API, fed
with the lines of the synthetic golden recording
`tests/fixtures/zz_golden/zz-7787/recording-*.ndjson.gz`, cycled in file order
(full frames and `"t":"pos"` frames as recorded), at `--rate` lines per second.

Read the numbers for what they are:
* the golden frames are tiny (8 synthetic players); a real full frame is far
  bigger and its size was NOT measured. `--pad-kb` inflates the full frames to
  show how the cost scales, it is not a measurement of a real server;
* the process contains ONLY the hub + server + a paced producer, not the memory
  reader, so CPU% and RSS are the INCREMENTAL cost of the live feature and not
  those of a whole SquadReader;
* it is a local run (loopback, whatever OS runs it), not a game server.

Binds 127.0.0.1 only (there is no host option). The Bearer token is generated per
run into a temporary file and never printed or taken from the command line.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

_REPO = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO / "tests" / "fixtures" / "zz_golden" / "zz-7787"
HOST = "127.0.0.1"
SCHEMA = "sqr-live-bench-1"


# --------------------------------------------------------------------------
# process metrics (no third-party dependency)
# --------------------------------------------------------------------------

def rss_bytes() -> Optional[int]:
    """Resident set size of this process, or None when it cannot be read."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD),
                            ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
            c = _Counters()
            c.cb = ctypes.sizeof(c)
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                          ctypes.byref(c), c.cb):
                return int(c.WorkingSetSize)
        except (OSError, AttributeError, ValueError):
            return None
        return None
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak) * (1 if sys.platform == "darwin" else 1024)
    except (ImportError, OSError, ValueError):
        return None


def percentile(sorted_vals: list[int], p: float) -> int:
    """Nearest-rank percentile of an ascending list (0 for an empty list)."""
    if not sorted_vals:
        return 0
    k = max(1, -(-int(p * len(sorted_vals)) // 100)) if p < 100 else len(sorted_vals)
    k = min(k, len(sorted_vals))
    return sorted_vals[k - 1]


def _mb(n: Optional[int]) -> Optional[float]:
    return None if n is None else round(n / (1024 * 1024), 2)


# --------------------------------------------------------------------------
# SSE client (runs in its own process)
# --------------------------------------------------------------------------

def run_client(port: int, token_file: Path, max_seconds: float) -> int:
    """Read `/export/v1/live/stream` until the server closes it (or the safety
    limit). Prints one JSON line: {"ok", "bytes", "events", "gaps", "full"}."""
    token = token_file.read_text(encoding="utf-8").strip()
    out: dict[str, Any] = {"ok": False, "bytes": 0, "events": 0, "gaps": 0, "full": 0}
    conn = http.client.HTTPConnection(HOST, port, timeout=15)
    try:
        conn.request("GET", "/export/v1/live/stream",
                     headers={"Authorization": f"Bearer {token}",
                              "Accept": "text/event-stream"})
        resp = conn.getresponse()
        out["status"] = resp.status
        if resp.status != 200:
            print(json.dumps(out), flush=True)
            return 1
        deadline = time.monotonic() + max_seconds
        tail = b""
        while time.monotonic() < deadline:
            try:
                chunk = resp.read1(65536)
            except (OSError, http.client.HTTPException):
                break
            if not chunk:
                break
            out["bytes"] += len(chunk)
            parts = (tail + chunk).split(b"\n\n")
            tail = parts.pop()                   # the incomplete last event
            for ev in parts:
                out["events"] += 1
                if b"event: gap\n" in ev or ev.startswith(b"event: gap"):
                    out["gaps"] += 1
                elif b"event: full\n" in ev:
                    out["full"] += 1
        out["ok"] = True
    except (OSError, http.client.HTTPException) as e:
        out["error"] = type(e).__name__
    finally:
        conn.close()
    print(json.dumps(out), flush=True)
    return 0 if out["ok"] else 1


# --------------------------------------------------------------------------
# one scenario (runs in its own process)
# --------------------------------------------------------------------------

_Line = tuple[str, str, Optional[str], Optional[str]]


def _load_lines(recording: Path, pad_kb: int) -> list[_Line]:
    """[(line, kind, matchId, matchState)] of the recording; full frames padded
    to about `pad_kb` KB when asked."""
    sys.path.insert(0, str(_REPO))
    from sqreader.live_fake import load_recording

    pad = "x" * (pad_kb * 1024) if pad_kb > 0 else ""
    out = []
    for it in load_recording(recording):
        line = it.line
        if pad and it.kind == "full":
            d = json.loads(line)
            d["_benchPad"] = pad
            line = json.dumps(d, ensure_ascii=False) + "\n"
        out.append((line, it.kind, it.match_id, it.match_state))
    return out


def run_worker(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(_REPO))
    from sqreader.export_api import configure_export
    from sqreader.httpsrv import _TickBeat, serve_in_background
    from sqreader.livehub import LiveHub

    n_subs = int(args.subs_one)
    lines = _load_lines(Path(args.recording), args.pad_kb)
    full_bytes = [len(x[0]) for x in lines if x[1] == "full"]
    rss_before = rss_bytes()

    tmp = tempfile.TemporaryDirectory(prefix="sqr-live-bench-")
    tmp_path = Path(tmp.name)
    token_file = tmp_path / "token"
    token_file.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        pass

    hub = LiveHub(server_id="zz-bench", hz=1.0, two_tier=any(x[1] == "pos" for x in lines),
                  max_clients=max(2, n_subs), ring_seconds=30.0, ring_max_mb=32.0)
    api = configure_export(token_file, server_id="zz-bench", stats_db=tmp_path / "stats.db",
                           recordings_dir=None, squad_port=None,
                           reader_state=lambda: "ok", data_dir=tmp_path, live=hub)
    if api is None:
        print("live_bench: Export API could not be built", file=sys.stderr)
        return 3
    beat = _TickBeat()
    srv = serve_in_background(HOST, 0, beat, export_api=api, stale_after_sec=3600.0)
    port = int(srv.server_address[1])

    clients: list[subprocess.Popen] = []
    exit_code = 0
    result: dict[str, Any] = {}
    try:
        for _ in range(n_subs):
            clients.append(subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--client", str(port),
                 "--token-file", str(token_file), "--client-max-seconds",
                 str(args.seconds + 30)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))
        deadline = time.monotonic() + 20
        while hub.stats()["subscribers"] < n_subs:
            if time.monotonic() > deadline:
                print("live_bench: subscribers did not attach", file=sys.stderr)
                return 3
            time.sleep(0.02)

        # warm-up: a few lines so the first-call cost is not in the sample
        for line, kind, mid, mstate in lines[:8]:
            hub.publish(line, kind, mid, mstate)
            time.sleep(0.005)
        rss_start = rss_bytes()

        samples: list[int] = []
        rss_max = rss_start or 0
        stop_sampler = threading.Event()

        def _sampler() -> None:
            nonlocal rss_max
            while not stop_sampler.wait(0.25):
                r = rss_bytes()
                if r is not None and r > rss_max:
                    rss_max = r

        sampler = threading.Thread(target=_sampler, daemon=True)
        sampler.start()

        interval = 1.0 / args.rate
        perf = time.perf_counter_ns
        cpu0 = time.process_time()
        wall0 = time.monotonic()
        i = 0
        while True:
            due = wall0 + i * interval
            now = time.monotonic()
            if now - wall0 >= args.seconds:
                break
            if due > now:
                time.sleep(due - now)
            line, kind, mid, mstate = lines[i % len(lines)]
            t0 = perf()
            hub.publish(line, kind, mid, mstate)
            samples.append(perf() - t0)
            i += 1
        wall = time.monotonic() - wall0
        cpu = time.process_time() - cpu0
        stop_sampler.set()
        rss_end = rss_bytes()
        stats = hub.stats()
        samples.sort()
        n = len(samples)

        hub.close()
        client_results = []
        for p in clients:
            try:
                out, _ = p.communicate(timeout=20)
                line_out = out.strip().splitlines()[-1] if out and out.strip() else "{}"
                client_results.append(json.loads(line_out))
            except (subprocess.TimeoutExpired, ValueError, IndexError):
                p.kill()
                client_results.append({"ok": False})

        result = {
            "subscribers": n_subs,
            "publishes": n,
            "seconds": round(wall, 3),
            "rateLinesPerSec": round(n / wall, 2) if wall > 0 else None,
            "publishNs": {
                "p50": percentile(samples, 50),
                "p99": percentile(samples, 99),
                "max": samples[-1] if samples else 0,
                "mean": (sum(samples) // n) if n else 0,
            },
            "cpuPercent": round(100.0 * cpu / wall, 2) if wall > 0 else None,
            "cpuMicrosPerLine": round(1e6 * cpu / n, 1) if n else None,
            "rssMB": {
                "beforeSetup": _mb(rss_before),
                "start": _mb(rss_start),
                "end": _mb(rss_end),
                "peak": _mb(rss_max),
                "growth": (_mb(rss_end - rss_start)
                           if rss_end is not None and rss_start is not None else None),
            },
            "hub": {"gaps": stats["gaps"], "bytesSent": stats["bytesSent"],
                    "ringFrames": stats["ringFrames"], "ringBytes": stats["ringBytes"]},
            "clients": [{"ok": c.get("ok", False), "bytes": c.get("bytes", 0),
                         "gaps": c.get("gaps", 0), "full": c.get("full", 0)}
                        for c in client_results],
            "fullFrameBytes": {"min": min(full_bytes), "max": max(full_bytes)}
            if full_bytes else None,
        }
        if n_subs and not all(c["ok"] for c in result["clients"]):
            exit_code = 4
    finally:
        hub.close()
        for p in clients:
            if p.poll() is None:
                p.kill()
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:                                    # noqa: BLE001
            pass
        tmp.cleanup()
    print(json.dumps(result), flush=True)
    return exit_code


# --------------------------------------------------------------------------
# orchestrator
# --------------------------------------------------------------------------

def _find_recording(explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise SystemExit(f"live_bench: recording not found: {p}")
        return p
    found = sorted(_GOLDEN_DIR.glob("recording-*.ndjson.gz"))
    if not found:
        raise SystemExit(f"live_bench: no golden recording under {_GOLDEN_DIR}")
    return found[0]


def format_table(report: dict[str, Any]) -> str:
    hdr = ["subs", "publishes", "p50 ns", "p99 ns", "max ns", "CPU %", "CPU us/line",
           "RSS MB", "RSS +MB", "gaps", "sent MB"]
    rows = []
    for s in report["scenarios"]:
        p = s["publishNs"]
        r = s["rssMB"]
        rows.append([
            str(s["subscribers"]), str(s["publishes"]), str(p["p50"]), str(p["p99"]),
            str(p["max"]), f"{s['cpuPercent']}", f"{s['cpuMicrosPerLine']}",
            "-" if r["end"] is None else f"{r['end']}",
            "-" if r["growth"] is None else f"{r['growth']}",
            str(sum(c["gaps"] for c in s["clients"])),
            f"{s['hub']['bytesSent'] / (1024 * 1024):.2f}"])
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(hdr)]
    lines = ["  ".join(h.rjust(w) for h, w in zip(hdr, widths, strict=True))]
    lines += ["  ".join(c.rjust(w) for c, w in zip(row, widths, strict=True)) for row in rows]
    env = report["env"]
    head = (f"live_bench: {env['platform']} python {env['python']}, "
            f"rate {report['params']['rateLinesPerSec']} lines/s, "
            f"{report['params']['seconds']} s per scenario, pad {report['params']['padKb']} KB, "
            f"recording {report['params']['recording']}")
    note = ("Synthetic golden frames, loopback, hub + server only (no memory reader): "
            "incremental cost of the live feed. NOT measured on a game server.")
    return "\n".join([head, *lines, note])


def run_all(args: argparse.Namespace) -> int:
    recording = _find_recording(args.recording)
    try:
        subs_list = [int(x) for x in args.subs.split(",") if x.strip() != ""]
    except ValueError:
        print("live_bench: --subs must be a comma list such as 0,1,2", file=sys.stderr)
        return 2
    if not subs_list or any(n < 0 or n > 8 for n in subs_list):
        print("live_bench: --subs values must be 0..8", file=sys.stderr)
        return 2
    if args.rate <= 0 or args.seconds <= 0 or args.pad_kb < 0:
        print("live_bench: --rate and --seconds must be > 0, --pad-kb >= 0",
              file=sys.stderr)
        return 2

    scenarios = []
    for n in subs_list:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker",
               "--subs-one", str(n), "--seconds", str(args.seconds), "--rate",
               str(args.rate), "--pad-kb", str(args.pad_kb), "--recording",
               str(recording)]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=args.seconds + 120)
        if p.returncode != 0 or not p.stdout.strip():
            print(f"live_bench: scenario with {n} subscriber(s) failed "
                  f"(exit {p.returncode}): {p.stderr.strip()[-400:]}", file=sys.stderr)
            return 1
        scenarios.append(json.loads(p.stdout.strip().splitlines()[-1]))

    try:
        rec_label = str(recording.relative_to(_REPO)).replace("\\", "/")
    except ValueError:
        rec_label = recording.name
    report = {
        "schema": SCHEMA,
        "env": {"platform": sys.platform, "python": sys.version.split()[0],
                "cpuCount": os.cpu_count(),
                "cpuClockResolutionSec": time.get_clock_info("process_time").resolution},
        "params": {"seconds": args.seconds, "rateLinesPerSec": args.rate,
                   "padKb": args.pad_kb, "recording": rec_label},
        "scenarios": scenarios,
        "notes": ["synthetic golden recording, loopback, hub + server only",
                  "CPU% and RSS are the incremental cost of the live feed, not a whole reader",
                  "CPU% comes from the process CPU clock; its resolution is in env "
                  "(cpuClockResolutionSec), so short runs under-report",
                  "not measured on a game server"],
    }
    text = json.dumps(report, indent=2) if args.json else format_table(report)
    print(text)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Benchmark the SquadReader live hub with 0/1/2 subscribers "
                    "(127.0.0.1 only; synthetic golden recording).")
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="length of each scenario (default 20)")
    ap.add_argument("--rate", type=float, default=5.0,
                    help="published lines per second (default 5, about a two-tier "
                         "reader: 1 full + 4 pos)")
    ap.add_argument("--subs", default="0,1,2",
                    help="comma list of subscriber counts (default 0,1,2)")
    ap.add_argument("--pad-kb", type=int, default=0,
                    help="inflate every full frame by this many KB (illustration only)")
    ap.add_argument("--recording", default=None,
                    help="recording to cycle (default: the zz-7787 golden recording)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument("--out", default=None, help="also write the JSON report to this file")
    # internal modes
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--subs-one", default="0", help=argparse.SUPPRESS)
    ap.add_argument("--client", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--token-file", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--client-max-seconds", type=float, default=60.0, help=argparse.SUPPRESS)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.client is not None:
        if not args.token_file:
            return 2
        return run_client(args.client, Path(args.token_file), args.client_max_seconds)
    if args.worker:
        return run_worker(args)
    return run_all(args)


if __name__ == "__main__":
    raise SystemExit(main())
