"""resolve_attacker(): LastHitBy -> name via soldier controller, then AController.PlayerState,
then TakeHitInfo.PawnInstigator -> SQPawn.PlayerState. Exact pointer matches only (no guess)."""
from __future__ import annotations

from sqreader.squad.snapshot import resolve_attacker


class _PM:
    """read_u64 over a dict of mapped qwords; anything else is unmapped (OSError)."""

    def __init__(self, qwords: dict[int, int]) -> None:
        self.q = qwords
        self.reads: list[int] = []

    def read_u64(self, addr: int) -> int:
        self.reads.append(addr)
        if addr not in self.q:
            raise OSError(f"unmapped {addr:#x}")
        return self.q[addr]


class _Paths:
    def __init__(self, pc_playerstate_off=0x2A0, pawn_playerstate_off=0x2B8):
        self.pc_playerstate_off = pc_playerstate_off
        self.pawn_playerstate_off = pawn_playerstate_off


CTRL_A, CTRL_B, CTRL_STALE = 0x1000, 0x2000, 0x3000
PS_A, PS_B = 0x9000, 0x9100
PAWN_B = 0x5000


def _ps_map():
    return {PS_A: "Ayse", PS_B: "Burak"}


def test_soldier_controller_map_wins_without_any_memory_read():
    pm = _PM({})
    out = resolve_attacker(pm, _Paths(), CTRL_A, 0, {CTRL_A: "Ayse"}, _ps_map())
    assert out == "Ayse"
    assert pm.reads == []


def test_seated_attacker_resolved_via_controller_playerstate():
    # Burak is in a vehicle: his soldier has no Controller, so ctrl_to_name lacks CTRL_B.
    paths = _Paths()
    pm = _PM({CTRL_B + paths.pc_playerstate_off: PS_B})
    out = resolve_attacker(pm, paths, CTRL_B, 0, {CTRL_A: "Ayse"}, _ps_map())
    assert out == "Burak"


def test_pawn_instigator_fallback_when_lasthitby_unknown():
    paths = _Paths()
    pm = _PM({PAWN_B + paths.pawn_playerstate_off: PS_B})
    # lhb points at a freed controller (unmapped) -> step 2 fails -> pawn path.
    out = resolve_attacker(pm, paths, CTRL_STALE, PAWN_B, {}, _ps_map())
    assert out == "Burak"
    # and with no lhb at all
    assert resolve_attacker(pm, paths, 0, PAWN_B, {}, _ps_map()) == "Burak"


def test_unknown_playerstate_pointer_is_not_guessed():
    paths = _Paths()
    pm = _PM({CTRL_STALE + paths.pc_playerstate_off: 0xDEAD, PAWN_B + paths.pawn_playerstate_off: 0xBEEF})
    assert resolve_attacker(pm, paths, CTRL_STALE, PAWN_B, {}, _ps_map()) is None


def test_missing_offsets_or_paths_fall_back_to_controller_map_only():
    pm = _PM({CTRL_B + 0x2A0: PS_B})
    assert resolve_attacker(pm, _Paths(pc_playerstate_off=None, pawn_playerstate_off=None), CTRL_B, PAWN_B, {}, _ps_map()) is None
    assert resolve_attacker(pm, None, CTRL_B, PAWN_B, {CTRL_B: "Burak"}, _ps_map()) == "Burak"
    assert resolve_attacker(pm, None, CTRL_B, PAWN_B, {}, _ps_map()) is None
    assert pm.reads == []


def test_empty_player_list_never_reads_memory():
    pm = _PM({})
    assert resolve_attacker(pm, _Paths(), CTRL_B, PAWN_B, {}, {}) is None
    assert pm.reads == []
