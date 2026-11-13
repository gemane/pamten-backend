"""Companies House officers: what the reader takes from an appointment and
what it leaves, the paging and the request window. No database, no network —
the records are the API's shape (probed 2026-11-13: 9,111 appointments at
dev's 280 UK companies), the names invented."""
import httpx
import pytest

from app.scraper import ch_officers as ch


def _item(**over):
    item = {"name": "SMITH, Jane Mary", "officer_role": "director", "appointed_on": "2014-10-16",
            "date_of_birth": {"month": 10, "year": 1979}, "nationality": "British",
            "country_of_residence": "England", "address": {"postal_code": "EX1 1AA"},
            "links": {"self": "/company/08415302/appointments/aaa",
                      "officer": {"appointments": "/officers/T-eg0NXIoSj2L_IyntMpc3kSXh8/appointments"}}}
    item.update(over)
    return item


def test_an_appointment_gives_the_person_the_seat_and_nothing_else():
    assert ch.read_officer(_item()) == {
        "officer_id": "T-eg0NXIoSj2L_IyntMpc3kSXh8", "first_name": "Jane Mary", "last_name": "Smith",
        "full_name": "Jane Mary Smith", "birth_date": "1979-10", "nationalities": ["GB"],
        "role": "Director", "since": "2014-10-16", "until": None}       # no address, no residence


@pytest.mark.parametrize("raw, nats", [
    ("British", ["GB"]),
    ("British,American", ["GB", "US"]),                 # two nationalities are one comma-joined text
    ("British, Swiss ,South African", ["GB", "CH", "ZA"]),
    ("British,English", ["GB"]),                        # one country, once
    ("Citizen Of Usa", ["Citizen Of Usa"]),             # not recognised: the register's words stay
    ("", []), (None, []), (" , ", []),
])
def test_nationalities_are_stored_as_the_other_sources_store_them(raw, nats):
    assert ch.nationalities({"nationality": raw}) == nats


@pytest.mark.parametrize("raw, name", [
    ("SMITH, Jane Mary", ("Jane Mary", "Smith", "Jane Mary Smith")),
    ("O'NEILL-SMITH, Dr John", ("John", "O'Neill-Smith", "John O'Neill-Smith")),
    ("VAN DER BERG, Anna", ("Anna", "Van Der Berg", "Anna Van Der Berg")),
    ("de Souza, Maria", ("Maria", "de Souza", "Maria de Souza")),       # the register's own mixed case stays
    ("  KHAN,   Mr Imran  ", ("Imran", "Khan", "Imran Khan")),
    ("Jane Smith", ("Jane", "Smith", "Jane Smith")),                    # no comma: already in order
    ("SMITH", ("", "Smith", "Smith")),
    ("", None), (None, None),
])
def test_officer_name(raw, name):
    assert ch.officer_name(raw) == name


@pytest.mark.parametrize("role, label", [
    ("director", "Director"), ("secretary", "Company Secretary"), ("llp-member", "LLP Member"),
    ("llp-designated-member", "LLP Designated Member"), ("nominee-director", "Nominee Director"),
    ("cic-manager", "Cic Manager"),                 # a role the table does not name: as the register words it
    ("", None), (None, None),
])
def test_role_label(role, label):
    assert ch.role_label(role) == label


@pytest.mark.parametrize("item", [
    _item(officer_role="corporate-secretary", name="PRISM COSEC LIMITED"),
    _item(officer_role="corporate-nominee-director", name="LONDON LAW SERVICES LIMITED"),
    # a company under a person's role still states its register
    _item(officer_role="director", name="ACME HOLDINGS LIMITED",
          identification={"identification_type": "eea", "registration_number": "5533248"}),
])
def test_a_company_holding_the_seat_is_not_read(item):
    assert ch.is_corporate(item) and ch.read_officer(item) is None


@pytest.mark.parametrize("item", [
    _item(links={}), _item(name=""), _item(officer_role=None),
    _item(links={"officer": {"appointments": "/somewhere/else"}}),
])
def test_a_record_without_id_name_or_role_is_not_read(item):
    assert ch.read_officer(item) is None


@pytest.mark.parametrize("dob, birth", [
    ({"month": 1, "year": 1964}, "1964-01"), ({"month": 12, "year": 1990, "day": 5}, "1990-12"),
    ({"year": 1964}, None), ({"month": 13, "year": 1964}, None), ({}, None), (None, None),
])
def test_birth_is_month_and_year_never_a_day(dob, birth):
    assert ch.birth_month({"date_of_birth": dob}) == birth


def test_a_resignation_is_read_with_its_day():
    rec = ch.read_officer(_item(resigned_on="2020-03-31"))
    assert (rec["since"], rec["until"]) == ("2014-10-16", "2020-03-31")
    assert ch.read_officer(_item(appointed_on=None, appointed_before="1991-12-31"))["since"] is None


# ── the API ──────────────────────────────────────────────────────────────────

def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), auth=("key", ""))


# the transport is a mock: `allow_network` only lifts the guard that judges by host
mocked = pytest.mark.allow_network


@mocked
def test_every_page_is_read_with_the_key_as_the_user():
    seen = []

    def handler(request):
        seen.append((request.url.params["start_index"], request.headers["authorization"]))
        start = int(request.url.params["start_index"])
        return httpx.Response(200, json={"total_results": 250,
                                         "items": [{"n": i} for i in range(start, min(start + 100, 250))]})
    items = ch.fetch_officers(_client(handler), "00102498")
    assert [i["n"] for i in items] == list(range(250))
    assert [s for s, _ in seen] == ["0", "100", "200"]
    assert seen[0][1] == "Basic a2V5Og=="            # "key:" — the key, no password


@mocked
def test_a_company_the_register_does_not_know_is_none_and_an_error_is_raised():
    assert ch.fetch_officers(_client(lambda r: httpx.Response(404)), "X") is None
    with pytest.raises(ch.OfficersError):
        ch.fetch_officers(_client(lambda r: httpx.Response(500)), "X")


@mocked
def test_the_reader_waits_when_the_window_is_used_up_and_asks_again_after_a_429():
    calls, slept = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"x-ratelimit-remain": "0", "x-ratelimit-reset": "0"})
        return httpx.Response(200, json={"total_results": 1, "items": [{"n": 1}]},
                              headers={"x-ratelimit-remain": "599", "x-ratelimit-reset": "0"})
    assert ch.fetch_officers(_client(handler), "X", sleep=slept.append) == [{"n": 1}]
    assert len(calls) == 2 and len(slept) == 1 and 1 <= slept[0] <= 305


def test_no_wait_while_requests_are_left():
    slept = []
    ch._wait_for_window(httpx.Response(200, headers={"x-ratelimit-remain": "300", "x-ratelimit-reset": "0"}),
                        slept.append)
    assert slept == []


def test_without_a_key_nothing_is_read():
    from unittest.mock import patch

    from app.config import settings
    with patch.object(settings, "COMPANIES_HOUSE_API_KEY", ""), \
         patch.object(ch, "companies_in_graph", side_effect=AssertionError("read")):
        assert ch.run_ch_officers() == {"status": "no_api_key"}


def test_a_dry_run_writes_nothing_not_even_a_run_row():
    from unittest.mock import patch

    from app.config import settings
    items = [_item(), _item(resigned_on="2020-01-01"), _item(officer_role="corporate-secretary")]
    with patch.object(settings, "COMPANIES_HOUSE_API_KEY", "k"), \
         patch("app.scraper.sources.get_source_enabled", side_effect=AssertionError("catalogue")), \
         patch.object(ch, "companies_in_graph", return_value=[{"id": "gb-coh:1", "name": "A Ltd", "number": "1"}]), \
         patch.object(ch, "fetch_officers", return_value=items), \
         patch.object(ch, "write_company_officers", side_effect=AssertionError("write")), \
         patch("app.scraper.run_log.record_run", side_effect=AssertionError("run row")), \
         patch("app.scraper.graph_writer._ensure_source", side_effect=AssertionError("source")):
        out = ch.run_ch_officers(dry_run=True)
    assert (out["status"], out["read"], out["written"], out["resigned"], out["corporate"]) == ("dry-run", 1, 1, 1, 1)


def test_a_source_switched_off_in_the_catalogue_reads_nothing():
    from unittest.mock import patch

    from app.config import settings
    with patch.object(settings, "COMPANIES_HOUSE_API_KEY", "k"), \
         patch("app.scraper.sources.get_source_enabled", return_value=False), \
         patch.object(ch, "companies_in_graph", side_effect=AssertionError("read")):
        assert ch.run_ch_officers() == {"status": "disabled"}
