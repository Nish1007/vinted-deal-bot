"""Title/brand/size matching and the fake-risk scoring: the logic that decides what counts as a deal."""
import pytest

import vinted_bot as vb
from conftest import make_listing, seller


# ---------------------------------------------------------------- brands
def test_brand_matches_whole_words_only():
    gucci = vb.compile_brands([{"name": "Gucci", "retail": 750}])[0]["rx"]
    assert gucci.search("gucci loafers")
    assert not gucci.search("guccio")          # a longer word is not the brand
    assert not gucci.search("sguccio")


def test_brand_aliases_and_models():
    lv = vb.compile_brands([{"name": "Louis Vuitton", "aliases": ["louis vuitton", "lv"], "retail": 950}])[0]
    assert lv["rx"].search("lv loafers")
    assert not lv["rx"].search("elvis")
    gucci = vb.compile_brands([{"name": "Gucci", "retail": 750, "models": {"jordaan|horsebit": 790}}])[0]
    assert [v for rx, v in gucci["models"] if rx.search("gucci horsebit loafer")] == [790]


def test_norm_flattens_curly_apostrophes():
    assert vb.norm("Church’s") == "church's"
    assert vb.norm(None) == ""


# ---------------------------------------------------------------- titles that are not the real thing
@pytest.mark.parametrize("title", [
    "Replica Gucci loafers", "Gucci inspired loafers", "gucci dupe", "Gucci copy", "AAA quality loafers",
    "gucci-style loafers", "Kids Gucci loafers", "Boys loafers", "Toddler shoes",
])
def test_rejected_titles(title):
    assert vb.REJECT_TITLE.search(vb.norm(title))


@pytest.mark.parametrize("title", ["Gucci Jordaan loafers", "Prada Monolith", "Kidston bag", "Copyright book"])
def test_real_titles_pass(title):
    assert not vb.REJECT_TITLE.search(vb.norm(title))


# ---------------------------------------------------------------- size matching
PROFILE = {"field_patterns": [r"^(uk\s*)?9(\s*[|/].*)?$"], "title_patterns": [r"\bsize\s*9\b"], "reject_field": ["kid"]}


@pytest.mark.parametrize("size", ["9", "UK 9", "9 / 43"])
def test_size_field_matches(size):
    assert vb.size_ok(make_listing(size=size), PROFILE, accept_title=False) == (True, "")


@pytest.mark.parametrize("size", ["10", "9.5", "", "19"])
def test_size_field_mismatch(size):
    assert vb.size_ok(make_listing(size=size, title="Gucci loafers"), PROFILE, accept_title=True)[0] is False


def test_size_taken_from_title_is_flagged():
    ok, note = vb.size_ok(make_listing(size="", title="Gucci loafers size 9"), PROFILE, accept_title=True)
    assert ok and "size from title" in note
    assert vb.size_ok(make_listing(size="", title="Gucci loafers size 9"), PROFILE, accept_title=False)[0] is False


def test_kids_sizes_are_rejected_even_if_the_number_fits():
    assert vb.size_ok(make_listing(size="kids 9"), PROFILE, accept_title=True)[0] is False


# ---------------------------------------------------------------- risk scoring
@pytest.fixture
def gucci():
    return vb.compile_brands([{"name": "Gucci", "retail": 750}])[0]


def rated(brand, extra=None, **listing):
    l = make_listing(**listing)
    l.ratio = l.price / 750                          # Gucci's retail estimate
    vb.assess(l, brand, extra or {}, 0.2)
    return l


def test_normal_cheap_listing_is_low_risk_with_a_good_seller(gucci):
    l = rated(gucci, seller(), price=120)
    assert l.risk == "LOW"
    assert "mentions proof/box/receipt" in l.flags


def test_extremely_cheap_is_high_risk(gucci):
    l = rated(gucci, price=30)                       # 4% of retail
    assert l.risk == "HIGH"
    assert any("bait/fake" in f for f in l.flags)


def test_unusually_cheap_is_medium(gucci):
    l = rated(gucci, price=75)                       # 10% of retail
    assert l.risk == "MEDIUM"
    assert any("unusually cheap" in f for f in l.flags)


def test_wrong_brand_field_raises_risk(gucci):
    l = rated(gucci, price=120, brand_field="Zara")
    assert any("brand field says 'Zara'" in f for f in l.flags)
    assert l.risk == "MEDIUM"


def test_missing_brand_is_flagged(gucci):
    assert "no brand set" in rated(gucci, price=120, brand_field="").flags


def test_hedging_about_authenticity_is_flagged(gucci):
    l = rated(gucci, seller(description="I cannot guarantee these are real, sold as seen"), price=120)
    assert "seller hedges on authenticity" in l.flags
    assert l.risk in ("MEDIUM", "HIGH")


@pytest.mark.parametrize("extra, expected_flag", [
    (seller(feedback=0), "seller has 0 reviews"),
    (seller(feedback=3), "seller has 3 reviews"),
    (seller(photos=2), "only 2 photos"),
    (seller(description="ok"), "no real description"),
])
def test_seller_signals(gucci, extra, expected_flag):
    assert expected_flag in rated(gucci, extra, price=120).flags


def test_established_sellers_lower_the_score(gucci):
    weak = rated(gucci, seller(feedback=1, description="x" * 30), price=75)
    strong = rated(gucci, seller(feedback=80, description="x" * 30), price=75)
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    assert order[strong.risk] <= order[weak.risk]
