"""
Identity of a 13D filing group.

The bloc belongs to the group, not to whoever filed the schedule — AB InBev's
52.3% was hanging off BRC, which merely submitted the form while nine parties
vote it. Getting the group's *identity* right is the whole difficulty: the filer
changes between amendments, and the roster changes as parties join and leave, so
neither can be an exact key.
"""
from app.scraper.runner import (
    _is_control_filing, _member_key, _same_member, _roster_overlap, _rosters_match,
    _split_member_key,
)


def _roster(*parties):
    """(name, cik) pairs → the stored roster shape."""
    return [_member_key(n, c) for n, c in parties]


ABI = (("BRC S.a R.L.", "0001301486"), ("Stichting Anheuser-Busch InBev", None),
       ("Eugenie Patri Sebastien S.A.", None), ("Rayvax Societe d'Investissements S.A.", None),
       ("Fonds Baillet Latour CV", None), ("Fonds Voorzitter Verhelst SC", None),
       ("Jorge Paulo Lemann", None), ("Carlos Alberto da Veiga Sicupira", None),
       ("Max Van Hoegaerden Herrmann Telles", None))


class TestWhichFilingsMakeAGroup:
    """13G "shared voting power" is an asset manager aggregating across its own
    subsidiaries — State Street over Berkshire, Morgan Stanley over Embraer. Only
    a 13D is filed with control intent."""

    def test_a_13d_is_a_control_filing(self):
        assert _is_control_filing("SCHEDULE 13D") is True
        assert _is_control_filing("SCHEDULE 13D/A") is True
        assert _is_control_filing("SC 13D") is True

    def test_a_13g_is_not(self):
        assert _is_control_filing("SCHEDULE 13G") is False
        assert _is_control_filing("SCHEDULE 13G/A") is False
        assert _is_control_filing("SC 13G/A") is False

    def test_an_absent_form_type_is_not(self):
        assert _is_control_filing(None) is False
        assert _is_control_filing("") is False


class TestMemberIdentity:
    def test_a_member_keeps_both_identifiers(self):
        # Not "CIK if present, else name": EDGAR gives a CIK only to registrants
        # — one of AB InBev's nine — and pre-2024 filings carry names alone.
        # Encoded "cik|name" because the roster is stored as a node property and
        # ArcadeDB refuses a list containing maps.
        cik, name = _split_member_key(_member_key("BRC S.a R.L.", "1301486"))
        assert cik == "0001301486"
        assert name

    def test_a_member_without_a_cik_still_has_a_key(self):
        cik, name = _split_member_key(_member_key("Fonds Baillet Latour CV", None))
        assert cik is None
        assert name == "fonds baillet latour cv"

    def test_the_same_party_matches_across_the_xml_boundary(self):
        # The case a single-identifier key would miss: a CIK in a post-2024 XML
        # amendment, name only in the SGML-era one before it. AB InBev's filing
        # chain spans that boundary.
        with_cik = _member_key("BRC S.a R.L.", "1301486")
        name_only = _member_key("BRC S.à r.l.", None)
        assert _same_member(with_cik, name_only)

    def test_ciks_win_over_differing_spellings(self):
        a = _member_key("Morgan Stanley & Co. LLC", "0000895421")
        b = _member_key("MORGAN STANLEY", "895421")
        assert _same_member(a, b)

    def test_different_parties_do_not_match(self):
        assert not _same_member(_member_key("Rayvax", None),
                                _member_key("Fonds Baillet Latour CV", None))
        assert not _same_member(_member_key("A Corp", "111"), _member_key("B Corp", "222"))


class TestGroupIdentity:
    def test_the_same_roster_is_the_same_group(self):
        assert _rosters_match(_roster(*ABI), _roster(*ABI))

    def test_a_different_filer_still_matches(self):
        # The reason the filer cannot be the key: whoever submits the next
        # amendment is incidental. Order changes, membership does not.
        reordered = _roster(*(ABI[3:] + ABI[:3]))
        assert _rosters_match(_roster(*ABI), reordered)

    def test_one_party_leaving_still_matches(self):
        # A continuing agreement whose roster shifts must follow the node, not
        # orphan it and mint a replacement — which exact-set matching would do.
        assert _rosters_match(_roster(*ABI), _roster(*ABI[:-1]))

    def test_one_party_joining_still_matches(self):
        joined = ABI + (("New Party S.A.", None),)
        assert _rosters_match(_roster(*ABI), _roster(*joined))

    def test_two_blocs_sharing_one_member_stay_apart(self):
        # AB InBev really has two overlapping agreements: the Stichting sits in
        # the families' pact AND in the Altria voting agreement. One shared
        # member out of three is below the threshold, so they stay two groups.
        altria_side = _roster(("Altria Group, Inc.", "0000764180"),
                              ("Bevco Lux S.a.r.l.", None),
                              ("Stichting Anheuser-Busch InBev", None))
        assert not _rosters_match(_roster(*ABI), altria_side)

    def test_sharing_two_members_is_not_enough_on_its_own(self):
        # The ratio matters as well as the count. A nine-party bloc and a
        # five-party one sharing two members have 2/5 = 40% of the smaller
        # roster in common — two different agreements that happen to overlap,
        # not one agreement seen twice. A count-only rule merges them.
        big = _roster(*ABI)
        small = _roster(ABI[0], ABI[1], ("Outsider A", None), ("Outsider B", None),
                        ("Outsider C", None))
        assert _roster_overlap(big, small) == 2
        assert not _rosters_match(big, small)

    def test_a_single_shared_member_is_never_enough(self):
        a = _roster(("A", None), ("B", None))
        b = _roster(("A", None), ("C", None))
        assert _roster_overlap(a, b) == 1
        assert not _rosters_match(a, b)

    def test_an_empty_roster_matches_nothing(self):
        assert not _rosters_match([], _roster(*ABI))
        assert not _rosters_match(_roster(*ABI), [])

    def test_overlap_counts_each_party_once(self):
        # Two entries for one name must not inflate the count into a match.
        dup = _roster(("A", None), ("A", None), ("X", None), ("Y", None))
        other = _roster(("A", None), ("P", None), ("Q", None), ("R", None))
        assert _roster_overlap(dup, other) == 1
        assert not _rosters_match(dup, other)
