"""
Maps Wikidata instance QIDs to Pamten entity types.
"""

# Wikidata Q-ids for entity types we care about
INSTANCE_TYPE_MAP = {
    "Q4830453": "company",   # business
    "Q891723":  "company",   # public company
    "Q167037":  "company",   # corporation
    "Q6881511": "company",   # enterprise
    "Q783794":  "company",   # company
    "Q2659062": "company",   # organization
    "Q1616075": "company",   # media company
    "Q18388277":"company",   # technology company
    "Q219577":  "holding",   # holding company
    "Q431289":  "brand",     # brand
}


def infer_entity_type(instances: list) -> str:
    for qid in instances:
        if qid in INSTANCE_TYPE_MAP:
            return INSTANCE_TYPE_MAP[qid]
    return "company"


def parse_full_name(full_name: str) -> tuple:
    """Split 'First Last' → ('First', 'Last'). Handles single-word names."""
    if not full_name:
        return ("", "")
    parts = full_name.strip().split(" ", 1)
    if len(parts) == 1:
        return ("", parts[0])
    return (parts[0], parts[1])
