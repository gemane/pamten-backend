"""Evidence that two persons of one name and birth month are one person, read
off the COMPANIES they are tied to (findings item 124, step 3).

A common name has a namesake in the same birth month as a matter of course, so
the month alone merges nobody under it. The register has no key for a person
of significant control — but the two companies say something:

- **a shared owner or officer**: somebody else is tied to both companies, as
  the same node or as two entries of one name and birth month (the co-owner's
  own duplicates). Measured on the full UK register against the identity-
  verification day, the cleanest evidence there is: of 173,756 such pairs with
  two verified entries 0.05 % differ in the day, against a floor of about 1 %;
- **one company owns the other**;
- **the same registered office** (its matching key) — unless the office is an agent's: 48 % of the
  5.6 million UK companies sit at one of 23,847 postcodes with more than 20
  companies each (`uk_mass_office_postcodes.txt`), and two namesakes can both
  be clients of one accountant.

It is evidence only BESIDE the same name and birth month: asked for a pair
whose birth dates agree, never for two undated bearers of a name.

Nothing here is personal data beyond what the graph holds: the companies'
addresses and the links between companies and people.
"""
from __future__ import annotations

import functools
import pathlib

from app import addresses

#: entities at one registered address in the graph above which it is an agent's
MASS_OFFICE_ENTITIES = 20
#: a shared owner or officer tied to more companies than this is an institution
#: (a fund manager, a group's holding company), not a sign of one person
CO_OWNER_MAX_COMPANIES = 50
#: a company with more ties than this is not read for whom it shares
COMPANY_MAX_TIES = 200
#: a person tied to more companies than this is not read at all (a nominee)
PERSON_MAX_COMPANIES = 50

#: the registered office's matching key and postcode, as the address registry names them
_OFFICE_KEY = addresses.field(addresses.REGISTERED, "key")
_OFFICE_POSTCODE = addresses.field(addresses.REGISTERED, "postcode")

CO_OWNER = "their companies share an owner or officer"
OWNS_OTHER = "one of their companies owns the other"
SAME_OFFICE = "their companies share a registered office"


@functools.lru_cache(maxsize=1)
def mass_office_postcodes() -> frozenset:
    """The UK postcodes that are the registered office of more than 20 live
    companies, without spaces, upper case."""
    path = pathlib.Path(__file__).parent / "scraper" / "data" / "uk_mass_office_postcodes.txt"
    return frozenset(line.strip() for line in path.read_text().splitlines()
                     if line.strip() and not line.startswith("#"))


def _postcode(value: str | None) -> str:
    return (value or "").replace(" ", "").upper()


class CompanySide:
    """The company-side evidence of one scan: reads each person's companies and
    their ties once, and answers `link(a, b)`."""

    def __init__(self, session):
        self.session = session
        self._people: dict[str, dict] = {}
        self._ties: dict[str, int] = {}
        self._at_office: dict[str, int] = {}

    # ── what is read ─────────────────────────────────────────────────────────

    def companies(self, pid: str) -> dict:
        """{company id: {"office", "mass", "entities", "around"}} for a person:
        ``around`` maps a token for everybody else tied to the company — the
        node, and for a dated person the name and birth month — to that node's
        (id, is a person); ``entities`` the companies it owns or is owned by."""
        if pid in self._people:
            return self._people[pid]
        from app.routers.persons import _birth_parts, _name_key
        out: dict = {}
        rows = list(self.session.run(
            "MATCH (x:Person {id: $id})-[]-(e:Entity) RETURN DISTINCT e.id AS id, "
            f"e.{_OFFICE_KEY} AS office, e.{_OFFICE_POSTCODE} AS postcode", id=pid))
        for row in rows if len(rows) <= PERSON_MAX_COMPANIES else []:
            ties = list(self.session.run(
                "MATCH (e:Entity {id: $eid})-[r:OWNS|HAS_ROLE]-(o) RETURN o.id AS id, o.full_name AS full_name, "
                f"o.birth_date AS birth, type(r) AS kind LIMIT {COMPANY_MAX_TIES + 1}", eid=row.get("id")))
            around: dict = {}
            entities: set = set()
            for t in ties if len(ties) <= COMPANY_MAX_TIES else []:
                oid, person = t.get("id"), t.get("full_name") is not None
                if not oid or oid == pid:
                    continue
                around[f"N|{oid}"] = (oid, person)
                parts = _birth_parts(t.get("birth")) if person else ()
                if person and len(parts) >= 2 and (key := _name_key(t.get("full_name"))):
                    around[f"P|{' '.join(key)}|{'-'.join(parts[:2])}"] = (oid, True)
                if not person and t.get("kind") == "OWNS":
                    entities.add(oid)
            office = (row.get("office") or "").strip() or None
            out[row.get("id")] = {"office": office, "mass": self._mass(office, row.get("postcode")),
                                  "entities": entities, "around": around}
        self._people[pid] = out
        return out

    def _mass(self, office: str | None, postcode: str | None) -> bool:
        if not office:
            return False
        if _postcode(postcode) in mass_office_postcodes():
            return True
        if office not in self._at_office:
            from app.db.arcadedb import run_sql
            self._at_office[office] = int((run_sql(
                f"SELECT count(*) AS n FROM Entity WHERE {_OFFICE_KEY} = :a", {"a": office}) or [{}])[0].get("n") or 0)
        return self._at_office[office] > MASS_OFFICE_ENTITIES

    def _tied_to(self, node: tuple) -> int:
        """How many companies a co-owner is tied to (capped just above the limit)."""
        oid, person = node
        if oid not in self._ties:
            label = "Person" if person else "Entity"
            rows = self.session.run(
                f"MATCH (o:{label} {{id: $id}})-[:OWNS|HAS_ROLE]-(e:Entity) RETURN DISTINCT e.id AS id "
                f"LIMIT {CO_OWNER_MAX_COMPANIES + 1}", id=oid)
            self._ties[oid] = len(list(rows))
        return self._ties[oid]

    # ── the answer ───────────────────────────────────────────────────────────

    def link(self, a: str, b: str) -> str | None:
        """What ties two persons through two DIFFERENT companies of theirs, or
        None: a shared owner or officer, an ownership between the companies,
        one registered office that is no agent's. (One company both are tied
        to is the scan's own "share a company".)"""
        ca, cb = self.companies(a), self.companies(b)
        found: set = set()
        for x, cx in ca.items():
            for y, cy in cb.items():
                if x == y:
                    continue
                for token in set(cx["around"]) & set(cy["around"]):
                    if max(self._tied_to(cx["around"][token]), self._tied_to(cy["around"][token])) \
                            <= CO_OWNER_MAX_COMPANIES:
                        return CO_OWNER                   # the cleanest: no need to look further
                if y in cx["entities"] or x in cy["entities"]:
                    found.add(OWNS_OTHER)
                if cx["office"] and cx["office"] == cy["office"] and not cx["mass"]:
                    found.add(SAME_OFFICE)
        return OWNS_OTHER if OWNS_OTHER in found else SAME_OFFICE if SAME_OFFICE in found else None
