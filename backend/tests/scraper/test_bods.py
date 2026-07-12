"""
Tests for the BODS (Beneficial Ownership Data Standard) importer.

All DB calls are mocked — these tests validate field mapping logic only.
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

def _make_mock_session(upsert_return="new-id"):
    """Return a mock db session whose .run().single() returns a record with id."""
    session = MagicMock()
    single_mock = MagicMock()
    single_mock.return_value = None  # default: no existing record → create
    session.run.return_value = single_mock
    return session


# ── _process_entity_statement ─────────────────────────────────────────────────

class TestProcessEntityStatement:
    def test_maps_fields_correctly(self):
        from app.scraper.bods import _process_entity_statement

        captured = {}

        def fake_upsert(**kwargs):
            captured.update(kwargs)
            return "eid-1"

        bods_map: dict = {}
        with patch("app.scraper.bods._upsert_entity_bods", side_effect=fake_upsert):
            result = _process_entity_statement(
                ENTITY_STMT, bods_map, "src-1", 92, None
            )

        assert result == "eid-1"
        assert bods_map["entity-001"] == "eid-1"
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
        captured = {}

        def fake_upsert(**kwargs):
            captured.update(kwargs)
            return "eid-2"

        with patch("app.scraper.bods._upsert_entity_bods", side_effect=fake_upsert):
            _process_entity_statement(stmt, {}, "src-1", 92, None)

        assert captured["entity_type"] == "holding"

    def test_filter_jurisdiction_skips_non_matching(self):
        from app.scraper.bods import _process_entity_statement

        bods_map: dict = {}
        with patch("app.scraper.bods._upsert_entity_bods") as mock_upsert:
            result = _process_entity_statement(
                ENTITY_STMT, bods_map, "src-1", 92, "DE"
            )

        assert result is None
        assert "entity-001" not in bods_map
        mock_upsert.assert_not_called()

    def test_filter_jurisdiction_accepts_matching(self):
        from app.scraper.bods import _process_entity_statement

        with patch("app.scraper.bods._upsert_entity_bods", return_value="eid-gb"):
            result = _process_entity_statement(
                ENTITY_STMT, {}, "src-1", 92, "GB"
            )

        assert result == "eid-gb"

    def test_missing_name_returns_none(self):
        from app.scraper.bods import _process_entity_statement

        stmt = {**ENTITY_STMT, "recordDetails": {"name": ""}}
        with patch("app.scraper.bods._upsert_entity_bods") as mock_upsert:
            result = _process_entity_statement(stmt, {}, "src-1", 92, None)

        assert result is None
        mock_upsert.assert_not_called()

    def test_partial_founding_date(self):
        from app.scraper.bods import _process_entity_statement

        stmt = {
            **ENTITY_STMT,
            "recordDetails": {
                **ENTITY_STMT["recordDetails"],
                "foundingDate": "2005",
            },
        }
        captured = {}

        with patch("app.scraper.bods._upsert_entity_bods",
                   side_effect=lambda **kw: captured.update(kw) or "eid"):
            _process_entity_statement(stmt, {}, "src-1", 92, None)

        assert captured["founded"] == 2005


# ── _process_person_statement ─────────────────────────────────────────────────

class TestProcessPersonStatement:
    def test_maps_fields_correctly(self):
        from app.scraper.bods import _process_person_statement

        captured = {}

        def fake_upsert(**kwargs):
            captured.update(kwargs)
            return "pid-1"

        bods_map: dict = {}
        with patch("app.scraper.bods._upsert_person_bods", side_effect=fake_upsert):
            result = _process_person_statement(PERSON_STMT, bods_map, "src-1", 97)

        assert result == "pid-1"
        assert bods_map["person-001"] == "pid-1"
        assert captured["full_name"] == "Jennifer Hewitson-Smith"
        assert captured["first_name"] == "Jennifer"
        assert captured["last_name"] == "Hewitson-Smith"
        assert captured["nationality"] == "GB"

    def test_partial_birth_date_stored_as_is(self):
        from app.scraper.bods import _process_person_statement

        captured = {}

        with patch("app.scraper.bods._upsert_person_bods",
                   side_effect=lambda **kw: captured.update(kw) or "pid"):
            _process_person_statement(PERSON_STMT, {}, "src-1", 97)

        assert captured["birth_date"] == "1978-07"

    def test_anonymous_person_skipped(self):
        from app.scraper.bods import _process_person_statement

        bods_map: dict = {}
        with patch("app.scraper.bods._upsert_person_bods") as mock_upsert:
            result = _process_person_statement(ANON_PERSON_STMT, bods_map, "src-1", 97)

        assert result is None
        assert "person-anon" not in bods_map
        mock_upsert.assert_not_called()


# ── _process_relationship_statement ──────────────────────────────────────────

class TestProcessRelationshipStatement:
    def _bods_map(self):
        return {"entity-001": "eid-1", "entity-002": "eid-2", "person-001": "pid-1"}

    def _name_map(self):
        return {}  # empty — tests don't need real names

    def test_shareholding_maps_correctly(self):
        from app.scraper.bods import _process_relationship_statement

        captured = {}

        def fake_owns(**kwargs):
            captured.update(kwargs)

        bods_map = self._bods_map()
        with patch("app.scraper.bods._upsert_owns_bods", side_effect=fake_owns):
            edges = _process_relationship_statement(
                RELATIONSHIP_STMT, bods_map, self._name_map(), "src-1", 97
            )

        assert edges == 1
        assert captured["stake_percent"] == 60.5
        assert captured["ownership_type"] == "majority"
        assert captured["since"] == "2016-04-06"
        assert captured["until"] is None

    def test_closed_record_sets_until(self):
        from app.scraper.bods import _process_relationship_statement

        captured = {}

        with patch("app.scraper.bods._upsert_owns_bods",
                   side_effect=lambda **kw: captured.update(kw)):
            _process_relationship_statement(
                CLOSED_RELATIONSHIP_STMT, self._bods_map(), self._name_map(), "src-1", 97
            )

        assert captured["until"] == "2023-05-01"

    def test_missing_share_exact_falls_back_to_minimum(self):
        from app.scraper.bods import _process_relationship_statement

        captured = {}

        with patch("app.scraper.bods._upsert_owns_bods",
                   side_effect=lambda **kw: captured.update(kw)):
            _process_relationship_statement(
                RELATIONSHIP_NO_EXACT, self._bods_map(), self._name_map(), "src-1", 97
            )

        assert captured["stake_percent"] == 25.0

    def test_unknown_interest_type_maps_to_minority(self):
        from app.scraper.bods import _process_relationship_statement

        captured = {}

        with patch("app.scraper.bods._upsert_owns_bods",
                   side_effect=lambda **kw: captured.update(kw)), \
             patch("app.scraper.bods._upsert_entity_bods", return_value="eid-new"):
            _process_relationship_statement(
                RELATIONSHIP_UNKNOWN_INTEREST, self._bods_map(), self._name_map(), "src-1", 97
            )

        assert captured["ownership_type"] == "minority"

    def test_senior_managing_official_creates_role_edge(self):
        from app.scraper.bods import _process_relationship_statement

        with patch("app.scraper.bods._upsert_role_bods") as mock_role, \
             patch("app.scraper.bods._upsert_owns_bods") as mock_owns:
            edges = _process_relationship_statement(
                RELATIONSHIP_ROLE, self._bods_map(), self._name_map(), "src-1", 97
            )

        assert edges == 1
        mock_role.assert_called_once()
        mock_owns.assert_not_called()

    def test_bods_to_pamten_id_lookup_resolves_correctly(self):
        from app.scraper.bods import _process_relationship_statement

        captured = {}
        bods_map = {"entity-001": "OWNED-UUID", "entity-002": "OWNER-UUID"}

        with patch("app.scraper.bods._upsert_owns_bods",
                   side_effect=lambda **kw: captured.update(kw)):
            _process_relationship_statement(
                RELATIONSHIP_STMT, bods_map, self._name_map(), "src-1", 97
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
        captured = {}

        with patch("app.scraper.bods._upsert_owns_bods",
                   side_effect=lambda **kw: captured.update(kw)):
            _process_relationship_statement(stmt, self._bods_map(), self._name_map(), "src-1", 97)

        assert captured["stake_percent"] == 8.5
        assert captured["ownership_type"] == "minority"

    def test_unresolved_party_creates_placeholder_and_writes_edge(self):
        # When interestedParty is not in bods_id_map, the importer creates a
        # placeholder Entity rather than skipping, so the edge is preserved.
        from app.scraper.bods import _process_relationship_statement

        bods_map = {"entity-001": "eid-1"}   # entity-002 intentionally absent
        owns_calls = []

        with patch("app.scraper.bods._upsert_entity_bods", return_value="placeholder-id") as mock_e, \
             patch("app.scraper.bods._upsert_owns_bods",
                   side_effect=lambda **kw: owns_calls.append(kw)):
            edges = _process_relationship_statement(
                RELATIONSHIP_STMT, bods_map, self._name_map(), "src-1", 97
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

        with patch("app.scraper.bods._upsert_entity_bods", return_value="eid") as mock_e, \
             patch("app.scraper.bods._upsert_person_bods", return_value="pid"), \
             patch("app.scraper.bods._upsert_owns_bods"), \
             patch("app.scraper.bods._upsert_role_bods"):
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

        with patch("app.scraper.bods._upsert_entity_bods", return_value="eid"), \
             patch("app.scraper.bods._upsert_person_bods", return_value="pid"), \
             patch("app.scraper.bods._upsert_owns_bods"), \
             patch("app.scraper.bods._upsert_role_bods"):
            counts = _run_import(
                iter(many_stmts),
                source_id="src-1",
                credibility_score=92,
                limit=5,
                filter_jurisdiction=None,
            )

        assert counts["entities"] == 5


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
