"""EDGAR's bulk name list as a local lookup (findings item 135).

`cik-lookup-data.txt` lists every name EDGAR knows a filer by — companies and
people, current and former names — one "NAME:CIK:" line each, about a million
lines and 40 MB, rewritten daily. A person's name search against it is a local
match instead of one or two requests to browse-edgar: a rebuild asked that
search a few thousand times, and when EDGAR changed the page sizes it accepts
(item 132) every one of them failed.

Off unless ``SEC_NAME_LIST_DIR`` names a directory with a persistent disk. The
list is downloaded there at most once in `MAX_AGE_HOURS` and kept as a sorted
index file read through `mmap` — a lookup is a binary search on disk, and the
process holds none of the million names. With no list at hand (the setting
empty, the first download failed) the caller asks the live search as before.

Only the PERSON lookup reads it so far (`person_ciks`). The names are public
filer names; nothing is written to the graph from here — the list only says
which CIKs a name may be, and the caller's issuer check decides.
"""
import logging
import mmap
import os
import re
import tempfile
import time

log = logging.getLogger(__name__)

LIST_URL = "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt"
INDEX_FILE = "cik-lookup.idx"
#: EDGAR rewrites the list once a day
MAX_AGE_HOURS = 24
#: after a failed download the next one waits this long: a rebuild's thousands
#: of lookups must not each retry a 40 MB download
RETRY_AFTER_SECONDS = 900

_SEP = b"\t"
_last_failure = 0.0


def normalize(name: str) -> str:
    """A name as the index keys it: upper case, dots dropped ("J." is "J"),
    commas as spaces, one space between words."""
    return re.sub(r"\s+", " ", name.upper().replace(".", "").replace(",", " ")).strip()


def build_index(raw: bytes) -> bytes:
    """The list as EDGAR serves it → the index: one "NAME<tab>CIK" line per
    name, names normalised (`normalize`), sorted by their bytes. Lines without
    a name or a CIK (the list opens with four) are left out."""
    lines = set()
    for line in raw.decode("latin-1").splitlines():
        name, _, cik = line.rstrip(":").rpartition(":")
        name = normalize(name)
        if name and cik.isdigit():
            lines.add(name.encode("utf-8") + _SEP + cik.zfill(10).encode("ascii"))
    return b"\n".join(sorted(lines)) + b"\n"


def _download() -> bytes:
    from app.scraper import sec_edgar
    return sec_edgar._response(LIST_URL, {}).content


def index_path(directory: str | None = None, download=_download, now=time.time) -> str | None:
    """The index file, refreshed when older than `MAX_AGE_HOURS`; None when the
    list is off or there is none to read. A failed refresh keeps the file at
    hand — yesterday's list misses a filer registered today, nothing else."""
    global _last_failure
    if directory is None:
        from app.config import settings
        directory = settings.SEC_NAME_LIST_DIR
    if not directory:
        return None
    path = os.path.join(directory, INDEX_FILE)
    have = os.path.exists(path)
    if have and now() - os.path.getmtime(path) < MAX_AGE_HOURS * 3600:
        return path
    if now() - _last_failure < RETRY_AFTER_SECONDS:
        return path if have else None
    try:
        index = build_index(download())
        if index.count(b"\n") < 1000:               # an error page, not the list
            raise ValueError(f"{index.count(b'\n')} names")
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=INDEX_FILE + ".")
        with os.fdopen(fd, "wb") as f:
            f.write(index)
        os.replace(tmp, path)                       # atomic: a reader sees the old or the new
        log.info("SEC EDGAR: name list refreshed — %d names", index.count(b"\n"))
        return path
    except Exception as exc:  # noqa: BLE001 — the live search stands in
        _last_failure = now()
        log.warning("SEC EDGAR: name list not refreshed (%s)%s", exc,
                    "; using the one at hand" if have else "; asking the live search")
        return path if have else None


class _Lines:
    """The index's lines as a sequence, for `bisect`: line i is found by
    binary search over the file's bytes, never by reading it whole."""

    def __init__(self, data):
        self.data = data

    def line_at(self, offset: int) -> tuple[int, bytes]:
        """The first whole line starting at or after ``offset`` → (its start, the line)."""
        start = 0 if offset == 0 else self.data.find(b"\n", offset - 1) + 1
        if start == 0 and offset:
            return len(self.data), b""
        end = self.data.find(b"\n", start)
        return start, self.data[start:end if end >= 0 else len(self.data)]

    def first_at_or_after(self, key: bytes) -> int:
        """Offset of the first line that sorts at or after ``key``."""
        lo, hi = 0, len(self.data)
        while lo < hi:
            mid = (lo + hi) // 2
            start, line = self.line_at(mid)
            if start >= len(self.data) or line >= key:
                hi = mid
            else:
                lo = mid + 1
        return self.line_at(lo)[0]


def names_starting(path: str, prefix: str, limit: int = 200) -> list[tuple[str, str]]:
    """The (name, CIK) lines whose name starts with ``prefix``, as EDGAR's own
    name search matches: "DAVIS CHRIS" lists DAVIS CHRISTOPHER C, "COOK TIM"
    lists COOK TIMOTHY D — the short form of a given name is how Wikidata
    names many of them, and a match held to whole words lost them (9 of 300
    people measured)."""
    key = normalize(prefix).encode("utf-8")
    if not key:
        return []
    out: list[tuple[str, str]] = []
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        lines = _Lines(data)
        offset = lines.first_at_or_after(key)
        while offset < len(data) and len(out) < limit:
            _, line = lines.line_at(offset)
            name, _, cik = line.partition(_SEP)
            if not name.startswith(key):
                break
            out.append((name.decode("utf-8"), cik.decode("ascii")))
            offset += len(line) + 1
    return out


def person_ciks(words: list[str], limit: int, path: str | None = None) -> list[str] | None:
    """The filer CIKs a person's name may be, from the list; None when there
    is no list to ask (the caller asks the live search).

    As the live search does: the surname-first form first ("DENHOLM ROBYN"
    for Robyn Denholm), then the name as given (an SEC-styled name already is
    surname first); the first form that lists anybody answers. A name that is
    not a person's is left out — "GATES BILL & MELINDA FOUNDATION" is what
    the list holds under "Gates Bill", and a foundation's Form 4 about the
    issuer would have given Bill Gates its CIK."""
    from app.scraper.mapper import is_person_name
    path = path or index_path()
    if not path:
        return None
    queries = [f"{words[-1]} {words[0]}"] if len(words) > 1 else []
    queries.append(" ".join(words))
    for query in queries:
        found: list[str] = []
        for name, cik in names_starting(path, query):
            if cik not in found and is_person_name(name):
                found.append(cik)
        if found:
            return found[:limit]
    return []
