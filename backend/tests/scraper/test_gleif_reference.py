"""GLEIF reference code-list resolution (bundled ELF + RA lists)."""

from app.scraper.gleif_reference import legal_form_name, registration_authority_name


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
