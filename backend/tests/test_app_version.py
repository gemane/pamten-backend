"""The force-upgrade switch.

A released mobile binary cannot be patched or recalled, so the only way to stop an
old client talking to the API is for the app to ask at startup. That makes two
properties matter more than the feature itself:

  * it must **fail open** — anything unknown answers "you are fine", because a bug
    that locks every user out would land on devices we cannot reach;
  * the **server** does the comparison, because the clients most needing correction
    are the ones running whatever comparison bug shipped with them.
"""
import pytest

from app.routers.app_version import app_version, parse_version


class TestParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("1.2.3", (1, 2, 3)),
        ("v1.2.3", (1, 2, 3)),        # a leading v is common in tags
        ("1.2", (1, 2, 0)),
        ("2", (2, 0, 0)),
        ("1.2.3+456", (1, 2, 3)),     # build metadata from CI
        ("1.2.3-beta.1", (1, 2, 3)),  # prerelease suffix
        ("  1.2.3 ", (1, 2, 3)),
    ])
    def test_versions_a_real_client_might_send(self, raw, expected):
        assert parse_version(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "banana", "v", "beta"])
    def test_unparseable_is_none_not_zero(self, raw):
        # (0,0,0) would compare as older than everything and force an upgrade.
        assert parse_version(raw) is None

    def test_double_digit_components_compare_numerically(self):
        """The classic: "1.10.0" sorts before "1.9.0" as a string. This is exactly
        the bug we do not want shipped inside a client we cannot patch."""
        assert parse_version("1.10.0") > parse_version("1.9.0")
        assert parse_version("2.0.0") > parse_version("1.99.99")


def _policy(monkeypatch, policy: dict):
    monkeypatch.setattr("app.routers.app_version._policy", lambda: policy)


class TestTheVerdict:
    def test_an_old_client_is_told_to_upgrade(self, monkeypatch):
        _policy(monkeypatch, {"ios": {"min_supported": "1.2.0", "store_url": "https://apps.example/x"}})
        r = app_version(platform="ios", version="1.1.9")
        assert r["update_required"] is True
        assert r["store_url"] == "https://apps.example/x"

    def test_a_current_client_is_left_alone(self, monkeypatch):
        _policy(monkeypatch, {"ios": {"min_supported": "1.2.0"}})
        assert app_version(platform="ios", version="1.2.0")["update_required"] is False
        assert app_version(platform="ios", version="9.0.0")["update_required"] is False

    def test_the_minimum_is_inclusive(self, monkeypatch):
        """Exactly the minimum is supported — otherwise setting min to the current
        release locks out everyone including the people who just updated."""
        _policy(monkeypatch, {"android": {"min_supported": "3.4.5"}})
        assert app_version(platform="android", version="3.4.5")["update_required"] is False

    def test_a_soft_prompt_is_separate_from_a_hard_block(self, monkeypatch):
        _policy(monkeypatch, {"ios": {"min_supported": "1.0.0", "latest": "2.0.0"}})
        r = app_version(platform="ios", version="1.5.0")
        assert r["update_available"] is True
        assert r["update_required"] is False

    def test_platforms_are_independent(self, monkeypatch):
        _policy(monkeypatch, {"ios": {"min_supported": "5.0.0"}, "android": {"min_supported": "1.0.0"}})
        assert app_version(platform="ios", version="2.0.0")["update_required"] is True
        assert app_version(platform="android", version="2.0.0")["update_required"] is False


class TestFailingOpen:
    """Every one of these must answer "you are fine"."""

    def test_no_policy_at_all(self, monkeypatch):
        _policy(monkeypatch, {})
        assert app_version(platform="ios", version="0.0.1")["update_required"] is False

    def test_a_platform_with_no_policy(self, monkeypatch):
        _policy(monkeypatch, {"ios": {"min_supported": "9.9.9"}})
        assert app_version(platform="android", version="0.0.1")["update_required"] is False

    def test_an_unknown_platform_string(self, monkeypatch):
        _policy(monkeypatch, {"ios": {"min_supported": "9.9.9"}})
        assert app_version(platform="palmos", version="0.0.1")["update_required"] is False

    def test_no_version_supplied(self, monkeypatch):
        _policy(monkeypatch, {"ios": {"min_supported": "9.9.9"}})
        assert app_version(platform="ios", version=None)["update_required"] is False

    def test_an_unparseable_client_version(self, monkeypatch):
        _policy(monkeypatch, {"ios": {"min_supported": "9.9.9"}})
        assert app_version(platform="ios", version="banana")["update_required"] is False

    def test_an_unparseable_minimum(self, monkeypatch):
        """A typo in the admin form must not brick every client."""
        _policy(monkeypatch, {"ios": {"min_supported": "not a version"}})
        assert app_version(platform="ios", version="1.0.0")["update_required"] is False

    def test_a_database_error(self, monkeypatch):
        """_policy swallows failures; assert the swallowing, not just the happy path."""
        def boom(*_a, **_k):
            raise RuntimeError("db down")
        monkeypatch.setattr("app.routers.app_version.run_command", boom)
        assert app_version(platform="ios", version="0.0.1")["update_required"] is False


class TestReachability:
    def test_it_is_served_unauthenticated(self, client):
        """A client too old to authenticate still has to learn it must upgrade."""
        assert client.get("/v1/app-version").status_code == 200

    def test_and_on_the_stable_unversioned_path(self, client):
        """Clients pin this URL for the life of the binary, so it must also exist
        off /v1 — a version check that a version bump can move is not a check."""
        assert client.get("/app-version").status_code == 200

    def test_setting_the_policy_needs_an_admin(self, client):
        assert client.put("/v1/app-version", json={"ios": {"min_supported": "1.0.0"}}) \
            .status_code in (401, 403)
