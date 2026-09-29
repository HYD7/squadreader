"""`_resolve_sqmap` must not be a denial-of-service lever.

The suffix-stripping regex used to be ``(A|B(_v\\d+)?|...|_v\\d+)+$``: a nested
quantifier with overlapping alternatives, exponential when the tail cannot match.
A single ~146-character, otherwise valid name made the HTTP thread spin for
seconds. These tests pin (a) the worst-case timing, (b) that the rewrite resolves
every name exactly like the old pattern did, and (c) the name-length cap.
"""
import random
import re
import time
from pathlib import Path

import pytest

from sqreader import httpsrv
from sqreader.httpsrv import (
    _SQMAP_MAX_NAME_LEN,
    _resolve_sqmap,
    _strip_sqmap_suffixes,
)

# The pattern that shipped before the fix (kept ONLY as an oracle for small inputs).
_OLD = re.compile(
    r"(_Minimap(_Large)?|_Skirmish(_v\d+)?|_Seed(_v\d+)?|_RAAS(_v\d+)?|"
    r"_AAS(_v\d+)?|_Invasion(_v\d+)?|_TC(_v\d+)?|_v\d+)+$",
    re.IGNORECASE,
)

_PIECES = [
    "_Minimap", "_Large", "_Minimap_Large", "_Skirmish", "_Seed", "_RAAS", "_AAS",
    "_Invasion", "_TC", "_v1", "_v22", "_V3", "_vX", "v1", "_", "a", "Map", "T_",
    "RAAS", "_minimap", "_MINIMAP_LARGE", "1", "-", "_v", "_Tc", "AAS", "_Seedv1", "_tc_V10",
]


def test_strip_matches_the_old_pattern():
    rng = random.Random(20260929)
    for _ in range(40000):
        s = "".join(rng.choice(_PIECES) for _ in range(rng.randint(1, 7)))
        assert _strip_sqmap_suffixes(s) == _OLD.sub("", s), s


@pytest.mark.parametrize(
    "name",
    [
        "T" + "_TC_v1" * 24 + "X",
        "T" + "_TC_v1" * 10 + "X",
        "A" + "_RAAS_v1" * 20 + "X",
        "A" + "_v1" * 40 + "X",
        "A" + "_Minimap_Large" * 10 + "X",
        "T_" + "_v1" * 21 + "X",
        "A_v" + "1" * 58 + "X",
        "a" * 150,
        "T_" + "a" * 62,
    ],
)
def test_worst_case_names_are_fast(tmp_path: Path, name: str):
    (tmp_path / "Fallujah.webp").write_bytes(b"x")
    t0 = time.perf_counter()
    got = _resolve_sqmap(tmp_path, name)
    took_ms = (time.perf_counter() - t0) * 1000
    assert got is None
    assert took_ms < 50, f"{len(name)} chars took {took_ms:.1f} ms"


def test_strip_is_fast_on_the_exponential_input():
    s = "T" + "_TC_v1" * 24 + "X"
    t0 = time.perf_counter()
    assert _strip_sqmap_suffixes(s) == s
    assert (time.perf_counter() - t0) * 1000 < 20


def test_long_names_are_refused(tmp_path: Path):
    (tmp_path / "Fallujah.webp").write_bytes(b"x")
    assert _resolve_sqmap(tmp_path, "Fallujah") == tmp_path / "Fallujah.webp"
    assert _resolve_sqmap(tmp_path, "a" * (_SQMAP_MAX_NAME_LEN + 1)) is None
    assert _SQMAP_MAX_NAME_LEN == 64
    assert httpsrv._SQMAP_MAX_STRIPS >= 21


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Fallujah", "Fallujah.webp"),
        ("Fallujah_Minimap", "Fallujah.webp"),
        ("Fallujah_Minimap_Large", "Fallujah.webp"),
        ("Fallujah_RAAS_v1", "Fallujah.webp"),
        ("Fallujah_Seed_v2_Minimap", "Fallujah.webp"),
        ("T_Fallujah_Minimap", "Fallujah.webp"),
        ("Stock", "T_Stock.webp"),
        ("T_AlBasrah_Minimap", "albasrah.webp"),
        ("Fools_Road_Minimap", "foolsroad.webp"),
        ("PacificProvingGrounds_V1_Minimap", "pacific.webp"),
        ("Logar_Valley_Minimap", "logar.webp"),
        ("Gorodok_fmodel", "Gorodok.webp"),
        ("gorodok_minimap", "Gorodok.webp"),
    ],
)
def test_resolution_examples_unchanged(tmp_path: Path, name: str, expected: str):
    for f in (
        "Fallujah.webp", "T_Stock.webp", "albasrah.webp", "foolsroad.webp",
        "pacific.webp", "logar.webp", "Gorodok.webp",
    ):
        (tmp_path / f).write_bytes(b"x")
    got = _resolve_sqmap(tmp_path, name)
    # Windows/macOS filesystems are case-insensitive, so the returned path keeps the
    # spelling of the candidate that matched; compare case-insensitively.
    assert got is not None and got.name.lower() == expected.lower()
