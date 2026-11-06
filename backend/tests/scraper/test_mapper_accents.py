"""The stored name key folds Latin accents and dashes, and leaves every other
script's letters alone (findings item 65, 2026-11-06): an accented Exhibit 21
row minted a twin of the GLEIF node whose record spells the name plain, because
`normalize_entity_name` kept the accents `name_key` and `legal_name_key` fold."""
import pytest

from app.scraper.mapper import fold_marks, normalize_entity_name


@pytest.mark.parametrize("accented,plain", [
    ("MercadoPago Perú S.R.L.", "MERCADOPAGO PERU S.R.L."),
    ("Yaborã Indústria Aeronáutica S.A.", "YABORA INDUSTRIA AERONAUTICA S.A."),
    ("Corporación Novavisión, S. de R.L. de C.V.", "CORPORACION NOVAVISION S DE RL DE CV"),
    ("BlackRock México Infraestructura II, S. de R.L. de C.V.", "BLACKROCK MEXICO INFRAESTRUCTURA II, S. DE R.L. DE C.V."),
    ("Société Générale", "SOCIETE GENERALE"),
    ("Nestlé S.A.", "NESTLE SA"),
])
def test_an_accented_name_keys_as_its_plain_spelling(accented, plain):
    assert normalize_entity_name(accented) == normalize_entity_name(plain)


@pytest.mark.parametrize("dashed", ["Embraer Aviation International – EAI", "Embraer Aviation International — EAI",
                                    "Embraer Aviation International ‐ EAI"])
def test_every_dash_is_the_plain_one(dashed):
    assert normalize_entity_name(dashed) == normalize_entity_name("EMBRAER AVIATION INTERNATIONAL - EAI")
    assert normalize_entity_name(dashed) == "embraer aviation international - eai"


@pytest.mark.parametrize("name", [
    "騰訊控股有限公司",                       # Chinese: no marks, untouched
    "애플코리아 유한회사",                     # Korean: Hangul syllables split by NFKD, put back by NFC
    "ソフトバンクグループ株式会社",            # Japanese: a voiced kana's mark is not an accent
])
def test_other_scripts_keep_their_letters(name):
    assert fold_marks(name) == name
    assert "グループ" in normalize_entity_name("ソフトバンクグループ株式会社")


def test_only_the_fullwidth_latin_letters_of_a_japanese_name_change():
    assert fold_marks("株式会社三菱ＵＦＪフィナンシャル・グループ") == "株式会社三菱UFJフィナンシャル・グループ"


def test_a_fullwidth_latin_letter_widens_to_the_plain_one():
    assert normalize_entity_name("ｅＦｒｏｎｔ株式会社") == "efront株式会社"


def test_letters_without_a_mark_stay_as_they_are():
    # ø, ß and æ are letters of their own, not accented ones: as before
    assert normalize_entity_name("Ørsted A/S") == "ørsted a/s"
    assert fold_marks("Straße") == "Straße"


def test_fold_marks_is_what_the_key_uses():
    assert fold_marks("Perú – Yaborã") == "Peru - Yabora"
