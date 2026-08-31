"""
The parity test: no OWNS writer may invent a property the schema does not know.

Eight drift bugs shared one shape — a property in one writer and not its
siblings, discovered by a person looking at two screens. This test makes the
comparison mechanical: it parses every OWNS write site out of the source,
extracts the property keys each writes, and fails with a readable diff when a
writer and the schema disagree.

Writers legitimately write SUBSETS (GLEIF has no stakes; PSC has no share
counts). The known subset per writer is encoded below, so the failure mode is
a diff against an explicit expectation — never a silent gap.
"""
import pathlib
import re

from app.scraper.edge_schema import OWNS_PROPS

BACKEND = pathlib.Path(__file__).resolve().parents[2]


def _owns_property_keys(path: str) -> dict[str, set]:
    """Every `[:OWNS {...}]` block in a file → {snippet_location: property_keys}.

    Handles both plain and f-string (double-brace) Cypher. A block whose body is
    generated from the schema (`{create_clause}`) is reported as GENERATED — the
    strongest possible answer.
    """
    text = (BACKEND / path).read_text()
    out: dict[str, set] = {}
    for m in re.finditer(r"\[r?:OWNS \{(\{)?(.*?)\}(\})?\]->", text, re.S):
        body = m.group(2)
        line = text[:m.start()].count("\n") + 1
        if "create_clause" in body:
            out[f"{path}:{line}"] = {"GENERATED"}
            continue
        keys = set(re.findall(r"([a-zA-Z_]+)\s*:\s*\$", body))
        if keys:
            out[f"{path}:{line}"] = keys
    return out


#: Every file that writes an OWNS edge, with the subset each writer is KNOWN to
#: carry. "GENERATED" means the clause comes from the schema — the ideal state.
#: Editing this dict is deliberate friction: a writer gaining or losing a
#: property must be acknowledged here, in review, rather than drifting.
EXPECTED: dict[str, list] = {
    # The module split moved both OWNS writers out of runner: the shared one to
    # graph_writer, the SEC one to sec_writer. Same two sites, new addresses.
    "app/scraper/graph_writer.py": [
        {"GENERATED"},        # _upsert_owns (the shared writer)
    ],
    "app/scraper/sec_writer.py": [
        {"GENERATED"},        # _upsert_owns_sec
    ],
    "app/scraper/maintenance.py": [
        {"GENERATED"},        # entity merge, outgoing
        {"GENERATED"},        # entity merge, incoming
        {"GENERATED"},        # person merge
    ],
    "app/scraper/gleif_incremental.py": [
        # The delta CREATE: consolidation facts only — GLEIF states no stakes.
        # ownership_type is a literal 'controlling', which the extractor cannot
        # see (it reads $params); the fold path adds also_ultimate/ultimate_*
        # via targeted SETs, which are safe by construction and out of scope.
        {"direct_or_indirect", "interest_types", "source_id",
         "credibility_score", "source_url", "since", "last_scraped_at"},
    ],
    "app/routers/relationships.py": [
        {"stake_percent", "ownership_type", "since", "until", "source_id",
         "credibility_score", "source_url", "source_date", "last_scraped_at",
         "value_usd"},
    ],
    # federation.py MERGEs then SETs (no property-map CREATE) and persons.py
    # builds its clause from local lists — both checked by the schema-membership
    # test below via their SET/list keys rather than a CREATE map.
    "app/routers/persons.py": [],
}


def test_every_owns_writer_stays_inside_the_schema():
    """No writer may put a property on an OWNS edge that OWNS_PROPS lacks —
    because the merge paths recreate edges from the schema, and an unknown
    property would silently vanish on the first dedup."""
    problems = []
    for path in EXPECTED:
        for loc, keys in _owns_property_keys(path).items():
            unknown = keys - set(OWNS_PROPS) - {"GENERATED"}
            if unknown:
                problems.append(f"{loc} writes {sorted(unknown)} — not in OWNS_PROPS")
    assert not problems, "\n".join(problems)


def test_each_writer_carries_exactly_its_known_subset():
    """Drift becomes a diff. A writer gaining or losing a property fails here
    with both sides printed, and the fix is either the writer or the EXPECTED
    entry — a decision made in review, not an accident."""
    problems = []
    for path, expected_blocks in EXPECTED.items():
        found = list(_owns_property_keys(path).items())
        if len(found) != len(expected_blocks):
            problems.append(
                f"{path}: {len(found)} OWNS write sites, expected "
                f"{len(expected_blocks)} — a writer was added or removed")
            continue
        for (loc, keys), expected in zip(found, expected_blocks):
            if keys != expected:
                gained = sorted(keys - expected)
                lost = sorted(expected - keys)
                problems.append(f"{loc}: gained {gained}, lost {lost}")
    assert not problems, "\n".join(problems)


def test_the_bulk_and_psc_writers_stay_inside_the_schema():
    """These build property DICTS rather than Cypher, so they are checked at
    the dict level: the literal keys in their edge-prop builders must all be
    schema properties."""
    sources = {
        # module-level function, not a method; capture through the props dict.
        "app/scraper/bulk_import.py": r"def _owns\(batch,(.*?)(?:\ndef |\nclass )",
        # Both PSC writers (bulk and incremental) consume PscMapped.edge_props
        # — the shared mapper is exactly why those two never drift from each
        # other — so checking the mapper checks them both.
        "app/scraper/companies_house_psc.py": r"edge_props\s*=\s*\{(.*?)\n\s{8}\}",
        # federation's MERGE-then-SET writer: keys are the r.x = targets.
        "app/routers/federation.py": r'MERGE \(a\)-\[r:OWNS\]->\(b\)(.*?)counts\["ownerships"\]',
    }
    problems = []
    for path, pattern in sources.items():
        text = (BACKEND / path).read_text()
        m = re.search(pattern, text, re.S)
        assert m, f"{path}: writer block not found — pattern needs updating"
        keys = set(re.findall(r'"([a-z_]+)":', m.group(1)))
        keys |= set(re.findall(r"([a-z_]+)\s*:\s*\$", m.group(1)))
        keys |= set(re.findall(r"r\.([a-z_]+)\s*=", m.group(1)))
        assert keys, f"{path}: the pattern matched but extracted no keys — vacuous"
        unknown = keys - set(OWNS_PROPS) - {"psc"}  # 'psc' = param prefix noise
        unknown = {k for k in unknown if not k.startswith("_")}
        if unknown - {"extra", "kind", "self_link", "company_id", "owner_id",
                      "owner_label", "owner_props", "company_props", "id"}:
            problems.append(f"{path}: {sorted(unknown)} not in OWNS_PROPS")
    assert not problems, "\n".join(problems)


def test_claim_props_knows_every_fact_a_claim_should_record():
    """claims lagged the edge schema once already (the share counts — instance
    #8). The claim is the per-source record of the SAME assertion, so every
    factual OWNS property belongs on it; provenance-mechanics fields do not."""
    from app.claims import claim_props

    import inspect
    claim_fields = set(inspect.signature(claim_props).parameters)
    factual = set(OWNS_PROPS) - {
        # mechanics of the edge, not of the assertion:
        "last_scraped_at", "stale", "shortcut", "interest_types",
        "direct_or_indirect", "psc_self_link", "also_ultimate",
        "ultimate_since", "ultimate_until", "until_reason", "value_usd",
        "file_date",
    }
    missing = factual - claim_fields
    assert not missing, (f"claim_props lacks {sorted(missing)} — a claim that "
                         f"cannot record these cannot be rechecked")
