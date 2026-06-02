from pydantic import BaseModel
from typing import Optional
from enum import Enum


class SourceType(str, Enum):
    news = "news"
    register = "register"
    wikipedia = "wikipedia"
    user = "user"
    scraper = "scraper"


class SourceCreate(BaseModel):
    name: str
    url: Optional[str] = None
    credibility_score: int               # 0-100
    type: SourceType


class SourceResponse(SourceCreate):
    id: str
