"""An entity's places: one registry for the fields every address kind is
stored in (app.addresses, 2026-10-26)."""
import ast
import re
from pathlib import Path

from app import addresses
from app.addresses import HEADQUARTERS, REGISTERED


class TestTheRegistry:
    def test_the_stored_names_are_the_ones_the_database_always_had(self):
        assert addresses.fields(REGISTERED) == {
            "street": "reg_street", "city": "reg_city", "postcode": "reg_postcode",
            "full": "address", "key": "address_normalized",
            "lat": "reg_lat", "lng": "reg_lng", "precision": "reg_geo_precision"}
        assert addresses.fields(HEADQUARTERS) == {
            "street": "hq_street", "city": "hq_city", "postcode": "hq_postcode", "country": "hq_country",
            "full": "hq_address", "lat": "hq_lat", "lng": "hq_lng", "precision": "hq_geo_precision"}

    def test_the_registered_office_has_no_country_and_the_headquarters_no_key(self):
        # the entity's `country` is where it is registered; only the
        # registered office is the dedup's corroborator
        assert addresses.field(REGISTERED, "country") is None
        assert addresses.field(HEADQUARTERS, "key") is None

    def test_only_the_registered_pin_refuses_a_country_centroid(self):
        assert addresses.KINDS[REGISTERED].geocode_country is False
        assert addresses.KINDS[HEADQUARTERS].geocode_country is True

    def test_coordinates_are_the_numbers_and_every_field_is_listed_once(self):
        assert addresses.coordinate_fields() == ["reg_lat", "reg_lng", "hq_lat", "hq_lng"]
        every = addresses.all_fields()
        assert len(every) == len(set(every)) == 16


class TestParts:
    def test_what_a_source_states_and_the_key_from_the_readable_address(self):
        assert addresses.parts(REGISTERED, street="1 High St", city="Leeds", postcode="LS1",
                               country="GB", full="1 High St, Leeds, LS1") == {
            "reg_street": "1 High St", "reg_city": "Leeds", "reg_postcode": "LS1",
            "address": "1 High St, Leeds, LS1", "address_normalized": "1 high st leeds ls1"}  # no country field

    def test_what_it_does_not_state_is_left_out_unless_asked_for_as_none(self):
        assert addresses.parts(HEADQUARTERS, city="Omaha") == {"hq_city": "Omaha"}
        assert addresses.parts(HEADQUARTERS, city="Omaha", empty=("full", "country")) == {
            "hq_city": "Omaha", "hq_address": None, "hq_country": None}
        assert addresses.parts(REGISTERED, empty=("key",)) == {"address_normalized": None}
        assert addresses.parts(REGISTERED, street="") == {}               # '' is not a value

    def test_read_gives_back_what_parts_wrote(self):
        node = addresses.parts(HEADQUARTERS, street="1 Apple Park Way", city="Cupertino",
                               country="US", full="1 Apple Park Way, Cupertino") | {"hq_lat": 37.3}
        got = addresses.read(node, HEADQUARTERS)
        assert (got["street"], got["city"], got["country"], got["lat"], got["postcode"]) == (
            "1 Apple Park Way", "Cupertino", "US", 37.3, None)

    def test_the_cypher_clauses_name_their_parameters_after_the_fields(self):
        assert addresses.fill_clause("e", HEADQUARTERS, ("city", "country")) == (
            "e.hq_city = COALESCE(e.hq_city, $hq_city),\n    e.hq_country = COALESCE(e.hq_country, $hq_country)")
        assert addresses.create_map(HEADQUARTERS, ("city", "key")) == "hq_city: $hq_city"   # no field, no entry


# ── The ratchet ────────────────────────────────────────────────────────────────
# The stored names may be spelled only in app/addresses.py — and, until each is
# converted, in the files below as often as they are today (code strings, not
# docstrings or comments). A file spelling them more often, or a new file
# spelling them at all, fails: a writer reaches a place through the registry.

_NAMES = re.compile(r"\b(?:hq|reg)_(?:street|city|postcode|country|address|lat|lng|geo_precision)\b"
                    r"|\baddress_normalized\b")
_STILL_SPELLED = {
    # Wikidata's own result dictionary uses the stored names as its keys,
    # and the runner reads them off it
    "app/scraper/wikidata.py": 14,
    "app/scraper/runner.py": 6,
    # the index on the dedup corroborator and the ISO-2 rule on the
    # headquarters' country: decisions, stated where the schema states them
    "app/db/schema.py": 3,
    # a heal of an older database's field names (registered_address)
    "app/field_twins.py": 3,
    # the SEC country backfill's change report and Wikidata's answer keys
    "app/scraper/maintenance.py": 5,
}


def _spelled(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docs.add(id(first.value))
    return sum(len(_NAMES.findall(n.value)) for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs)


def test_no_module_spells_the_address_fields_more_than_it_did():
    app = Path(__file__).resolve().parents[1] / "app"
    problems = []
    for path in sorted(app.rglob("*.py")):
        rel = str(path.relative_to(app.parent))
        if rel == "app/addresses.py":
            continue
        n = _spelled(path)
        allowed = _STILL_SPELLED.get(rel, 0)
        if n > allowed:
            problems.append(f"{rel}: spells the address fields {n}× (allowed {allowed}) — use app.addresses")
    assert not problems, "\n".join(problems)
