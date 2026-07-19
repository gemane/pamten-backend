"""
Verification flags — a user report that a node or edge looks wrong.

A flag targets either a node (an Entity/Person, by `node_id`) or an edge
(an OWNS/HAS_ROLE relationship, addressed by its natural key `from_id` + `to_id`
[+ `role` for HAS_ROLE]) — the same key the BODS importer reconciles on, so a
flag stays attached across re-scrapes. See docs/verification.md.
"""
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class FlagTargetKind(str, Enum):
    owns = "owns"        # an OWNS edge  (from_id -> to_id)
    role = "role"        # a HAS_ROLE edge (from_id -> to_id, + role)
    entity = "entity"    # an Entity node (node_id)
    person = "person"    # a Person node (node_id)


class FlagCategory(str, Enum):
    wrong_owner = "wrong-owner"
    wrong_percent = "wrong-percent"
    wrong_role = "wrong-role"
    not_real = "not-real"
    outdated = "outdated"
    duplicate = "duplicate"
    other = "other"


class FlagStatus(str, Enum):
    open = "open"
    reviewing = "reviewing"
    resolved = "resolved"    # reachable only once Phase-B resolution exists
    rejected = "rejected"


class FlagCreate(BaseModel):
    target_kind: FlagTargetKind
    category: FlagCategory
    note: Optional[str] = Field(default=None, max_length=1000)
    # Edge targets (owns / role):
    from_id: Optional[str] = None
    to_id: Optional[str] = None
    role: Optional[str] = None
    # Node targets (entity / person):
    node_id: Optional[str] = None

    @model_validator(mode="after")
    def _require_target_fields(self):
        if self.target_kind in (FlagTargetKind.owns, FlagTargetKind.role):
            if not self.from_id or not self.to_id:
                raise ValueError("edge flags require from_id and to_id")
            if self.target_kind == FlagTargetKind.role and not self.role:
                raise ValueError("role flags require role")
        else:  # entity / person
            if not self.node_id:
                raise ValueError("node flags require node_id")
        return self


class FlagStatusUpdate(BaseModel):
    # Phase A allows the triage transitions; `resolved` waits for Phase-B actions.
    status: FlagStatus

    @model_validator(mode="after")
    def _phase_a_only(self):
        if self.status == FlagStatus.resolved:
            raise ValueError("resolving a flag needs a Phase-B resolution action")
        return self


class PinRequest(BaseModel):
    """A moderator-corrected value for an OWNS edge — at least one field required."""
    stake_percent: Optional[float] = Field(default=None, ge=0, le=100)
    ownership_type: Optional[str] = None

    @model_validator(mode="after")
    def _at_least_one(self):
        if self.stake_percent is None and not self.ownership_type:
            raise ValueError("provide stake_percent and/or ownership_type")
        return self
