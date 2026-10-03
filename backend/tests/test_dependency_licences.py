"""Every package in the runtime lock carries an allowed licence.

The project's rule: permissive licences only — MIT, Apache 2.0, BSD, ISC, PSF,
MPL 2.0 (public-domain dedications such as the Unlicense and MIT-0 count too) —
and nothing copyleft or source-available (GPL, AGPL, LGPL, SSPL, BUSL, Commons
Clause). It used to be checked by hand for the packages we name; the lock also
pins the ones they pull in, and a licence can change between versions (orjson
added MPL-2.0 at 3.11), so a re-pin is checked here, not remembered.

A package offered under SEVERAL licences, the licensee's choice ("GPL-2.0-or-
later OR Apache-2.0" — odfpy), is fine under the permissive one. The metadata
cannot say so: PyPI classifiers list licences without saying OR or AND, so the
patterns refuse anything that names a forbidden licence. Such a package is
taken under its permissive option by naming it in DUAL_LICENSED, with where the
offer is stated and who checked it when — a record, not a bypass: the licence
taken must itself be allowed, and the entry must name a locked package, or it
is stale and fails.
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


#: Packages offered under more than one licence, and the permissive one we take
#: them under. Checked by hand against the licensor's own statement, not the
#: classifiers. Format: name → (licence taken, where the offer is stated, who/when).
DUAL_LICENSED: dict[str, tuple[str, str, str]] = {
    # "odfpy": ("Apache-2.0", "README: GPL-2.0-or-later OR Apache-2.0", "checked 2026-10-03"),
}


def effective_licence(name: str, declared: str) -> str:
    """The licence a package is used under: its declaration, or — for a package
    offered under several — the permissive option recorded in DUAL_LICENSED."""
    if name.lower() in {k.lower() for k in DUAL_LICENSED}:
        taken = next(v for k, v in DUAL_LICENSED.items() if k.lower() == name.lower())[0]
        return taken
    return declared


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
    lic = effective_licence(name, lic)
    assert not FORBIDDEN.search(lic), f"{name}: {lic}"
    assert ALLOWED.search(lic), f"{name}: {lic} is not on the allowed list"


class TestDualLicensed:
    """The record of packages taken under one of several offered licences."""

    def test_every_entry_names_a_locked_package_under_an_allowed_licence(self):
        locked = {n.lower() for n in locked_packages()}
        for name, (taken, where, who) in DUAL_LICENSED.items():
            assert name.lower() in locked, f"{name} is recorded as dual-licensed but not locked — stale entry"
            assert ALLOWED.search(taken) and not FORBIDDEN.search(taken), f"{name}: taking it under {taken!r} is not allowed"
            assert where and who, f"{name}: say where the offer is stated and who checked it"

    def test_a_recorded_package_is_judged_by_the_licence_taken(self, monkeypatch):
        monkeypatch.setitem(DUAL_LICENSED, "Fakepkg", ("Apache-2.0", "README: GPL OR Apache", "test"))
        declared = "Apache Software License; GNU General Public License (GPL)"
        assert FORBIDDEN.search(declared)                                   # refused as declared…
        lic = effective_licence("fakepkg", declared)                        # …case-insensitively
        assert lic == "Apache-2.0" and ALLOWED.search(lic) and not FORBIDDEN.search(lic)

    def test_an_unrecorded_package_keeps_its_declaration(self):
        assert effective_licence("fastapi", "MIT") == "MIT"
        assert effective_licence("something-gpl", "GPL-3.0-only") == "GPL-3.0-only"

    def test_the_record_cannot_launder_a_copyleft_choice(self, monkeypatch):
        """Recording a GPL package as taken under the GPL is still a failure."""
        monkeypatch.setitem(DUAL_LICENSED, "fastapi", ("GPL-3.0-only", "x", "y"))
        with pytest.raises(AssertionError):
            TestDualLicensed().test_every_entry_names_a_locked_package_under_an_allowed_licence()


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
