"""Tests for the geocode backfill (DB + geocoder mocked).

A company has two places, and they are different questions: where it is run
(`hq_address` → `hq_lat`) and where it is registered (`address` → `reg_lat`).
BARCLAYS CAPITAL (CAYMAN) is registered at its agent's door on Grand Cayman and
run from London, which is the whole point of the map's Registered/Headquarters
switch — and for a long time the switch had nothing to draw, because only the HQ
pass existed.

So what these tests mostly pin is that the two passes stay separate: same
address columns in, different coordinate columns out, and neither silently
standing in for the other.
"""

from unittest.mock import patch

from app.scraper import geocode_backfill


def run(rows, *, target="hq", full=None, approx=(48.2, 16.37)):
    """Run one backfill against mocked rows, returning (result, commands)."""
    commands = []
    with patch.object(geocode_backfill, "run_query", return_value=rows), \
         patch.object(geocode_backfill, "run_command",
                      side_effect=lambda s, p=None: commands.append((s, p))), \
         patch.object(geocode_backfill, "geocode_full", return_value=full), \
         patch.object(geocode_backfill, "geocode_address", return_value=approx):
        result = geocode_backfill.backfill(target=target)
    return result, commands


class TestHeadquartersPass:
    def test_geocodes_entities_with_an_hq_but_no_coords(self):
        result, commands = run([{"id": "e1", "city": "Vienna", "country": "AT"}])
        assert result["geocoded"] == 1
        sets = [c for c in commands if "SET e.hq_lat" in c[0]]
        assert len(sets) == 1
        assert sets[0][1]["lat"] == 48.2 and sets[0][1]["lng"] == 16.37

    def test_writes_only_to_the_entity(self):
        """Nothing should touch a Location node — the type no longer exists, and a
        stray write against a missing type fails silently on a real database."""
        _, commands = run([{"id": "e1", "city": "Vienna", "country": "AT"}])
        assert not any("Location" in c[0] for c in commands)

    def test_prefers_a_full_address_over_city_and_country(self):
        """A street-level hit gives a real pin; city/country is only the fallback,
        and the precision is recorded so the map can show a circle instead."""
        rows = [{"id": "e1", "full": "1 A St, Berlin", "city": "Berlin", "country": "DE"}]
        with patch.object(geocode_backfill, "run_query", return_value=rows), \
             patch.object(geocode_backfill, "run_command") as cmd, \
             patch.object(geocode_backfill, "geocode_full",
                          return_value=((52.5, 13.4), "exact")) as full, \
             patch.object(geocode_backfill, "geocode_address") as approx:
            geocode_backfill.backfill(target="hq")
        full.assert_called_once()
        approx.assert_not_called()
        assert cmd.call_args[0][1]["prec"] == "exact"

    def test_skips_when_nothing_matches(self):
        result, commands = run([{"id": "e1", "city": "Nowhere", "country": "XX"}],
                               approx=None)
        assert result["geocoded"] == 0
        assert commands == []          # nothing written when geocoding fails


class TestRegisteredPass:
    def test_writes_the_registered_coordinates_not_the_hq_ones(self):
        # The bug this prevents: a registered pass that quietly overwrites the HQ
        # pin would move every company to its agent's office.
        _, commands = run([{"id": "e1", "full": "c/o Maples, Grand Cayman", "country": "KY"}],
                          target="registered", full=((19.3, -81.4), "approx"))
        assert "SET e.reg_lat" in commands[0][0]
        assert "hq_lat" not in commands[0][0]

    def test_selects_on_its_own_missing_coordinate(self):
        """Resumability is per pass: an entity with an HQ pin but no registered one
        must still be picked up, or the second pass can never catch up."""
        with patch.object(geocode_backfill, "run_query", return_value=[]) as q, \
             patch.object(geocode_backfill, "run_command"):
            geocode_backfill.backfill(target="registered")
        assert "e.reg_lat IS NULL" in q.call_args[0][0]

    def test_reads_the_display_address_not_the_normalised_one(self):
        """`registered_address` is lowercased and punctuation-stripped for dedup —
        "c o maples corporate services limited george town" geocodes worse than the
        display form it was derived from."""
        with patch.object(geocode_backfill, "run_query", return_value=[]) as q, \
             patch.object(geocode_backfill, "run_command"):
            geocode_backfill.backfill(target="registered")
        sql = q.call_args[0][0]
        assert "e.address AS full" in sql
        assert "registered_address" not in sql

    def test_does_NOT_fall_back_to_the_country(self):
        """No pin beats a wrong one.

        Geocoding a bare country returns its centroid, and the first run of this
        pass put 51 American companies in a field in Kansas and 39 British ones
        in the Irish Sea, each captioned as a registered office. An absent pin
        says "we do not know"; a centroid says something false. The country still
        shades on the map either way.
        """
        result, commands = run([{"id": "e1", "full": None, "country": "KY"}],
                               target="registered")
        assert result["geocoded"] == 0
        assert commands == []

    def test_does_not_even_ask_the_database_for_the_country(self):
        # Belt and braces: the column is not selected, so a future edit cannot
        # accidentally reintroduce the fallback by reading a value that is there.
        with patch.object(geocode_backfill, "run_query", return_value=[]) as q, \
             patch.object(geocode_backfill, "run_command"):
            geocode_backfill.backfill(target="registered")
        assert "null AS country" in q.call_args[0][0]

    def test_a_town_level_hit_from_the_address_is_still_kept(self):
        """'c/o Maples, George Town KY1-1104' resolves to Grand Cayman, not a
        building. Approximate but derived from the address, so it is a real
        place and worth a pin — unlike a country centroid."""
        result, commands = run([{"id": "e1", "full": "c/o Maples, George Town", "country": "KY"}],
                               target="registered", full=((19.3, -81.4), "approx"))
        assert result["geocoded"] == 1
        assert commands[0][1]["prec"] == "approx"


class TestBothPasses:
    def test_both_is_the_default(self):
        with patch.object(geocode_backfill, "run_query", return_value=[]) as q, \
             patch.object(geocode_backfill, "run_command"):
            result = geocode_backfill.backfill()
        # Forgetting the registered pass is invisible until someone switches the
        # map to Registered and finds the pins gone, so it must not need asking for.
        assert set(result["passes"]) == {"hq", "registered"}
        assert len(q.call_args_list) == 2

    def test_totals_add_the_passes_up(self):
        # The row needs a full address: the registered pass has no coarse
        # fallback, so city/country alone would geocode under HQ and not under
        # registered — which is correct, but would not exercise the sum.
        result, commands = run([{"id": "e1", "full": "1 A St, Vienna", "city": "Vienna",
                                 "country": "AT"}],
                               target="both", full=((48.2, 16.37), "exact"))
        assert result["passes"]["hq"]["geocoded"] == 1
        assert result["passes"]["registered"]["geocoded"] == 1
        assert result["geocoded"] == 2
        assert {"SET e.hq_lat" in c[0] for c in commands} == {True, False}

    def test_an_unknown_target_is_refused(self):
        # Rather than silently geocoding nothing, which would look like "no work
        # to do" in the import log.
        import pytest
        with pytest.raises(ValueError):
            geocode_backfill.backfill(target="hq_country")

    def test_the_limit_reaches_every_pass(self):
        with patch.object(geocode_backfill, "run_query", return_value=[]) as q, \
             patch.object(geocode_backfill, "run_command"):
            geocode_backfill.backfill(limit=25)
        assert all("LIMIT 25" in c.args[0] for c in q.call_args_list)
