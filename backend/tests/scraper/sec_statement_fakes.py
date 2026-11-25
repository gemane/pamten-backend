"""Fakes the 13D/G statement tests share: a `_get_text` that serves given pages and
404s on anything else, and the fixture reader. Not a test module."""




def _serve(atom: str, pages: dict):
    """A fake `_get_text` that 404s on anything it was not given.

    The old idiom fell back to the Atom feed for an unknown URL, so a code path
    that started requesting a new document (say `primary_doc.xml`) would be
    handed a feed, fail to parse it, quietly take the fallback branch, and the
    test would pass for the wrong reason. Unknown URLs now raise the way EDGAR
    does, which is what makes these tests able to say the legacy path was used.
    """
    import httpx

    def _get_text(url, params=None):
        if params is not None:      # the browse call is the only one with params
            return atom
        if url not in pages:
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", url),
                response=httpx.Response(404))
        return pages[url]
    return _get_text


def _fixture(name: str) -> str:
    """A real filing, trimmed to what the parser reads.

    Trimmed, never invented: the 13D/13G schema split was invisible for exactly
    as long as the fixtures were written from imagination.
    """
    import pathlib
    return (pathlib.Path(__file__).parent / "fixtures" / name).read_text()
