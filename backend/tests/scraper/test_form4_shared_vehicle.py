"""A position several insiders of one issuer report alike is a vehicle, not a
part of each one's holding (findings item 111). The fixtures are America
Movil's real Form 3s of 2026-03."""
from pathlib import Path

import xml.etree.ElementTree as ET

import pytest

from app.scraper.sec_edgar import (SharesOutstanding, _form4_blocks, _form4_holdings, _parse_form34_xml,
                                   split_shared_blocks, vehicle_name, without_vehicles)

FIXTURES = Path(__file__).parent / "fixtures"
SERIES_B = SharesOutstanding(60_264_000_000.0, "2026-02-28", {})      # America Movil's cover count


def _ex(who: str) -> dict:
    ex = _parse_form34_xml((FIXTURES / f"amx_form3_{who}.xml").read_text())
    ex.update(source_url=f"https://www.sec.gov/f3-{who}-index.htm", source_date=f"2026-03-1{len(who) % 9}")
    return ex


def test_the_blocks_of_a_form_3_are_its_positions_and_sum_to_the_holding():
    root = ET.fromstring((FIXTURES / "amx_form3_carlos.xml").read_text())
    blocks = _form4_blocks(root)
    assert [(b["indirect"], b["nature"], b["shares"]) for b in blocks] == [
        (False, "", 2325617791.0),
        (True, "By Control Trust", 17742515522.0),
        (True, "By Control Empresarial Trust by Control Empresarial", 10933355873.0),
        (True, "By GFI", 196009735.0)]
    assert _form4_holdings(root) == {"Series B Shares": sum(b["shares"] for b in blocks)}


def test_a_block_two_insiders_report_alike_is_taken_out_once():
    family = [_ex("carlos"), _ex("patrick"), _ex("daniel"), _ex("vanessa")]
    vehicles = split_shared_blocks(family, SERIES_B)
    assert sorted((v["name"], v["shares"], len(v["reporters"])) for v in vehicles) == [
        ("Control Empresarial Trust by Control Empresarial", 10933355873.0, 3),
        ("Control Trust", 17742515522.0, 2),
        ("GFI", 196009735.0, 2)]
    assert {ex["name"]: ex["shares_owned"] for ex in family} == {
        "Domit Carlos Slim": 2325617791.0,
        "Domit Patrick Slim": 1236591858.0 + 1139074.0 + 4891412.0,        # direct + spouse + children
        "Aboumrad Daniel Antonio Hajj": 29824857979.0,                       # "By Spouse" holds them: as filed
        "Slim Vanessa Hajj": 2444476.0}
    ce = next(v for v in vehicles if v["name"].startswith("Control Empresarial"))
    assert ce["title"] == "Series B Shares" and ce["source_url"]


def test_one_reporter_or_a_different_count_is_no_vehicle():
    assert split_shared_blocks([_ex("carlos")], SERIES_B) == []
    carlos, patrick = _ex("carlos"), _ex("patrick")
    for b in patrick["blocks"]:
        b["shares"] += 1 if b["indirect"] else 0
    assert split_shared_blocks([carlos, patrick], SERIES_B) == []
    assert carlos["shares_owned"] == 31197498921.0


def test_the_same_person_twice_is_one_reporter():
    a, b = _ex("carlos"), _ex("carlos")
    assert split_shared_blocks([a, b], SERIES_B) == []


def test_a_direct_position_is_never_a_vehicle():
    a, b = _ex("carlos"), _ex("carlos")
    b["person_cik"], b["name"] = "0000000001", "Somebody Else"
    b["blocks"] = [x for x in b["blocks"] if not x["indirect"]]
    assert split_shared_blocks([a, b], SERIES_B) == []


@pytest.mark.parametrize("nature, name", [("By Control Trust", "Control Trust"), ("by GFI", "GFI"),
                                          ("Held by  the Smith Family Trust.", "the Smith Family Trust"),
                                          ("Trust", "Trust")])
def test_the_holder_is_named_as_the_filer_wrote_it(nature, name):
    assert vehicle_name(nature) == name


def test_a_persons_own_filing_loses_a_known_vehicle():
    holding = {"shares_owned": 17742615522.0, "share_class": "Series B Shares",
               "holdings": {"Series B Shares": 17742615522.0},
               "blocks": [{"title": "Series B Shares", "indirect": False, "nature": "", "shares": 100000.0},
                          {"title": "Series B Shares", "indirect": True, "nature": "By Control Trust",
                           "shares": 17742515522.0}]}
    known = [{"name": "Control Trust", "share_class": "Series B Shares", "shares": 17742515522}]
    got = without_vehicles(holding, known, 60_264_000_000.0)
    assert got["shares_owned"] == 100000.0 and got["holdings"] == {"Series B Shares": 100000.0}
    # another count is another position
    assert without_vehicles(holding, [{**known[0], "shares": 1}], None) is holding
    # nothing of their own left
    only = {**holding, "holdings": {"Series B Shares": 17742515522.0}, "blocks": holding["blocks"][1:]}
    assert without_vehicles(only, known, None) is None


def test_the_same_count_under_other_words_is_no_vehicle():
    # two holders that happen to hold the same count are two holders
    carlos, patrick = _ex("carlos"), _ex("patrick")
    for b in patrick["blocks"]:
        if b["indirect"]:
            b["nature"] = b["nature"] + " II"
    assert split_shared_blocks([carlos, patrick], SERIES_B) == []


def test_a_small_block_or_one_that_names_nobody_is_no_vehicle():
    # the guard over 83 issuers: TSMC's officers each hold 7,036 shares "LTI Trust"
    # (equal grants), Alignment Healthcare's General Atlantic funds "See footnote",
    # NYT's Sulzberger family "trust" (a kind of holder, not a name)
    carlos, patrick = _ex("carlos"), _ex("patrick")
    assert split_shared_blocks([carlos, patrick], SharesOutstanding(60_264_000_000_000.0, None, {})) == []
    carlos, patrick = _ex("carlos"), _ex("patrick")
    for b in [*carlos["blocks"], *patrick["blocks"]]:
        if b["indirect"]:
            b["nature"] = "See footnote"
    assert split_shared_blocks([carlos, patrick], SERIES_B) == []
    carlos, patrick = _ex("carlos"), _ex("patrick")
    for b in [*carlos["blocks"], *patrick["blocks"]]:
        if b["indirect"]:
            b["nature"] = "trust" if "Control Trust" in b["nature"] else b["nature"]
    assert "trust" not in {v["name"] for v in split_shared_blocks([carlos, patrick], SERIES_B)}
    # no count of the class: no measure, no vehicle
    assert split_shared_blocks([_ex("carlos"), _ex("patrick")], None) == []
