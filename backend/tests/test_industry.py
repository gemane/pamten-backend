"""Industry classification (app.industry): the bundle, the mappings, the
statements and the entity stamp — pure logic, the SQL layer mocked.
Against a real ArcadeDB: tests/integration/test_industry_it.py."""
from unittest.mock import patch

import pytest

from app import industry
from app.industry import (
    ch_company_url, nace_for, nace_tree, parse_uk_sic, principal, profile_for, record,
    scheme_name, statement_key, statement_props, us_sic_from_submissions,
)


class TestBundle:
    def test_the_tree_is_complete(self):
        b = industry.bundle()
        levels = [n["level"] for n in b["nace"].values()]
        # NACE Rev. 2: 21 sections, 88 divisions, 272 groups, 615 classes
        assert (levels.count(1), levels.count(2), levels.count(3), levels.count(4)) == (21, 88, 272, 615)
        assert b["schemes"]["us-sic"]["codes"] and b["schemes"]["uk-sic"]["codes"]

    def test_labels_in_both_languages(self):
        node = industry.nace_node("23.32")
        assert node["label"]["en"].startswith("Manufacture of bricks")
        assert node["label"]["de"].startswith("Herstellung von Ziegeln")

    def test_scheme_names(self):
        assert scheme_name("uk-sic") == "UK SIC 2007"
        assert scheme_name("us-sic").startswith("US SIC")
        assert scheme_name("gics") is None


class TestNaceTree:
    def test_a_class_has_four_levels_section_first(self):
        tree = nace_tree("23.32")
        assert [t["code"] for t in tree] == ["C", "23", "23.3", "23.32"]
        assert [t["level"] for t in tree] == [1, 2, 3, 4]
        assert tree[0]["label"]["en"] == "Manufacturing"

    def test_a_section_or_division_is_a_shorter_path(self):
        assert [t["code"] for t in nace_tree("J")] == ["J"]
        assert [t["code"] for t in nace_tree("21")] == ["C", "21"]

    def test_unknown_or_empty(self):
        assert nace_tree(None) == []
        assert nace_tree("99.99") == []


class TestUkSic:
    """A UK SIC 2007 subclass is a NACE class with a fifth digit."""

    @pytest.mark.parametrize("code,nace", [
        ("23320", "23.32"), ("62012", "62.01"), ("64209", "64.20"), ("01110", "01.11"),
    ])
    def test_the_four_digits_are_the_nace_class(self, code, nace):
        assert nace_for("uk-sic", code) == nace

    @pytest.mark.parametrize("code", ["74990", "98000", "99999"])
    def test_the_uk_only_codes_mean_nothing_in_nace(self, code):
        assert nace_for("uk-sic", code) is None
        assert industry.code_entry("uk-sic", code)["label"]   # the register's wording stays

    def test_the_register_page_needs_a_number(self):
        page = "https://find-and-update.company-information.service.gov.uk/company/"
        assert ch_company_url("gb-coh:05299520", "05299520") == page + "05299520"
        assert ch_company_url("gb-coh:05299520", None) == page + "05299520"      # the id carries it
        assert ch_company_url("lei:5493001KJTIIGC8Y1R12", " ") is None          # no number, no link
        assert ch_company_url(None, None) is None

    def test_parse_the_register_cell(self):
        assert parse_uk_sic("23320 - Manufacture of bricks, tiles and construction products, in baked clay") == (
            "23320", "Manufacture of bricks, tiles and construction products, in baked clay")
        assert parse_uk_sic("99999 - Dormant Company") == ("99999", "Dormant Company")
        assert parse_uk_sic("None Supplied") is None
        assert parse_uk_sic("") is None
        assert parse_uk_sic(None) is None


class TestUsSic:
    """A SEC code reaches NACE through the Census and UN crosswalks and stops at
    the deepest node holding a majority of its paths."""

    def test_a_tight_industry_reaches_a_class(self):
        assert nace_for("us-sic", "3711") == "29.10"      # motor vehicles
        assert nace_for("us-sic", "2911") == "19.20"      # petroleum refining
        assert nace_for("us-sic", "6022") == "64.19"      # commercial banks

    def test_a_catch_all_stops_higher(self):
        assert nace_for("us-sic", "7370") == "J"          # computer programming, data processing, etc.
        assert nace_for("us-sic", "5812") == "56"         # eating places
        assert nace_for("us-sic", "2834") == "21"         # pharmaceutical preparations

    def test_the_hand_decided_ones(self):
        assert nace_for("us-sic", "7372") == "58.29"      # prepackaged software: publishers
        assert nace_for("us-sic", "6770") is None         # blank checks: no activity

    def test_the_sec_group_codes_are_mapped(self):
        # EDGAR's 3250 is the 1987 list's group 325 (3251, 3253, 3255, 3259); NAICS
        # 2017 folds them all into one industry that spans clay building
        # materials, refractories and other ceramics → the division
        assert nace_for("us-sic", "3250") == "23"

    def test_from_the_submissions_document(self):
        assert us_sic_from_submissions({"sic": "3711", "sicDescription": "Motor Vehicles & Passenger Car Bodies"}) == (
            "3711", "Motor Vehicles & Passenger Car Bodies")
        # a three-digit code is zero-padded; a missing title comes from the list
        assert us_sic_from_submissions({"sic": "100"}) == ("0100", "AGRICULTURAL PRODUCTION-CROPS")
        assert us_sic_from_submissions({"sic": ""}) is None
        assert us_sic_from_submissions({}) is None
        assert us_sic_from_submissions(None) is None


class TestStatements:
    def test_key_is_per_entity_scheme_and_position(self):
        assert statement_key("e1", "uk-sic", 1) != statement_key("e1", "uk-sic", 2)
        assert statement_key("e1", "uk-sic", 1) != statement_key("e2", "uk-sic", 1)
        assert statement_key("e1", "uk-sic", 1) == statement_key("e1", "uk-sic", 1)

    def test_props_carry_the_mapping_and_the_source(self):
        p = statement_props("e1", "uk-sic", "23320", "Bricks", source_id="s1",
                            source_url="https://example.com/c/1", source_date="2026-07-01")
        assert p["nace_code"] == "23.32" and p["position"] == 1
        assert p["source_id"] == "s1" and p["source_url"].startswith("https://")
        assert p["label"] == "Bricks" and p["last_seen_at"]

    def test_a_missing_label_comes_from_the_list(self):
        assert statement_props("e1", "us-sic", "3711", None, source_id="s")["label"] == "MOTOR VEHICLES & PASSENGER CAR BODIES"

    def test_record_upserts_then_stamps(self):
        calls = []
        with patch("app.db.arcadedb.run_sql", side_effect=lambda q, p=None, **k: calls.append((q, p)) or []):
            props = record("e1", "uk-sic", "23320", "Bricks", source_id="s1")
        assert props["nace_code"] == "23.32"
        assert "UPSERT WHERE key = :key" in calls[0][0] and calls[0][1]["key"] == props["key"]
        assert "UPDATE Entity SET nace_code" in calls[-1][0]

    def test_record_without_a_source_writes_nothing(self):
        with patch("app.db.arcadedb.run_sql") as run:
            assert record("e1", "uk-sic", "23320", "x", source_id="") is None
        run.assert_not_called()

    def test_record_is_best_effort(self):
        with patch("app.db.arcadedb.run_sql", side_effect=RuntimeError("down")):
            assert record("e1", "uk-sic", "23320", "x", source_id="s") is None


class TestPrincipal:
    """Which statement decides the tree."""

    def test_a_register_code_beats_a_crosswalked_one(self):
        us = {"scheme": "us-sic", "position": 1, "nace_code": "J"}
        uk = {"scheme": "uk-sic", "position": 1, "nace_code": "62.01"}
        assert principal([us, uk]) is uk

    def test_the_first_position_wins(self):
        a = {"scheme": "uk-sic", "position": 2, "nace_code": "62.02"}
        b = {"scheme": "uk-sic", "position": 1, "nace_code": "62.01"}
        assert principal([a, b]) is b

    def test_a_statement_without_a_nace_node_yields(self):
        dormant = {"scheme": "uk-sic", "position": 1, "nace_code": None}
        us = {"scheme": "us-sic", "position": 1, "nace_code": "29.10"}
        assert principal([dormant, us]) is us
        assert principal([dormant]) is None
        assert principal([]) is None


class TestProfile:
    def test_tree_and_statements(self):
        rows = [{"key": "k", "entity_id": "e", "scheme": "uk-sic", "code": "23320", "label": "Bricks",
                 "position": 1, "nace_code": "23.32", "source_id": "s1",
                 "source_url": "https://example.com/c", "source_date": "2026-07-01"}]
        with patch("app.industry.statements_for", return_value=rows):
            p = profile_for("e", None, {"s1": "Companies House"})
        assert p["nace_code"] == "23.32"
        assert [t["code"] for t in p["tree"]] == ["C", "23", "23.3", "23.32"]
        assert p["statements"][0]["scheme_name"] == "UK SIC 2007"
        assert p["statements"][0]["source_name"] == "Companies House"
        assert [t["code"] for t in p["statements"][0]["tree"]] == ["C", "23", "23.3", "23.32"]

    def test_every_statement_carries_its_own_path(self):
        rows = [{"scheme": "uk-sic", "code": "23320", "label": "Bricks", "position": 1, "nace_code": "23.32", "source_id": "s"},
                {"scheme": "uk-sic", "code": "46730", "label": "Wholesale", "position": 2, "nace_code": "46.73", "source_id": "s"},
                {"scheme": "us-sic", "code": "3250", "label": "Structural Clay", "position": 1, "nace_code": "23", "source_id": "t"}]
        with patch("app.industry.statements_for", return_value=rows):
            p = profile_for("e", None)
        assert [t["code"] for t in p["tree"]] == ["C", "23", "23.3", "23.32"]           # the principal's
        assert [[t["code"] for t in s["tree"]] for s in p["statements"]] == [
            ["C", "23", "23.3", "23.32"], ["G", "46", "46.7", "46.73"], ["C", "23"]]

    def test_the_stamped_code_draws_the_tree_when_statements_are_unreadable(self):
        with patch("app.industry.statements_for", side_effect=RuntimeError("down")):
            p = profile_for("e", "62.01")
        assert p["nace_code"] == "62.01" and p["statements"] == []

    def test_nothing_known_is_none(self):
        with patch("app.industry.statements_for", return_value=[]):
            assert profile_for("e", None) is None

    def test_a_uk_only_code_shows_the_statement_but_no_tree(self):
        rows = [{"scheme": "uk-sic", "code": "99999", "label": "Dormant Company", "position": 1,
                 "nace_code": None, "source_id": "s1"}]
        with patch("app.industry.statements_for", return_value=rows):
            p = profile_for("e", None)
        assert p["nace_code"] is None and p["tree"] == [] and p["statements"][0]["code"] == "99999"
        assert p["statements"][0]["tree"] == []
