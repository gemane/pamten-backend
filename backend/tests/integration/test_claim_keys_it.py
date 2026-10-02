"""Claim keys follow their content — through a merge, a rename and the repair —
against a real ArcadeDB.

A claim's key hashes (kind, from, to, source, canonical role). Two paths wrote
keys that did not: the merge re-keyed ROLE claims without the role, and an id
rename re-pointed claims without re-keying them. On dev: 343 stale keys of
8,678 claims, 119 assertions stored twice.
"""
import pytest

pytestmark = pytest.mark.integration

SEC = "src-sec"


def _claims(it_db):
    rows = it_db.run_sql("SELECT FROM Claim")
    return sorted(({k: v for k, v in r.items() if not k.startswith("@")} for r in rows),
                  key=lambda c: (c["from_id"], c["to_id"], str(c.get("role"))))


def _record(**over):
    from app.claims import KIND_OWNS, record_claim
    record_claim(**{**dict(kind=KIND_OWNS, from_id="a", to_id="b", source_id=SEC), **over})


def _keys_match(it_db):
    from app.claims import key_for
    return all(key_for(c) == c["claim_key"] for c in _claims(it_db))


class TestTheMerge:
    def test_a_persons_roles_stay_separate_claims(self, it_db):
        from app.claims import KIND_ROLE, migrate_claims
        for role in ("CEO", "Director"):
            _record(kind=KIND_ROLE, from_id="p-dead", to_id="co", role=role)
        assert migrate_claims("p-dead", "p-keep") == 2
        got = _claims(it_db)
        assert [(c["from_id"], c["role"]) for c in got] == [("p-keep", "CEO"), ("p-keep", "Director")]
        assert _keys_match(it_db)
        # the next scrape updates these two; it does not write two more beside them
        for role in ("CEO", "Director"):
            _record(kind=KIND_ROLE, from_id="p-keep", to_id="co", role=role)
        assert len(_claims(it_db)) == 2

    def test_role_synonyms_are_one_claim(self, it_db):
        """"Director" and "Board Member" are one seat (the canonical role keys it)."""
        from app.claims import KIND_ROLE, migrate_claims
        _record(kind=KIND_ROLE, from_id="p-dead", to_id="co", role="Director")
        _record(kind=KIND_ROLE, from_id="p-keep", to_id="co", role="Board Member")
        migrate_claims("p-dead", "p-keep")
        assert len(_claims(it_db)) == 1

    def test_a_moved_claim_keeps_everything_it_had(self, it_db):
        from app.claims import migrate_claims
        _record(from_id="dead", to_id="target", stake_percent=8.05, filing_type="13G",
                source_url="https://sec.example.test/x", source_date="2026-02-01")
        before = _claims(it_db)[0]
        assert before["first_seen_at"]
        migrate_claims("dead", "keep")
        after = _claims(it_db)[0]
        assert after["from_id"] == "keep" and after["claim_key"] != before["claim_key"]
        for k in before:
            if k not in ("from_id", "claim_key"):
                assert after.get(k) == before[k], k            # first_seen_at included

    def test_folding_keeps_the_claim_seen_last_and_the_earliest_first_sight(self, it_db):
        from app.claims import claim_key, migrate_claims
        for node, stake, first, last in (("dead", 9.0, "2026-01-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
                                         ("keep", 5.0, "2026-03-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00")):
            it_db.run_sql("INSERT INTO Claim SET claim_key = :k, kind = 'owns', from_id = :f, to_id = 'target', "
                          "source_id = :s, stake_percent = :st, first_seen_at = :fs, last_seen_at = :ls",
                          {"k": claim_key("owns", node, "target", SEC), "f": node, "s": SEC,
                           "st": stake, "fs": first, "ls": last})
        migrate_claims("dead", "keep")
        [c] = _claims(it_db)
        assert (c["from_id"], c["stake_percent"]) == ("keep", 9.0)        # the dead node's was seen last
        assert c["first_seen_at"] == "2026-01-01T00:00:00+00:00"
        assert c["last_seen_at"] == "2026-09-01T00:00:00+00:00"
        assert _keys_match(it_db)

    def test_folding_carries_a_listing_date_the_kept_claim_lacks(self, it_db):
        from app.claims import claim_key, migrate_claims
        it_db.run_sql("INSERT INTO Claim SET claim_key = :k, kind = 'owns', from_id = 'nc', to_id = 'dead', "
                      "source_id = :s, since = '2013-06-30', since_basis = 'first_listed', "
                      "since_source_url = 'u', last_seen_at = '2026-01-01T00:00:00+00:00'",
                      {"k": claim_key("owns", "nc", "dead", SEC), "s": SEC})
        it_db.run_sql("INSERT INTO Claim SET claim_key = :k, kind = 'owns', from_id = 'nc', to_id = 'keep', "
                      "source_id = :s, last_seen_at = '2026-09-01T00:00:00+00:00'",
                      {"k": claim_key("owns", "nc", "keep", SEC), "s": SEC})
        migrate_claims("dead", "keep")
        [c] = _claims(it_db)
        assert (c["since"], c["since_basis"], c["since_source_url"]) == ("2013-06-30", "first_listed", "u")


class TestTheRename:
    def test_a_renamed_nodes_claims_are_rekeyed(self, it_db):
        from app.database import db
        from app.merged_ids import rename_node_id
        it_db.run_command("CREATE (:Entity {id:'gb-coh:4680292', name:'News Corp Holdings UK & Ireland'})")
        _record(from_id="nc", to_id="gb-coh:4680292", filing_type="EX-21")
        with db.get_session() as session:
            assert rename_node_id(session, "Entity", "gb-coh:4680292", "gb-coh:04680292") is True
        [c] = _claims(it_db)
        assert c["to_id"] == "gb-coh:04680292" and _keys_match(it_db)
        # the next scrape finds its claim instead of writing a second one (the dev duplicate)
        _record(from_id="nc", to_id="gb-coh:04680292", filing_type="EX-21")
        assert len(_claims(it_db)) == 1


class TestTheRepair:
    def _seed_the_damage(self, it_db):
        from app.claims import KIND_ROLE, claim_key
        # (1) the old merge: a role claim keyed WITHOUT its role, and the correctly
        #     keyed one the next scrape wrote beside it
        it_db.run_sql("INSERT INTO Claim SET claim_key = :k, kind = 'role', from_id = 'p', to_id = 'co', "
                      "source_id = :s, role = 'CEO', first_seen_at = '2026-01-01T00:00:00+00:00', "
                      "last_seen_at = '2026-02-01T00:00:00+00:00'",
                      {"k": claim_key("role", "p", "co", SEC), "s": SEC})
        _record(kind=KIND_ROLE, from_id="p", to_id="co", role="CEO")
        # (2) the old rename: re-pointed, still keyed for the old id, no twin
        it_db.run_sql("INSERT INTO Claim SET claim_key = :k, kind = 'owns', from_id = 'nc', "
                      "to_id = 'gb-coh:04680292', source_id = :s, stake_percent = 100.0",
                      {"k": claim_key("owns", "nc", "gb-coh:4680292", SEC), "s": SEC})
        # (3) a healthy claim
        _record(from_id="x", to_id="y")

    def test_dry_run_counts_and_changes_nothing(self, it_db):
        from app.claims import heal_claim_keys
        self._seed_the_damage(it_db)
        before = _claims(it_db)
        assert heal_claim_keys(dry_run=True) == {"claims": 4, "stale": 2, "moved": 0, "folded": 0}
        assert _claims(it_db) == before

    def test_it_rekeys_in_place_and_folds_the_doubles(self, it_db):
        from app.claims import heal_claim_keys
        self._seed_the_damage(it_db)
        res = heal_claim_keys(page=2)                       # several pages
        assert (res["stale"], res["moved"], res["folded"]) == (2, 1, 1)
        got = _claims(it_db)
        assert len(got) == 3 and _keys_match(it_db)
        role = next(c for c in got if c["kind"] == "role")
        assert role["first_seen_at"] == "2026-01-01T00:00:00+00:00"       # the earlier sight survives the fold
        assert next(c for c in got if c["to_id"] == "gb-coh:04680292")["stake_percent"] == 100.0
        # a second run finds nothing left
        again = heal_claim_keys()
        assert (again["claims"], again["stale"]) == (3, 0)


def test_a_claim_without_a_source_does_not_break_a_merge(it_db):
    """Such a row should not exist (record_claim refuses it), but a merge must not die on one."""
    from app.claims import migrate_claims
    it_db.run_sql("INSERT INTO Claim SET claim_key = 'legacy', kind = 'owns', from_id = 'dead', to_id = 'x'")
    assert migrate_claims("dead", "keep") == 1
    [c] = _claims(it_db)
    assert c["from_id"] == "keep" and c["claim_key"] != "legacy"
