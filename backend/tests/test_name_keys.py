"""`heal_name_keys`: the stored name key recomputed where the normaliser
changed, and the pairs the old key hid named. Pure-logic check over a faked
page walk."""
from app import name_keys


def _patch(monkeypatch, rows):
    from app.db import paging
    calls = []
    monkeypatch.setattr(paging, "iter_id_pages", lambda vtype, columns="": iter([rows]))
    monkeypatch.setattr(name_keys, "run_sqlscript", lambda script, params=None: calls.append((script, params)))
    return calls


ROWS = [
    {"id": "lei:1", "rid": "#1:0", "name": "GRUPO TELEVISA S A B", "name_normalized": "grupo televisa s a b", "country": "MX"},
    {"id": "sec:1", "rid": "#1:1", "name": "GRUPO TELEVISA, S.A.B.", "name_normalized": "grupo televisa sab", "country": "MX"},
    {"id": "lei:2", "rid": "#1:2", "name": "NESTLE PERU S A", "name_normalized": "nestle peru s a", "country": "PE"},
    {"id": "x", "rid": "#1:3", "name": "Apple Inc.", "name_normalized": "apple", "country": "US"},
    {"id": "nameless", "rid": "#1:4", "name": None, "name_normalized": None, "country": None},
    # keyed by the writer from the issuer, not from its display name
    {"id": "vg", "rid": "#1:5", "name": "Voting group · 5 parties", "name_normalized": "voting group mercadolibre",
     "country": None, "type": "voting_group"},
]


def test_the_dry_run_counts_names_the_new_pair_and_writes_nothing(monkeypatch):
    calls = _patch(monkeypatch, ROWS)
    res = name_keys.heal_name_keys()
    assert (res["scanned"], res["changed"], res["applied"]) == (6, 2, False)
    assert res["sample"] == [("GRUPO TELEVISA S A B", "grupo televisa s a b", "grupo televisa sab"),
                             ("NESTLE PERU S A", "nestle peru s a", "nestle peru")]
    # the two Televisas share a key for the first time; Nestle Peru shares with nobody
    assert res["newly_shared"] == {"grupo televisa sab | MX": ["GRUPO TELEVISA S A B", "GRUPO TELEVISA, S.A.B."]}
    assert calls == []


def test_apply_rewrites_only_the_changed_keys(monkeypatch):
    calls = _patch(monkeypatch, ROWS)
    res = name_keys.heal_name_keys(apply=True)
    assert res["applied"] is True and res["changed"] == 2
    [(script, params)] = calls
    assert script.count("UPDATE") == 2 and "#1:0" in script and "#1:2" in script and "#1:3" not in script
    assert "#1:5" not in script                      # a voting group keeps the writer's key
    assert set(params.values()) == {"grupo televisa sab", "nestle peru"}


def test_a_pair_that_already_shared_its_key_is_not_news(monkeypatch):
    rows = [{"id": "a", "rid": "#2:0", "name": "Berkshire Hathaway Inc.", "name_normalized": "berkshire hathaway", "country": "US"},
            {"id": "b", "rid": "#2:1", "name": "BERKSHIRE HATHAWAY INC.", "name_normalized": "berkshire hathaway", "country": "US"}]
    _patch(monkeypatch, rows)
    res = name_keys.heal_name_keys()
    assert res["changed"] == 0 and res["newly_shared"] == {}


def test_a_voting_group_keeps_the_key_its_writer_gave_it(monkeypatch):
    rows = [{"id": "vg", "rid": "#3:0", "name": "Voting group · 9 parties", "country": None,
             "name_normalized": "voting group anheuser-busch inbev /", "type": "voting_group"}]
    calls = _patch(monkeypatch, rows)
    res = name_keys.heal_name_keys(apply=True)
    assert res["changed"] == 0 and res["sample"] == [] and calls == []
