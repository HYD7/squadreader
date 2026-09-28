#!/usr/bin/env python3
"""Write the deterministic Zero Zone integration fixture to a directory.

    python scripts/make_zz_fixture.py --out /tmp/zz-fixture

The logic lives in tests/zz_fixture.py; this is only the command line. The
snapshots pass through the reader's real StatsStore / recorder / SqrxWriter
code (see that module's docstring for the layout and what is authored).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))                 # import sqreader
sys.path.insert(0, str(_REPO / "tests"))       # import zz_fixture

from zz_fixture import build_zz_fixture  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, type=Path,
                    help="output directory (must not exist or must be empty)")
    args = ap.parse_args(argv)
    try:
        result = build_zz_fixture(args.out)
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"fixture written to {result.root}")
    for key in sorted(result.matches):
        m = result.matches[key]
        print(f"  {key}: {m.server_id}  {m.kind:<10}  {m.sqrx_path.relative_to(result.root)}")
    print(f"manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
