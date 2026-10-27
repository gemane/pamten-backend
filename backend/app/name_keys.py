"""The stored name key (`Entity.name_normalized`) kept in step with the
normaliser.

`mapper.normalize_entity_name` is what every by-name lookup and the same-name
dedup scan key on, and the key is STORED on the node at write time. When the
normaliser learns something — 2026-10-25: a run of spaced single letters is an
abbreviation, so "GRUPO TELEVISA S A B" keys like "Grupo Televisa, S.A.B." —
every node written before that carries the old key, and the two Televisas
stay two companies until their keys are recomputed. This heal recomputes them.
"""
from __future__ import annotations

import logging


log = logging.getLogger(__name__)

_CHUNK = 500

#: A 13D voting group used to sit among the entities with a key of its
#: writer's own ("voting group <issuer>"), which the first dev run of this
#: heal rewrote from the display name (2026-10-25). Since 2026-10-26 a group
#: is its own vertex type, so the Entity scan never meets one.
