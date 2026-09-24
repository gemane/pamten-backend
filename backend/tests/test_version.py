"""The product version: from the release tag, never from a hardcoded string."""
from app.version import DEV_VERSION, resolve_version


class TestResolveVersion:
    def test_a_release_build_reports_the_tag_version(self):
        assert resolve_version({"APP_VERSION": "1.2.3"}) == "1.2.3"

    def test_the_tags_leading_v_is_tolerated(self):
        assert resolve_version({"APP_VERSION": "v1.2.3"}) == "1.2.3"

    def test_whitespace_from_a_build_arg_is_ignored(self):
        assert resolve_version({"APP_VERSION": " 2.0.10\n"}) == "2.0.10"

    def test_no_version_is_a_development_build(self):
        assert resolve_version({}) == DEV_VERSION == "0.0.0-dev"

    def test_a_development_build_names_its_commit_when_known(self):
        assert resolve_version({"GIT_COMMIT": "0123456789abcdef"}) == "0.0.0-dev+0123456"
        # Render's own variable, for the dev deploys that have no build arg
        assert resolve_version({"RENDER_GIT_COMMIT": "fedcba9876543210"}) == "0.0.0-dev+fedcba9"

    def test_a_malformed_version_falls_back_instead_of_failing(self):
        """A typo in a deploy must not take the API down; the release workflow
        validates the tag, so this only guards hand-made builds."""
        for bad in ("1.2", "1.2.3.4", "01.2.3", "1.2.3-rc1", "latest", "v", "1.2.x"):
            assert resolve_version({"APP_VERSION": bad}) == DEV_VERSION, bad


def test_the_api_reports_the_resolved_version(client):
    from app.version import APP_VERSION
    body = client.get("/").json()
    assert body["version"] == APP_VERSION
    assert client.get("/openapi.json").json()["info"]["version"] == APP_VERSION


def test_no_hardcoded_version_is_left_in_the_app():
    """The number lives in the git tag only; a literal here would drift again."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert '"0.1.0"' not in src
