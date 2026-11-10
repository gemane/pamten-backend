"""The securities bridge (findings item 71): CUSIP → US ISIN → GLEIF's LEI-ISIN
file → LEI. Identifiers, the file reader, the plan's classes and the gates; the
writes are in tests/integration/test_gleif_isin_it.py. Rows are real ones from
GLEIF's file of 2026-11-06."""
import zipfile
from datetime import date
from unittest.mock import patch

import pytest

from app.scraper import gleif_isin as gi

SIRIUS_LEI, SIRIUS_CUSIP, SIRIUS_ISIN = "54930055RWDMVXAGZK98", "829933100", "US8299331004"
NESTLE_LEI = "KY37LUS27QQX7BB93L28"
ROWS = [
    ("00EHHQ2ZHDCFXJCPCL46", "US92204Q1031"),
    (SIRIUS_LEI, SIRIUS_ISIN),
    (NESTLE_LEI, "US6410694060"),                  # Nestlé's ADR: the foreign issuer's LEI
    ("549300X7CW3B8850WA94", "US4642872000"),      # one iShares fund
    ("549300ZZZZZZZZZZZZ01", "US4642874329"),      # another fund of the same family
    ("029200038B4L4ZI1E579", "NGSDCBANCO00"),      # not a US ISIN
]


@pytest.fixture
def mapping(tmp_path):
    path = tmp_path / "isin-lei-20261106T081508.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("lei-isin-20261106T081508.csv", "LEI,ISIN\n" + "".join(f"{a},{b}\n" for a, b in ROWS))
    return gi.local_file(str(path))


class TestIdentifiers:
    @pytest.mark.parametrize("cusip,isin", [
        ("037833100", "US0378331005"),            # Apple
        (SIRIUS_CUSIP, SIRIUS_ISIN),
        ("641069406", "US6410694060"),            # Nestlé ADR
        ("92204Q103", "US92204Q1031"),            # a CUSIP with a letter
    ])
    def test_a_cusip_gives_its_us_isin(self, cusip, isin):
        assert gi.us_isin(cusip) == isin

    @pytest.mark.parametrize("bad", ["037833101", "03783310", "0378331000", "", None, "03783 3100"])
    def test_an_invalid_cusip_gives_none(self, bad):
        # a wrong check digit or length is no security: an ISIN from it could name another issuer
        assert gi.cusip_valid(bad) is False and gi.us_isin(bad) is None

    def test_lower_case_is_read(self):
        assert gi.us_isin("92204q103") == "US92204Q1031"


class TestTheFile:
    def test_only_the_isins_asked_are_read(self, mapping):
        got = gi.leis_for(mapping.path, {SIRIUS_ISIN, "US0000000000"})
        assert got == {SIRIUS_ISIN: {SIRIUS_LEI}}

    def test_the_day_comes_from_gleifs_file_name(self, mapping):
        assert mapping.day == "2026-11-06"
        with pytest.raises(ValueError):
            gi.local_file("/tmp/whatever.zip")

    def test_an_unexpected_header_is_an_error_not_an_empty_answer(self, tmp_path):
        path = tmp_path / "isin-lei-20261106T081508.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("x.csv", "ISIN,LEI\nUS8299331004,54930055RWDMVXAGZK98\n")
        with pytest.raises(ValueError):
            gi.leis_for(str(path), {SIRIUS_ISIN})

    def test_the_newest_file_comes_from_gleifs_api(self):
        api = {"data": [{"attributes": {"fileName": "isin-lei-20261107T081500.zip",
                                        "uploadedAt": "2026-11-07T08:15:00Z",
                                        "downloadLink": "https://mapping.gleif.org/api/v2/isin-lei/x/download"}}]}
        with patch("app.scraper.gleif_incremental._api_json", return_value=api) as call:
            got = gi.latest_file()
        assert got == {"file_name": "isin-lei-20261107T081500.zip", "uploaded_at": "2026-11-07T08:15:00Z",
                       "url": "https://mapping.gleif.org/api/v2/isin-lei/x/download"}
        assert call.call_args.args[0] == gi.MAPPING_API

    def test_fetch_downloads_the_newest_and_drops_the_older_files(self, tmp_path):
        # the bridge runs daily after the GLEIF delta: each day's ~33 MB file must
        # not pile up; files that are not GLEIF's LEI-ISIN files are left alone
        for name in ("isin-lei-20261105T081500.zip", "isin-lei-20261106T081508.zip.part", "notes.txt",
                     "isin-lei.zip"):
            (tmp_path / name).write_bytes(b"x")
        meta = {"file_name": "isin-lei-20261107T081500.zip", "uploaded_at": "2026-11-07T08:15:00Z",
                "url": "https://mapping.gleif.org/x"}

        def download(url, dest):
            open(dest, "wb").write(b"zip")
        with patch.object(gi, "latest_file", return_value=meta), \
             patch("app.scraper.gleif_incremental._download", side_effect=download) as dl:
            got = gi.fetch(str(tmp_path))
        assert dl.call_count == 1
        assert got.day == "2026-11-07" and got.path == str(tmp_path / "isin-lei-20261107T081500.zip")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["isin-lei-20261107T081500.zip", "isin-lei.zip", "notes.txt"]

    def test_fetch_reuses_todays_file(self, tmp_path):
        (tmp_path / "isin-lei-20261107T081500.zip").write_bytes(b"zip")
        meta = {"file_name": "isin-lei-20261107T081500.zip", "uploaded_at": "2026-11-07T08:15:00Z",
                "url": "https://mapping.gleif.org/x"}
        with patch.object(gi, "latest_file", return_value=meta), \
             patch("app.scraper.gleif_incremental._download") as dl:
            gi.fetch(str(tmp_path))
        assert dl.call_count == 0


def _plan(mapping, nodes, holders, kept=frozenset(), owners=()):
    """`plan` over fake nodes; ``holders``: LEI → the rows `SELECT … WHERE lei_id` answers;
    ``owners``: the pairs an OWNS edge joins."""
    def sql(q, params=None):
        assert "lei_id = :l" in q
        return holders.get(params["l"], [])
    with patch.object(gi, "_nodes_with_cusips", return_value=nodes), \
         patch("app.db.arcadedb.run_sql", side_effect=sql), \
         patch("app.scraper.maintenance.one_owns_the_other",
               side_effect=lambda a, b: frozenset((a, b)) in {frozenset(o) for o in owners}):
        return gi.plan(mapping, kept_separate=set(kept))


class TestThePlan:
    SIRIUS = {"id": "sec:0000908937", "name": "SIRIUS XM HOLDINGS INC.", "sec_cik": "0000908937",
              "lei_id": None, "cusips": [SIRIUS_CUSIP]}

    def test_an_lei_a_node_holds_is_a_join(self, mapping):
        p = _plan(mapping, [self.SIRIUS], {SIRIUS_LEI: [{"id": f"lei:{SIRIUS_LEI}", "name": "SIRIUS XM INC.", "sec_cik": None}]})
        [j] = p.joins
        assert (j["id"], j["lei"], j["isin"], j["lei_node"]) == ("sec:0000908937", SIRIUS_LEI, SIRIUS_ISIN, f"lei:{SIRIUS_LEI}")
        assert p.attaches == [] and p.counts()["joins"] == 1

    def test_a_filer_and_its_subsidiary_are_a_contradiction(self, mapping):
        # findings item 94: Sirius XM's own Exhibit 21 lists 'Sirius XM Inc.' —
        # the GLEIF node its old CUSIP maps to — as its subsidiary
        p = _plan(mapping, [self.SIRIUS], {SIRIUS_LEI: [{"id": f"lei:{SIRIUS_LEI}", "name": "SIRIUS XM INC.", "sec_cik": None}]},
                  owners=[("sec:0000908937", f"lei:{SIRIUS_LEI}")])
        assert p.joins == []
        assert [c["why"] for c in p.contradictions] == [f"lei:{SIRIUS_LEI} and the node own one another"]

    def test_an_lei_no_node_holds_is_an_attach(self, mapping):
        p = _plan(mapping, [self.SIRIUS], {})
        assert [a["lei"] for a in p.attaches] == [SIRIUS_LEI] and p.joins == []

    def test_cusips_of_several_issuers_are_an_umbrella(self, mapping):
        ishares = {"id": "sec:0000930667", "name": "iShares, Inc.", "sec_cik": "0000930667", "lei_id": None,
                   "cusips": ["464287200", "464287432"]}
        p = _plan(mapping, [ishares], {})
        assert [u["id"] for u in p.umbrellas] == ["sec:0000930667"] and p.attaches == [] and p.joins == []
        assert len(p.umbrellas[0]["leis"]) == 2

    def test_a_node_holding_another_lei_is_a_contradiction(self, mapping):
        node = {**self.SIRIUS, "lei_id": "549300MGJZCNMJLBAJ67"}
        p = _plan(mapping, [node], {})
        assert p.contradictions[0]["why"] == "the node holds LEI 549300MGJZCNMJLBAJ67"

    def test_a_node_holding_the_same_lei_is_confirmed(self, mapping):
        p = _plan(mapping, [{**self.SIRIUS, "lei_id": SIRIUS_LEI}], {})
        assert [c["id"] for c in p.confirmed] == ["sec:0000908937"] and not p.joins and not p.attaches

    def test_the_leis_node_with_another_cik_is_a_contradiction(self, mapping):
        p = _plan(mapping, [self.SIRIUS], {SIRIUS_LEI: [{"id": f"lei:{SIRIUS_LEI}", "name": "X", "sec_cik": "0000000001"}]})
        assert p.joins == [] and "holds LEI" in p.contradictions[0]["why"]

    def test_two_nodes_holding_the_lei_are_a_contradiction(self, mapping):
        two = [{"id": "a", "name": "A", "sec_cik": None}, {"id": "b", "name": "B", "sec_cik": None}]
        p = _plan(mapping, [self.SIRIUS], {SIRIUS_LEI: two})
        assert p.joins == [] and p.contradictions[0]["why"] == f"2 nodes hold LEI {SIRIUS_LEI}"

    def test_a_pair_kept_apart_is_never_joined(self, mapping):
        holder = f"lei:{SIRIUS_LEI}"
        p = _plan(mapping, [self.SIRIUS], {SIRIUS_LEI: [{"id": holder, "name": "SIRIUS XM INC.", "sec_cik": None}]},
                  kept={frozenset(("sec:0000908937", holder))})
        assert p.joins == [] and [k["lei_node"] for k in p.kept_apart] == [holder]

    def test_an_unmapped_or_invalid_cusip_is_unmapped(self, mapping):
        nodes = [{"id": "u1", "name": "U", "lei_id": None, "cusips": ["037833100"]},       # not in the file
                 {"id": "u2", "name": "V", "lei_id": None, "cusips": ["BADCUSIP0"]}]
        p = _plan(mapping, nodes, {})
        assert p.unmapped == 2 and p.counts()["nodes"] == 2

    def test_the_scalar_cusip_counts_too(self, mapping):
        node = {**self.SIRIUS, "cusips": None, "cusip": SIRIUS_CUSIP}
        assert [a["lei"] for a in _plan(mapping, [node], {}).attaches] == [SIRIUS_LEI]


class TestTheGates:
    def test_off_unless_switched_on(self, mapping):
        with patch.object(gi, "enabled", return_value=False):
            with pytest.raises(PermissionError):
                gi.run(path=mapping.path)

    def test_the_gleif_catalogue_switch_decides(self):
        # no environment flag of its own (the conformance rule for GLEIF's bulk paths)
        with patch("app.scraper.register_pointer._gleif_enabled", return_value=False):
            assert gi.enabled() is False
        with patch("app.scraper.register_pointer._gleif_enabled", return_value=True):
            assert gi.enabled() is True

    def test_a_stale_file_is_refused(self, mapping):
        with patch.object(gi, "enabled", return_value=True):
            with pytest.raises(ValueError, match="days old"):
                gi.run(path=mapping.path, today=date(2026, 11, 20))

    def test_a_dry_run_writes_nothing_and_logs_the_run(self, mapping):
        import contextlib
        rows = []

        @contextlib.contextmanager
        def fake_run(source, target, reason=None):
            row = {}
            rows.append((source, target, reason, row))
            yield row
        with patch.object(gi, "enabled", return_value=True), \
             patch("app.scraper.run_log.record_run", fake_run), \
             patch.object(gi, "_nodes_with_cusips", return_value=[TestThePlan.SIRIUS]), \
             patch("app.db.arcadedb.run_sql", return_value=[]), \
             patch("app.scraper.maintenance._not_duplicate_pairs", return_value=set()), \
             patch.object(gi, "apply") as write:
            res = gi.run(path=mapping.path, today=date(2026, 11, 7))
        write.assert_not_called()
        assert res["attaches"] == 1 and res["applied"] is False
        assert rows[0][:3] == ("gleif-isin", "all", "dry-run") and "(dry run)" in rows[0][3]["note"]
