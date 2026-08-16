"""
Whether what a source found is in the country the user asked for.

One rule, in one place, because every instant source has to apply it and they
must all apply it the same way. A user who picks Germany and searches "Alphabet"
must not be handed Alphabet Inc of Mountain View — the sources have no idea a
country was chosen unless we tell them, and left to themselves each returns the
most famous company by that name.

**Unknown is a mismatch.** Asked for a company in Germany, a match that states
no country at all is not an answer to that question — and it is the same rule
the source-side searches already enforce for free: a Wikidata item without P17
simply is not in the index being searched. Making the checked sources agree with
the filtered ones is what keeps "found in Germany" meaning one thing.

The cost is real and worth stating: Deutsche Bank AG files with the SEC and
leaves `stateOfIncorporation` empty, so a German search will not turn it up
through EDGAR. A record that cannot say where it is cannot be the answer to
"where".
"""


def matches_requested(found: str | None, requested: str | None) -> bool:
    """Does a found country satisfy the requested one?

    - No country requested → everything matches; the filter is off.
    - Source can't say → rejected (see the module docstring).
    - Otherwise ISO 3166-1 alpha-2, compared case- and whitespace-insensitively.
    """
    want = (requested or "").strip().upper()
    if not want:
        return True
    return (found or "").strip().upper() == want


def country_mismatch(query: str, found: str | None, requested: str | None) -> dict:
    """The result a source returns when it found the wrong country.

    Shaped like every other source result — `total` is what the run log reads —
    so a rejection travels the same path as an empty search rather than needing
    its own handling at every call site.
    """
    return {"status": "country_mismatch", "query": query, "total": 0, "scraped": [],
            "found_country": found, "requested_country": requested}
