"""
Computing a delta from a source that publishes none (DB not involved).

Companies House overwrites one full snapshot daily, so the change set is worked
out here by digesting the file and comparing it against the last one. Two things
have to hold for that to be trustworthy, and both are tested here:

* the digest moves for every field the graph reads, and **only** for those; and
* the merge walk classifies added / changed / vanished correctly at the edges of
  the file, where two-cursor merges habitually go wrong.

End-to-end application is covered against a real ArcadeDB in
tests/integration/test_ch_psc_incremental_it.py.
"""
import gzip
import json
import zipfile

import pytest

from app.scraper.ch_psc_incremental import (
    DiffResult, churn_allowed, churn_pct, days_since, diff_digests, record_digest,
    self_link_of, snapshot_date, snapshot_entry, write_digest,
)

LINK = "/company/07434180/persons-with-significant-control/individual/abc123"


def record(**over):
    data = {
        "kind": "individual-person-with-significant-control",
        "name": "Ann Owner",
        "nationality": "British",
        "country_of_residence": "England",
        "date_of_birth": {"month": 4, "year": 1975},
        "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
        "notified_on": "2016-04-06",
        "address": {"locality": "London", "country": "England"},
        "links": {"self": LINK},
        # Not read by the graph — see the class about them below.
        "etag": "a" * 40,
        "identity_verification_details": {"appointment_verification_statement_date": "2026-01-01"},
    }
    data.update(over.pop("data", {}))
    rec = {"company_number": "07434180", "data": data}
    rec.update(over)
    return rec


def digest_file(tmp_path, rows, name="d.tsv.gz"):
    """A pre-sorted digest sidecar, written as the real one is."""
    path = tmp_path / name
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for link, dig in sorted(rows):
            fh.write(f"{link}\t{dig}\n")
    return str(path)


class TestWhatMovesTheDigest:
    """The digest covers a projection of the record — the fields `psc_record`
    reads — rather than the raw line or Companies House's `etag`.

    Both of those are cheaper and both are wrong the same way:
    `identity_verification_details` is being rolled out across the register, so
    either would report millions of records as changed for something the graph
    never stores. One false positive costs an idempotent rewrite; millions cost a
    multi-hour run that changes nothing.
    """

    def test_the_same_record_digests_the_same(self):
        assert record_digest(record()) == record_digest(record())

    @pytest.mark.parametrize("field,value", [
        ("kind", "corporate-entity-person-with-significant-control"),
        ("name", "Someone Else"),
        ("nationality", "French"),
        ("country_of_residence", "Wales"),
        ("date_of_birth", {"month": 5, "year": 1975}),
        ("natures_of_control", ["ownership-of-shares-25-to-50-percent"]),
        ("notified_on", "2017-01-01"),
        ("ceased_on", "2020-01-31"),
        ("identification", {"registration_number": "12345678"}),
        ("address", {"locality": "Leeds"}),
        ("name_elements", {"forename": "Ann"}),
    ])
    def test_every_field_the_graph_reads_moves_it(self, field, value):
        assert record_digest(record(data={field: value})) != record_digest(record())

    def test_ceased_on_moves_it(self):
        # Called out on its own because it is the single most important one: 17.9%
        # of the register carries it, and it is how a PSC's control ends. A digest
        # blind to it would make every cessation invisible to the refresh.
        assert record_digest(record(data={"ceased_on": "2020-01-31"})) != record_digest(record())

    def test_a_different_company_moves_it(self):
        assert record_digest(record(company_number="00000001")) != record_digest(record())

    @pytest.mark.parametrize("field,value", [
        ("etag", "b" * 40),
        ("identity_verification_details", {"appointment_verification_statement_date": "2026-06-01"}),
    ])
    def test_fields_the_graph_ignores_do_not(self, field, value):
        # The whole reason the digest is a projection rather than the line.
        assert record_digest(record(data={field: value})) == record_digest(record())

    def test_key_order_does_not(self):
        a = record()
        b = {"data": dict(reversed(list(a["data"].items()))), "company_number": a["company_number"]}
        assert record_digest(a) == record_digest(b)


class TestFindingTheKeyWithoutParsing:
    """Pass B tests ~15.6M lines for membership and parses only the few that
    changed, so the link is pulled out by regex. Getting it wrong means silently
    skipping a record."""

    def test_finds_the_link(self):
        assert self_link_of(json.dumps(record()).encode()) == LINK

    def test_agrees_with_the_parsed_record(self):
        line = json.dumps(record()).encode()
        assert self_link_of(line) == json.loads(line)["data"]["links"]["self"]

    def test_is_not_fooled_by_the_word_self_in_a_string(self):
        rec = record(data={"address": {"locality": 'Self Help House, "self": "/nope"'}})
        assert self_link_of(json.dumps(rec).encode()) == LINK

    def test_is_not_fooled_by_another_object_with_a_self_key(self):
        # Position is not a safe tiebreak. Companies House emits keys
        # alphabetically, so `links` sits in the MIDDLE of the line — neither the
        # first nor the last `"self"` is reliably the right one. Hence anchoring on
        # the links object.
        line = (b'{"company_number":"1","data":{'
                b'"identification":{"self":"/wrong/before"},'
                b'"links":{"self":"' + LINK.encode() + b'"},'
                b'"other":{"self":"/wrong/after"}}}')
        assert self_link_of(line) == LINK

    def test_takes_the_link_from_a_links_object_wherever_it_sits(self):
        for line in (b'{"data":{"links":{"self":"/a"},"name":"z"}}',
                     b'{"data":{"name":"z","links":{"self":"/a"}}}'):
            assert self_link_of(line) == "/a"

    def test_returns_none_without_one(self):
        assert self_link_of(b'{"company_number":"1","data":{"kind":"x"}}') is None

    def test_returns_none_when_only_another_object_has_a_self(self):
        assert self_link_of(b'{"data":{"identification":{"self":"/nope"}}}') is None


class TestTheMergeWalk:
    """Added / changed / vanished, over two byte-sorted files. The edges of the
    file are where two-cursor merges go wrong, so each end is covered."""

    def _diff(self, tmp_path, prev, new):
        return diff_digests(digest_file(tmp_path, prev, "p.gz"),
                            digest_file(tmp_path, new, "n.gz"))

    def test_nothing_changed(self, tmp_path):
        d = self._diff(tmp_path, [("/a", "1"), ("/b", "2")], [("/a", "1"), ("/b", "2")])
        assert (d.added, d.changed, d.vanished) == (0, 0, [])
        assert d.total == 0

    def test_a_changed_digest(self, tmp_path):
        d = self._diff(tmp_path, [("/a", "1")], [("/a", "2")])
        assert d.changed == 1 and d.touched == {"/a"} and d.added == 0

    def test_an_added_record(self, tmp_path):
        d = self._diff(tmp_path, [("/a", "1")], [("/a", "1"), ("/b", "2")])
        assert d.added == 1 and d.touched == {"/b"}

    def test_a_vanished_record(self, tmp_path):
        d = self._diff(tmp_path, [("/a", "1"), ("/b", "2")], [("/a", "1")])
        assert d.vanished == ["/b"] and d.touched == set()

    def test_the_first_key(self, tmp_path):
        # Added before everything, and vanished from the front — the two the cursor
        # ordering gets wrong first.
        assert self._diff(tmp_path, [("/b", "1")], [("/a", "1"), ("/b", "1")]).touched == {"/a"}
        assert self._diff(tmp_path, [("/a", "1"), ("/b", "1")], [("/b", "1")]).vanished == ["/a"]

    def test_the_last_key(self, tmp_path):
        assert self._diff(tmp_path, [("/a", "1")], [("/a", "1"), ("/z", "1")]).touched == {"/z"}
        assert self._diff(tmp_path, [("/a", "1"), ("/z", "1")], [("/a", "1")]).vanished == ["/z"]

    def test_an_empty_previous_digest(self, tmp_path):
        d = self._diff(tmp_path, [], [("/a", "1"), ("/b", "2")])
        assert d.added == 2 and d.vanished == []

    def test_an_empty_new_snapshot(self, tmp_path):
        # Everything gone: real only if the download was truncated, which is
        # precisely what the churn guard is for.
        d = self._diff(tmp_path, [("/a", "1"), ("/b", "2")], [])
        assert d.added == 0 and sorted(d.vanished) == ["/a", "/b"]

    def test_all_three_at_once(self, tmp_path):
        d = self._diff(tmp_path,
                       [("/a", "1"), ("/b", "2"), ("/c", "3")],
                       [("/a", "1"), ("/b", "9"), ("/d", "4")])
        assert d.changed == 1 and d.added == 1 and d.vanished == ["/c"]
        assert d.touched == {"/b", "/d"}
        assert d.total == 3


class TestTheChurnGuard:
    """A snapshot diff can rewrite the whole graph in one run if something upstream
    shifts — a schema change, a truncated download, an edited projection. The guard
    refuses *before* any write, which is why the diff is computed in full first."""

    def _diff(self, changed, prev_records):
        return DiffResult(changed=changed, prev_records=prev_records)

    def test_ordinary_movement_passes(self):
        ok, _ = churn_allowed(self._diff(1000, 100000), max_pct=5.0, days=1)
        assert ok

    def test_a_rewrite_of_everything_is_refused(self):
        ok, why = churn_allowed(self._diff(100000, 100000), max_pct=5.0, days=1)
        assert not ok and "100.00%" in why and "--force" in why

    def test_the_allowance_scales_with_the_gap(self):
        # A week's changes really are about seven days' worth; refusing them would
        # make a missed run unrecoverable without --force.
        d = self._diff(6000, 100000)                       # 6%
        assert not churn_allowed(d, max_pct=5.0, days=1)[0]
        assert churn_allowed(d, max_pct=5.0, days=7)[0]

    def test_percentages_are_of_the_previous_register(self):
        assert churn_pct(self._diff(500, 10000)) == 5.0

    def test_an_empty_baseline_does_not_divide_by_zero(self):
        assert churn_pct(DiffResult(added=5, new_records=5)) == 100.0


class TestSnapshotIdentity:
    def test_the_date_comes_from_the_entry_name(self):
        # The register's own truth-time. A closed edge is dated with it, so the same
        # file applied a week late still produces the same graph.
        assert snapshot_date("persons-with-significant-control-snapshot-2026-07-27.txt") \
            == "2026-07-27"

    def test_an_unexpected_entry_name_is_refused(self):
        with pytest.raises(ValueError, match="no snapshot date"):
            snapshot_date("psc.txt")

    def test_a_zip_must_hold_exactly_one_member(self, tmp_path):
        z = tmp_path / "two.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("a-2026-07-27.txt", "{}")
            zf.writestr("b-2026-07-27.txt", "{}")
        with pytest.raises(ValueError, match="expected one entry"):
            snapshot_entry(zipfile.ZipFile(z))

    def test_days_since_handles_an_unknown_date(self):
        assert days_since(None) == 1
        assert days_since("not-a-date") == 1


class TestDigestingASnapshot:
    def _zip(self, tmp_path, records, date_="2026-07-27"):
        z = tmp_path / "psc.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr(f"persons-with-significant-control-snapshot-{date_}.txt",
                        "\n".join(json.dumps(r) for r in records))
        return str(z)

    def test_writes_a_sorted_digest(self, tmp_path):
        recs = [record(data={"links": {"self": f"/link/{c}"}}) for c in "cab"]
        out = str(tmp_path / "out.tsv.gz")
        counts = write_digest(self._zip(tmp_path, recs), out)

        assert counts["records"] == 3 and counts["snapshot_date"] == "2026-07-27"
        with gzip.open(out, "rt") as fh:
            links = [ln.split("\t")[0] for ln in fh]
        # Byte order, which is what the merge walk assumes.
        assert links == ["/link/a", "/link/b", "/link/c"]

    def test_a_record_with_no_link_is_counted_not_dropped_silently(self, tmp_path):
        recs = [record(), {"company_number": "1", "data": {"kind": "x"}}]
        out = str(tmp_path / "out.tsv.gz")
        counts = write_digest(self._zip(tmp_path, recs), out)
        assert counts["records"] == 2 and counts["skipped"] == 1

    def test_the_same_snapshot_digested_twice_diffs_to_nothing(self, tmp_path):
        # The property the whole design rests on: re-running against an unchanged
        # snapshot is not merely idempotent, it is a no-op — the diff is empty, so
        # nothing is even attempted.
        z = self._zip(tmp_path, [record(), record(data={"links": {"self": "/link/b"}})])
        a, b = str(tmp_path / "a.gz"), str(tmp_path / "b.gz")
        write_digest(z, a)
        write_digest(z, b)
        assert diff_digests(a, b).total == 0


class TestTheBaselineIsRotatedNotOverwritten:
    def test_one_generation_is_kept(self, tmp_path):
        # A run applied against the wrong baseline should be re-derivable rather
        # than needing a full re-import of a 2.2 GB snapshot.
        from app.scraper.ch_psc_incremental import rotate_digest

        live = tmp_path / "psc-digest.tsv.gz"
        live.write_bytes(b"old")
        new = tmp_path / "new.tsv.gz"
        new.write_bytes(b"new")

        rotate_digest(str(new), str(live))

        assert live.read_bytes() == b"new"
        assert (tmp_path / "psc-digest.tsv.gz.prev").read_bytes() == b"old"
        assert not new.exists(), "the new digest should be moved into place, not copied"

    def test_the_first_rotation_needs_no_previous(self, tmp_path):
        from app.scraper.ch_psc_incremental import rotate_digest

        live = tmp_path / "psc-digest.tsv.gz"
        new = tmp_path / "new.tsv.gz"
        new.write_bytes(b"new")
        rotate_digest(str(new), str(live))
        assert live.read_bytes() == b"new"
