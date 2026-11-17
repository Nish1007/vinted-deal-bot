"""What the bot remembers (seen listings), what it tells you (alerts) and what it writes (reports)."""
import sqlite3

import vinted_bot as vb
from conftest import make_listing


# ---------------------------------------------------------------- seen listings
def test_first_sighting_is_new_then_not():
    db = vb.open_db()
    first = make_listing(price=80)
    vb.mark_seen(db, "loafers", [first])
    assert first.is_new and not first.price_drop

    again = make_listing(price=80)
    vb.mark_seen(db, "loafers", [again])
    assert not again.is_new and not again.price_drop


def test_a_price_drop_is_reported_once_and_remembered():
    db = vb.open_db()
    vb.mark_seen(db, "loafers", [make_listing(price=80)])
    cheaper = make_listing(price=60)
    vb.mark_seen(db, "loafers", [cheaper])
    assert cheaper.price_drop and not cheaper.is_new
    assert db.execute("SELECT price FROM seen WHERE id=1").fetchone() == (60,)

    same = make_listing(price=60)
    vb.mark_seen(db, "loafers", [same])
    assert not same.price_drop


def test_a_price_rise_is_not_a_drop():
    db = vb.open_db()
    vb.mark_seen(db, "loafers", [make_listing(price=60)])
    dearer = make_listing(price=90)
    vb.mark_seen(db, "loafers", [dearer])
    assert not dearer.price_drop


def test_the_same_listing_is_tracked_separately_per_target():
    db = vb.open_db()
    vb.mark_seen(db, "loafers", [make_listing()])
    other = make_listing()
    vb.mark_seen(db, "boots", [other])
    assert other.is_new


def test_the_database_lives_next_to_the_script(tmp_path):
    vb.open_db().close()
    assert (tmp_path / "vinted_seen.db").exists()
    assert sqlite3.connect(tmp_path / "vinted_seen.db").execute("SELECT count(*) FROM seen").fetchone() == (0,)


# ---------------------------------------------------------------- alerts
class Posts(list):
    pass


def capture_posts(monkeypatch):
    posts = Posts()
    monkeypatch.setattr(vb.requests, "post", lambda url, **kw: posts.append((url, kw)))
    return posts


def deal(id, risk, new=True, drop=False):
    l = make_listing(id=id, url=f"/items/{id}-x", brand="Gucci", retail=750, ratio=0.1, risk=risk)
    l.is_new, l.price_drop = new, drop
    return l


def test_alerts_only_for_fresh_deals_within_the_risk_ceiling(monkeypatch):
    monkeypatch.setenv("VINTED_DISCORD_WEBHOOK", "https://discord.test/hook")
    posts = capture_posts(monkeypatch)
    deals = [deal(1, "LOW"), deal(2, "MEDIUM"), deal(3, "HIGH"), deal(4, "LOW", new=False), deal(5, "LOW", new=False, drop=True)]
    vb.notify({"notify": {"min_risk": "MEDIUM"}}, "Loafers", deals, "https://www.vinted.test")
    sent = [kw["json"]["content"] for _, kw in posts]
    assert len(sent) == 3                                    # LOW, MEDIUM and the price drop
    assert not any("/items/3-x" in s or "/items/4-x" in s for s in sent)
    assert all(url == "https://discord.test/hook" for url, _ in posts)
    assert "risk LOW" in sent[0] and "https://www.vinted.test/items/1-x" in sent[0]


def test_ntfy_gets_a_title_and_a_click_link(monkeypatch):
    monkeypatch.setenv("VINTED_NTFY_TOPIC", "my-topic")
    posts = capture_posts(monkeypatch)
    vb.notify({"notify": {}}, "Loafers", [deal(1, "LOW")], "https://www.vinted.test")
    (url, kw), = posts
    assert url == "https://ntfy.sh/my-topic"
    assert kw["headers"]["Click"] == "https://www.vinted.test/items/1-x"


def test_no_webhook_means_no_requests(monkeypatch):
    posts = capture_posts(monkeypatch)
    vb.notify({"notify": {"discord_webhook": "", "ntfy_topic": ""}}, "Loafers", [deal(1, "LOW")], "https://x")
    assert not posts


def test_a_failing_alert_never_crashes_the_run(monkeypatch):
    monkeypatch.setenv("VINTED_NTFY_TOPIC", "t")

    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(vb.requests, "post", boom)
    vb.notify({"notify": {}}, "Loafers", [deal(1, "LOW")], "https://x")      # must not raise


def test_webhooks_in_the_environment_beat_the_config_file(monkeypatch):
    monkeypatch.setenv("VINTED_DISCORD_WEBHOOK", "https://from-env")
    posts = capture_posts(monkeypatch)
    vb.notify({"notify": {"discord_webhook": "https://from-config"}}, "L", [deal(1, "LOW")], "https://x")
    assert posts[0][0] == "https://from-env"


# ---------------------------------------------------------------- reports
def test_report_escapes_listing_text(tmp_path):
    nasty = make_listing(title='<script>alert("x")</script>', brand="<b>Gucci</b>", retail=750, ratio=0.1, risk="LOW")
    nasty.flags = ["<img src=x onerror=1>"]
    path = vb.write_report("Loafers", [nasty], "https://www.vinted.test", 0.2, "loafers")
    html = path.read_text(encoding="utf-8")
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "<img src=x" not in html and "&lt;img src=x" in html
    assert (tmp_path / "reports" / "latest.html").read_text(encoding="utf-8") == html


def test_report_with_no_deals_says_so():
    path = vb.write_report("Loafers", [], "https://www.vinted.test", 0.2, "loafers")
    assert "No deals this run." in path.read_text(encoding="utf-8")


def test_seller_label():
    assert vb.seller_label(make_listing()) == "?"
    l = make_listing(feedback=12, reputation=0.96)
    assert vb.seller_label(l) == "12 reviews 4.8★"
