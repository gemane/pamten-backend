"""GLEIF reference code-list resolution (bundled ELF + RA lists)."""

from app.scraper.gleif_reference import (legal_form_name, make_register_id,
    registration_authority_name, register_for_place)


class TestLegalForm:
    def test_known_code(self):
        assert legal_form_name("H0PO") == "Private Limited Company"

    def test_unknown_or_empty(self):
        assert legal_form_name("ZZZZ") is None
        assert legal_form_name(None) is None
        assert legal_form_name("") is None


class TestRegistrationAuthority:
    def test_known_code(self):
        assert registration_authority_name("RA000585") == "Companies Register"

    def test_unknown_or_empty(self):
        assert registration_authority_name("RA999999") is None
        assert registration_authority_name(None) is None
        assert registration_authority_name("") is None


def test_lists_are_non_trivial():
    # guards against a truncated/empty bundle slipping in
    from app.scraper.gleif_reference import _load
    assert len(_load("gleif_elf.json")) > 2000
    assert len(_load("gleif_ra.json")) > 500


class TestSoleRegisterForCountry:
    def test_a_country_with_exactly_one_register_maps(self):
        from app.scraper.gleif_reference import sole_register_for_country
        # Vatican City has a single register in the RA list — the shape of
        # country the conservative mapping exists for.
        assert sole_register_for_country("VA") is not None

    def test_multi_register_countries_map_to_nothing(self):
        from app.scraper.gleif_reference import sole_register_for_country
        # GB has 16 registers; DE has 177 per-court ones sharing HRB numbering —
        # the exact case where a bare country+number key would merge strangers.
        assert sole_register_for_country("GB") is None
        assert sole_register_for_country("DE") is None

    def test_case_and_whitespace_tolerant(self):
        from app.scraper.gleif_reference import sole_register_for_country
        assert sole_register_for_country(" va ") == sole_register_for_country("VA")
        assert sole_register_for_country(None) is None
        assert sole_register_for_country("") is None

    def test_multi_country_codes_count_once_per_country(self):
        # OHADA's RCCM (RA000814) serves 8 countries: each of those countries
        # must see it as ONE register there, not as eight.
        from app.scraper.gleif_reference import _load, _sole_registers
        ohada = _load("gleif_ra.json")["RA000814"]
        assert len(ohada["countries"]) > 1
        sole = _sole_registers()
        for country in ohada["countries"]:
            if sole.get(country) == "RA000814":
                break
        else:  # pragma: no cover - only reached if the bundle changes shape
            raise AssertionError("no OHADA country resolves to the shared code")


class TestMakeRegisterId:
    def test_the_happy_path(self):
        from app.scraper.gleif_reference import make_register_id
        assert make_register_id("RA000585", "07524813") == "RA000585:07524813"

    def test_whitespace_in_the_number_is_removed(self):
        # "HRB 12345" and "HRB12345" are the same registration
        from app.scraper.gleif_reference import make_register_id
        assert make_register_id("RA000242", "HRB 12345") == "RA000242:HRB12345"
        assert make_register_id(" ra000242 ", "HRB\t12 345") == "RA000242:HRB12345"

    def test_case_and_leading_zeros_are_preserved(self):
        from app.scraper.gleif_reference import make_register_id
        assert make_register_id("RA000548", "CHE-105.962.823") == "RA000548:CHE-105.962.823"
        assert make_register_id("RA000585", "00048839") == "RA000585:00048839"

    def test_missing_parts_yield_none(self):
        from app.scraper.gleif_reference import make_register_id
        assert make_register_id(None, "123") is None
        assert make_register_id("RA000585", None) is None
        assert make_register_id("", "123") is None
        assert make_register_id("RA000585", "  ") is None

    def test_placeholder_codes_never_identify(self):
        """RA999999 = self-registered: two unrelated companies both numbered
        '123' under it would merge. Guarded in code, not just in the bundle."""
        from app.scraper.gleif_reference import make_register_id
        for code in ("RA777777", "RA888888", "RA999999", "ra999999"):
            assert make_register_id(code, "123") is None


class TestZeroInsensitiveNumbers:
    """Leading zeros are identity in some registers and formatting in others —
    the audited US states are the ONLY place we say formatting."""

    def test_the_two_texas_spellings_converge(self):
        # GLEIF publishes 0805587591; the Companies House filer wrote 805587591.
        assert make_register_id("RA000637", "0805587591") == "RA000637:805587591"
        assert make_register_id("RA000637", "805587591") == "RA000637:805587591"

    def test_everyone_else_keeps_their_zeros(self):
        assert make_register_id("RA000585", "07524813") == "RA000585:07524813"

    def test_non_numeric_numbers_are_untouched_even_in_texas(self):
        assert make_register_id("RA000637", "0X1") == "RA000637:0X1"

    def test_all_zeros_survive_as_zero(self):
        assert make_register_id("RA000602", "000") == "RA000602:0"


class TestRegisterForPlace:
    """The place-level counterpart of the sole-register rule: "USA" names 64
    registers, "Delaware" names exactly one."""

    def test_the_state_name_resolves(self):
        assert register_for_place("US", "Delaware") == "RA000602"

    def test_the_forms_sources_actually_use(self):
        assert register_for_place("US", "State of Delaware") == "RA000602"
        assert register_for_place("US", "Delaware, USA") == "RA000602"
        assert register_for_place("US", "DE") == "RA000602", "the us_de form"

    def test_a_state_name_implies_the_country(self):
        # A PSC's country_registered saying "Delaware" IS the country statement.
        assert register_for_place(None, "Delaware") == "RA000602"

    def test_the_curated_overrides_pick_the_corporate_register(self):
        # Texas lists trust-company and credit-union registries beside the
        # Corporations Section; the override picks the one companies live in.
        assert register_for_place("US", "Texas") == "RA000637"
        assert register_for_place("US", "New York") == "RA000628"

    def test_the_bavaria_trap_stays_shut(self):
        # Bavaria's only listed register is a Foundations Directory — an HRB
        # number stamped there would be a false merge key. Unaudited countries
        # never mint from places.
        assert register_for_place("DE", "Bavaria") is None

    def test_unknown_places_yield_none(self):
        assert register_for_place("US", "Atlantis") is None
        assert register_for_place("US", "") is None
        assert register_for_place(None, None) is None


class TestBundleIntegrity:
    """The shipped gleif_ra.json, regenerated by scripts/build_gleif_ra_bundle.py."""

    def test_every_entry_has_a_name_and_iso2_countries(self):
        from app.scraper.gleif_reference import _load
        bundle = _load("gleif_ra.json")
        for code, entry in bundle.items():
            assert isinstance(entry, dict) and entry.get("name"), code
            for c in entry.get("countries", []):
                assert len(c) == 2 and c == c.upper(), (code, c)

    def test_placeholder_codes_are_absent(self):
        from app.scraper.gleif_reference import _load
        bundle = _load("gleif_ra.json")
        assert not {"RA777777", "RA888888", "RA999999"} & set(bundle)

    def test_a_stale_flat_bundle_degrades_not_crashes(self, monkeypatch):
        """If an old {code: name} bundle ever ships, names still resolve and the
        sole-register map is empty — no register_id is worse than a crash."""
        import app.scraper.gleif_reference as ref
        monkeypatch.setattr(ref, "_load", lambda name: {"RA000585": "Companies Register"})
        ref._sole_registers.cache_clear()
        try:
            assert ref.registration_authority_name("RA000585") == "Companies Register"
            assert ref.sole_register_for_country("GB") is None
        finally:
            ref._sole_registers.cache_clear()
