"""
Unit tests for the Settings startup validators.

The SECRET_KEY guards run at import time, so a bad value takes the whole app
(and the whole test suite) down at collection rather than failing one test —
which is exactly what happened when the 32-char minimum landed without the CI
workflow's own SECRET_KEY being bumped. Hence the last test here, which checks
the workflow file itself.
"""
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import INSECURE_DEFAULT_SECRET_KEY, Settings

# The DB settings have no defaults; every Settings() below needs them.
_DB = {
    "ARCADEDB_URL":      "http://localhost:2480",
    "ARCADEDB_USERNAME": "test",
    "ARCADEDB_PASSWORD": "test",
    "ARCADEDB_DATABASE": "test",
}


def test_short_secret_key_is_rejected():
    with pytest.raises(ValidationError, match="SECRET_KEY is too short"):
        Settings(**_DB, SECRET_KEY="short-key")


def test_secret_key_just_under_the_minimum_is_rejected():
    with pytest.raises(ValidationError, match=r"too short \(31 chars\)"):
        Settings(**_DB, SECRET_KEY="k" * 31)


def test_secret_key_at_the_minimum_is_accepted():
    s = Settings(**_DB, SECRET_KEY="k" * 32)
    assert len(s.SECRET_KEY) == 32


def test_insecure_default_is_rejected_when_debug_is_off():
    # Long enough to clear the length check, so this is the default-value guard.
    assert len(INSECURE_DEFAULT_SECRET_KEY) >= 32
    with pytest.raises(ValidationError, match="insecure default"):
        Settings(**_DB, SECRET_KEY=INSECURE_DEFAULT_SECRET_KEY, DEBUG=False)


def test_insecure_default_is_tolerated_in_debug():
    s = Settings(**_DB, SECRET_KEY=INSECURE_DEFAULT_SECRET_KEY, DEBUG=True)
    assert s.SECRET_KEY == INSECURE_DEFAULT_SECRET_KEY


def test_ci_workflow_secret_keys_pass_the_length_check():
    # conftest only setdefault()s its test key, so a SECRET_KEY exported by the
    # CI workflow wins — if it is too short, every job dies during collection.
    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    keys = re.findall(r"^\s*SECRET_KEY:\s*(\S+)\s*$", workflow.read_text(), re.MULTILINE)
    assert keys, f"no SECRET_KEY entries found in {workflow}"
    for key in keys:
        assert len(key) >= 32, f"CI SECRET_KEY is {len(key)} chars, needs >= 32: {key}"
