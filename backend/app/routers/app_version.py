"""
Minimum supported app version — the force-upgrade switch for released clients.

A shipped mobile binary cannot be patched or recalled. When an old version has to
stop talking to the API — a protocol change, a bug that corrupts data, a security
fix — the only lever is the app asking, at startup, whether it is still allowed to
run. This endpoint is that lever, and it has to exist *before* the first release:
a client shipped without the check can never be told to upgrade.

Three design decisions, each of which is the point rather than an implementation
detail:

**The server decides, not the client.** The app sends its version and receives a
verdict. Putting the comparison in the client means the clients that most need
correcting are the ones running the buggy comparison — and semver comparison is
exactly where those bugs live ("1.10.0" sorts before "1.9.0" as a string).

**It fails open.** Anything unexpected — no policy set, an unparseable version, a
database error — answers "you are fine". A bug here that locked every user out of
the app would be far worse than a stale minimum, and the failure would arrive on
devices we cannot reach.

**It is unauthenticated and cheap.** A client too old to authenticate still needs
to be told to upgrade, and this is called on every app start.

The policy lives in the database, not in configuration, so it can be changed
immediately by an admin. Needing a deploy to lock out a broken client is a poor
position on the day it matters.
"""
import json
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.auth.dependencies import require_admin
from app.db.arcadedb import run_command, run_sql

router = APIRouter(prefix="/app-version", tags=["App"])
log = logging.getLogger(__name__)

#: One row in AppSetting holds the whole policy as JSON, so a read is a single
#: indexed lookup and an update is atomic — no half-applied policy where the
#: minimum has moved but the store URL has not.
_SETTING_KEY = "app-version-policy"

_VERSION = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")

_PLATFORMS = ("ios", "android", "web")


def parse_version(value: str | None) -> tuple[int, int, int] | None:
    """"1.10.2" → (1, 10, 2). None when it is not a version we can compare.

    Tolerant on purpose: a leading "v", missing minor/patch, and trailing build
    metadata ("1.2.3+456", "1.2.3-beta") all parse, because a released client may
    send any of them and refusing to understand it must not force an upgrade.
    """
    if not value:
        return None
    m = _VERSION.match(str(value))
    if not m:
        return None
    return tuple(int(p) if p else 0 for p in m.groups())        # type: ignore[return-value]


def _policy() -> dict:
    """The stored policy, or an empty one. Never raises."""
    try:
        rows = run_command("MATCH (s:AppSetting {key:$k}) RETURN s.value AS value",
                           {"k": _SETTING_KEY})
        if rows and rows[0].get("value"):
            return json.loads(rows[0]["value"])
    except Exception as exc:                                     # noqa: BLE001
        # Fail open: an unreachable database must not lock every client out.
        log.warning("app-version policy unreadable, treating as unset: %s", exc)
    return {}


class VersionPolicy(BaseModel):
    """Per-platform policy. Every field optional — an unset platform imposes nothing."""
    min_supported: str | None = Field(None, description='Oldest version allowed to run, e.g. "1.2.0"')
    latest: str | None = Field(None, description="Newest published version, for a soft prompt")
    store_url: str | None = Field(None, description="Where to send the user to update")
    message: str | None = Field(None, description="Shown to the user instead of the default text")


class PolicyUpdate(BaseModel):
    ios: VersionPolicy | None = None
    android: VersionPolicy | None = None
    web: VersionPolicy | None = None


@router.get("")
def app_version(
    platform: str | None = Query(None, description="ios | android | web"),
    version: str | None = Query(None, description="The version the client is running"),
):
    """Whether this client may keep running, and whether a newer version exists.

    Unauthenticated by design. Answers `update_required: false` whenever it cannot
    be certain of the opposite.
    """
    key = (platform or "").strip().lower()
    policy = _policy().get(key if key in _PLATFORMS else "", {}) or {}

    minimum = parse_version(policy.get("min_supported"))
    latest = parse_version(policy.get("latest"))
    current = parse_version(version)

    # Every clause below defaults to False when anything is unknown.
    update_required = bool(minimum and current and current < minimum)
    update_available = bool(latest and current and current < latest)

    return {
        "platform": key or None,
        "min_supported": policy.get("min_supported"),
        "latest": policy.get("latest"),
        "update_required": update_required,
        "update_available": update_available,
        "store_url": policy.get("store_url"),
        "message": policy.get("message"),
    }


@router.put("", dependencies=[Depends(require_admin)])
def set_app_version(policy: PolicyUpdate):
    """Replace the version policy. Admin only.

    A full replace rather than a merge: a partial update is how you end up having
    raised the minimum on iOS while Android still points at last year's store URL.
    """
    payload = {p: v.model_dump(exclude_none=True)
               for p, v in ((p, getattr(policy, p)) for p in _PLATFORMS) if v}
    run_sql(
        "UPDATE AppSetting SET key = :k, value = :v, updated_at = :now UPSERT WHERE key = :k",
        {"k": _SETTING_KEY, "v": json.dumps(payload),
         "now": datetime.now(timezone.utc).isoformat()})
    return {"status": "ok", "policy": payload}
