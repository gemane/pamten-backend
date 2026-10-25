"""The vertex types a node id can live in (app.db.anchors)."""


def test_a_voting_group_is_its_own_vertex_type():
    """Since 2026-10-26 a 13D filing group is a VotingGroup, not an Entity of
    type `voting_group` that every legal-person query had to fence off; the
    label helpers know all three types and coerce anything else to Entity."""
    from app.db.anchors import NODE_LABELS, label_or_entity
    assert NODE_LABELS == ("Entity", "Person", "VotingGroup")
    assert label_or_entity("VotingGroup") == "VotingGroup"
    assert label_or_entity("Person") == "Person"
    assert label_or_entity("voting_group") == "Entity" and label_or_entity(None) == "Entity"
