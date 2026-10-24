from pydantic import BaseModel
from typing import Optional, List


class PersonCreate(BaseModel):
    first_name: str
    last_name: str
    alias: Optional[List[str]] = []
    nationalities: Optional[List[str]] = []   # ISO-2 codes; the scalar twin went on 2026-10-25
    birth_date: Optional[str] = None
    death_date: Optional[str] = None
    birth_place: Optional[str] = None
    description: Optional[str] = None
    wikipedia_url: Optional[str] = None


class PersonResponse(PersonCreate):
    id: str
    full_name: str


class PersonMergeRequest(BaseModel):
    keep_id: str   # the canonical person to keep
    dup_id: str    # the duplicate to fold in and delete


class KeepSeparateRequest(BaseModel):
    ids: List[str]  # person ids in a group confirmed to be DIFFERENT people
