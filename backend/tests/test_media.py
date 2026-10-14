"""Images with their credit, served from here rather than hotlinked.

The policy under test: only Commons files under a licence a visible credit
satisfies are served; a Wikipedia-hosted (fair-use) image, a namesake's
page or a refused licence means no image; the credit comes from the file's
metadata; the bytes and credit are cached (bucket + memory) so a panel open
costs Wikimedia nothing after the first; a definite "no image" is remembered,
a transient failure is not. Wikimedia is a seam (`media._get`), never reached.
"""
import hashlib
import json
from unittest.mock import patch

import httpx
import pytest

from app import media, objectstore
from app.config import settings
from tests.test_objectstore import FakeS3

LOGO_URL = ("https://upload.wikimedia.org/wikipedia/commons/thumb/b/bb/Tesla_T_symbol.svg/"
            "250px-Tesla_T_symbol.svg.png")
PERSON_URL = "https://en.wikipedia.org/wiki/Elon_Musk"
PNG_ETAG = media.fingerprint(b"PNGBYTES")


def _imageinfo(license_name="CC BY-SA 4.0", artist="<a href='x'>Jane Doe</a>", required="true",
               mime="image/png"):
    return {
        "thumburl": "https://upload.wikimedia.org/thumb/x.png", "url": "https://upload.wikimedia.org/x.png",
        "mime": mime, "thumbmime": mime,
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Tesla_T_symbol.svg",
        "extmetadata": {
            "LicenseShortName": {"value": license_name},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
            "Artist": {"value": artist},
            "AttributionRequired": {"value": required},
        },
    }


class TestAuthor:
    def test_hidden_duplicate_in_the_unknown_template_is_dropped_and_unknown_is_nobody(self):
        # the real SoftBank_Group_logo.svg Artist value
        artist = 'Unknown author<span style="display: none;">Unknown author</span>'
        assert media._plain(artist) == "Unknown author"
        assert media.author_from(artist, None) is None

    def test_a_credit_that_is_only_a_url_names_nobody(self):
        credit = ('<a rel="nofollow" class="external free" href="https://group.example/logo.svg">'
                  'https://group.example/logo.svg</a>')
        assert media.author_from("Unknown author", credit) is None
        assert media.author_from(None, credit) is None

    def test_a_named_credit_stands_in_for_a_missing_artist(self):
        assert media.author_from(None, "Own work by <b>Someone</b>") == "Own work by Someone"
        assert media.author_from("  ", "Press kit, Example Corp") == "Press kit, Example Corp"

    def test_hidden_elements_go_but_visible_markup_stays_as_text(self):
        html = '<a href="x">Jane Doe</a> <span style="display:none">(hidden)</span><i>photographer</i>'
        assert media._plain(html) == "Jane Doe photographer"

    def test_unknown_credit_does_not_rescue_an_unknown_artist(self):
        assert media.author_from("Anonymous", "unknown") is None
        info = _imageinfo(artist="Unknown author")
        info["extmetadata"]["Credit"] = {"value": "https://example.com/logo.svg"}
        assert media.credit_from_imageinfo("X.svg", info).author is None


def test_fingerprint_is_twelve_hex_digits_of_the_bytes():
    assert PNG_ETAG == hashlib.sha1(b"PNGBYTES").hexdigest()[:12]
    assert media.fingerprint(b"other") != PNG_ETAG


def test_the_credit_carries_the_bytes_fingerprint(store, wikimedia):
    credit = media.image_info("entity", "e1", {"logo_url": LOGO_URL})
    assert credit.etag == PNG_ETAG
    assert json.loads(store.objects["images/entity/e1.json"])["etag"] == PNG_ETAG


def test_a_credit_written_before_the_fingerprint_is_fetched_again_once(store, wikimedia):
    media.image_info("entity", "e1", {"logo_url": LOGO_URL})
    media._memory.clear()
    old = json.loads(store.objects["images/entity/e1.json"]); old.pop("etag")
    store.objects["images/entity/e1.json"] = json.dumps(old).encode()
    calls = len(wikimedia.calls)
    assert media.image_info("entity", "e1", {"logo_url": LOGO_URL}).etag == PNG_ETAG
    assert len(wikimedia.calls) > calls
    assert media.image_info("entity", "e1", {"logo_url": LOGO_URL}).etag == PNG_ETAG
    assert len(wikimedia.calls) == calls + 2   # imageinfo + bytes, once


def test_svg_original_gets_the_thumbnails_png_type():
    """Commons leaves thumbmime empty when it rendered an SVG to PNG; the bytes
    we serve are that PNG, so the type must not be the original's svg+xml."""
    info = _imageinfo(mime="image/svg+xml")
    info["thumbmime"] = None
    info["thumburl"] = ("https://thumb.wikimedia.org/wikipedia/commons/thumb/f/fa/Apple_logo_black.svg/"
                        "330px-Apple_logo_black.svg.png?utm_source=commons")
    assert media.thumb_content_type(info) == "image/png"
    assert media.credit_from_imageinfo("Apple_logo_black.svg", info).content_type == "image/png"


def test_thumb_type_prefers_thumbmime_then_the_originals_mime():
    assert media.thumb_content_type({"thumbmime": "image/webp", "thumburl": "x.png"}) == "image/webp"
    assert media.thumb_content_type({"thumburl": "https://x/y.jpg?u=1"}) == "image/jpeg"
    assert media.thumb_content_type({"url": "https://x/y", "mime": "image/gif"}) == "image/gif"
    assert media.thumb_content_type({}) == "image/jpeg"


class FakeWikimedia:
    """`media._get` stand-in: answers by URL, counts calls, can fail."""
    def __init__(self, imageinfo=None, summary=None, fail=False):
        self.imageinfo, self.summary, self.fail = imageinfo, summary, fail
        self.calls: list[str] = []

    def __call__(self, url, params=None):
        self.calls.append(url)
        if self.fail:
            raise httpx.ConnectError("down")
        if url == media.COMMONS_API:
            pages = ([{"imageinfo": [self.imageinfo]}] if self.imageinfo
                     else [{"missing": True}])
            return httpx.Response(200, json={"query": {"pages": pages}},
                                  request=httpx.Request("GET", url))
        if url.startswith(media.WIKIPEDIA_SUMMARY):
            if self.summary is None:
                raise httpx.HTTPStatusError("404", request=httpx.Request("GET", url),
                                            response=httpx.Response(404, request=httpx.Request("GET", url)))
            return httpx.Response(200, json=self.summary, request=httpx.Request("GET", url))
        return httpx.Response(200, content=b"PNGBYTES", request=httpx.Request("GET", url))


@pytest.fixture
def store():
    s3 = FakeS3()
    media._memory.clear()
    # Bucket AND endpoint: the store counts as on only with both (CI has no
    # .env, so a fixture that patched the bucket alone ran with the store off).
    with patch.object(settings, "OBJECT_STORE_BUCKET", "b"), \
         patch.object(settings, "OBJECT_STORE_ENDPOINT", "fsn1.example.test"), \
         patch.object(objectstore, "_get_client", lambda: s3):
        yield s3
    media._memory.clear()


@pytest.fixture
def wikimedia(monkeypatch):
    fake = FakeWikimedia(imageinfo=_imageinfo())
    monkeypatch.setattr(media, "_get", fake)
    return fake


# ── pure parts ────────────────────────────────────────────────────────────────

class TestCommonsFileFromUrl:
    def test_reads_the_file_behind_a_thumbnail_and_a_plain_upload_url(self):
        assert media.commons_file_from_url(LOGO_URL) == "Tesla_T_symbol.svg"
        assert media.commons_file_from_url(
            "https://upload.wikimedia.org/wikipedia/commons/1/1a/Elon_Musk_2022.jpg") == "Elon_Musk_2022.jpg"

    def test_decodes_and_underscores(self):
        assert media.commons_file_from_url(
            "https://upload.wikimedia.org/wikipedia/commons/a/ab/Tesla%20Motors.svg") == "Tesla_Motors.svg"

    def test_a_project_hosted_fair_use_image_is_not_a_commons_file(self):
        assert media.commons_file_from_url(
            "https://upload.wikimedia.org/wikipedia/en/thumb/e/e8/Logo.svg/250px-Logo.svg.png") is None

    def test_anything_else_is_none(self):
        assert media.commons_file_from_url("https://example.com/logo.png") is None
        assert media.commons_file_from_url(None) is None
        assert media.commons_file_from_url("") is None


class TestLicensePolicy:
    @pytest.mark.parametrize("name", ["CC BY-SA 4.0", "CC BY 3.0 de", "CC BY-SA 2.5", "CC0",
                                      "CC0 1.0", "Public domain", "PD-US", "PD"])
    def test_a_visible_credit_satisfies_these(self, name):
        assert media.license_allowed(name)

    @pytest.mark.parametrize("name", ["CC BY-NC-SA 4.0", "CC BY-ND 4.0", "CC BY-NC 2.0", "GFDL",
                                      "Fair use", "Copyrighted", "", None])
    def test_these_are_refused(self, name):
        assert not media.license_allowed(name)


class TestCredit:
    def test_reads_author_as_plain_text_licence_and_file_page(self):
        c = media.credit_from_imageinfo("Tesla_T_symbol.svg", _imageinfo())
        assert c.author == "Jane Doe"
        assert c.license == "CC BY-SA 4.0"
        assert c.license_url == "https://creativecommons.org/licenses/by-sa/4.0"
        assert c.file_page == "https://commons.wikimedia.org/wiki/File:Tesla_T_symbol.svg"
        assert c.attribution_required is True
        assert c.content_type == "image/png"
        assert c.source == "Wikimedia Commons"

    def test_a_public_domain_file_needs_no_attribution(self):
        c = media.credit_from_imageinfo("X.png", _imageinfo("Public domain", artist=None, required="false"))
        assert c.attribution_required is False
        assert c.author is None

    def test_a_refused_licence_gives_no_credit(self):
        assert media.credit_from_imageinfo("X.png", _imageinfo("CC BY-NC 4.0")) is None

    def test_credit_field_stands_in_for_a_missing_artist(self):
        info = _imageinfo(artist=None)
        info["extmetadata"]["Credit"] = {"value": "Own work by <b>Someone</b>"}
        assert media.credit_from_imageinfo("X.png", info).author == "Own work by Someone"


class TestWikipediaImage:
    def test_the_lead_image_on_commons_is_the_file(self, monkeypatch):
        fake = FakeWikimedia(summary={"type": "standard", "originalimage": {
            "source": "https://upload.wikimedia.org/wikipedia/commons/1/1a/Elon_Musk_2022.jpg"}})
        monkeypatch.setattr(media, "_get", fake)
        assert media.wikipedia_image_file(PERSON_URL) == "Elon_Musk_2022.jpg"
        assert fake.calls == [media.WIKIPEDIA_SUMMARY + "Elon_Musk"]

    def test_a_fair_use_lead_image_is_not_served(self, monkeypatch):
        monkeypatch.setattr(media, "_get", FakeWikimedia(summary={"type": "standard", "originalimage": {
            "source": "https://upload.wikimedia.org/wikipedia/en/1/1a/Portrait.jpg"}}))
        assert media.wikipedia_image_file(PERSON_URL) is None

    def test_a_disambiguation_page_is_nobody(self, monkeypatch):
        monkeypatch.setattr(media, "_get", FakeWikimedia(summary={"type": "disambiguation", "originalimage": {
            "source": "https://upload.wikimedia.org/wikipedia/commons/1/1a/Someone.jpg"}}))
        assert media.wikipedia_image_file(PERSON_URL) is None

    def test_no_page_no_url_no_file(self, monkeypatch):
        monkeypatch.setattr(media, "_get", FakeWikimedia(summary=None))
        assert media.wikipedia_image_file(PERSON_URL) is None
        assert media.wikipedia_image_file(None) is None
        assert media.wikipedia_image_file("https://en.wikipedia.org/") is None


# ── the lookup with its caches ────────────────────────────────────────────────

class TestImageLookup:
    def test_first_open_asks_commons_once_and_fetches_the_bytes(self, store, wikimedia):
        got = media.image_bytes("entity", "e1", {"logo_url": LOGO_URL})
        assert got == (b"PNGBYTES", "image/png")
        assert wikimedia.calls == [media.COMMONS_API, "https://upload.wikimedia.org/thumb/x.png"]
        assert json.loads(store.objects["images/entity/e1.json"])["license"] == "CC BY-SA 4.0"
        assert store.objects["images/entity/e1.bin"] == b"PNGBYTES"

    def test_second_open_costs_wikimedia_nothing(self, store, wikimedia):
        media.image_bytes("entity", "e1", {"logo_url": LOGO_URL})
        media.image_bytes("entity", "e1", {"logo_url": LOGO_URL})
        assert media.image_info("entity", "e1", {"logo_url": LOGO_URL}).author == "Jane Doe"
        assert len(wikimedia.calls) == 2

    def test_a_restart_reads_the_bucket_not_wikimedia(self, store, wikimedia):
        media.image_bytes("entity", "e1", {"logo_url": LOGO_URL})
        media._memory.clear()
        assert media.image_bytes("entity", "e1", {"logo_url": LOGO_URL}) == (b"PNGBYTES", "image/png")
        assert len(wikimedia.calls) == 2

    def test_a_refused_licence_is_remembered_as_no_image(self, store, monkeypatch):
        fake = FakeWikimedia(imageinfo=_imageinfo("CC BY-NC 4.0"))
        monkeypatch.setattr(media, "_get", fake)
        assert media.image_info("entity", "e1", {"logo_url": LOGO_URL}) is None
        assert media.image_info("entity", "e1", {"logo_url": LOGO_URL}) is None
        assert fake.calls == [media.COMMONS_API]
        assert json.loads(store.objects["images/entity/e1.json"]) == {"none": True, "fetched_at": pytest.approx(
            json.loads(store.objects["images/entity/e1.json"])["fetched_at"])}

    def test_a_record_without_a_pointer_is_not_looked_up_nor_remembered(self, store, wikimedia):
        assert media.image_info("entity", "e1", {"logo_url": None}) is None
        assert media.image_info("person", "p1", {"wikipedia_url": None}) is None
        assert wikimedia.calls == []
        assert store.objects == {}

    def test_a_transient_failure_is_not_remembered(self, store, monkeypatch):
        fake = FakeWikimedia(fail=True)
        monkeypatch.setattr(media, "_get", fake)
        with pytest.raises(httpx.ConnectError):
            media.image_info("entity", "e1", {"logo_url": LOGO_URL})
        assert store.objects == {}
        fake.fail, fake.imageinfo = False, _imageinfo()
        assert media.image_info("entity", "e1", {"logo_url": LOGO_URL}).license == "CC BY-SA 4.0"

    def test_a_stale_credit_is_asked_again(self, store, wikimedia):
        media.image_info("entity", "e1", {"logo_url": LOGO_URL})
        old = json.loads(store.objects["images/entity/e1.json"])
        old["fetched_at"] -= media.POSITIVE_TTL + 1
        store.objects["images/entity/e1.json"] = json.dumps(old).encode()
        media._memory.clear()
        media.image_info("entity", "e1", {"logo_url": LOGO_URL})
        assert wikimedia.calls.count(media.COMMONS_API) == 2

    def test_a_credit_without_its_bytes_fetches_them_again(self, store, wikimedia):
        media.image_bytes("entity", "e1", {"logo_url": LOGO_URL})
        del store.objects["images/entity/e1.bin"]
        media._memory.clear()
        assert media.image_bytes("entity", "e1", {"logo_url": LOGO_URL}) == (b"PNGBYTES", "image/png")

    def test_person_goes_through_the_wikipedia_page(self, store, monkeypatch):
        fake = FakeWikimedia(imageinfo=_imageinfo(mime="image/jpeg"), summary={
            "type": "standard", "originalimage": {
                "source": "https://upload.wikimedia.org/wikipedia/commons/1/1a/Elon_Musk_2022.jpg"}})
        monkeypatch.setattr(media, "_get", fake)
        c = media.image_info("person", "p1", {"wikipedia_url": PERSON_URL})
        assert c.file == "Elon_Musk_2022.jpg"
        assert fake.calls[0].startswith(media.WIKIPEDIA_SUMMARY)

    def test_forget_drops_both_layers(self, store, wikimedia):
        media.image_bytes("entity", "e1", {"logo_url": LOGO_URL})
        media.forget("entity", "e1")
        assert store.objects == {}
        media.image_bytes("entity", "e1", {"logo_url": LOGO_URL})
        assert wikimedia.calls.count(media.COMMONS_API) == 2

    def test_works_without_a_bucket_from_memory(self, wikimedia):
        media._memory.clear()
        with patch.object(settings, "OBJECT_STORE_BUCKET", ""), \
             patch.object(settings, "OBJECT_STORE_ENDPOINT", ""):
            assert media.image_bytes("entity", "e1", {"logo_url": LOGO_URL}) == (b"PNGBYTES", "image/png")
            media.image_bytes("entity", "e1", {"logo_url": LOGO_URL})
        assert len(wikimedia.calls) == 2
        media._memory.clear()


# ── the endpoints ─────────────────────────────────────────────────────────────

class TestEndpoints:
    def test_info_is_public_and_names_the_credit(self, client, fake_db, store, wikimedia):
        fake_db.queue([{"logo_url": LOGO_URL, "wikipedia_url": None}])
        r = client.get("/media/entity/e1")
        assert r.status_code == 200
        assert r.json() == {"url": f"/media/entity/e1/file?v={PNG_ETAG}", "author": "Jane Doe", "license": "CC BY-SA 4.0",
                            "license_url": "https://creativecommons.org/licenses/by-sa/4.0",
                            "file_page": "https://commons.wikimedia.org/wiki/File:Tesla_T_symbol.svg",
                            "attribution_required": True, "source": "Wikimedia Commons"}

    def test_file_serves_the_bytes_cacheable(self, client, fake_db, store, wikimedia):
        fake_db.queue([{"logo_url": LOGO_URL, "wikipedia_url": None}])
        r = client.get("/media/entity/e1/file")
        assert r.status_code == 200
        assert r.content == b"PNGBYTES"
        assert r.headers["content-type"].startswith("image/png")
        assert r.headers["cache-control"] == "public, max-age=86400"
        assert r.headers["etag"] == f'"{PNG_ETAG}"'

    def test_the_fingerprinted_url_is_cacheable_for_a_year_a_wrong_one_for_a_day(self, client, fake_db, store, wikimedia):
        row = [{"logo_url": LOGO_URL, "wikipedia_url": None}]
        fake_db.queue(row, row)
        r = client.get(f"/media/entity/e1/file?v={PNG_ETAG}")
        assert r.status_code == 200 and r.content == b"PNGBYTES"
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
        r = client.get("/media/entity/e1/file?v=stale0stale0")   # a URL handed out before the bytes changed
        assert r.status_code == 200 and r.content == b"PNGBYTES"
        assert r.headers["cache-control"] == "public, max-age=86400"

    def test_a_matching_if_none_match_is_304(self, client, fake_db, store, wikimedia):
        row = [{"logo_url": LOGO_URL, "wikipedia_url": None}]
        fake_db.queue(row, row)
        r = client.get("/media/entity/e1/file", headers={"If-None-Match": f'"{PNG_ETAG}"'})
        assert r.status_code == 304 and r.content == b""
        r = client.get("/media/entity/e1/file", headers={"If-None-Match": '"other"'})
        assert r.status_code == 200

    def test_an_id_with_a_colon_reaches_the_lookup(self, client, fake_db, store, wikimedia):
        fake_db.queue([{"logo_url": LOGO_URL, "wikipedia_url": None}])
        r = client.get("/media/entity/lei:5493001KJTIIGC8Y1R12")
        assert r.status_code == 200
        assert r.json()["url"] == f"/media/entity/lei:5493001KJTIIGC8Y1R12/file?v={PNG_ETAG}"
        assert fake_db.calls[0][1] == {"id": "lei:5493001KJTIIGC8Y1R12"}

    def test_no_image_is_404_not_an_error(self, client, fake_db, store, wikimedia):
        fake_db.queue([{"logo_url": None, "wikipedia_url": None}])
        assert client.get("/media/person/p1").status_code == 404

    def test_a_merged_id_is_followed(self, client, fake_db, store, wikimedia):
        # miss, one forwarding hop, the hop's own miss, the survivor's record
        fake_db.queue([], [{"new_id": "e2"}], [], [{"logo_url": LOGO_URL, "wikipedia_url": None}])
        assert client.get("/media/entity/e1").status_code == 200

    def test_unknown_node_and_unknown_kind_are_404(self, client, fake_db, store, wikimedia):
        fake_db.queue([], [])
        assert client.get("/media/entity/nope").status_code == 404
        assert client.get("/media/thing/e1").status_code == 404

    def test_wikimedia_down_is_503_and_not_remembered(self, client, fake_db, store, monkeypatch):
        monkeypatch.setattr(media, "_get", FakeWikimedia(fail=True))
        fake_db.queue([{"logo_url": LOGO_URL, "wikipedia_url": None}])
        assert client.get("/media/entity/e1").status_code == 503
        assert store.objects == {}
