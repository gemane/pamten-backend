"""
Whether what a source found is in the country the user asked for.

One rule, in one place, because every instant source has to apply it and they
must all apply it the same way. A user who picks Germany and searches "Alphabet"
must not be handed Alphabet Inc of Mountain View — the sources have no idea a
country was chosen unless we tell them, and left to themselves each returns the
most famous company by that name.

**Unknown is not a mismatch.** A source that states no country for its match is
not claiming a different one, and many records legitimately have none (a
Wikidata item without P17 is common). Rejecting those would empty out the
sparse sources for no gain: the thing being defended against is a source
answering with a company that is demonstrably somewhere else.
"""


def matches_requested(found: str | None, requested: str | None) -> bool:
    """Does a found country satisfy the requested one?

    - No country requested → everything matches; the filter is off.
    - Source doesn't know → not a mismatch (see the module docstring).
    - Otherwise ISO 3166-1 alpha-2, compared case- and whitespace-insensitively.
    """
    want = (requested or "").strip().upper()
    if not want:
        return True
    got = (found or "").strip().upper()
    if not got:
        return True
    return got == want


def country_mismatch(query: str, found: str | None, requested: str | None) -> dict:
    """The result a source returns when it found the wrong country.

    Shaped like every other source result — `total` is what the run log reads —
    so a rejection travels the same path as an empty search rather than needing
    its own handling at every call site.
    """
    return {"status": "country_mismatch", "query": query, "total": 0, "scraped": [],
            "found_country": found, "requested_country": requested}
