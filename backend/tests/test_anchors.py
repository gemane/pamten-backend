

def test_not_a_voting_group_lets_a_typeless_entity_through():
    """ArcadeDB 26.10.1 compares three-valued: `type <> 'voting_group'` alone
    drops every entity without a type. The fence must say IS NULL OR."""
    from app.db.anchors import not_a_voting_group
    assert not_a_voting_group() == "(type IS NULL OR type <> 'voting_group')"
    assert not_a_voting_group("e.") == "(e.type IS NULL OR e.type <> 'voting_group')"
