"""Wikidata label resolution.

Wikidata's label service does not return nothing when an item has no label in the
requested language — it returns **the QID as the label**. We asked for "en" only,
so any item without an English label was stored under its Q-number as a name:
HashiCorp's CEO arrived as "Q132983199" rather than David McJannet.

That is not a rare edge. Wikidata added the `mul` language code in 2024 for labels
identical across every language, which is exactly what a personal name is, so
newer person items increasingly have a `mul` label and no `en` one at all. A
QID stored as a name also poisons search_text and name_normalized, leaving the
record unfindable by the name it should have had.

Two defences, tested here: a language fallback chain, and a refusal to accept a
bare Q-number as a name whatever the chain returns.
"""
from app.scraper.wikidata import _LABEL_LANGUAGES, _LABEL_SERVICE, _label, _v


def row(**kw):
    """A SPARQL binding row: {"key": {"value": ...}}."""
    return {k: {"value": v} for k, v in kw.items()}


class TestTheFallbackChain:
    def test_english_is_preferred(self):
        assert _LABEL_LANGUAGES.split(",")[0] == "en"

    def test_mul_is_in_the_chain_and_early(self):
        """The whole reason this bug existed. `mul` must beat the regional
        languages, since a name identical in every language belongs there."""
        langs = _LABEL_LANGUAGES.split(",")
        assert "mul" in langs
        assert langs.index("mul") < langs.index("de")

    def test_the_service_clause_uses_the_chain(self):
        assert f'"{_LABEL_LANGUAGES}"' in _LABEL_SERVICE
        assert 'wikibase:language "en"' not in _LABEL_SERVICE   # the old, broken form


class TestTheQidBackstop:
    def test_a_real_label_passes_through(self):
        assert _label(row(ceoLabel="David McJannet"), "ceoLabel") == "David McJannet"

    def test_a_bare_qid_is_refused(self):
        """The exact value that reached the database."""
        assert _label(row(ceoLabel="Q132983199"), "ceoLabel") is None

    def test_a_missing_key_is_still_none(self):
        assert _label(row(), "ceoLabel") is None

    def test_a_name_that_merely_starts_with_q_is_kept(self):
        # Refusing anything beginning with Q would be worse than the bug.
        for name in ("Qualcomm", "Q Holdings Ltd", "Quinn", "Q8 Oils"):
            assert _label(row(itemLabel=name), "itemLabel") == name

    def test_a_qid_inside_a_longer_name_is_kept(self):
        assert _label(row(itemLabel="Studio Q12345 GmbH"), "itemLabel") == \
            "Studio Q12345 GmbH"

    def test_non_label_values_are_untouched(self):
        """`_v` stays raw: entity URIs legitimately contain a QID, and only the
        label sites route through the backstop."""
        uri = "http://www.wikidata.org/entity/Q132983199"
        assert _v(row(ceo=uri), "ceo") == uri


class TestNoLabelMeansNoRecord:
    """An officer we cannot name is left out, not recorded as a Q-number. A fake
    name is worse than a gap because it looks like data — and the writers already
    skip a falsy label, so returning None is what makes that happen."""

    def test_the_writer_skip_condition_sees_none(self):
        ceo = {"qid": "Q132983199", "label": _label(row(ceoLabel="Q132983199"), "ceoLabel")}
        assert not ceo.get("label")      # the guard in runner.run_wikidata_scrape

    def test_a_named_officer_is_not_skipped(self):
        ceo = {"qid": "Q132983199", "label": _label(row(ceoLabel="David McJannet"), "ceoLabel")}
        assert ceo.get("label")
