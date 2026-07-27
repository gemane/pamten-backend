"""
Tests for the BODS (Beneficial Ownership Data Standard) importer.

All DB writes are mocked — these tests validate field-mapping logic only.
The importer buffers writes in a ``_BatchWriter`` and flushes them as batched
``sqlscript`` requests, so the ``_process_*`` functions take a ``batch`` and call
the ``_entity``/``_person``/``_owns``/``_role`` enqueue helpers; the tests patch
those helpers to capture what would be written.
"""

import pytest
from unittest.mock import patch, MagicMock

from app.scraper.bods import _bods_record_url


# ── Provenance record-URL helper (pure) ───────────────────────────────────────

class TestBodsRecordUrl:
    def test_gleif_ref_builds_gleif_record_url(self):
        ref = "XI-LEI-529900T8BM49AURSDO55"
        assert _bods_record_url(ref, {}) == \
            "https://search.gleif.org/#/record/529900T8BM49AURSDO55"

    def test_falls_back_to_statement_source_url(self):
        stmt = {"source": {"type": ["officialRegister"], "url": "https://find-and-update.company-information.service.gov.uk/company/01234567"}}
        assert _bods_record_url("some-uk-psc-ref", stmt) == \
            "https://find-and-update.company-information.service.gov.uk/company/01234567"

    def test_none_when_no_lei_and_no_source_url(self):
        assert _bods_record_url("some-ref", {}) is None
        assert _bods_record_url(None, {}) is None


# ── Fixtures: minimal valid BODS statements ───────────────────────────────────

ENTITY_STMT = {
    "recordId":     "entity-001",
    "recordType":   "entity",
    "recordStatus": "new",
    "statementDate": "2021-02-09",
    "recordDetails": {
        "name": "AstraZeneca PLC",
        "entityType": {"type": "registeredEntity"},
        "foundingDate": "1992-06-17",
        "jurisdiction": {"code": "GB", "name": "United Kingdom"},
        "identifiers": [
            {"scheme": "GB-COH", "id": "02723534"},
            {"scheme": "XI-LEI", "id": "PY6ZZQWO2IZFZC3IOL08"},
        ],
    },
}

PERSON_STMT = {
    "recordId":     "person-001",
    "recordType":   "person",
    "recordStatus": "new",
    "recordDetails": {
        "personType": "knownPerson",
        "names": [
            {
                "type": "legal",
                "fullName": "Jennifer Hewitson-Smith",
                "givenName": "Jennifer",
                "familyName": "Hewitson-Smith",
            }
        ],
        "birthDate": "1978-07",
        "nationalities": [{"code": "GB"}],
    },
}

ANON_PERSON_STMT = {
    "recordId":     "person-anon",
    "recordType":   "person",
    "recordStatus": "new",
    "recordDetails": {
        "personType": "anonymousPerson",
    },
}

RELATIONSHIP_STMT = {
    "recordId":     "rel-001",
    "recordType":   "relationship",
    "recordStatus": "new",
    "recordDetails": {
        "subject":          "entity-001",
        "interestedParty":  "entity-002",
        "interests": [
            {
                "type": "shareholding",
                "startDate": "2016-04-06",
                "endDate": None,
                "share": {"exact": 60.5, "minimum": None, "maximum": None},
            }
        ],
    },
}

RELATIONSHIP_MULTI_INTEREST = {
    "recordId":     "rel-multi",
    "recordType":   "relationship",
    "recordStatus": "new",
    "recordDetails": {
        "subject":          "entity-001",
        "interestedParty":  "entity-002",
        # Two interests for the SAME pair that both map to "controlling"
        # (GLEIF direct+ultimate consolidation) — must NOT become two OWNS edges.
        "interests": [
            {"type": "votingRights", "startDate": "2020-01-01"},
            {"type": "appointmentOfBoard", "startDate": "2020-01-01"},
        ],
    },
}

CLOSED_RELATIONSHIP_STMT = {
    "recordId":     "rel-002",
    "recordType":   "relationship",
    "recordStatus": "closed",
    "statementDate": "2023-05-01",
    "recordDetails": {
        "subject":          "entity-001",
        "interestedParty":  "entity-002",
        "interests": [
            {
                "type": "shareholding",
                "startDate": "2010-01-01",
                "endDate": None,
                "share": {"exact": 30.0},
            }
        ],
    },
}

RELATIONSHIP_NO_EXACT = {
    "recordId":     "rel-003",
    "recordType":   "relationship",
    "recordStatus": "new",
    "recordDetails": {
        "subject":          "entity-001",
        "interestedParty":  "entity-002",
        "interests": [
            {
                "type": "shareholding",
                "share": {"exact": None, "minimum": 25.0, "maximum": 35.0},
            }
        ],
    },
}

RELATIONSHIP_UNKNOWN_INTEREST = {
    "recordId":     "rel-004",
    "recordType":   "relationship",
    "recordStatus": "new",
    "recordDetails": {
        "subject":          "entity-001",
        "interestedParty":  "entity-002",
        "interests": [{"type": "someNewUnknownType", "share": {}}],
    },
}

RELATIONSHIP_ROLE = {
    "recordId":     "rel-005",
    "recordType":   "relationship",
    "recordStatus": "new",
    "recordDetails": {
        "subject":          "entity-001",
        "interestedParty":  "person-001",
        "interests": [{"type": "seniorManagingOfficial", "startDate": "2020-01-01"}],
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _capturing_node(store, return_id):
    """A fake _entity/_person: capture kwargs (+node_id) and return a fixed id."""
    def fake(batch, node_id, **kwargs):
        store.update(kwargs)
        store["node_id"] = node_id
        return return_id
    return fake


def _capturing_edge(store):
    """A fake _owns/_role: capture the kwargs it was enqueued with."""
    def fake(batch, **kwargs):
        store.update(kwargs)
    return fake


# ── _entity_node_id: stable identity ──────────────────────────────────────────

class TestEntityNodeId:
    def test_prefers_lei(self):
        from app.scraper.bods import _entity_node_id
        assert _entity_node_id("LEI123", "COH9", "rec-1") == "lei:LEI123"

    def test_falls_back_to_companies_house(self):
        from app.scraper.bods import _entity_node_id
        assert _entity_node_id(None, "COH9", "rec-1") == "gb-coh:COH9"

    def test_falls_back_to_record_id_when_no_external_id(self):
        from app.scraper.bods import _entity_node_id
        assert _entity_node_id(None, None, "rec-1") == "rec-1"

    def test_same_lei_different_recordid_yields_same_id(self):
        # The whole point: two dumps, same company, different recordId → one id.
        from app.scraper.bods import _entity_node_id
        assert _entity_node_id("LEI-X", None, "AT-001") == _entity_node_id("LEI-X", None, "GLEIF-999")

    def test_id_less_entity_keys_on_normalized_name(self):
        from app.scraper.bods import _entity_node_id
        from app.scraper.mapper import normalize_entity_name
        name = "Government Of The Emirate Of Abu Dhabi"
        assert _entity_node_id(None, None, "rec-1", name=name) == f"name:{normalize_entity_name(name)}"

    def test_id_less_same_name_different_recordid_collapses(self):
        # The UK PSC fix: one interested party re-declared per controlled company
        # (different recordId each time) must resolve to a single node.
        from app.scraper.bods import _entity_node_id
        a = _entity_node_id(None, None, "GB-COH-ENT-A", name="Government Of The Emirate Of Abu Dhabi")
        b = _entity_node_id(None, None, "GB-COH-ENT-B", name="Government Of The Emirate Of Abu Dhabi")
        assert a == b

    def test_name_variant_stays_separate(self):
        # A meaningful variant normalizes differently and must NOT collapse.
        from app.scraper.bods import _entity_node_id
        base = _entity_node_id(None, None, "r1", name="Government Of Abu Dhabi")
        thru = _entity_node_id(None, None, "r2", name="Government Of Abu Dhabi (Through Mubadala)")
        assert base != thru

    def test_hard_id_wins_over_name(self):
        from app.scraper.bods import _entity_node_id
        assert _entity_node_id("LEI-X", None, "r1", name="Anything") == "lei:LEI-X"
        assert _entity_node_id(None, "COH9", "r1", name="Anything") == "gb-coh:COH9"

    def test_falls_back_to_record_id_when_no_id_and_no_name(self):
        from app.scraper.bods import _entity_node_id
        assert _entity_node_id(None, None, "rec-1", name="") == "rec-1"
        assert _entity_node_id(None, None, "rec-1", name=None) == "rec-1"


# ── _DiskMap temp location (avoids filling a small tmpfs /tmp) ─────────────────

class TestDiskMapTmpDir:
    def test_uses_configured_tmp_dir(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.scraper.bods import _DiskMap
        monkeypatch.setattr(settings, "SCRAPER_TMP_DIR", str(tmp_path))
        m = _DiskMap()
        try:
            assert m._path.startswith(str(tmp_path))   # spilled to the big disk, not /tmp
            m["k"] = "v"
            assert m["k"] == "v" and "k" in m
        finally:
            m.close()

    def test_creates_the_dir_if_missing(self, tmp_path, monkeypatch):
        from app.config import settings
        from app.scraper.bods import _tmp_dir
        target = tmp_path / "does" / "not" / "exist"
        monkeypatch.setattr(settings, "SCRAPER_TMP_DIR", str(target))
        assert _tmp_dir() == str(target)
        assert target.is_dir()


# ── _legal_form_type (GLEIF legal form → finer category) ──────────────────────

class TestLegalFormType:
    def test_foundation_forms(self):
        from app.scraper.bods import _legal_form_type
        assert _legal_form_type("Stiftung des privaten Rechts") == "foundation"
        assert _legal_form_type("stichting") == "foundation"
        assert _legal_form_type("Fundación") == "foundation"

    def test_fund_forms(self):
        from app.scraper.bods import _legal_form_type
        assert _legal_form_type("Mutual Fund-Sub Scheme") == "fund"
        assert _legal_form_type("Fonds à forme sociétale") == "fund"
        assert _legal_form_type("Statutory Trust") == "fund"

    def test_nonprofit_forms(self):
        from app.scraper.bods import _legal_form_type
        assert _legal_form_type("eingetragener Verein") == "nonprofit"
        assert _legal_form_type("Association loi 1901") == "nonprofit"

    def test_plain_company_form_is_none(self):
        from app.scraper.bods import _legal_form_type
        assert _legal_form_type("Gesellschaft mit beschränkter Haftung") is None
        assert _legal_form_type("Private Limited Company") is None
        assert _legal_form_type(None) is None


class TestProcessEntityStatementCategory:
    def test_legal_form_refines_registered_entity_to_foundation(self):
        from app.scraper.bods import _process_entity_statement
        captured: dict = {}
        stmt = {**ENTITY_STMT, "recordDetails": {
            **ENTITY_STMT["recordDetails"],
            "entityType": {"type": "registeredEntity", "details": "Stiftung"}}}
        with patch("app.scraper.bods._entity", side_effect=_capturing_node(captured, "eid-1")):
            _process_entity_statement(stmt, {}, MagicMock(), "src-1", 92, None)
        assert captured["entity_type"] == "foundation"

    def test_plain_legal_form_stays_company(self):
        from app.scraper.bods import _process_entity_statement
        captured: dict = {}
        stmt = {**ENTITY_STMT, "recordDetails": {
            **ENTITY_STMT["recordDetails"],
            "entityType": {"type": "registeredEntity", "details": "Private Limited Company"}}}
        with patch("app.scraper.bods._entity", side_effect=_capturing_node(captured, "eid-1")):
            _process_entity_statement(stmt, {}, MagicMock(), "src-1", 92, None)
        assert captured["entity_type"] == "company"


# ── source_statement_ids: per-statement provenance survives the name collapse ──

class TestSourceStatementIds:
    def _id_less_stmt(self, record_id, name="Government Of The Emirate Of Abu Dhabi"):
        return {
            "recordId": record_id, "recordType": "entity", "recordStatus": "new",
            "recordDetails": {"name": name, "entityType": {"type": "registeredEntity"},
                              "identifiers": []},
        }

    def test_hard_id_entity_lists_its_own_statement(self):
        from app.scraper.bods import _process_entity_statement
        captured: dict = {}
        with patch("app.scraper.bods._entity", side_effect=_capturing_node(captured, "eid-1")):
            _process_entity_statement(ENTITY_STMT, {}, MagicMock(), "src-1", 92, None)
        assert captured["source_statement_ids"] == ["entity-001"]

    def test_id_less_collapse_accumulates_all_statement_ids(self):
        # 3 filings re-declare the same id-less party → one node, all 3 ids listed.
        from app.scraper.bods import _process_entity_statement, _DiskMap
        stmt_map = _DiskMap()
        try:
            node_ids, last = set(), {}
            for rid in ("GB-COH-ENT-A", "GB-COH-ENT-B", "GB-COH-ENT-C"):
                captured: dict = {}
                with patch("app.scraper.bods._entity",
                           side_effect=_capturing_node(captured, "eid")):
                    _process_entity_statement(self._id_less_stmt(rid), {}, MagicMock(),
                                              "src-1", 92, None, stmt_map)
                node_ids.add(captured["node_id"])
                last = captured
            assert len(node_ids) == 1                        # collapsed to one node
            assert last["source_statement_ids"] == ["GB-COH-ENT-A", "GB-COH-ENT-B", "GB-COH-ENT-C"]
        finally:
            stmt_map.close()

    def test_reprocessing_same_statement_is_idempotent(self):
        from app.scraper.bods import _process_entity_statement, _DiskMap
        stmt_map = _DiskMap()
        try:
            captured: dict = {}
            for _ in range(2):
                captured = {}
                with patch("app.scraper.bods._entity",
                           side_effect=_capturing_node(captured, "eid")):
                    _process_entity_statement(self._id_less_stmt("GB-COH-ENT-A"), {}, MagicMock(),
                                              "src-1", 92, None, stmt_map)
            assert captured["source_statement_ids"] == ["GB-COH-ENT-A"]
        finally:
            stmt_map.close()


# ── _registered_address ───────────────────────────────────────────────────────

class TestRegisteredAddress:
    def test_prefers_registered_over_business_and_normalizes(self):
        from app.scraper.bods import _registered_address
        details = {"addresses": [
            {"type": "business", "address": "Biz St 1, Town", "country": {"code": "GB"}, "postCode": "B1"},
            {"type": "registered", "address": "Reg Rd. 2, City", "country": {"code": "GB"}, "postCode": "R2 3XY"},
        ]}
        assert _registered_address(details) == "reg rd 2 city r2 3xy gb"

    def test_falls_back_to_business_then_first(self):
        from app.scraper.bods import _registered_address
        assert _registered_address(
            {"addresses": [{"type": "business", "address": "Only Biz", "country": {"code": "US"}}]}
        ) == "only biz us"

    def test_none_when_no_addresses(self):
        from app.scraper.bods import _registered_address
        assert _registered_address({}) is None
        assert _registered_address({"addresses": []}) is None


# ── _process_entity_statement ─────────────────────────────────────────────────

class TestProcessEntityStatement:
    def test_captures_registered_address(self):
        from app.scraper.bods import _process_entity_statement
        captured: dict = {}
        stmt = {**ENTITY_STMT, "recordDetails": {
            **ENTITY_STMT["recordDetails"],
            "addresses": [{"type": "registered", "address": "1 King St, London",
                           "country": {"code": "GB"}, "postCode": "SW1"}]}}
        with patch("app.scraper.bods._entity", side_effect=_capturing_node(captured, "eid-1")):
            _process_entity_statement(stmt, {}, MagicMock(), "src-1", 92, None)
        assert captured["registered_address"] == "1 king st london sw1 gb"

    def test_maps_fields_correctly(self):
        from app.scraper.bods import _process_entity_statement

        captured: dict = {}
        bods_map: dict = {}
        with patch("app.scraper.bods._entity", side_effect=_capturing_node(captured, "eid-1")):
            result = _process_entity_statement(
                ENTITY_STMT, bods_map, MagicMock(), "src-1", 92, None
            )

        assert result == "eid-1"
        assert bods_map["entity-001"] == "eid-1"
        # Node is keyed on the LEI (stable across dumps), NOT the per-dump recordId —
        # this is what stops re-imports from duplicating the same company.
        assert captured["node_id"] == "lei:PY6ZZQWO2IZFZC3IOL08"
        assert captured["name"] == "AstraZeneca PLC"
        assert captured["entity_type"] == "company"
        assert captured["country"] == "GB"  # canonical ISO-2, matching the Wikidata scraper
        assert captured["founded"] == 1992
        assert captured["lei_id"] == "PY6ZZQWO2IZFZC3IOL08"
        assert captured["companies_house_id"] == "02723534"

    def test_entity_type_mapping(self):
        from app.scraper.bods import _process_entity_statement

        stmt = {
            **ENTITY_STMT,
            "recordDetails": {
                **ENTITY_STMT["recordDetails"],
                "entityType": {"type": "arrangement"},
            },
        }
        captured: dict = {}
        with patch("app.scraper.bods._entity", side_effect=_capturing_node(captured, "eid-2")):
            _process_entity_statement(stmt, {}, MagicMock(), "src-1", 92, None)

        assert captured["entity_type"] == "holding"

    def test_filter_jurisdiction_skips_non_matching(self):
        from app.scraper.bods import _process_entity_statement

        bods_map: dict = {}
        with patch("app.scraper.bods._entity") as mock_entity:
            result = _process_entity_statement(
                ENTITY_STMT, bods_map, MagicMock(), "src-1", 92, "DE"
            )

        assert result is None
        assert "entity-001" not in bods_map
        mock_entity.assert_not_called()

    def test_filter_jurisdiction_accepts_matching(self):
        from app.scraper.bods import _process_entity_statement

        with patch("app.scraper.bods._entity", return_value="eid-gb"):
            result = _process_entity_statement(
                ENTITY_STMT, {}, MagicMock(), "src-1", 92, "GB"
            )

        assert result == "eid-gb"

    def test_missing_name_returns_none(self):
        from app.scraper.bods import _process_entity_statement

        stmt = {**ENTITY_STMT, "recordDetails": {"name": ""}}
        with patch("app.scraper.bods._entity") as mock_entity:
            result = _process_entity_statement(stmt, {}, MagicMock(), "src-1", 92, None)

        assert result is None
        mock_entity.assert_not_called()

    def test_partial_founding_date(self):
        from app.scraper.bods import _process_entity_statement

        stmt = {
            **ENTITY_STMT,
            "recordDetails": {
                **ENTITY_STMT["recordDetails"],
                "foundingDate": "2005",
            },
        }
        captured: dict = {}
        with patch("app.scraper.bods._entity", side_effect=_capturing_node(captured, "eid")):
            _process_entity_statement(stmt, {}, MagicMock(), "src-1", 92, None)

        assert captured["founded"] == 2005


# ── _process_person_statement ─────────────────────────────────────────────────

class TestProcessPersonStatement:
    def test_maps_fields_correctly(self):
        from app.scraper.bods import _process_person_statement

        captured: dict = {}
        bods_map: dict = {}
        with patch("app.scraper.bods._person", side_effect=_capturing_node(captured, "pid-1")):
            result = _process_person_statement(PERSON_STMT, bods_map, MagicMock(), "src-1", 97)

        assert result == "pid-1"
        assert bods_map["person-001"] == "pid-1"
        assert captured["node_id"] == "person-001"
        assert captured["full_name"] == "Jennifer Hewitson-Smith"
        assert captured["first_name"] == "Jennifer"
        assert captured["last_name"] == "Hewitson-Smith"
        assert captured["nationality"] == "GB"

    def test_partial_birth_date_stored_as_is(self):
        from app.scraper.bods import _process_person_statement

        captured: dict = {}
        with patch("app.scraper.bods._person", side_effect=_capturing_node(captured, "pid")):
            _process_person_statement(PERSON_STMT, {}, MagicMock(), "src-1", 97)

        assert captured["birth_date"] == "1978-07"

    def test_anonymous_person_skipped(self):
        from app.scraper.bods import _process_person_statement

        bods_map: dict = {}
        with patch("app.scraper.bods._person") as mock_person:
            result = _process_person_statement(ANON_PERSON_STMT, bods_map, MagicMock(), "src-1", 97)

        assert result is None
        assert "person-anon" not in bods_map
        mock_person.assert_not_called()


class TestSharePct:
    def test_prefers_exact_then_minimum(self):
        from app.scraper.bods import _share_pct
        assert _share_pct({"exact": 12.5, "minimum": 10}) == 12.5
        assert _share_pct({"exact": None, "minimum": 25, "maximum": 50}) == 25.0   # band → floor
        assert _share_pct({}) is None
        assert _share_pct(None) is None


# ── _process_relationship_statement ──────────────────────────────────────────

class TestProcessRelationshipStatement:
    def _bods_map(self):
        return {"entity-001": "eid-1", "entity-002": "eid-2", "person-001": "pid-1"}

    def _name_map(self):
        return {}  # empty — tests don't need real names

    def test_shareholding_maps_correctly(self):
        from app.scraper.bods import _process_relationship_statement

        captured: dict = {}
        bods_map = self._bods_map()
        with patch("app.scraper.bods._owns", side_effect=_capturing_edge(captured)):
            edges = _process_relationship_statement(
                RELATIONSHIP_STMT, bods_map, self._name_map(), MagicMock(), "src-1", 97
            )

        assert edges == 1
        assert captured["stake_percent"] == 60.5
        assert captured["ownership_type"] == "majority"
        assert captured["since"] == "2016-04-06"
        assert captured["until"] is None

    def test_multiple_interests_same_type_emit_one_edge(self):
        from app.scraper.bods import _process_relationship_statement

        calls: list = []

        def capture(batch, **kw):
            calls.append(kw)

        with patch("app.scraper.bods._owns", side_effect=capture):
            edges = _process_relationship_statement(
                RELATIONSHIP_MULTI_INTEREST, self._bods_map(), self._name_map(),
                MagicMock(), "src-1", 97,
            )

        # Both interests → "controlling" for the same pair → a single OWNS edge.
        assert edges == 1
        assert len(calls) == 1
        assert calls[0]["ownership_type"] == "controlling"

    def test_closed_record_sets_until(self):
        from app.scraper.bods import _process_relationship_statement

        captured: dict = {}
        with patch("app.scraper.bods._owns", side_effect=_capturing_edge(captured)):
            _process_relationship_statement(
                CLOSED_RELATIONSHIP_STMT, self._bods_map(), self._name_map(), MagicMock(), "src-1", 97
            )

        assert captured["until"] == "2023-05-01"

    def test_missing_share_exact_falls_back_to_minimum(self):
        from app.scraper.bods import _process_relationship_statement

        captured: dict = {}
        with patch("app.scraper.bods._owns", side_effect=_capturing_edge(captured)):
            _process_relationship_statement(
                RELATIONSHIP_NO_EXACT, self._bods_map(), self._name_map(), MagicMock(), "src-1", 97
            )

        assert captured["stake_percent"] == 25.0

    def test_unknown_interest_type_maps_to_minority(self):
        from app.scraper.bods import _process_relationship_statement

        captured: dict = {}
        with patch("app.scraper.bods._owns", side_effect=_capturing_edge(captured)), \
             patch("app.scraper.bods._entity", return_value="eid-new"):
            _process_relationship_statement(
                RELATIONSHIP_UNKNOWN_INTEREST, self._bods_map(), self._name_map(), MagicMock(), "src-1", 97
            )

        assert captured["ownership_type"] == "minority"

    def test_senior_managing_official_creates_role_edge(self):
        from app.scraper.bods import _process_relationship_statement

        with patch("app.scraper.bods._role") as mock_role, \
             patch("app.scraper.bods._owns") as mock_owns:
            edges = _process_relationship_statement(
                RELATIONSHIP_ROLE, self._bods_map(), self._name_map(), MagicMock(), "src-1", 97
            )

        assert edges == 1
        mock_role.assert_called_once()
        mock_owns.assert_not_called()

    def test_voting_rights_populate_voting_not_stake(self):
        # A voting-rights band (UK PSC: 75–100%) → voting_power_pct, NOT the
        # economic stake_percent (which must stay unset without a shareholding).
        from app.scraper.bods import _process_relationship_statement
        stmt = {**RELATIONSHIP_STMT, "recordDetails": {
            **RELATIONSHIP_STMT["recordDetails"],
            "interests": [{"type": "votingRights", "share": {"minimum": 75, "maximum": 100}}]}}
        captured: dict = {}
        with patch("app.scraper.bods._owns", side_effect=_capturing_edge(captured)):
            _process_relationship_statement(stmt, self._bods_map(), self._name_map(),
                                            MagicMock(), "src-1", 97)
        assert captured["voting_power_pct"] == 75.0
        assert captured["stake_percent"] is None
        assert captured["ownership_type"] == "controlling"
        assert captured["interest_types"] == ["votingRights"]

    def test_shareholding_and_voting_kept_separate_on_one_edge(self):
        # Economic 10% + voting 75% in one statement → one edge carrying both,
        # typed "controlling" (voting control), interest types recorded.
        from app.scraper.bods import _process_relationship_statement
        calls: list = []
        stmt = {**RELATIONSHIP_STMT, "recordDetails": {
            **RELATIONSHIP_STMT["recordDetails"],
            "interests": [
                {"type": "shareholding", "share": {"exact": 10.0}},
                {"type": "votingRights", "share": {"minimum": 75}},
            ]}}
        with patch("app.scraper.bods._owns", side_effect=lambda batch, **kw: calls.append(kw)):
            edges = _process_relationship_statement(stmt, self._bods_map(), self._name_map(),
                                                    MagicMock(), "src-1", 97)
        assert edges == 1 and len(calls) == 1
        assert calls[0]["stake_percent"] == 10.0
        assert calls[0]["voting_power_pct"] == 75.0
        assert calls[0]["ownership_type"] == "controlling"
        assert calls[0]["interest_types"] == ["shareholding", "votingRights"]

    def test_bods_to_pamten_id_lookup_resolves_correctly(self):
        from app.scraper.bods import _process_relationship_statement

        captured: dict = {}
        bods_map = {"entity-001": "OWNED-UUID", "entity-002": "OWNER-UUID"}
        with patch("app.scraper.bods._owns", side_effect=_capturing_edge(captured)):
            _process_relationship_statement(
                RELATIONSHIP_STMT, bods_map, self._name_map(), MagicMock(), "src-1", 97
            )

        assert captured["owned_id"] == "OWNED-UUID"
        assert captured["owner_id"] == "OWNER-UUID"

    def test_shareholding_below_20_maps_to_minority(self):
        # Thresholds: >50% majority, 20-50% controlling, <20% minority
        from app.scraper.bods import _process_relationship_statement

        stmt = {
            "recordId":     "rel-minority",
            "recordType":   "relationship",
            "recordStatus": "new",
            "recordDetails": {
                "subject":         "entity-001",
                "interestedParty": "entity-002",
                "interests": [
                    {
                        "type": "shareholding",
                        "share": {"exact": 8.5},
                    }
                ],
            },
        }
        captured: dict = {}
        with patch("app.scraper.bods._owns", side_effect=_capturing_edge(captured)):
            _process_relationship_statement(stmt, self._bods_map(), self._name_map(), MagicMock(), "src-1", 97)

        assert captured["stake_percent"] == 8.5
        assert captured["ownership_type"] == "minority"

    def test_unresolved_party_creates_placeholder_and_writes_edge(self):
        # When interestedParty is not in bods_id_map, the importer creates a
        # placeholder Entity rather than skipping, so the edge is preserved.
        from app.scraper.bods import _process_relationship_statement

        bods_map = {"entity-001": "eid-1"}   # entity-002 intentionally absent
        owns_calls: list = []

        with patch("app.scraper.bods._entity", return_value="placeholder-id") as mock_e, \
             patch("app.scraper.bods._owns",
                   side_effect=lambda batch, **kw: owns_calls.append(kw)):
            edges = _process_relationship_statement(
                RELATIONSHIP_STMT, bods_map, self._name_map(), MagicMock(), "src-1", 97
            )

        # A placeholder was created for the unknown party
        mock_e.assert_called_once()
        assert bods_map["entity-002"] == "placeholder-id"
        # The edge was still written
        assert edges == 1
        assert owns_calls[0]["owner_id"] == "placeholder-id"


# ── _run_import: filter_jurisdiction and limit ────────────────────────────────

class TestRunImport:
    def _make_statements(self):
        return [
            ENTITY_STMT,
            {**ENTITY_STMT, "recordId": "entity-DE",
             "recordDetails": {**ENTITY_STMT["recordDetails"],
                               "jurisdiction": {"code": "DE"}}},
            PERSON_STMT,
            RELATIONSHIP_STMT,
        ]

    def test_filter_jurisdiction_skips_non_matching_entities(self):
        from app.scraper.bods import _run_import

        with patch("app.scraper.bods._entity", return_value="eid"), \
             patch("app.scraper.bods._person", return_value="pid"), \
             patch("app.scraper.bods._owns"), \
             patch("app.scraper.bods._role"):
            counts = _run_import(
                iter(self._make_statements()),
                source_id="src-1",
                credibility_score=92,
                limit=None,
                filter_jurisdiction="GB",
            )

        assert counts["entities"] == 1   # only GB entity passes
        assert counts["skipped"] >= 1    # DE entity is skipped

    def test_limit_stops_processing(self):
        from app.scraper.bods import _run_import

        many_stmts = [
            {**ENTITY_STMT, "recordId": f"ent-{i}",
             "recordDetails": {**ENTITY_STMT["recordDetails"], "name": f"Co {i}"}}
            for i in range(20)
        ]

        with patch("app.scraper.bods._entity", return_value="eid"), \
             patch("app.scraper.bods._person", return_value="pid"), \
             patch("app.scraper.bods._owns"), \
             patch("app.scraper.bods._role"):
            counts = _run_import(
                iter(many_stmts),
                source_id="src-1",
                credibility_score=92,
                limit=5,
                filter_jurisdiction=None,
            )

        assert counts["entities"] == 5


# ── _BatchWriter: batching and flush semantics ───────────────────────────────

class TestBatchWriter:
    def test_flushes_when_batch_size_reached(self):
        from app.scraper.bods import _BatchWriter

        with patch("app.scraper.bods.run_sqlscript") as mock_sql:
            b = _BatchWriter(batch_size=2)
            b.entity("e1", {"name": "A", "country": "GB"})
            mock_sql.assert_not_called()          # under the threshold
            b.entity("e2", {"name": "B", "country": "US"})
            assert mock_sql.called                 # threshold reached → auto-flush

    def test_nodes_flushed_before_edges(self):
        from app.scraper.bods import _BatchWriter

        scripts: list = []
        with patch("app.scraper.bods.run_sqlscript",
                   side_effect=lambda script, params=None: scripts.append(script)):
            b = _BatchWriter(batch_size=100)
            b.owns("e1", "Entity", "e2", {"stake_percent": 50.0})
            b.entity("e1", {"name": "A"})
            b.flush()

        # Entity upsert must be issued before the edge CREATE, so endpoints exist.
        joined = "\n---\n".join(scripts)
        assert "UPDATE Entity" in joined
        assert "CREATE EDGE OWNS" in joined
        assert joined.index("UPDATE Entity") < joined.index("CREATE EDGE OWNS")

    def test_empty_flush_issues_no_request(self):
        from app.scraper.bods import _BatchWriter

        with patch("app.scraper.bods.run_sqlscript") as mock_sql:
            _BatchWriter().flush()
            mock_sql.assert_not_called()


# ── Runner permission checks ──────────────────────────────────────────────────

class TestRunnerPermissions:
    def test_gleif_raises_when_master_disabled(self, monkeypatch):
        from app.scraper import runner as r

        monkeypatch.setattr(r.settings, "SCRAPER_ENABLED", False)

        with pytest.raises(PermissionError, match="SCRAPER_ENABLED"):
            r.run_import_bods_gleif()

    def test_gleif_raises_when_source_flag_disabled(self, monkeypatch):
        from app.scraper import runner as r

        monkeypatch.setattr(r.settings, "SCRAPER_ENABLED", True)
        monkeypatch.setattr(r.settings, "SCRAPER_BODS_GLEIF_ENABLED", False)

        with pytest.raises(PermissionError, match="SCRAPER_BODS_GLEIF_ENABLED"):
            r.run_import_bods_gleif()

    def test_uk_psc_raises_when_master_disabled(self, monkeypatch):
        from app.scraper import runner as r

        monkeypatch.setattr(r.settings, "SCRAPER_ENABLED", False)

        with pytest.raises(PermissionError, match="SCRAPER_ENABLED"):
            r.run_import_bods_uk_psc()

    def test_uk_psc_raises_when_source_flag_disabled(self, monkeypatch):
        from app.scraper import runner as r

        monkeypatch.setattr(r.settings, "SCRAPER_ENABLED", True)
        monkeypatch.setattr(r.settings, "SCRAPER_BODS_UK_PSC_ENABLED", False)

        with pytest.raises(PermissionError, match="SCRAPER_BODS_UK_PSC_ENABLED"):
            r.run_import_bods_uk_psc()


class TestPostBodsImport:
    """Every BODS import auto-runs nominee flagging + edge dedup, best-effort."""

    def test_runs_nominees_and_edge_dedup(self):
        from app.scraper import runner as r
        with patch("app.scraper.maintenance.flag_nominee_entities", return_value={"flagged": 3}), \
             patch("app.scraper.maintenance.deduplicate_owns_edges", return_value={"duplicates_removed": 5}):
            out = r._post_bods_import()
        assert out == {"nominees": {"flagged": 3}, "edge_dedup": {"duplicates_removed": 5}}

    def test_best_effort_one_step_failing_still_runs_the_other(self):
        from app.scraper import runner as r
        with patch("app.scraper.maintenance.flag_nominee_entities", side_effect=RuntimeError("boom")), \
             patch("app.scraper.maintenance.deduplicate_owns_edges", return_value={"duplicates_removed": 0}):
            out = r._post_bods_import()
        assert "nominees" not in out                       # failure swallowed
        assert out["edge_dedup"] == {"duplicates_removed": 0}


# ── _flush_script: retry-with-backoff (survives transient proxy 504s) ─────────

class TestFlushRetry:
    def test_retries_then_succeeds(self):
        from app.scraper import bods

        calls = {"n": 0}

        def flaky(script, params):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("ArcadeDB command failed [504]: gateway timeout")
            return [{"ok": True}]

        with patch("app.scraper.bods.run_sqlscript", side_effect=flaky), \
             patch("app.scraper.bods.time.sleep") as sleep:
            out = bods._flush_script("UPDATE Entity ...", {"a": 1})

        assert out == [{"ok": True}]
        assert calls["n"] == 3            # failed twice, third attempt worked
        assert sleep.call_count == 2      # backed off before each retry

    def test_reraises_after_exhausting_attempts(self):
        from app.scraper import bods

        with patch("app.scraper.bods.run_sqlscript",
                   side_effect=RuntimeError("504")) as sql, \
             patch("app.scraper.bods.time.sleep"):
            with pytest.raises(RuntimeError, match="504"):
                bods._flush_script("UPDATE Entity ...", {})

        assert sql.call_count == bods._FLUSH_ATTEMPTS


# ── bulk-load mode: drop secondary indexes for the load, rebuild after ────────

class TestBulkLoad:
    def test_secondary_index_list_excludes_id_and_other_types(self):
        from app.scraper.bods import _bulk_load_secondary_indexes

        names = _bulk_load_secondary_indexes()
        assert "Entity[name_normalized]" in names
        assert "Person[full_name]" in names
        # never drop the id indexes the import relies on
        assert "Entity[id]" not in names
        assert "Person[id]" not in names
        # only Entity/Person are touched
        assert all(n.startswith("Entity[") or n.startswith("Person[") for n in names)

    def test_bulk_load_drops_then_rebuilds_around_the_import(self):
        from app.scraper import bods

        order = []
        with patch("app.scraper.bods._drop_secondary_indexes",
                   side_effect=lambda: order.append("drop")), \
             patch("app.scraper.bods._rebuild_indexes",
                   side_effect=lambda: order.append("rebuild")), \
             patch("app.scraper.bods._entity", return_value="eid"), \
             patch("app.scraper.bods._person", return_value="pid"), \
             patch("app.scraper.bods._owns"), \
             patch("app.scraper.bods._role"), \
             patch("app.scraper.bods.run_sqlscript"):
            bods._run_import(
                iter([ENTITY_STMT]),
                source_id="src-1",
                credibility_score=92,
                limit=None,
                filter_jurisdiction=None,
                bulk_load=True,
            )

        assert order == ["drop", "rebuild"]   # drop before load, rebuild after

    def test_no_index_changes_without_bulk_load(self):
        from app.scraper import bods

        with patch("app.scraper.bods._drop_secondary_indexes") as drop, \
             patch("app.scraper.bods._rebuild_indexes") as rebuild, \
             patch("app.scraper.bods._entity", return_value="eid"), \
             patch("app.scraper.bods._person", return_value="pid"), \
             patch("app.scraper.bods._owns"), \
             patch("app.scraper.bods._role"), \
             patch("app.scraper.bods.run_sqlscript"):
            bods._run_import(
                iter([ENTITY_STMT]),
                source_id="src-1",
                credibility_score=92,
                limit=None,
                filter_jurisdiction=None,
            )

        drop.assert_not_called()
        rebuild.assert_not_called()
