"""A suppressed person, company, holding or seat is shown by NO public read
(findings item 130), against a real ArcadeDB and through the app's own routes.

The filter used to be written out per route, and the routes that forgot it —
the plain record reads, the timeline, the stake series, the provenance list,
the portrait — showed a suppressed person to anyone. So this file does not
test routes one by one: it walks **every GET route the app declares**. A new
public read has to be added to `WALK` (and pass), or to `NOT_WALKED` with the
reason it cannot show graph data — the first test fails otherwise.
"""
import io
import zipfile
from types import SimpleNamespace

import pytest

from app import media
from app.claims import record_claim
from app.db.arcadedb import run_sql

pytestmark = pytest.mark.integration

P, C = "p-hidden", "co-hidden"                     # the suppressed person and company
#: the registered office Acme Holdings shares with the suppressed company
DOOR = "1 letterbox lane london ec1a 1bb gb"
#: what must never be served: their names, their ids, the records that state them
HIDDEN = ("Quillfeather", P, "Hidden Trading", C, "example.com/filing-hidden", "example.com/record-hidden-co")
#: the suppressed seat of a person who is otherwise visible: its role and its record
SEAT = ("Treasurer", "example.com/seat-suppressed")

#: every public GET route → the calls that would show the suppressed things if
#: the route forgot the guard: (url, what must not appear). `None` = the route
#: must answer 404 (a suppressed node is one that is not there).
WALK: dict[str, list[tuple[str, tuple | None]]] = {
    "/v1/search/": [("/v1/search/?q=Quillfeather", HIDDEN), ("/v1/search/?q=Hidden%20Trading", HIDDEN)],
    "/v1/search/geographic": [("/v1/search/geographic?country=GB", HIDDEN)],
    # the letterbox view: the suppressed company shares the visible one's registered office
    "/v1/search/address": [(f"/v1/search/address?key={DOOR.replace(' ', '%20')}", HIDDEN)],
    "/v1/search/person/{person_id}/full-profile": [(f"/v1/search/person/{P}/full-profile", None),
                                                   ("/v1/search/person/p-seat/full-profile", HIDDEN + SEAT)],
    "/v1/search/entity/{entity_id}/full-profile": [(f"/v1/search/entity/{C}/full-profile", None),
                                                   ("/v1/search/entity/co-a/full-profile", HIDDEN + SEAT)],
    "/v1/persons/": [("/v1/persons/?limit=100", HIDDEN)],
    "/v1/persons/{person_id}": [(f"/v1/persons/{P}", None)],
    "/v1/entities/": [("/v1/entities/?limit=100", HIDDEN)],
    "/v1/entities/{entity_id}": [(f"/v1/entities/{C}", None)],
    "/v1/entities/by-country/{country}": [("/v1/entities/by-country/GB", HIDDEN)],
    "/v1/entities/without-country": [("/v1/entities/without-country?basis=hq", HIDDEN)],
    "/v1/relationships/owners/{entity_id}": [("/v1/relationships/owners/co-a", HIDDEN),
                                             (f"/v1/relationships/owners/{C}", ("Acme",))],
    "/v1/relationships/ownership-tree/{entity_id}": [("/v1/relationships/ownership-tree/co-a", HIDDEN),
                                                     (f"/v1/relationships/ownership-tree/{C}", ("Hidden Sub",))],
    "/v1/relationships/subsidiary-tree/{entity_id}": [("/v1/relationships/subsidiary-tree/co-a", HIDDEN),
                                                      (f"/v1/relationships/subsidiary-tree/{C}", None)],
    "/v1/relationships/history/{entity_id}": [("/v1/relationships/history/co-a", HIDDEN + SEAT),
                                              (f"/v1/relationships/history/{C}", ("Acme", "Hidden Sub")),
                                              # its own events — a rename names nobody else
                                              (f"/v1/relationships/history/{C}", ("Formerly Hidden",))],
    "/v1/relationships/stake-history": [(f"/v1/relationships/stake-history?from_id={P}&to_id=co-a", ("2024-01-01",)),
                                        (f"/v1/relationships/stake-history?from_id=co-a&to_id={C}", ("2023-02-02",))],
    "/v1/sources/person/{person_id}": [(f"/v1/sources/person/{P}", HIDDEN),
                                       ("/v1/sources/person/p-seat", HIDDEN + SEAT)],
    "/v1/sources/entity/{entity_id}": [("/v1/sources/entity/co-a", HIDDEN + SEAT),
                                       (f"/v1/sources/entity/{C}", HIDDEN)],
    "/v1/export/entity/{entity_id}": [("/v1/export/entity/co-a?all_levels=true", HIDDEN + SEAT),
                                      (f"/v1/export/entity/{C}", None)],
    "/v1/media/{kind}/{node_id}": [(f"/v1/media/person/{P}", None), (f"/v1/media/entity/{C}", None)],
    "/v1/media/{kind}/{node_id}/file": [(f"/v1/media/person/{P}/file", None), (f"/v1/media/entity/{C}/file", None)],
}

#: GET routes that are not walked, each with why it cannot show a suppressed record
NOT_WALKED = {
    "/": "the service banner", "/health": "liveness only",
    "/v1/app-version": "the client version policy", "/v1/stats": "totals only, no names or ids",
    "/v1/entities/countries": "a count per country", "/v1/entities/by-country": "a count per country",
    "/v1/flags/summary": "flag counts for an id the caller already has",
    "/v1/sources/": "the sources themselves (GLEIF, SEC …), no graph records",
    "/v1/sources/{source_id}": "one source's own description",
    "/v1/analytics/status": "whether analytics is on",
    **{p: "scraper configuration and switches, no graph records" for p in (
        "/v1/scraper/bods/status", "/v1/scraper/open-corporates/status", "/v1/scraper/registry",
        "/v1/scraper/sec-edgar/status", "/v1/scraper/source/{name}/status", "/v1/scraper/sources",
        "/v1/scraper/status")},
}


def _public_get_routes() -> set[str]:
    from app.main import app
    return {path for path, ops in app.openapi()["paths"].items()
            if "get" in ops and not ops["get"].get("security")}


def test_every_public_get_route_is_walked_or_says_why_not():
    declared = _public_get_routes()
    assert declared - set(WALK) - set(NOT_WALKED) == set(), "a new public read: add it to WALK, or to NOT_WALKED with the reason"
    assert (set(WALK) | set(NOT_WALKED)) - declared == set(), "a route this file names is gone or needs a login now"
    assert not set(WALK) & set(NOT_WALKED)


def _graph(it_db):
    """Acme Holdings with a visible subsidiary and director — and, around it,
    everything that gets suppressed: a person holding a stake and a seat, a
    subsidiary with a subsidiary of its own, and one more seat of the visible
    director. Every edge has its claim, as the writers leave it."""
    for sid in ("wikidata", "sec-edgar", "gleif"):
        it_db.run_command(f"CREATE (:Source {{id: '{sid}', name: '{sid}', type: 'registry', url: 'https://example.org/{sid}'}})")
    for eid, name, extra in (("co-a", "Acme Holdings Ltd", ""), ("co-sub", "Acme Sub Ltd", ""),
                             (C, "Hidden Trading Ltd", ", source_id: 'gleif', source_url: 'https://example.com/record-hidden-co', "
                                                       "logo_url: 'https://upload.wikimedia.org/logo.png', "
                                                       "former_names: ['Formerly Hidden Ltd|2021-06-30']"),
                             ("co-hsub", "Hidden Sub Ltd", "")):
        it_db.run_command(f"CREATE (:Entity {{id: '{eid}', name: '{name}', type: 'company', country: 'GB', "
                          f"name_normalized: '{name.lower()}', search_text: '{name.lower()}'{extra}}})")
    run_sql(f"UPDATE Entity SET address = '1 Letterbox Lane, London, EC1A 1BB, GB', address_normalized = '{DOOR}' "
            f"WHERE id IN ['co-a', 'co-sub', '{C}']")
    it_db.run_command(f"CREATE (:Person {{id: '{P}', full_name: 'Zebedee Quillfeather', first_name: 'Zebedee', "
                      "last_name: 'Quillfeather', search_text: 'zebedee quillfeather', birth_date: '1970-01', "
                      "source_id: 'wikidata', source_url: 'https://example.com/filing-hidden', "
                      "image_url: 'https://upload.wikimedia.org/x.jpg'})")
    it_db.run_command("CREATE (:Person {id: 'p-seat', full_name: 'Visible Person', first_name: 'Visible', "
                      "last_name: 'Person', search_text: 'visible person'})")

    def seat(pid, eid, role, url):
        it_db.run_command(f"MATCH (p:Person {{id: '{pid}'}}), (e:Entity {{id: '{eid}'}}) CREATE (p)-[:HAS_ROLE "
                          f"{{role: '{role}', source_id: 'wikidata', since: '2020-01-01', source_url: '{url}'}}]->(e)")
        record_claim(kind="role", from_id=pid, to_id=eid, source_id="wikidata", role=role, source_url=url)

    def owns(a, b, pct, url, label="Entity"):
        it_db.run_command(f"MATCH (a:{label} {{id: '{a}'}}), (b:Entity {{id: '{b}'}}) CREATE (a)-[:OWNS "
                          f"{{stake_percent: {pct}, source_id: 'sec-edgar', since: '2019-01-01', source_url: '{url}'}}]->(b)")
        record_claim(kind="owns", from_id=a, to_id=b, source_id="sec-edgar", stake_percent=pct, source_url=url)

    seat(P, "co-a", "Chair", "https://example.com/filing-hidden")
    seat("p-seat", "co-a", "Treasurer", "https://example.com/seat-suppressed")
    seat("p-seat", "co-sub", "Director", "https://example.com/seat-visible")
    owns(P, "co-a", 30.0, "https://example.com/filing-hidden", label="Person")
    owns("co-a", "co-sub", 100.0, "https://example.com/sub-visible")
    owns("co-a", C, 100.0, "https://example.com/record-hidden-co")
    owns(C, "co-hsub", 100.0, "https://example.com/record-hidden-co")
    for frm, to, day in ((P, "co-a", "2024-01-01"), ("co-a", C, "2023-02-02")):
        run_sql(f"INSERT INTO StakeHistory SET from_id = '{frm}', to_id = '{to}', source_id = 'sec-edgar', "
                f"as_of = '{day}', stake_percent = 25.0, previous_percent = 2.0")


def _suppress():
    run_sql(f"INSERT INTO Suppression SET id = 's1', target_kind = 'person', node_id = '{P}', from_id = '', to_id = '', role = ''")
    run_sql(f"INSERT INTO Suppression SET id = 's2', target_kind = 'entity', node_id = '{C}', from_id = '', to_id = '', role = ''")
    # the suppress action deletes the seat; the next import writes it again — this is that state
    run_sql("INSERT INTO Suppression SET id = 's3', target_kind = 'role', node_id = '', from_id = 'p-seat', "
            "to_id = 'co-a', role = 'Treasurer'")


@pytest.fixture
def client(it_db, monkeypatch):
    """The app over the scratch database. Wikimedia is the outside world: any
    node with an image field has a credit and bytes."""
    from fastapi.testclient import TestClient
    from app.main import app
    credit = SimpleNamespace(etag="abc", author="A", license="CC0", license_url=None, file_page="https://example.org/f",
                             attribution_required=False, source="Wikimedia Commons", content_type="image/png")
    has = lambda record: record.get("image_url") or record.get("logo_url")     # noqa: E731
    monkeypatch.setattr(media, "image_info", lambda kind, node_id, record: credit if has(record) else None)
    monkeypatch.setattr(media, "image_bytes", lambda kind, node_id, record: (b"png", "image/png") if has(record) else None)
    _graph(it_db)
    return TestClient(app, raise_server_exceptions=False)


def _body(response) -> str:
    if "opendocument" in response.headers.get("content-type", ""):
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            return z.read("content.xml").decode()
    return response.content.decode("utf-8", "replace")


def _shown(client) -> dict[str, list[str]]:
    """Per call: what it serves of the things it must not — `['served']` for a
    route that should answer 404 and answers with a record."""
    out = {}
    for calls in WALK.values():
        for url, needles in calls:
            r = client.get(url)
            assert r.status_code < 500, (url, r.status_code, r.text[:300])
            if needles is None:
                out[url] = ["served"] if r.status_code != 404 else []
            else:
                body = _body(r) if r.status_code == 200 else ""
                out[f"{url} {needles[0]}"] = [n for n in needles if n in body]
    return out


def test_without_a_suppression_every_walked_call_shows_what_it_is_asked_about(client):
    """The walk is only worth something if each call reaches the data: before
    anything is suppressed, every one of them serves a name, id or record that
    the next test forbids."""
    silent = [url for url, found in _shown(client).items() if not found]
    assert silent == []


def test_no_public_read_shows_a_suppressed_person_company_holding_or_seat(client):
    _suppress()
    leaks = {url: found for url, found in _shown(client).items() if found}
    assert leaks == {}


def test_what_is_not_suppressed_stays(client):
    """The guard hides what is suppressed and nothing else."""
    _suppress()
    assert "Acme Sub" in client.get("/v1/relationships/subsidiary-tree/co-a").text
    assert "Visible Person" in client.get("/v1/persons/p-seat").text
    profile = client.get("/v1/search/person/p-seat/full-profile").text
    assert "Acme Sub" in profile and "Director" in profile
    assert "example.com/seat-visible" in client.get("/v1/sources/person/p-seat").text
    assert "example.com/sub-visible" in client.get("/v1/sources/entity/co-sub").text
    assert client.get("/v1/entities/co-a").status_code == 200
    names = [e["party"].get("name") or e["party"].get("full_name")
             for e in client.get("/v1/relationships/history/co-a").json() if e.get("party")]
    assert "Acme Sub Ltd" in names
