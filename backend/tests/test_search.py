"""
Unit tests for the search router's pure helpers (no DB needed). The endpoint
itself is exercised end-to-end in tests/integration/test_person_profile_it.py.
"""
import pytest

from app.routers.search import (
    _dedupe_positions, _dedupe_holdings, _clean, _ownership_summary, _class_key,
)


def _owner(stake, share_class=None):
    return {"owner": {"id": "x"},
            "relationship": {"stake_percent": stake, "share_class": share_class}}


class TestOwnershipSummary:
    def test_free_float_is_the_residual_when_all_known(self):
        s = _ownership_summary([_owner(7.0), _owner(5.0)])
        assert s["disclosed_pct"] == 12.0
        assert s["free_float_pct"] == 88.0
        assert s["exceeds_100"] is False

    def test_no_free_float_when_an_owner_stake_is_unknown(self):
        # can't tell what's left if one owner's % is missing
        s = _ownership_summary([_owner(30.0), _owner(None)])
        assert s["unknown_owners"] == 1
        assert s["free_float_pct"] is None

    def test_flags_over_100_and_no_free_float(self):
        s = _ownership_summary([_owner(80.0), _owner(63.0)])
        assert s["disclosed_pct"] == 143.0
        assert s["exceeds_100"] is True
        assert s["free_float_pct"] is None

    def test_no_free_float_when_fully_held(self):
        # residual below the 0.5% noise threshold → nothing to show
        assert _ownership_summary([_owner(100.0)])["free_float_pct"] is None
        assert _ownership_summary([_owner(99.8)])["free_float_pct"] is None

    def test_no_owners_or_no_known_stakes(self):
        assert _ownership_summary([])["disclosed_pct"] is None
        assert _ownership_summary([_owner(None)])["free_float_pct"] is None


def _held(stake, mode):
    return {"owner": {"id": "x"},
            "relationship": {"stake_percent": stake, "direct_or_indirect": mode}}


class TestIndirectHoldings:
    """Findings item 101: an `indirect` holding is held through somebody
    else's shares — beside a direct owner's stated stake it is not added."""

    def test_an_ultimate_parent_beside_the_direct_one_is_not_summed(self):
        # Distribuidora La Florida: 3-101-949828 S.A. direct 100 %, Heineken N.V. indirect 100 %
        s = _ownership_summary([_held(100.0, "direct"), _held(100.0, "indirect")])
        assert (s["disclosed_pct"], s["exceeds_100"]) == (100.0, False)

    def test_the_remainder_beside_a_looked_through_owner_is_no_free_float(self):
        # the group may hold the other 25 % through another company
        s = _ownership_summary([_held(75.0, "direct"), _held(100.0, "indirect")])
        assert (s["disclosed_pct"], s["free_float_pct"]) == (75.0, None)

    def test_an_owner_without_a_mode_counts_as_holding_the_shares(self):
        # a 13D/G edge carries no mode: its stake is a direct owner's for this rule
        s = _ownership_summary([_held(60.0, None), _held(100.0, "indirect")])
        assert s["disclosed_pct"] == 60.0

    @pytest.mark.parametrize("owners, disclosed", [
        ([_held(100.0, "indirect")], 100.0),                         # alone: the best figure there is
        ([_held(None, "direct"), _held(100.0, "indirect")], 100.0),  # OxyChem: the direct stake unstated
        ([_held(30.0, "indirect"), _held(40.0, "indirect")], 70.0),  # only indirect owners: summed as before
    ])
    def test_an_indirect_stake_counts_when_no_direct_one_is_stated(self, owners, disclosed):
        assert _ownership_summary(owners)["disclosed_pct"] == disclosed

    def test_a_stake_less_indirect_owner_still_counts_as_unknown(self):
        s = _ownership_summary([_held(75.0, "direct"), _held(None, "indirect")])
        assert (s["unknown_owners"], s["free_float_pct"]) == (1, None)


class TestClean:
    def test_strips_arcadedb_metadata_keys(self):
        row = {"@rid": "#1:0", "@type": "Entity", "@cat": "v",
               "id": "acme", "name": "Acme"}
        assert _clean(row) == {"id": "acme", "name": "Acme"}

    def test_keeps_all_data_keys(self):
        row = {"id": "x", "name": "X", "country": "US", "search_text": "X"}
        assert _clean(row) == row


def _row(entity, rel):
    return {"entity": entity, "rel": rel}


class TestDedupePositions:
    def test_collapses_the_same_spell_asserted_twice(self):
        # Two sources describing one appointment. Same company, same role, same
        # start — one row.
        rows = [
            _row({"id": "tesla", "name": "Tesla"}, {"role": "CEO", "since": "2008-10-01"}),
            _row({"id": "tesla", "name": "Tesla"}, {"role": "CEO", "since": "2008-10-01"}),
        ]
        assert len(_dedupe_positions(rows)) == 1

    def test_keeps_two_spells_of_the_same_job(self):
        # Steve Jobs sat on Apple's board from 1977, left in 1985 and came back
        # in 1997. Keying on (company, role) alone threw one of those away — the
        # bug that left the timeline showing a single board seat.
        rows = [
            _row({"id": "apple", "name": "Apple"},
                 {"role": "Board Member", "since": "1977-03-01", "until": "1985-09-01"}),
            _row({"id": "apple", "name": "Apple"},
                 {"role": "Board Member", "since": "1997-01-01", "until": "2011-10-05"}),
        ]
        out = _dedupe_positions(rows)
        assert [p["role"]["since"] for p in out] == ["1977-03-01", "1997-01-01"]

    def test_an_undated_spell_is_not_merged_into_a_dated_one(self):
        # They are not known to be the same appointment, and guessing costs more
        # than the extra row: it would silently date a role the source never dated.
        rows = [
            _row({"id": "tesla", "name": "Tesla"}, {"role": "CEO", "since": None}),
            _row({"id": "tesla", "name": "Tesla"}, {"role": "CEO", "since": "2008-10-01"}),
        ]
        assert len(_dedupe_positions(rows)) == 2

    def test_the_better_informed_copy_of_a_spell_wins(self):
        # Same spell from two sources, one of which knows it ended.
        rows = [
            _row({"id": "tesla", "name": "Tesla"}, {"role": "CEO", "since": "2008-10-01"}),
            _row({"id": "tesla", "name": "Tesla"},
                 {"role": "CEO", "since": "2008-10-01", "until": "2024-01-01"}),
        ]
        out = _dedupe_positions(rows)
        assert len(out) == 1 and out[0]["role"]["until"] == "2024-01-01"

    def test_keeps_distinct_roles_at_the_same_entity(self):
        rows = [
            _row({"id": "spacex", "name": "SpaceX"}, {"role": "CEO", "since": None}),
            _row({"id": "spacex", "name": "SpaceX"}, {"role": "Founder", "since": None}),
        ]
        out = _dedupe_positions(rows)
        assert {p["role"]["role"] for p in out} == {"CEO", "Founder"}

    def test_keeps_same_role_at_different_entities(self):
        rows = [
            _row({"id": "a", "name": "Alpha"}, {"role": "CEO"}),
            _row({"id": "b", "name": "Beta"},  {"role": "CEO"}),
        ]
        assert len(_dedupe_positions(rows)) == 2

    def test_skips_null_entities(self):
        assert _dedupe_positions([_row(None, None)]) == []

    def test_sorted_by_entity_then_role(self):
        rows = [
            _row({"id": "b", "name": "Beta"},  {"role": "CEO"}),
            _row({"id": "a", "name": "Alpha"}, {"role": "Founder"}),
        ]
        out = _dedupe_positions(rows)
        assert [p["entity"]["name"] for p in out] == ["Alpha", "Beta"]

    def test_spells_of_one_job_are_ordered_oldest_first(self):
        rows = [
            _row({"id": "apple", "name": "Apple"}, {"role": "Board Member", "since": "1997-01-01"}),
            _row({"id": "apple", "name": "Apple"}, {"role": "Board Member", "since": "1977-03-01"}),
        ]
        out = _dedupe_positions(rows)
        assert [p["role"]["since"] for p in out] == ["1977-03-01", "1997-01-01"]


class TestDedupeHoldings:
    def test_collapses_same_entity_keeping_largest_stake(self):
        rows = [
            _row({"id": "tesla", "name": "Tesla"}, {"stake_percent": 10}),
            _row({"id": "tesla", "name": "Tesla"}, {"stake_percent": 20.5}),
        ]
        out = _dedupe_holdings(rows)
        assert len(out) == 1
        assert out[0]["relationship"]["stake_percent"] == 20.5

    def test_keeps_distinct_entities(self):
        rows = [
            _row({"id": "a", "name": "Alpha"}, {"stake_percent": 5}),
            _row({"id": "b", "name": "Beta"},  {"stake_percent": 5}),
        ]
        assert len(_dedupe_holdings(rows)) == 2

    def test_skips_null_entities(self):
        assert _dedupe_holdings([_row(None, None)]) == []


class TestShareClassIdentity:
    """A percentage is only addable to another when both measure the same
    security. Grupo Televisa's filers report 22.3% of its Series A/B/Preferred
    shares beside 9.7% of its CPOs — different instruments, different
    denominators — and adding them gave the company 115.9% of itself."""

    def test_a_descriptive_tail_does_not_make_a_new_class(self):
        assert _class_key("Common Stock") == _class_key(
            "Common Stock, par value $0.0001 per share")
        assert _class_key("Ordinary Shares") == _class_key(
            "Ordinary Shares, without nominal value")

    def test_parenthetical_glosses_are_ignored(self):
        # Real Televisa filings differ only by the abbreviations they define.
        assert _class_key('Series A Shares ("A Shares"), Series B Shares ("B Shares")') \
            == _class_key("Series A Shares; Series B Shares")

    def test_order_and_separator_do_not_matter(self):
        assert _class_key("Series B Shares and Series A Shares") \
            == _class_key("Series A Shares; Series B Shares")

    def test_trailing_prose_is_cut(self):
        assert _class_key("Global Depositary Shares") == _class_key(
            "Global Depositary Shares, each representing five CPOs")

    def test_genuinely_different_securities_stay_apart(self):
        # The whole point: these must never merge.
        assert _class_key("Series A Shares; Series B Shares") != _class_key(
            "Certificados de Participacion Ordinarios (CPOs)")
        assert _class_key("Common Stock") != _class_key("Global Depositary Shares")

    def test_an_unstated_class_is_none(self):
        assert _class_key(None) is None
        assert _class_key("   ") is None


class TestClassAwareSummary:
    def test_one_class_behaves_exactly_as_before(self):
        s = _ownership_summary([_owner(7.0, "Common Stock"),
                                _owner(5.0, "Common Stock, $0.01 par value")])
        assert s["disclosed_pct"] == 12.0
        assert s["free_float_pct"] == 88.0
        assert s["multi_class"] is False

    def test_unnamed_classes_do_not_trigger_a_split(self):
        # Every pre-2024 filing lacks a class title; those can't contradict
        # anyone, so they must not suppress the total for the whole graph.
        s = _ownership_summary([_owner(7.0), _owner(5.0)])
        assert s["disclosed_pct"] == 12.0
        assert s["multi_class"] is False

    def test_two_securities_yield_no_single_total(self):
        # Saying nothing beats saying 115.9%.
        s = _ownership_summary([_owner(22.3, "Series A Shares; Series B Shares"),
                                _owner(9.7, "Certificados de Participacion Ordinarios")])
        assert s["multi_class"] is True
        assert s["disclosed_pct"] is None
        assert s["free_float_pct"] is None
        assert s["exceeds_100"] is False

    def test_one_security_over_100_is_flagged_though_there_is_no_total(self):
        # Symbotic's Class A summed 133 % on dev and nothing was flagged (item 110)
        s = _ownership_summary([_owner(86.6, "Class A common stock, par value $0.0001 per share"),
                                _owner(39.9, "Class A Common Stock"),
                                _owner(6.5, "Class A Common Stock"),
                                _owner(0.7, "Class V-1 Common Stock")])
        assert s["multi_class"] is True and s["disclosed_pct"] is None
        assert s["exceeds_100"] is True
        assert (s["exceeds_pct"], s["exceeds_class"]) == (133.0, "Class A common stock, par value $0.0001 per share")
        assert s["free_float_pct"] is None

    def test_several_securities_each_under_100_are_not_flagged(self):
        s = _ownership_summary([_owner(60.0, "Class A"), _owner(30.0, "Class A"), _owner(70.0, "Class B")])
        assert (s["exceeds_100"], s["exceeds_pct"], s["exceeds_class"]) == (False, None, None)

    def test_a_total_over_100_carries_its_figure(self):
        s = _ownership_summary([_owner(80.0), _owner(63.0)])
        assert (s["exceeds_pct"], s["exceeds_class"]) == (143.0, None)

    def test_the_tolerance_holds_per_security(self):
        s = _ownership_summary([_owner(60.0, "Class A"), _owner(40.4, "Class A"), _owner(5.0, "Class B")])
        assert s["exceeds_100"] is False

    def test_each_security_still_gets_its_own_total(self):
        s = _ownership_summary([
            _owner(22.3, "Series A Shares"), _owner(9.0, "Series A Shares"),
            _owner(9.7, "CPOs"),
        ])
        by = {b["share_class"]: b for b in s["by_class"]}
        assert by["Series A Shares"]["disclosed_pct"] == 31.3
        assert by["Series A Shares"]["owners"] == 2
        assert by["CPOs"]["disclosed_pct"] == 9.7

    def test_the_breakdown_is_ordered_largest_first_with_unnamed_last(self):
        s = _ownership_summary([_owner(5.0, "CPOs"), _owner(40.0),
                                _owner(30.0, "Series A Shares")])
        assert [b["share_class"] for b in s["by_class"]] == [
            "Series A Shares", "CPOs", None]

    def test_a_single_named_class_beside_unnamed_ones_still_totals(self):
        # Televisa's oldest filings name no class; one that does cannot make
        # the company multi-class on its own.
        s = _ownership_summary([_owner(44.2), _owner(5.3, "Common Stock")])
        assert s["multi_class"] is False
        assert s["disclosed_pct"] == 49.5


class TestAliasAwareRank:
    """The Samsung case: the register-backed node whose LEGAL name is Korean
    must win a Latin query through its aliases and the register's other
    names — and credibility must break match-quality ties."""

    @staticmethod
    def _key(node, q):
        from app.routers.search import _rank
        return _rank(node, q, q.split(), 0, __import__(
            "app.scraper.mapper", fromlist=["normalize_entity_name"]
        ).normalize_entity_name(q))

    def test_an_exact_alias_beats_a_name_that_merely_starts_with(self):
        samsung_kr = {"name": "삼성전자(주)", "name_normalized": "삼성전자(주)",
                      "aliases": ["Samsung", "Samsung Electronics Co., Ltd."],
                      "wikidata_id": "Q21121070", "name_credibility": 92}
        samsung_group = {"name": "Samsung Group", "name_normalized": "samsung group",
                         "aliases": [], "wikidata_id": "Q20716", "name_credibility": 80}
        assert self._key(samsung_kr, "samsung") < self._key(samsung_group, "samsung")

    def test_the_registers_other_names_count_like_aliases(self):
        node = {"name": "삼성전자(주)", "name_normalized": "삼성전자(주)",
                "other_names": ["SAMSUNG ELECTRONICS CO., LTD"],
                "name_credibility": 92}
        plain = {"name": "Samsung Heavy Industries", "name_normalized": "samsung heavy industries",
                 "name_credibility": 92}
        assert self._key(node, "samsung electronics") < self._key(plain, "samsung electronics")

    def test_alias_token_coverage_counts_not_just_alias_exactness(self):
        # A query that is NOT the normalized alias ("samsung ltd") must still
        # credit the alias's token coverage — this is the case that dies when
        # aliases only feed the exact-match check.
        kr = {"name": "삼성전자(주)", "name_normalized": "삼성전자(주)",
              "aliases": ["Samsung Electronics Co., Ltd"], "name_credibility": 92}
        group = {"name": "Samsung Group", "name_normalized": "samsung group",
                 "wikidata_id": "Q20716", "name_credibility": 80}
        assert self._key(kr, "samsung ltd") < self._key(group, "samsung ltd")

    def test_credibility_breaks_a_tie_after_match_quality_and_notability(self):
        register = {"name": "Acme Corp", "name_normalized": "acme corp",
                    "wikidata_id": "Q1", "name_credibility": 92}
        wiki = {"name": "Acme Corp", "name_normalized": "acme corp",
                "wikidata_id": "Q2", "name_credibility": 80}
        assert self._key(register, "acme corp") < self._key(wiki, "acme corp")

    def test_a_better_name_match_still_beats_higher_credibility(self):
        # Credibility is a TIEBREAK, not a trump: searching "Heineken Vietnam"
        # must surface the subsidiary however credible the parent is.
        subsidiary = {"name": "Heineken Vietnam Brewery", "name_normalized": "heineken vietnam brewery",
                      "name_credibility": 80}
        parent = {"name": "Heineken Holding", "name_normalized": "heineken holding",
                  "wikidata_id": "Q1", "name_credibility": 98}
        assert self._key(subsidiary, "heineken vietnam") < self._key(parent, "heineken vietnam")

    # Findings item 54 (2026-11-05): Wikidata's one item for T. Rowe Price
    # (Q3511946) carries "T. Rowe Price Associates, Inc." as an alias and
    # points at the GROUP's CIK, while the adviser of that name is a legal
    # entity of its own (CIK 80255). With both exact, the Group won on
    # `notable`, and the scrape list's "T. Rowe Price Associates, Inc." meant
    # the Group whenever Wikidata had answered and the adviser when it had not.
    GROUP = {"name": "PRICE T ROWE GROUP INC", "name_normalized": "price t rowe group",
             "aliases": ["T. Rowe Price Associates, Inc.", "T. Rowe Price Group, Inc."],
             "wikidata_id": "Q3511946", "name_credibility": 98}
    ASSOCIATES = {"name": "PRICE T ROWE ASSOCIATES INC /MD/", "name_normalized": "price t rowe associates",
                  "aliases": ["T. Rowe Price Associates, Inc."], "name_credibility": 98}
    ADVISER_BY_NAME = {"name": "T. Rowe Price Associates, Inc.", "name_normalized": "t rowe price associates",
                       "name_credibility": 98}

    def test_a_nodes_own_name_beats_another_nodes_alias(self):
        q = "t. rowe price associates, inc."
        assert self._key(self.ADVISER_BY_NAME, q) < self._key(self.GROUP, q)

    def test_the_own_name_in_another_word_order_is_the_own_name(self):
        # EDGAR files the adviser surname-first; the natural spelling is the
        # same filer, not a namesake — and so beats the Group's alias (item 59
        # is the search side of the same inversion)
        q = "t. rowe price associates, inc."
        assert self._key(self.ASSOCIATES, q)[0] == 0
        assert self._key(self.ASSOCIATES, q) < self._key(self.GROUP, q)

    def test_the_own_name_in_the_natural_order_finds_the_group(self):
        q = "t. rowe price group, inc."
        assert self._key(self.GROUP, q) < self._key(self.ADVISER_BY_NAME, q)
        assert self._key(self.GROUP, q)[0] == 0 and self._key(self.ADVISER_BY_NAME, q)[0] == 3

    def test_an_alias_in_other_words_is_an_alias(self):
        from app.routers.search import _same_words
        assert _same_words("price t rowe associates", "t rowe price associates")
        assert not _same_words("price t rowe group", "t rowe price associates")
        assert not _same_words("", "") and not _same_words("acme", "")
        # the Group lists the adviser's name; its own words are others
        assert self._key(self.GROUP, "t. rowe price associates, inc.")[0] == 1
        # and the alias still finds the node no node is named by
        kr = {"name": "삼성전자(주)", "name_normalized": "삼성전자(주)", "aliases": ["Samsung"]}
        assert self._key(kr, "samsung")[0] == 1

    def test_the_registers_other_name_is_the_nodes_own(self):
        register = {"name": "삼성전자(주)", "name_normalized": "삼성전자(주)",
                    "other_names": ["SAMSUNG ELECTRONICS CO., LTD"], "name_credibility": 92}
        wiki = {"name": "Samsung Electronics America, Inc.", "name_normalized": "samsung electronics america",
                "aliases": ["Samsung Electronics"], "wikidata_id": "Q1", "name_credibility": 80}
        assert self._key(register, "samsung electronics") < self._key(wiki, "samsung electronics")
        assert self._key(register, "samsung electronics")[0] == 0

    def test_an_alias_in_force_beats_a_name_that_ended(self):
        in_force = {"name": "Acme Holdings plc", "name_normalized": "acme holdings",
                    "aliases": ["Acme"], "name_credibility": 92}
        ended = {"name": "Zenith Corp", "name_normalized": "zenith",
                 "aliases": ["Acme"], "former_names": ["Acme|2020-01-01"], "name_credibility": 98,
                 "wikidata_id": "Q9"}
        assert self._key(in_force, "acme") < self._key(ended, "acme")
        assert self._key(in_force, "acme")[0] == 1 and self._key(ended, "acme")[0] == 2


class TestAControllersBeneficialStake:
    """Findings item 102 (the user's choice of 2026-11-11: controlled only):
    BRC's 13D/A on AmBev, 71.9 %, counts the shares AB InBev holds, 61.73 %,
    and BRC controls AB InBev by its bloc — 133.6 % summed."""

    @staticmethod
    def _o(oid, pct, ft):
        return {"owner": {"id": oid}, "relationship": {"stake_percent": pct, "filing_type": ft}}

    BRC = ("sec:0001301486", 71.9, "13D/A")
    ABI = ("lei:5493008H3828EMEXB082", 61.73, "EX-8.1")

    def test_a_13d_of_the_controller_of_another_owner_is_not_added(self):
        s = _ownership_summary([self._o(*self.BRC), self._o(*self.ABI)],
                               {"lei:5493008H3828EMEXB082": {"sec:0001301486"}})
        assert (s["disclosed_pct"], s["exceeds_100"], s["free_float_pct"]) == (61.73, False, None)

    def test_without_control_every_stake_is_added(self):
        # Vanguard holds a few % of State Street: passive, not through it
        s = _ownership_summary([self._o("v", 7.0, "13G"), self._o("ss", 6.0, "13G")], {})
        assert s["disclosed_pct"] == 13.0

    def test_a_direct_holding_of_a_controller_is_added(self):
        # Chubb INA International 99.998 % beside its own subsidiary's 0.002 %:
        # two parts of 100 %, both stated in the subsidiary list
        s = _ownership_summary([self._o("parent", 99.998, "EX-21"), self._o("sub", 0.002, "EX-21")],
                               {"sub": {"parent"}})
        assert s["disclosed_pct"] == 100.0

    def test_two_owners_controlling_each_other_look_through_neither(self):
        s = _ownership_summary([self._o("a", 40.0, "13D"), self._o("b", 30.0, "13D")],
                               {"a": {"b"}, "b": {"a"}})
        assert s["disclosed_pct"] == 70.0

    def test_a_controlled_owner_without_a_stated_stake_leaves_the_controller_counted(self):
        s = _ownership_summary([self._o(*self.BRC), self._o("lei:5493008H3828EMEXB082", None, "EX-8.1")],
                               {"lei:5493008H3828EMEXB082": {"sec:0001301486"}})
        assert s["disclosed_pct"] == 71.9


@pytest.mark.parametrize("edge, yes", [
    ({"filing_type": "EX-21"}, True), ({"source_id": "gleif"}, True), ({"ownership_type": "majority"}, True),
    ({"stake_percent": 50.1}, True), ({"voting_power_pct": 52.3, "ownership_type": "minority"}, True),
    ({"stake_percent": 50.0}, False), ({"stake_percent": 7.0, "filing_type": "13G"}, False), ({}, False)])
def test_what_control_is(edge, yes):
    from app.routers.search import controls
    assert controls(edge) is yes
