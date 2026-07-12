"""Tests for the Entity.country full-name → ISO-2 migration (DB mocked)."""

from unittest.mock import patch

from app.scraper import maintenance


def _run(rows):
    return (
        patch.object(maintenance, "run_query", return_value=rows),
        patch.object(maintenance, "run_command"),
    )


def test_converts_full_names_to_iso2():
    rows = [{"country": "Brazil"}, {"country": "China"}, {"country": "United Kingdom"}]
    q, c = _run(rows)
    with q, c as cmd:
        result = maintenance.normalize_entity_countries()
    assert result["converted"] == [
        {"from": "Brazil", "to": "BR"},
        {"from": "China", "to": "CN"},
        {"from": "United Kingdom", "to": "GB"},
    ]
    assert result["skipped"] == 0
    assert cmd.call_count == 3
    stmt, params = cmd.call_args_list[0].args
    assert "SET e.country = $new" in stmt
    assert params == {"old": "Brazil", "new": "BR"}


def test_leaves_iso2_codes_untouched():
    q, c = _run([{"country": "BR"}, {"country": "AT"}])
    with q, c as cmd:
        result = maintenance.normalize_entity_countries()
    assert result["converted"] == []
    assert result["skipped"] == 2
    cmd.assert_not_called()


def test_leaves_unrecognized_values_untouched():
    q, c = _run([{"country": "Atlantis"}, {"country": ""}])
    with q, c as cmd:
        result = maintenance.normalize_entity_countries()
    assert result["converted"] == []
    assert result["skipped"] == 2
    cmd.assert_not_called()


def test_is_idempotent_second_run_converts_nothing():
    # after a first run the DB only contains codes
    q, c = _run([{"country": "BR"}, {"country": "CN"}, {"country": "GB"}])
    with q, c as cmd:
        result = maintenance.normalize_entity_countries()
    assert result["converted"] == []
    cmd.assert_not_called()


def test_converts_variant_spellings_and_mixed_case():
    rows = [
        {"country": "USA"},
        {"country": "Czechia"},
        {"country": "south korea"},
        {"country": "BRAZIL"},
    ]
    q, c = _run(rows)
    with q, c:
        result = maintenance.normalize_entity_countries()
    assert result["converted"] == [
        {"from": "USA", "to": "US"},
        {"from": "Czechia", "to": "CZ"},
        {"from": "south korea", "to": "KR"},
        {"from": "BRAZIL", "to": "BR"},
    ]


def test_canonicalizes_lowercase_and_padded_codes():
    q, c = _run([{"country": "br"}, {"country": " AT "}])
    with q, c:
        result = maintenance.normalize_entity_countries()
    assert result["converted"] == [
        {"from": "br", "to": "BR"},
        {"from": " AT ", "to": "AT"},
    ]
