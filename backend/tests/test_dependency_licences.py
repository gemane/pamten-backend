"""Every package in the runtime lock carries an allowed licence.

The project's rule: permissive licences only — MIT, Apache 2.0, BSD, ISC, PSF,
MPL 2.0 (public-domain dedications such as the Unlicense and MIT-0 count too) —
and nothing copyleft or source-available (GPL, AGPL, LGPL, SSPL, BUSL, Commons
Clause). It used to be checked by hand for the packages we name; the lock also
pins the ones they pull in, and a licence can change between versions (orjson
added MPL-2.0 at 3.11), so a re-pin is checked here, not remembered.
"""
import re
from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path

import pytest

LOCK = Path(__file__).resolve().parents[1] / "requirements.txt"

ALLOWED = re.compile(r"\b(MIT|MIT-0|Apache|BSD|ISC|ISCL|PSF|Python Software Foundation|"
                     r"MPL|Mozilla Public License|Unlicense|CC0|Public Domain)\b", re.I)
FORBIDDEN = re.compile(r"\b(A?GPL|LGPL|GNU|SSPL|BUSL|Business Source|Commons Clause|"
                       r"Elastic License|proprietary|commercial)\b", re.I)


def licence_of(name: str) -> str:
    """The licence a distribution declares: the SPDX expression when there is one,
    else the classifiers, else the free-text field."""
    m = metadata(name)
    expr = (m.get("License-Expression") or "").strip()
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in (m.get_all("Classifier") or [])
                   if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    return (m.get("License") or "").strip().splitlines()[0] if (m.get("License") or "").strip() else ""


def locked_packages() -> list[str]:
    return [line.split("==")[0].strip() for line in LOCK.read_text().splitlines()
            if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*==", line)]


def test_the_lock_is_not_empty():
    names = locked_packages()
    assert "fastapi" in names and len(names) > 20


@pytest.mark.parametrize("name", locked_packages())
def test_every_locked_package_has_an_allowed_licence(name):
    try:
        lic = licence_of(name)
    except PackageNotFoundError:
        pytest.skip(f"{name} is not installed in this environment")
    assert lic, f"{name} declares no licence — check it by hand before locking it"
    assert not FORBIDDEN.search(lic), f"{name}: {lic}"
    assert ALLOWED.search(lic), f"{name}: {lic} is not on the allowed list"


class TestTheClassifier:
    """The two patterns themselves, on the shapes seen in real metadata."""

    @pytest.mark.parametrize("lic", ["MIT", "Apache-2.0 OR BSD-3-Clause", "MPL-2.0 AND (Apache-2.0 OR MIT)",
                                     "The Unlicense (Unlicense)", "ISC License (ISCL)", "PSF-2.0",
                                     "BSD License; Apache Software License", "MIT-0"])
    def test_allowed(self, lic):
        assert ALLOWED.search(lic) and not FORBIDDEN.search(lic)

    @pytest.mark.parametrize("lic", ["GPL-3.0-only", "AGPL-3.0", "LGPL-2.1-or-later",
                                     "GNU General Public License v3 (GPLv3)", "SSPL-1.0",
                                     "BUSL-1.1", "MIT with Commons Clause", "Elastic License 2.0"])
    def test_forbidden(self, lic):
        assert FORBIDDEN.search(lic)
