"""
Conformance test — the minimum requirement every HTTP-API scraper must meet:
identify the application (and a contact) via a User-Agent on every request.

Wikimedia and SEC EDGAR both reject/refuse anonymous traffic (we've hit their
403), and it's the baseline courtesy for any polite scraper. This checks the
User-Agent is actually SENT on each scraper's real request path — not merely
declared as a constant — so a new scraper that forgets it fails here.

Note: BODS pulls bulk files from S3 (no identification required) and is gated /
tested separately; the sources that talk to identifying APIs are covered here.
"""
from unittest.mock import patch, MagicMock

import pytest


def _fake_response(json_val=None, text=""):
    r = MagicMock()
    r.json.return_value = {} if json_val is None else json_val
    r.text = text
    r.status_code = 200
    r.raise_for_status.return_value = None
    return r


def _call_wikidata():
    from app.scraper import wikidata
    with patch("httpx.get", return_value=_fake_response({"search": []})) as g, patch("time.sleep"):
        wikidata.search_entity("acme")
    return g


def _call_sec_edgar():
    from app.scraper import sec_edgar
    with patch("httpx.get", return_value=_fake_response({})) as g, patch("time.sleep"):
        sec_edgar._get("https://data.sec.gov/x")
    return g


def _call_open_corporates():
    from app.scraper import open_corporates
    with patch("httpx.get", return_value=_fake_response({})) as g, patch("time.sleep"):
        open_corporates._get("/companies/search")
    return g


# name → a callable that triggers exactly one outbound GET and returns the mock
API_SCRAPERS = {
    "wikidata":         _call_wikidata,
    "sec_edgar":        _call_sec_edgar,
    "open_corporates":  _call_open_corporates,
}


@pytest.mark.parametrize("name", list(API_SCRAPERS))
class TestScraperSendsIdentifyingUserAgent:
    def _sent_user_agent(self, name: str) -> str:
        mock_get = API_SCRAPERS[name]()
        assert mock_get.called, f"{name}: expected an HTTP request"
        headers = mock_get.call_args.kwargs.get("headers") or {}
        return headers.get("User-Agent", "")

    def test_user_agent_is_sent(self, name):
        assert self._sent_user_agent(name), f"{name}: request sent with no User-Agent header"

    def test_user_agent_identifies_the_app(self, name):
        ua = self._sent_user_agent(name)
        assert "Pamten" in ua, f"{name}: User-Agent must identify the app — got {ua!r}"

    def test_user_agent_includes_a_contact(self, name):
        # ToS require a reachable contact so the source can get in touch.
        ua = self._sent_user_agent(name)
        assert "@" in ua or "http" in ua, \
            f"{name}: User-Agent must include a contact (email or URL) — got {ua!r}"


# ── Shared helpers for node/edge-writer conformance ──────────────────────────
from contextlib import contextmanager           # noqa: E402
from app.config import Settings, settings        # noqa: E402
from app.scraper import runner, bods             # noqa: E402
from app.scraper.sources import KNOWN_SOURCES    # noqa: E402


def _create_query(module_name: str, invoke) -> str:
    """Run a node/edge upsert with the DB mocked (new-node path), and return the
    CREATE Cypher it issued — so we can assert what fields it stamps."""
    session = MagicMock()
    run_mock = MagicMock()
    run_mock.single.return_value = None          # nothing exists yet → CREATE path
    session.run.return_value = run_mock

    @contextmanager
    def _ctx():
        yield session

    with patch(f"app.scraper.{module_name}.db.get_session", _ctx):
        invoke()

    creates = [c.args[0] for c in session.run.call_args_list if c.args and "CREATE " in c.args[0]]
    return creates[0] if creates else ""


# ── 1. Gating: nothing runs unless explicitly enabled ────────────────────────

class TestScraperGating:
    def test_master_flag_defaults_off(self):
        # The kill-switch must be opt-in — off unless deliberately enabled.
        assert Settings.model_fields["SCRAPER_ENABLED"].default is False

    SOURCE_FLAGS = {
        "wikidata":        "SCRAPER_WIKIDATA_ENABLED",
        "sec_edgar":       "SCRAPER_SEC_EDGAR_ENABLED",
        "open_corporates": "SCRAPER_OPENCORPORATES_ENABLED",
        "bods_gleif":      "SCRAPER_BODS_GLEIF_ENABLED",
        "bods_uk_psc":     "SCRAPER_BODS_UK_PSC_ENABLED",
    }

    @pytest.mark.parametrize("source,flag", list(SOURCE_FLAGS.items()))
    def test_every_source_has_an_enable_flag(self, source, flag):
        assert source in KNOWN_SOURCES, f"{source} missing from KNOWN_SOURCES"
        assert flag in Settings.model_fields, f"missing config flag {flag} for {source}"

    ENTRY_POINTS = [
        ("run_scrape",                 lambda: runner.run_scrape("Acme")),
        ("run_scrape_sec_edgar",       lambda: runner.run_scrape_sec_edgar("Acme")),
        ("run_scrape_open_corporates", lambda: runner.run_scrape_open_corporates("Acme")),
        ("run_scrape_all",             lambda: runner.run_scrape_all("Acme")),
    ]

    @pytest.mark.parametrize("name,call", ENTRY_POINTS)
    def test_entry_point_refuses_when_master_disabled(self, name, call):
        with patch.object(settings, "SCRAPER_ENABLED", False):
            with pytest.raises(PermissionError):
                call()


# ── 2. Provenance: every edge a scraper writes is attributable ───────────────

# (label, module, invoke) — one per OWNS/HAS_ROLE writer across all scrapers.
EDGE_WRITERS = [
    ("wikidata OWNS",        "runner", lambda: runner._upsert_owns("o", "n", "s", source_url="u", source_date="d")),
    ("wikidata HAS_ROLE",    "runner", lambda: runner._upsert_role("p", "e", "CEO", "s", source_url="u")),
    ("sec_edgar HAS_ROLE",   "runner", lambda: runner._upsert_role_sec("p", "e", "CEO", "s", source_url="u", source_date="d")),
    ("sec_edgar OWNS",       "runner", lambda: runner._upsert_owns_sec("o", "n", "s", "minority", "2024-01-01", 5.0, source_url="u")),
    ("open_corp HAS_ROLE",   "runner", lambda: runner._upsert_role_oc("p", "e", "Director", None, None, "s", source_url="u")),
    ("bods OWNS",            "bods",   lambda: bods._upsert_owns_bods("o", "n", 50.0, "majority", None, None, "s", 90, "u", "d")),
    ("bods HAS_ROLE",        "bods",   lambda: bods._upsert_role_bods("p", "e", "Official", None, None, "s", 90, "u", "d")),
]


@pytest.mark.parametrize("label,module,invoke", EDGE_WRITERS)
class TestEdgeProvenance:
    def test_edge_stamps_full_provenance(self, label, module, invoke):
        q = _create_query(module, invoke)
        assert q, f"{label}: no CREATE issued"
        for field in ("source_url", "source_date", "last_scraped_at"):
            assert field in q, f"{label}: CREATE must stamp {field} — got:\n{q}"


# ── 3. Minimum fields captured from the source ───────────────────────────────

ENTITY_WRITERS = [
    ("wikidata entity", "runner", lambda: runner._upsert_entity("Acme", "company", "US", 2000, None, None, "Q1")),
    ("bods entity",     "bods",   lambda: bods._upsert_entity_bods("Acme", "company", "US", None, "LEI1", None, "s", 90)),
]

# Persons from a source that carries a date of birth must persist it.
PERSON_WRITERS_WITH_DOB = [
    ("wikidata person", "runner", lambda: runner._upsert_person("Elon Musk", None, None, "Q1", birth_date="1971-06-28")),
    ("bods person",     "bods",   lambda: bods._upsert_person_bods("Jane Doe", None, None, None, "1980-01")),
]


class TestMinimumFields:
    @pytest.mark.parametrize("label,module,invoke", ENTITY_WRITERS)
    def test_entity_captures_name_and_country(self, label, module, invoke):
        q = _create_query(module, invoke)
        assert "name:" in q, f"{label}: entity must capture a name"
        assert "country:" in q, f"{label}: entity must capture a country"

    @pytest.mark.parametrize("label,module,invoke", PERSON_WRITERS_WITH_DOB)
    def test_person_captures_name_and_birth_date(self, label, module, invoke):
        q = _create_query(module, invoke)
        assert "full_name:" in q, f"{label}: person must capture a full name"
        assert "birth_date:" in q, f"{label}: person from a DOB source must persist birth_date"

    def test_bare_person_captures_at_least_a_name(self):
        # SEC/OC officers arrive as a name only — the floor is still a full_name.
        q = _create_query("runner", lambda: runner._upsert_person_by_name("Jane Doe"))
        assert "full_name:" in q
