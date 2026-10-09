"""Tests for the pure log+memory damage event merger."""
from sqreader.squad.killmerge import DamageEventMerger

PLAYERS = [{"name": "Bob", "eosId": "E-BOB"}, {"name": "Al", "eosId": "E-AL"}]


def log_ev(victim="Bob", killed=True, **kw):
    ev = {"victim": victim, "victimEosId": None, "victimTeam": None,
          "attacker": "Al", "attackerEosId": None, "selfInflicted": False,
          "causerWeapon": None, "causerClass": None, "damageType": "Bullet",
          "hitDistance": None, "headshot": None, "wounded": not killed,
          "killed": killed, "ts": 1700000000.0}
    ev.update(kw)
    return ev


def mem_ev(victim="Bob", eos="E-BOB", killed=True, **kw):
    ev = {"victim": victim, "victimEosId": eos, "victimTeam": 2,
          "victimSoldier": "BP_Soldier_1", "victimPos": [1, 2, 3],
          "attacker": "Al", "attackerEosId": "E-AL", "attackerCtrlAddr": 99,
          "selfInflicted": False, "ts": 123.0, "damage": 100.0,
          "damageType": "Bullet", "causerClass": "BP_Rifle_C",
          "causerWeapon": "Rifle", "hitDistance": 42.0, "headshot": True,
          "killed": killed, "wounded": not killed, "bone": "head"}
    ev.update(kw)
    return ev


def run(m, logs, mems, tick, now=None, healthy=True):
    return m.merge(logs, mems, PLAYERS, tick=tick,
                   now=tick * 1.0 if now is None else now, log_healthy=healthy)


def test_same_tick_merge():
    out = run(DamageEventMerger(), [log_ev()], [mem_ev()], 10)
    assert len(out) == 1
    e = out[0]
    assert e["source"] == "log+memory"
    assert e["ts"] == 1700000000.0
    assert e["victimEosId"] == "E-BOB" and e["victimTeam"] == 2
    assert e["victimPos"] == [1, 2, 3] and e["bone"] == "head"
    assert e["damage"] == 100.0 and e["headshot"] is True
    assert e["hitDistance"] == 42.0 and e["causerClass"] == "BP_Rifle_C"
    assert e["attackerEosId"] == "E-AL" and e["causerWeapon"] == "Rifle"
    assert e["attackerCtrlAddr"] == 99


def test_memory_earlier_ticks_match():
    for back in (1, 2):
        m = DamageEventMerger()
        run(m, [], [mem_ev()], 10)
        out = run(m, [log_ev()], [], 10 + back)
        assert out[0]["source"] == "log+memory"


def test_outside_window_no_match():
    m = DamageEventMerger()
    run(m, [], [mem_ev()], 10)
    assert run(m, [log_ev()], [], 14)[0]["source"] == "log"
    m = DamageEventMerger()
    run(m, [], [mem_ev()], 10, now=0.0)
    assert run(m, [log_ev()], [], 11, now=7.0)[0]["source"] == "log"


def test_unmatched_memory_depends_on_health():
    assert run(DamageEventMerger(), [], [mem_ev()], 1, healthy=True) == []
    out = run(DamageEventMerger(), [], [mem_ev()], 1, healthy=False)
    assert len(out) == 1 and out[0]["source"] == "memory"


def test_kinds_separate_and_single_use():
    m = DamageEventMerger()
    mems = [mem_ev(killed=False), mem_ev(killed=True, bone="spine")]
    logs = [log_ev(killed=True), log_ev(killed=False), log_ev(killed=True)]
    out = run(m, logs, mems, 5)
    assert [o["source"] for o in out] == ["log+memory", "log+memory", "log"]
    assert out[0]["bone"] == "spine"


def test_log_fields_win():
    out = run(DamageEventMerger(),
              [log_ev(attacker="Zed", causerWeapon="Knife", damageType="Melee")],
              [mem_ev()], 3)
    e = out[0]
    assert e["attacker"] == "Zed" and e["causerWeapon"] == "Knife"
    assert e["damageType"] == "Melee"


def test_empty_attacker_filled_unless_self_inflicted():
    out = run(DamageEventMerger(), [log_ev(attacker=None)], [mem_ev()], 3)
    assert out[0]["attacker"] == "Al"
    out = run(DamageEventMerger(),
              [log_ev(attacker=None, selfInflicted=True)], [mem_ev()], 3)
    assert out[0]["attacker"] is None


def test_clan_tag_base_name_matches_without_eos():
    mem = mem_ev(victim="Bob", eos=None)
    out = run(DamageEventMerger(), [log_ev(victim="  bob ")], [mem], 3)
    assert out[0]["source"] == "log+memory"


def test_eos_priority():
    mem = mem_ev(victim="Bob", eos="E-OTHER")
    out = run(DamageEventMerger(), [log_ev(victim="Bob")], [mem], 3)
    assert out[0]["source"] == "log"


def test_empty_and_inputs_not_mutated():
    assert run(DamageEventMerger(), [], [], 1) == []
    le, me = log_ev(), mem_ev()
    snap = (dict(le), dict(me))
    run(DamageEventMerger(), [le], [me], 1)
    assert (le, me) == snap
