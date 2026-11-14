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
    # what follows a second comma is the register's title field (findings item 120)
    ("MERRICK, Julie, Dr", ("Julie", "Merrick", "Julie Merrick")),
    ("NODA, Hidehiko, Mr.", ("Hidehiko", "Noda", "Hidehiko Noda")),
    ("KINGMAN, John Oliver Frank, Sir", ("John Oliver Frank", "Kingman", "John Oliver Frank Kingman")),
    ("COTTAM, Harold, Managing Director", ("Harold", "Cottam", "Harold Cottam")),
    ("POWELL, Charles David, The Right Honourable Viscount St. Davids", ("Charles David", "Powell", "Charles David Powell")),
])
def test_officer_name(raw, name):
    assert ch.officer_name(raw) == name


@pytest.mark.parametrize("raw, title", [
    ("MERRICK, Julie, Dr", True), ("SMITH, Jane Mary", False), ("Jane Smith", False), ("", False), (None, False),
])
def test_a_name_has_a_title_part_after_its_second_comma(raw, title):
    assert ch.has_title_part(raw) is title


def test_the_registers_own_split_of_the_name_is_taken_where_it_is_given():
    """The one name of 524 whose third part is forenames, not a title: the
    register's split has them, the list's text does not say."""
    split = {"title": "Mr", "forename": "Peter", "other_forenames": "John  Anton", "surname": "O'NEILL"}
    assert ch.officer_name("O'NEILL, Peter, John Anton", split) == ("Peter John Anton", "O'Neill", "Peter John Anton O'Neill")
    assert ch.officer_name("MERRICK, Julie, Dr", {"title": "Dr", "forename": "Julie", "surname": "MERRICK"}) == \
        ("Julie", "Merrick", "Julie Merrick")
    # a split without a surname says nothing: the text is read
    assert ch.officer_name("MERRICK, Julie, Dr", {"title": "Dr", "forename": "Julie"})[2] == "Julie Merrick"
    assert ch.officer_name("MERRICK, Julie, Dr", {})[2] == "Julie Merrick"


def test_an_appointment_with_the_split_is_read_by_it():
    rec = ch.read_officer(_item(name="O'NEILL, Peter, John Anton",
                                name_elements={"forename": "Peter", "other_forenames": "John Anton", "surname": "O'NEILL"}))
    assert (rec["first_name"], rec["last_name"], rec["full_name"]) == ("Peter John Anton", "O'Neill", "Peter John Anton O'Neill")


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


# ── a name with a title part: the register's own split (findings item 120) ───

SPLIT = {"title": "Dr", "forename": "Julie", "surname": "MERRICK"}


def _appointments(*responses):
    """A client that answers the appointments page with these, in order."""
    calls = []

    def handler(request):
        calls.append((request.url.path, dict(request.url.params)))
        r = responses[min(len(calls), len(responses)) - 1]
        if isinstance(r, Exception):
            raise r
        return r
    return _client(handler), calls


def _page(elements=SPLIT, **kw):
    return httpx.Response(200, json={"items": [{"name": "Julie MERRICK", "name_elements": elements}]}, **kw)


@mocked
def test_the_split_is_one_request_for_one_item_of_the_officers_page():
    client, calls = _appointments(_page())
    assert ch.fetch_name_elements(client, "abc123") == SPLIT
    assert calls == [("/officers/abc123/appointments", {"items_per_page": "1"})]


@mocked
@pytest.mark.parametrize("first", [httpx.Response(500), httpx.Response(502), httpx.ConnectError("down"),
                                   httpx.ReadTimeout("stalled")])
def test_a_failed_request_is_tried_once_more(first):
    client, calls = _appointments(first, _page())
    assert ch.fetch_name_elements(client, "abc123") == SPLIT and len(calls) == 2


@mocked
def test_two_failures_give_none_and_no_third_request():
    client, calls = _appointments(httpx.Response(500), httpx.ConnectError("down"), _page())
    assert ch.fetch_name_elements(client, "abc123") is None and len(calls) == 2


@mocked
def test_a_page_the_register_does_not_have_is_not_asked_for_twice():
    client, calls = _appointments(httpx.Response(404), _page())
    assert ch.fetch_name_elements(client, "abc123") is None and len(calls) == 1


@mocked
@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"items": []}), httpx.Response(200, json={"items": [{"name": "Julie MERRICK"}]}),
    httpx.Response(200, json={"items": [{"name_elements": {"forename": "Julie"}}]}), httpx.Response(200, text="not json"),
])
def test_a_page_without_a_split_gives_none(response):
    client, calls = _appointments(response)
    assert ch.fetch_name_elements(client, "abc123") is None and len(calls) == 1


@mocked
def test_the_window_is_respected_for_the_name_request_too():
    slept = []
    client, calls = _appointments(httpx.Response(429, headers={"x-ratelimit-remain": "0", "x-ratelimit-reset": "0"}),
                                  _page(headers={"x-ratelimit-remain": "599", "x-ratelimit-reset": "0"}))
    assert ch.fetch_name_elements(client, "abc123", sleep=slept.append) == SPLIT
    assert len(calls) == 2 and len(slept) == 1


def _titled(oid="abc123", **over):
    item = _item(name="MERRICK, Julie, Dr", **over)
    item["links"] = {"officer": {"appointments": f"/officers/{oid}/appointments"}}
    return item


def test_only_serving_people_with_a_title_part_are_asked_and_each_officer_once():
    """1,049 of dev's 1,122 serving officers have no title part and cost
    nothing; an officer at three companies is asked once for the run."""
    from unittest.mock import patch
    plain = _item(name="SMITH, Jane Mary")
    left = _titled("left", resigned_on="2020-01-01")
    firm = {**_titled("firm"), "officer_role": "corporate-secretary"}
    here, again = _titled("abc123"), _titled("abc123")
    known: dict = {}
    with patch.object(ch, "fetch_name_elements", return_value=dict(SPLIT)) as fetch:
        assert ch.add_name_elements(None, [plain, left, firm, here], known) == 1
        assert ch.add_name_elements(None, [again], known) == 0          # the next company: known
    assert [c.args[1] for c in fetch.call_args_list] == ["abc123"]
    assert here["name_elements"] == SPLIT == again["name_elements"]
    assert all("name_elements" not in i for i in (plain, left, firm))


def test_a_split_that_could_not_be_read_leaves_the_title_part_out():
    from unittest.mock import patch
    item = _titled()
    with patch.object(ch, "fetch_name_elements", return_value=None):
        ch.add_name_elements(None, [item], {})
    assert "name_elements" not in item
    assert ch.read_officer(item)["full_name"] == "Julie Merrick"
