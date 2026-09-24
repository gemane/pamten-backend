"""The product version — one number for the API, the web app and the Android app.

The single source is the git tag on `main` (`v1.2.3`). Nothing in the repository
carries the number: a release is tagging, and the tag-triggered release build
passes it in as `APP_VERSION` (a Docker build arg, see the Dockerfile and
`.github/workflows/release.yml`). Anything that is not a tagged release build —
Render's dev deploys, a laptop, the test suite — reports a development version
instead of pretending to be a release: `0.0.0-dev`, plus the commit when the
platform tells us one (Render sets `RENDER_GIT_COMMIT`).

Semantic versioning (major.minor.patch) is not a style choice here. The
minimum-app-version switch (`routers/app_version.py`) compares versions
numerically, and the Android build derives its ever-increasing build number
from the same three parts, so the number has to parse.
"""
from __future__ import annotations

import os
import re

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
DEV_VERSION = "0.0.0-dev"


def resolve_version(env: dict[str, str] | None = None) -> str:
    """`APP_VERSION` when it is a plain `major.minor.patch` (a leading `v` from
    the tag is tolerated), else the development version with the commit, if known.

    A malformed `APP_VERSION` falls back to the development version rather than
    raising: a typo in a deploy must not take the API down. The release workflow
    validates the tag before it ever gets here, so in practice this only catches
    hand-made builds.
    """
    env = os.environ if env is None else env
    raw = (env.get("APP_VERSION") or "").strip()
    if raw.startswith("v"):
        raw = raw[1:]
    if SEMVER.match(raw):
        return raw
    commit = (env.get("GIT_COMMIT") or env.get("RENDER_GIT_COMMIT") or "").strip()
    return f"{DEV_VERSION}+{commit[:7]}" if commit else DEV_VERSION


APP_VERSION = resolve_version()
