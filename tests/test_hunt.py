"""End-to-end hunts against a fake Vinted: which listings survive the filters, and in what order."""
import vinted_bot as vb
from conftest import FakeClient, args, make_listing, seller


def L(id, title, price, **kw):
    return make_listing(id=id, title=title, url=f"/items/{id}-x", price=float(price), **kw)


# ---------------------------------------------------------------- retail-ratio hunts
def retail_world():
    listings = [
        L(1, "Gucci loafers", 80),                                          # 11% of retail, good seller: keep
        L(2, "Gucci Horsebit loafers", 100),                                # model price 790: keep
        L(3, "Replica Gucci loafers", 60),                                  # fake wording: dropped
        L(4, "Gucci loafers", 70, size="7"),                                # wrong size: dropped
        L(5, "Gucci loafers", 60, business=True),                           # business seller: dropped
        L(6, "Gucci loafers", 20),                                          # 3% of retail: kept but HIGH risk
        L(7, "Gucci loafers", 90),                                          # seller has 2 reviews: dropped
        L(8, "Prada loafers", 150, brand_field="Prada"),                    # other brand in the target: keep
        L(9, "Nike trainers", 30, brand_field="Nike"),                      # not a target brand: dropped
        L(10, "Gucci loafers", 400),                                        # 53% of retail: too dear
    ]
    sellers = {
        "/items/1-x": seller(), "/items/2-x": seller(feedback=80), "/items/6-x": seller(description="n/a " * 6, feedback=6, photos=5),
        "/items/7-x": seller(feedback=2), "/items/8-x": seller(feedback=15),
    }
    return FakeClient(listings, sellers)


def test_only_real_deals_survive(cfg):
    client = retail_world()
    path, deals = vb.hunt(client, vb.open_db(), cfg, "loafers", args())
    assert sorted(d.id for d in deals) == [1, 2, 6, 8]
    assert path.exists()


def test_cheap_filters_run_before_any_listing_page_is_opened(cfg):
    client = retail_world()
    vb.hunt(client, vb.open_db(), cfg, "loafers", args())
    opened = set(client.opened)
    assert opened == {"/items/1-x", "/items/2-x", "/items/6-x", "/items/7-x", "/items/8-x"}
    for dropped in (3, 4, 5, 9, 10):
        assert f"/items/{dropped}-x" not in opened          # fewer requests to Vinted = politer


def test_safest_deals_come_first(cfg):
    _, deals = vb.hunt(retail_world(), vb.open_db(), cfg, "loafers", args())
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    risks = [order[d.risk] for d in deals]
    assert risks == sorted(risks)
    assert deals[-1].id == 6 and deals[-1].risk == "HIGH"


def test_model_specific_retail_price_is_used(cfg):
    _, deals = vb.hunt(retail_world(), vb.open_db(), cfg, "loafers", args())
    by_id = {d.id: d for d in deals}
    assert by_id[1].retail == 750 and by_id[2].retail == 790


def test_search_ceiling_is_the_retail_ratio_and_max_price_tightens_it(cfg):
    client = retail_world()
    vb.hunt(client, vb.open_db(), cfg, "loafers", args())
    assert client.searches[0][1] == 150                        # ceil(750 * 0.20)
    tight = retail_world()
    vb.hunt(tight, vb.open_db(), cfg, "loafers", args(max_price=100))
    assert tight.searches[0][1] == 100


def test_brands_flag_limits_the_search(cfg):
    client = retail_world()
    vb.hunt(client, vb.open_db(), cfg, "loafers", args(brands="prada"))
    assert [s[0].split()[0] for s in client.searches] == ["Prada"]


def test_repeat_runs_mark_deals_as_seen(cfg):
    db = vb.open_db()
    _, first = vb.hunt(retail_world(), db, cfg, "loafers", args())
    _, second = vb.hunt(retail_world(), db, cfg, "loafers", args())
    assert all(d.is_new for d in first)
    assert not any(d.is_new for d in second)


def test_allow_business_and_min_reviews_flags(cfg):
    client = retail_world()
    client.sellers["/items/5-x"] = seller()
    _, deals = vb.hunt(client, vb.open_db(), cfg, "loafers", args(allow_business=True, min_reviews=0))
    assert {5, 7} <= {d.id for d in deals}


def test_hide_risk_drops_likely_fakes_from_the_report(cfg):
    cfg["settings"]["hide_risk"] = ["HIGH"]
    _, deals = vb.hunt(retail_world(), vb.open_db(), cfg, "loafers", args())
    assert 6 not in {d.id for d in deals}


# ---------------------------------------------------------------- style hunts (keyword + price cap, no retail price)
def style_world():
    listings = [
        L(1, "Dr Martens boots", 30),
        L(2, "Dr Martens boots", 50),                    # over the £40 cap
        L(3, "Clarks boots", 20),                        # doesn't name the look
        L(4, "Dr. Martens 1460", 25, size="8"),          # wrong size
        L(5, "Dr Martens", 10),                          # seller has 1 review
        L(6, "Docs boots black", 15),                    # 'docs' alias in the pattern
        L(7, "Dr Martens replica boots", 12),           # fake wording
        L(8, "Dr Martens boots", 18),                    # seller unknown: cannot be checked, so dropped
    ]
    sellers = {f"/items/{i}-x": seller() for i in (1, 2, 3, 4, 6, 7)}
    sellers["/items/5-x"] = seller(feedback=1)
    return FakeClient(listings, sellers)


def test_style_hunt_keeps_matching_listings_cheapest_first(cfg):
    _, deals = vb.hunt(style_world(), vb.open_db(), cfg, "boots", args())
    assert [d.id for d in deals] == [6, 1]
    assert deals[0].risk == "n/a"                       # no retail price, so no fake-risk score
    assert any(f.startswith("matches:") for f in deals[0].flags)


def test_style_hunt_uses_relevance_order_and_the_price_cap(cfg):
    client = style_world()
    vb.hunt(client, vb.open_db(), cfg, "boots", args())
    text, price_to, page, order = client.searches[0]
    assert (text, price_to, order) == ("dr martens boots", 40, "relevance")


def test_max_price_is_a_hard_ceiling_over_the_target_cap(cfg):
    _, deals = vb.hunt(style_world(), vb.open_db(), cfg, "boots", args(max_price=20))
    assert [d.id for d in deals] == [6]


def test_per_search_price_caps(cfg):
    cfg["targets"]["boots"]["queries"] = [{"q": "dr martens boots", "max_price": 20}]
    client = style_world()
    _, deals = vb.hunt(client, vb.open_db(), cfg, "boots", args())
    assert client.searches[0][1] == 20
    assert [d.id for d in deals] == [6]


def test_always_match_brand_skips_the_must_have_check(cfg):
    tgt = cfg["targets"]["boots"]
    tgt["must_have"] = [r"anime|manga"]
    tgt["always_match"] = r"young\s?la"
    client = FakeClient([L(1, "YoungLA hoodie", 20, brand_field="YoungLA"), L(2, "Plain hoodie", 10)],
                        {"/items/1-x": seller(), "/items/2-x": seller()})
    _, deals = vb.hunt(client, vb.open_db(), cfg, "boots", args())
    assert [d.id for d in deals] == [1]
    assert "favourite brand" in deals[0].flags[0]
