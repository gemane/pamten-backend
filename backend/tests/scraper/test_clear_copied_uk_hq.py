"""Unit tests for maintenance.clear_copied_uk_headquarters — the one-off heal
that undoes the UK importers' copy of the registered office into hq_* (PSC
checklist finding #7). The page walk and the script writer are faked; the
real SQL is pinned by tests/integration/test_clear_copied_uk_hq_it.py."""
from app.scraper import maintenance as m

OFFICE = "1 HIGH STREET, HARROGATE, HG1 1AA"


def _copied(**over):
    """A gb-coh node as both importers left it: hq_* equal to the registered office."""
    row = {"id": "gb-coh:00000001", "rid": "#1:1", "address": None, "registered_address": OFFICE,
           "hq_address": OFFICE, "hq_city": "HARROGATE", "hq_country": "GB",
           "hq_street": "1 HIGH STREET", "hq_postcode": "HG1 1AA",
           "hq_lat": 53.99, "hq_lng": -1.54, "hq_geo_precision": "street",
           "reg_street": None, "reg_city": None, "reg_postcode": None,
           "reg_lat": None, "reg_lng": None, "reg_geo_precision": None}
    row.update(over)
    return row


def _run(monkeypatch, pages_by_prefix: dict, apply=True):
    monkeypatch.setattr(m, "iter_id_pages",
                        lambda vtype, prefix="", page=5000, columns="": iter(pages_by_prefix.get(prefix, [])))
    scripts = []
    monkeypatch.setattr(m, "run_sqlscript", lambda script, params=None: scripts.append((script, params)))
    return m.clear_copied_uk_headquarters(apply=apply), scripts


class TestClearCopiedUkHeadquarters:
    def test_a_copied_headquarters_is_cleared_and_its_parts_and_pin_move_under_registered(self, monkeypatch):
        res, scripts = _run(monkeypatch, {"gb-coh:": [[_copied()]]})
        assert res == {"nodes": 1, "copied": 1, "pins_moved": 1, "addresses_filled": 1, "applied": True}
        assert len(scripts) == 1
        script, params = scripts[0]
        assert script.startswith("UPDATE #1:1 SET ")
        for f in m._COPIED_HQ_FIELDS:
            assert f"{f} = null" in script
        # the parts and the pin were made from the registered office: kept, under reg_*
        assert {k: v for k, v in params.items()} == {
            "reg_street_0": "1 HIGH STREET", "reg_city_0": "HARROGATE", "reg_postcode_0": "HG1 1AA",
            "reg_lat_0": 53.99, "reg_lng_0": -1.54, "reg_geo_precision_0": "street",
            "address_0": OFFICE}

    def test_a_registered_part_already_there_is_kept(self, monkeypatch):
        # GLEIF put its own legal-address parts and pin on the merged node: not replaced
        row = _copied(reg_street="1 High Street", reg_lat=53.9, reg_lng=-1.5, reg_geo_precision="street",
                      address="1 High Street, Harrogate")
        res, scripts = _run(monkeypatch, {"gb-coh:": [[row]]})
        assert (res["copied"], res["pins_moved"], res["addresses_filled"]) == (1, 0, 0)
        _, params = scripts[0]
        assert params == {"reg_city_0": "HARROGATE", "reg_postcode_0": "HG1 1AA"}

    def test_a_real_headquarters_is_left_alone(self, monkeypatch):
        # GLEIF's HeadquartersAddress differs from the registered office (and GLEIF's
        # registered_address is a lowercased normalised form anyway)
        row = _copied(hq_address="Unilever House, 100 Victoria Embankment, London",
                      registered_address="port sunlight wirral ch62 4zd gb", address="Port Sunlight, Wirral")
        res, scripts = _run(monkeypatch, {"gb-coh:": [[row]]})
        assert (res["copied"], res["addresses_filled"]) == (0, 0)
        assert scripts == []

    def test_a_missing_display_address_is_filled_even_without_a_copied_headquarters(self, monkeypatch):
        row = _copied(hq_address=None, hq_city=None, hq_country=None, hq_street=None, hq_postcode=None,
                      hq_lat=None, hq_lng=None, hq_geo_precision=None)
        res, scripts = _run(monkeypatch, {"chpsc:": [[row]]})
        assert (res["copied"], res["addresses_filled"]) == (0, 1)
        script, params = scripts[0]
        assert script == "UPDATE #1:1 SET address = :address_0" and params == {"address_0": OFFICE}

    def test_both_uk_prefixes_are_walked(self, monkeypatch):
        res, scripts = _run(monkeypatch, {"gb-coh:": [[_copied()]],
                                          "chpsc:": [[_copied(id="chpsc:1:2", rid="#2:2")]]})
        assert (res["nodes"], res["copied"]) == (2, 2)
        assert [s.split(" ")[1] for s, _ in scripts] == ["#1:1", "#2:2"]

    def test_a_dry_run_counts_and_writes_nothing(self, monkeypatch):
        res, scripts = _run(monkeypatch, {"gb-coh:": [[_copied(), _copied(id="gb-coh:2", rid="#1:2")]]},
                            apply=False)
        assert (res["copied"], res["pins_moved"], res["applied"]) == (2, 2, False)
        assert scripts == []
