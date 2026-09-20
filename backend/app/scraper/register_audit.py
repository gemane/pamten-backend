"""Which register a country's companies actually sit on — audited from GLEIF.

The registration-authority table lists every register GLEIF knows for a
country: the Netherlands has four, Japan four, Germany 176. Only 25 of 229
countries list exactly one, so a source that states a country and a register
number (a Companies House PSC record for a foreign controller) could mint a
`register_id` for almost nobody — while in most of those countries GLEIF's own
records sit on ONE general corporate register nearly every time; the rest are
sector registries (funds, supervisors, pension bodies) beside it.

This module measures that on the full LEI-CDF golden copy: for every country,
the share of GENERAL entities on each register and the shape of their numbers.
A country enters the map only where one register clearly dominates
(`min_share`) over enough records (`min_records`); genuinely split countries
— Germany per court, the US and Canada per state, Spain, Brazil — fail the
share test and stay out, as the Bavaria trap demands. The output JSON is
committed as data and read by `gleif_reference.general_register_for_country`.

Scanning is a regex over the decompressed byte stream, not a JSON parse: the
pure-Python ijson walk of 3.4M records takes a quarter of an hour, the regex
a couple of minutes, and the three fields sit in a fixed order in every record.
"""
from __future__ import annotations

import collections
import datetime as _dt
import json
import logging
import os
import re
import zipfile
from typing import IO, Iterator

log = logging.getLogger(__name__)

#: RegistrationAuthority block, then LegalJurisdiction, then (optionally)
#: EntityCategory — the schema's order, pretty-printed by GLEIF.
_RECORD_RX = re.compile(
    rb'"RegistrationAuthorityID"\s*:\s*\{\s*"\$"\s*:\s*"(?P<ra>[^"]+)"\s*\}'
    rb'(?:\s*,\s*"RegistrationAuthorityEntityID"\s*:\s*\{\s*"\$"\s*:\s*"(?P<num>[^"]*)"\s*\})?'
    rb'\s*\}\s*,\s*"LegalJurisdiction"\s*:\s*\{\s*"\$"\s*:\s*"(?P<jur>[^"]+)"\s*\}'
    rb'(?:\s*,\s*"EntityCategory"\s*:\s*\{\s*"\$"\s*:\s*"(?P<cat>[^"]+)"\s*\})?'
)
_TAIL = 4096            # longer than any one record's three fields
_CHUNK = 8 << 20

#: Placeholder codes GLEIF uses when no register applies.
_PLACEHOLDERS = {"RA888888", "RA999999"}


def _open(path: str) -> IO[bytes]:
    if path.lower().endswith(".zip"):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not names:
            raise ValueError(f"No .json file inside {path}")
        return zf.open(names[0])
    return open(path, "rb")


def _shape(number: str) -> str:
    """"CHE-105.909.036" → "AAA-999.999.999": the register's numbering style."""
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", number))


def scan_registrations(path: str) -> Iterator[tuple[str, str, str | None, str]]:
    """Yield (country ISO-2, RA code, number, category) per record that states a
    register. Country is the jurisdiction's country part ("US-DE" → "US")."""
    raw = _open(path)
    buf = b""
    while True:
        chunk = raw.read(_CHUNK)
        if not chunk:
            break
        buf += chunk
        last = 0
        for m in _RECORD_RX.finditer(buf):
            last = m.end()
            jur = m.group("jur").decode()
            yield (jur.split("-")[0][:2].upper(), m.group("ra").decode(),
                   (m.group("num") or b"").decode() or None,
                   (m.group("cat") or b"GENERAL").decode())
        buf = buf[max(last, len(buf) - _TAIL):] if last else buf[-_TAIL:]
    raw.close()


def audit_registers(path: str, min_share: float = 0.90, min_records: int = 200) -> dict:
    """The general-register map plus the audit that justifies it.

    Counts GENERAL entities only — a fund sits on a fund register by design and
    would vote for the wrong one. Returns {"generated", "source", "min_share",
    "min_records", "countries": {iso: {...}}, "rejected": {iso: {...}}} where each
    country carries the dominant register, its share, the record count, the
    number shapes seen on it and the runner-up — enough to re-check by hand.
    """
    by_country: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    shapes: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    scanned = 0
    for country, ra, number, category in scan_registrations(path):
        scanned += 1
        if category != "GENERAL" or ra in _PLACEHOLDERS or not country:
            continue
        by_country[country][ra] += 1
        if number:
            shapes[(country, ra)][_shape(number)] += 1
    countries: dict[str, dict] = {}
    rejected: dict[str, dict] = {}
    for country, counter in sorted(by_country.items()):
        total = sum(counter.values())
        (top, n_top), *rest = counter.most_common(2) + [(None, 0)]
        entry = {
            "code": top, "share": round(n_top / total, 4), "records": total,
            "registers_seen": len(counter),
            "shapes": [s for s, _ in shapes[(country, top)].most_common(3)],
            "runner_up": ([rest[0][0], round(rest[0][1] / total, 4)] if rest and rest[0][0] else None),
        }
        if total >= min_records and n_top / total >= min_share:
            countries[country] = entry
        else:
            rejected[country] = entry
    log.info("register audit: %s records scanned, %d countries mapped, %d rejected",
             f"{scanned:,}", len(countries), len(rejected))
    return {
        "generated": _dt.date.today().isoformat(),
        "source": os.path.basename(path),
        "records_scanned": scanned,
        "min_share": min_share,
        "min_records": min_records,
        "countries": countries,
        "rejected": rejected,
    }


def write_audit(result: dict, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def audit_psc_registers(path: str, top: int = 40) -> dict:
    """What each register rule catches among the FOREIGN corporate controllers
    of a Companies House PSC snapshot — the measure behind the alias table.

    Per country: controllers with a number, how many each rule keyed (named /
    sole / place / format / general), how many stayed unkeyed, and the
    phrasings those unkeyed filers used most — the next aliases to add.
    """
    from app.scraper.companies_house_psc import _psc_country, resolve_register_code, REGISTER_RULES

    by_country: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    unkeyed: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    total = foreign = 0
    zf = zipfile.ZipFile(path)
    name = next(n for n in zf.namelist() if n.lower().endswith((".txt", ".json")))
    with zf.open(name) as fh:
        for line in fh:
            if b"corporate-entity" not in line:
                continue
            try:
                data = json.loads(line).get("data") or {}
            except ValueError:
                continue
            if not (data.get("kind") or "").startswith("corporate-entity"):
                continue
            total += 1
            ident = data.get("identification") or {}
            country_text = (ident.get("country_registered") or "").strip()
            iso2 = _psc_country(ident)
            if iso2 == "GB" or (not country_text and not iso2):
                continue
            foreign += 1
            key = iso2 or f"?{country_text[:30]}"
            number = (ident.get("registration_number") or "").strip()
            if not number or number.upper() in ("N/A", "NA", "NONE", "-"):
                by_country[key]["no_number"] += 1
                continue
            by_country[key]["with_number"] += 1
            code, rule = resolve_register_code(iso2, ident, number) if iso2 else (None, None)
            if rule:
                by_country[key][rule] += 1
            else:
                by_country[key]["unkeyed"] += 1
                unkeyed[key][(ident.get("place_registered") or "").strip()[:50] or "∅"] += 1
    countries = {}
    for c, cnt in sorted(by_country.items(), key=lambda kv: -kv[1]["with_number"])[:top]:
        countries[c] = {**{r: cnt.get(r, 0) for r in REGISTER_RULES},
                        "with_number": cnt.get("with_number", 0), "no_number": cnt.get("no_number", 0),
                        "unkeyed": cnt.get("unkeyed", 0),
                        "unkeyed_phrasings": unkeyed[c].most_common(6)}
    keyed = sum(sum(cnt.get(r, 0) for r in REGISTER_RULES) for cnt in by_country.values())
    with_number = sum(cnt.get("with_number", 0) for cnt in by_country.values())
    return {"corporate": total, "foreign": foreign, "with_number": with_number, "keyed": keyed,
            "by_rule": {r: sum(cnt.get(r, 0) for cnt in by_country.values()) for r in REGISTER_RULES},
            "countries": countries}
