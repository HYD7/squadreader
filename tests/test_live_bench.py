"""`scripts/live_bench.py`: a short run must exit 0 and report every scenario.

Numbers are not asserted (they depend on the machine); only the shape, that the
producer really published, and that real subscribers really received the stream.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "live_bench.py"


def _run(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--seconds", "1", "--rate", "50", *extra],
        capture_output=True, text=True, timeout=120, cwd=str(REPO))


def test_short_run_json_zero_and_one_subscriber(tmp_path):
    out_file = tmp_path / "report.json"
    p = _run("--subs", "0,1", "--json", "--out", str(out_file))
    assert p.returncode == 0, p.stderr
    report = json.loads(p.stdout)
    assert json.loads(out_file.read_text(encoding="utf-8")) == report
    assert report["schema"] == "sqr-live-bench-1"
    assert [s["subscribers"] for s in report["scenarios"]] == [0, 1]
    for s in report["scenarios"]:
        assert s["publishes"] > 10
        ns = s["publishNs"]
        assert 0 < ns["p50"] <= ns["p99"] <= ns["max"]
        assert s["cpuPercent"] is not None
    zero, one = report["scenarios"]
    assert zero["hub"]["bytesSent"] == 0 and zero["clients"] == []
    assert len(one["clients"]) == 1
    assert one["clients"][0]["ok"] is True
    assert one["clients"][0]["bytes"] > 0
    assert one["hub"]["bytesSent"] > 0


def test_short_run_table_output():
    p = _run("--subs", "0")
    assert p.returncode == 0, p.stderr
    assert "p50 ns" in p.stdout and "p99 ns" in p.stdout
    assert "NOT measured on a game server" in p.stdout


def test_bad_subs_argument_is_refused():
    p = subprocess.run([sys.executable, str(SCRIPT), "--subs", "x"],
                       capture_output=True, text=True, timeout=60, cwd=str(REPO))
    assert p.returncode == 2
