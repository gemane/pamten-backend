"""Unit tests for maintenance.clear_copied_uk_headquarters — the one-off heal
that undoes the UK importers' copy of the registered office into hq_* (PSC
checklist finding #7). The page walk and the script writer are faked; the
real SQL is pinned by tests/integration/test_clear_copied_uk_hq_it.py."""
from app.scraper import maintenance as m

OFFICE = "1 HIGH STREET, HARROGATE, HG1 1AA"


def _copied(**over):
    """A gb-coh node as both importers left it: hq_* equal to the registered office."""
    row = {"id": "gb-coh:00000001", "rid": "#1:1", "address": OFFICE,
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
    def test_a_copied_headquarters_and_its_pin_are_cleared_and_the_parts_move_under_registered(self, monkeypatch):
        res, scripts = _run(monkeypatch, {"gb-coh:": [[_copied()]]})
        assert res == {"nodes": 1, "copied": 1, "pins_cleared": 1, "applied": True}
        assert len(scripts) == 1
        script, params = scripts[0]
        assert script.startswith("UPDATE #1:1 SET ")
        for f in ("hq_address", "hq_city", "hq_country", "hq_street", "hq_postcode",
                  "hq_lat", "hq_lng", "hq_geo_precision"):
            assert f"{f} = null" in script
        # the parts were made from the registered office: kept, under reg_*; the pin is
        # not moved (it may predate the copy and be another source's HQ point)
        assert {k: v for k, v in params.items()} == {
            "reg_street_0": "1 HIGH STREET", "reg_city_0": "HARROGATE", "reg_postcode_0": "HG1 1AA"}

    def test_a_registered_part_already_there_is_kept(self, monkeypatch):
        # GLEIF put its own legal-address parts and pin on the merged node: not replaced
        row = _copied(reg_street="1 High Street", reg_lat=53.9, reg_lng=-1.5, reg_geo_precision="street")
        res, scripts = _run(monkeypatch, {"gb-coh:": [[row]]})
        assert (res["copied"], res["pins_cleared"]) == (1, 1)
        _, params = scripts[0]
        assert params == {"reg_city_0": "HARROGATE", "reg_postcode_0": "HG1 1AA"}

    def test_a_real_headquarters_is_left_alone(self, monkeypatch):
        # GLEIF's HeadquartersAddress differs from the registered office
        row = _copied(hq_address="Unilever House, 100 Victoria Embankment, London",
                      address="Port Sunlight, Wirral")
        res, scripts = _run(monkeypatch, {"gb-coh:": [[row]]})
        assert res["copied"] == 0
        assert scripts == []
