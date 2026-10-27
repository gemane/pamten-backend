"""An LEI confirmed by the filer's own filings — the bridge between a GLEIF
node (LEI, no CIK) and the EDGAR filer of the same name (CIK, no LEI).

EDGAR's submissions document carries an LEI for few operating companies, and
Wikidata states a CIK for few foreign filers, so a company can end up as two
nodes that share no hard id: Unilever's Companies House node (LEI
549300MKFYEKVRWML317) and EDGAR's UNILEVER PLC (CIK 0000217410), kept apart by
nothing a merge may rely on (2026-10-27). The filer itself settles it. Its
own filings state its LEI wherever a European or bond-market rule asks for one
— Unilever's directors'-dealing notices ("Name Unilever PLC · LEI 5493…"),
Berkshire's and Alphabet's note prospectuses ("Issuer … LEI …") — and EDGAR's
full-text search finds them under the filer's CIK.

A search hit only says the code occurs somewhere in a document of that filer.
A financing subsidiary's prospectus names its parent as guarantor, LEI and
all, so the document is read too:

- every 20-character code in it that passes the ISO 17442 check (mod 97-10)
  is an LEI — a look-alike string fails the checksum;
- the node's LEI as the ONLY LEI of a document filed by the filer alone
  confirms it;
- among several LEIs — or in a document several filers share, as an issuer
  and its guarantor do (Kraft Heinz Foods Co and Kraft Heinz Co, one 424B5
  under both CIKs) — it confirms only when the filer's name labels the LEI:
  the name, then nothing but legal-form and label words up to the code.
  "Sysco Corporation’s Legal Entity Identifier is 5RGW…" is Sysco Corp's;
  "Sysco Holdings’ Legal Entity Identifier is 2549…", in the same
  prospectus, is not, and Fiserv's "Issuer Legal Entity Identifier …"
  names nobody.

Asked only where a name already proposes the pair (the SEC scrape and the
entity dedup). A miss or an error is "not confirmed", never "a different
company": many filers state their LEI in no filing at all (Apple has none).
"""

from __future__ import annotations

import logging
import re
import unicodedata

import httpx

log = logging.getLogger(__name__)

#: The documents read per check. The newest hits are enough: a filer that
#: states its LEI states it in every notice of the kind.
MAX_DOCUMENTS = 2

#: How far before the LEI the filer's name may stand, in characters of the
#: tag-stripped text. "Details of the issuer … a) Name Unilever PLC b) LEI"
#: is about 60; a table row in a prospectus about as much.
NAME_WINDOW = 300

_LEI_SHAPE = re.compile(r"\b[A-Z0-9]{18}[0-9]{2}\b")

#: The words that may stand between a name and the LEI it labels: legal forms
#: ("Unilever PLC b) LEI") and the label itself ("Sysco Corporation’s Legal
#: Entity Identifier is"). Not "group" or "holdings", which the issuer
#: matcher drops as noise: here they name a different company — Sysco
#: Holdings beside Sysco Corporation. Single letters ("b)", "S.A.") never count.
_LABEL_WORDS = frozenset({"lei", "legal", "entity", "identifier", "code", "is", "number", "no"})


def _form_words() -> frozenset:
    from app.scraper.sec_edgar import _LEGAL_FORM_WORDS
    return frozenset(_LEGAL_FORM_WORDS) - {"group", "holding", "holdings"} | {"the"}


def lei_checksum_ok(code: str | None) -> bool:
    """Whether a 20-character code passes the ISO 17442 check digits
    (ISO 7064 mod 97-10: letters as 10…35, the number mod 97 is 1)."""
    if not code or len(code) != 20 or not code.isalnum():
        return False
    digits = "".join(str(int(ch, 36)) for ch in code.upper())
    return int(digits) % 97 == 1


def leis_in(text: str) -> list[tuple[str, int]]:
    """Every checksum-valid LEI in a text, with where it starts."""
    return [(m.group(), m.start()) for m in _LEI_SHAPE.finditer(text or "")
            if lei_checksum_ok(m.group())]


def _tokens(text: str) -> list[str]:
    """Lower-case words, accents folded, of two letters or more."""
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", text or "")
                     if not unicodedata.combining(ch)).lower()
    return [t for t in re.findall(r"[^\W_]+", folded) if len(t) > 1]


def name_labels(window: str, name: str) -> bool:
    """Whether `name` is the last name in `window` and labels what follows:
    its words in order, then only legal-form and label words to the end."""
    forms = _form_words()
    wanted = [t for t in _tokens(name) if t not in forms]
    if not wanted:
        return False
    words = _tokens(window)
    tail_ok = forms | _LABEL_WORDS
    for i in range(len(words) - len(wanted), -1, -1):
        if words[i:i + len(wanted)] == wanted:
            return all(t in tail_ok for t in words[i + len(wanted):])
    return False


def document_states_lei(text: str, lei: str, names: list[str], sole_filer: bool = True) -> bool:
    """Whether a filing's text states `lei` as the filer's own: the only LEI
    of a document the filer filed alone, or one the filer's name (any of
    `names`) labels within `NAME_WINDOW` characters before it."""
    found = leis_in(text)
    at = [pos for code, pos in found if code == lei]
    if not at:
        return False
    if sole_filer and {code for code, _ in found} == {lei}:
        return True
    # Another LEI between the name and this one is a word no label holds, so
    # a name labels only the code that follows it — never the next row's.
    return any(name_labels(text[max(pos - NAME_WINDOW, 0):pos], n)
               for pos in at for n in names if n)


def _document_url(cik: str, hit_id: str) -> str | None:
    """`0000217410-23-000039:a991-directorpdmrshareho.htm` → the Archives
    URL of that document (immutable, so `sec_cache` keeps it)."""
    from app.scraper.sec_edgar import _cik_int
    accession, _, filename = (hit_id or "").partition(":")
    if not accession or not filename:
        return None
    return (f"https://www.sec.gov/Archives/edgar/data/{_cik_int(cik)}/"
            f"{accession.replace('-', '')}/{filename}")


def confirm_filer_lei(cik: str, lei: str, names: list[str],
                      max_documents: int = MAX_DOCUMENTS) -> dict | None:
    """The filing of filer `cik` that states `lei` as its own, or None.

    Returns ``{cik, lei, source_url, form, file_date}`` — the document is the
    provenance the caller stores. Only hits filed under the CIK count, and
    at most `max_documents` are read; a hit other filers share needs the
    filer's name on the LEI. None means "not confirmed": no hit, a
    document that names the LEI only beside someone else, or EDGAR
    unreachable — never evidence that the two are different companies."""
    from app.scraper.mapper import normalize_cik
    from app.scraper.sec_edgar import SEARCH_URL, _get, _get_text, _plain_text
    cik = normalize_cik(cik)
    lei = (lei or "").strip().upper()
    if not cik or not lei_checksum_ok(lei):
        return None
    try:
        data = _get(SEARCH_URL, {"q": f'"{lei}"', "ciks": cik})
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("SEC LEI: search for %s under CIK %s failed: %s", lei, cik, exc)
        return None
    read = 0
    for hit in (data.get("hits") or {}).get("hits") or []:
        if read >= max_documents:
            break
        src = hit.get("_source") or {}
        if cik not in (src.get("ciks") or []):
            continue
        url = _document_url(cik, hit.get("_id"))
        if not url:
            continue
        read += 1
        try:
            text = _plain_text(_get_text(url))
        except httpx.HTTPError as exc:
            log.warning("SEC LEI: could not read %s: %s", url, exc)
            continue
        if document_states_lei(text, lei, names, sole_filer=src.get("ciks") == [cik]):
            log.info("SEC LEI: CIK %s states LEI %s in %s", cik, lei, url)
            return {"cik": cik, "lei": lei, "source_url": url,
                    "form": src.get("form"), "file_date": src.get("file_date")}
    return None
