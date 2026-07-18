from pydantic import BaseModel
from typing import Optional


class PeerCreate(BaseModel):
    name: str                              # display name of the trusted peer
    base_url: str                          # e.g. https://peer.example.com
    credibility_score: int = 60            # how much to trust this peer's claims
    auth_token: Optional[str] = None       # bearer token for the peer's /federation/export
