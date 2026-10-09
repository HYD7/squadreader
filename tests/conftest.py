"""Make the `sqreader` package importable no matter where pytest is
invoked from (repo root is one level up from this tests/ dir), and
expose a shared FakeProcessMemory helper for mem.py-driven tests
that must not touch /proc/pid/mem."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR.parent))  # repo root — for `import sqreader`
sys.path.insert(0, str(_TESTS_DIR))         # tests dir  — for `from conftest import ...`


@pytest.fixture(autouse=True)
def _upstream_net_policy(monkeypatch):
    """The fork defaults to the `local-only` network policy (sqreader.netpolicy).
    The pre-existing push / update / offset / notify tests exercise the upstream
    behaviour with fake central hosts (`c.test`, `central.example`), so they run
    under an explicit `allow-central`. tests/test_netpolicy.py overrides this per
    test to prove the default and the blocking."""
    monkeypatch.setenv("SQREADER_NET_POLICY", "allow-central")
    monkeypatch.delenv("SQREADER_NET_ALLOW_HOSTS", raising=False)
    yield


class FakeProcessMemory:
    """In-memory ProcessMemory stand-in for tests.

    Backs the address space with a list of (addr, bytes) segments and
    mirrors the ProcessMemory read()/try_read()/read_many()/try_read_many()
    surface without touching /proc/pid/mem. Unregistered addresses raise
    OSError from ``read`` and return ``None`` from ``try_read`` — matching
    the real class's behavior so tests exercise identical control flow.
    """

    def __init__(self, segments: dict[int, bytes] | None = None) -> None:
        self._segments: list[tuple[int, bytes]] = []
        if segments:
            for addr, data in segments.items():
                self._segments.append((addr, bytes(data)))

    def add(self, addr: int, data: bytes) -> None:
        self._segments.append((addr, bytes(data)))

    def _lookup(self, addr: int, size: int) -> bytes | None:
        for base, data in self._segments:
            if base <= addr and addr + size <= base + len(data):
                off = addr - base
                return data[off:off + size]
        return None

    def read(self, addr: int, size: int) -> bytes:
        if size <= 0:
            return b""
        buf = self._lookup(addr, size)
        if buf is None:
            raise OSError(f"unmapped read at 0x{addr:x}+{size}")
        return buf

    def try_read(self, addr: int, size: int) -> bytes | None:
        if addr <= 0 or addr >= 0x800000000000 or size <= 0:
            return None
        return self._lookup(addr, size)

    def read_many(
        self, requests: Sequence[tuple[int, int]],
    ) -> list[bytes]:
        return [self.read(addr, size) for addr, size in requests]

    def try_read_many(
        self, requests: Sequence[tuple[int, int]],
    ) -> list[bytes | None]:
        return [self.try_read(addr, size) for addr, size in requests]


def end_match(store, snap):
    """Drive a match to a CONFIRMED end.

    One `WaitingPostMatch` tick used to be enough, and that was the bug: a
    single torn read ended a match and published whatever score was current.
    `StatsStore` now applies the recorder's rule — a recognised inactive state,
    `MATCH_TRANSITION_CONFIRM_TICKS` times in a row — so tests have to end a
    match the way the game does.

    Deliberately a test helper rather than a knob on StatsStore: a configurable
    threshold would put two different end-of-match policies back in the
    codebase, which is exactly what this change exists to remove.
    """
    from sqreader.recording_lifecycle import MATCH_TRANSITION_CONFIRM_TICKS
    for _ in range(MATCH_TRANSITION_CONFIRM_TICKS):
        store.record_tick(snap)


def warm_up(store, snap):
    """Feed `snap` through the stats warm-up window.

    `StatsStore` ignores the collector counters (kills, captures, ...) for the
    first `_STATS_WARMUP_TICKS` ticks of a new match (they are stored as 0), so
    a test that expects counters in `player_matches` must record that many
    ticks first. Pass a snapshot without kill events so none are duplicated.
    """
    from sqreader.stats import _STATS_WARMUP_TICKS
    for _ in range(_STATS_WARMUP_TICKS):
        store.record_tick(snap)
