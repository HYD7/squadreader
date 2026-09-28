#!/usr/bin/env python3
"""Regenerate the Export API golden responses for the Zero Zone tests.

    python scripts/dump_zz_golden.py [--out tests/fixtures/zz_golden]

Builds the synthetic fixture (tests/zz_fixture.py), starts the Export API on a
real HTTP server (127.0.0.1, random port) for each of the two fixture servers,
and writes what it answers under the output directory::

    zz-7787/  info.json  matches.json  match-<matchId>.json
              recording-<recId>.ndjson.gz  recording-<recId>.headers.json
    zz-7788/  (same)

The output is deterministic: run it twice and the bytes are identical. JSON is
re-serialised with a fixed layout; the recording body is decompressed and
gzipped again with mtime 0 and a fixed level (the server's own gzip framing
carries a platform-dependent OS byte, so its raw bytes are not stored).
README.md in the output directory is hand-written and left untouched.
"""
from __future__ import annotations

import argparse
import gzip
import json
import secrets
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))                 # import sqreader
sys.path.insert(0, str(_REPO / "tests"))       # import zz_fixture

from zz_fixture import SERVER_A_ID, SERVER_B_ID, build_zz_fixture  # noqa: E402

from sqreader.export_api import ExportApi  # noqa: E402
from sqreader.httpsrv import _TickBeat, serve_in_background  # noqa: E402

DEFAULT_OUT = _REPO / "tests" / "fixtures" / "zz_golden"
GZIP_LEVEL = 9
# What the golden recording headers keep (the rest is transport noise: Date, ...).
_KEPT_HEADERS = ("Cache-Control", "Content-Type", "X-Sqr-Match-Id",
                 "X-Sqr-Total-Frames")
# Synthetic values, only so the golden `info.json` shows every field filled in.
_PORTS = {SERVER_A_ID: 7787, SERVER_B_ID: 7788}


def _dump_json(path: Path, obj: Any) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    path.write_bytes(text.encode("utf-8"))


def _get(port: int, token: str, path: str, *, encoding: str | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    if encoding:
        headers["Accept-Encoding"] = encoding
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, dict(resp.headers.items()), resp.read()


def _golden_for_server(fx_root: Path, server_id: str, out: Path) -> list[str]:
    token = secrets.token_hex(24)           # random per run; never written out
    api = ExportApi(
        token=token, server_id=server_id,
        stats_db=fx_root / server_id / "stats.db",
        recordings_dir=fx_root / server_id / "recordings",
        squad_port=_PORTS[server_id], container_id=f"zz-container-{_PORTS[server_id]}")
    srv = serve_in_background(
        "127.0.0.1", 0, _TickBeat(),
        recordings_dir=fx_root / server_id / "recordings",
        stats_db=fx_root / server_id / "stats.db", export_api=api)
    port = srv.server_address[1]
    written: list[str] = []
    try:
        out.mkdir(parents=True)

        def emit_json(name: str, path: str) -> Any:
            status, _hdrs, body = _get(port, token, path)
            assert status == 200, (path, status)
            obj = json.loads(body)
            _dump_json(out / name, obj)
            written.append(name)
            return obj

        emit_json("info.json", "/export/v1/info")
        listing = emit_json("matches.json", "/export/v1/matches?limit=100")
        assert listing["nextCursor"] is None, "fixture must fit one page"
        for item in listing["matches"]:
            emit_json(f"match-{item['matchId']}.json",
                      f"/export/v1/matches/{item['matchId']}")
            for rec in item["recordings"]:
                status, hdrs, body = _get(
                    port, token, f"/export/v1/recordings/{rec['id']}",
                    encoding="gzip")
                assert status == 200 and hdrs.get("Content-Encoding") == "gzip"
                ndjson = gzip.decompress(body)
                lines = ndjson.count(b"\n")
                assert str(lines) == hdrs["X-Sqr-Total-Frames"], (rec["id"], lines)
                gz_name = f"recording-{rec['id']}.ndjson.gz"
                (out / gz_name).write_bytes(
                    gzip.compress(ndjson, compresslevel=GZIP_LEVEL, mtime=0))
                kept = {k: hdrs[k] for k in _KEPT_HEADERS}
                kept["Content-Encoding"] = "gzip"     # of the stored .gz file
                _dump_json(out / f"recording-{rec['id']}.headers.json",
                           dict(sorted(kept.items())))
                written += [gz_name, f"recording-{rec['id']}.headers.json"]
    finally:
        srv.shutdown()
        srv.server_close()
    return written


def generate(out_dir: Path) -> list[str]:
    """Write the golden files under `out_dir`; return their relative names.

    Only the per-server directories are replaced; anything else in `out_dir`
    (the README) stays.
    """
    out_dir = Path(out_dir)
    names: list[str] = []
    with tempfile.TemporaryDirectory(prefix="zz-golden-") as tmp:
        fx = build_zz_fixture(Path(tmp) / "fixture")
        for sid in (SERVER_A_ID, SERVER_B_ID):
            target = out_dir / sid
            if target.exists():
                shutil.rmtree(target)
            names += [f"{sid}/{n}" for n in _golden_for_server(fx.root, sid, target)]
    return sorted(names)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"output directory (default {DEFAULT_OUT})")
    args = ap.parse_args(argv)
    for name in generate(args.out):
        print(f"  {name}")
    print(f"golden files written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
