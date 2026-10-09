from sqreader import cli
from sqreader.squad.killmerge import DamageEventMerger


class FakeTailer:
    path = "/x/SquadGame.log"

    def __init__(self, events, ok=True):
        self.events, self.ok = events, ok

    def drain(self):
        ev, self.events = self.events, []
        return ev

    def healthy(self):
        return self.ok


PLAYERS = [{"name": "Vic", "eosId": "E1"}]


def _log():
    return {"victim": "Vic", "attacker": "Bob", "killed": True,
            "causerWeapon": "AK"}


def _mem():
    return {"victim": "Vic", "victimEosId": "E1", "killed": True,
            "damage": 100.0}


def test_same_victim_single_event():
    snap = {"damageEvents": [_mem()]}
    cli._merge_damage_events(snap, PLAYERS, FakeTailer([_log()]),
                             DamageEventMerger(), 1, {})
    assert len(snap["damageEvents"]) == 1
    assert snap["damageEvents"][0]["attacker"] == "Bob"


def test_unhealthy_passes_memory():
    snap = {"damageEvents": [_mem()]}
    cli._merge_damage_events(snap, PLAYERS, FakeTailer([], ok=False),
                             DamageEventMerger(), 1, {})
    assert [e["victim"] for e in snap["damageEvents"]] == ["Vic"]
    assert snap["damageEvents"][0]["source"] == "memory"


def test_warning_once(capsys):
    t, m, st = FakeTailer([]), DamageEventMerger(), {}
    cli._merge_damage_events({}, PLAYERS, t, m, 0, st)
    assert capsys.readouterr().err == ""      # first observation: silent
    t.ok = False
    for i in range(1, 4):
        cli._merge_damage_events({}, PLAYERS, t, m, i, st)
    err = capsys.readouterr().err
    assert err.count("okunamıyor") == 1
    t.ok = True
    for i in range(4, 7):
        cli._merge_damage_events({}, PLAYERS, t, m, i, st)
    err = capsys.readouterr().err
    assert err.count("tekrar okunuyor") == 1


def test_first_tick_unhealthy_no_warning(capsys):
    cli._merge_damage_events({}, PLAYERS, FakeTailer([], ok=False),
                             DamageEventMerger(), 0, {})
    assert capsys.readouterr().err == ""


def test_merge_exception_falls_back_to_log_events(capsys):
    class Boom:
        def merge(self, *a, **k):
            raise RuntimeError("x")

    snap = {"damageEvents": [_mem()]}
    cli._merge_damage_events(snap, PLAYERS, FakeTailer([_log()]), Boom(), 1, {})
    assert [e["attacker"] for e in snap["damageEvents"]] == ["Bob"]
    assert "kill merge error" in capsys.readouterr().err
