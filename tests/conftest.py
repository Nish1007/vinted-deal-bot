"""Shared test setup. Nothing in this suite touches the network: the Vinted client is faked."""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import vinted_bot as vb  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Keep the seen-listings database and HTML reports out of the repo, and never really sleep."""
    monkeypatch.setattr(vb, "HERE", tmp_path)
    monkeypatch.setattr(vb.time, "sleep", lambda *_: None)
    for name in ("VINTED_DISCORD_WEBHOOK", "VINTED_NTFY_TOPIC"):
        monkeypatch.delenv(name, raising=False)


def make_listing(**over):
    base = dict(id=1, title="Gucci loafers", url="/items/1-gucci-loafers", price=80.0, total=83.0, currency="GBP",
                brand_field="Gucci", size="9", condition="very good", thumb="https://img/1.jpg", favs=3)
    base.update(over)
    return vb.Listing(**base)


def product_item(id=1, title="Gucci loafers", price="80.0", brand="Gucci", second="9 · Very good", business=False):
    """The shape of one search result inside Vinted's embedded Next.js data."""
    return {"productItem": {
        "id": id, "title": title, "url": f"/items/{id}-x",
        "price": {"amount": price, "currencyCode": "GBP"}, "totalItemPrice": {"amount": str(float(price) + 3)},
        "itemBox": {"firstLine": brand, "secondLine": second},
        "thumbnailUrl": f"https://img/{id}.jpg", "favouriteCount": 2, "user": {"isBusiness": business},
    }}


def next_page(blob: str, pieces: int = 2) -> str:
    """Wrap `blob` the way Vinted does: as several self.__next_f.push([1,"..."]) script chunks."""
    step = max(1, len(blob) // pieces + 1)
    chunks = [blob[i:i + step] for i in range(0, len(blob), step)]
    return "<html>" + "".join(f"<script>self.__next_f.push([1,{json.dumps(c)}])</script>" for c in chunks) + "</html>"


class FakeClient:
    """Stands in for vb.Vinted: returns canned listings and seller details, records what was asked."""
    base = "https://www.vinted.test"

    def __init__(self, listings, sellers=None):
        self.listings, self.sellers, self.searches, self.opened = listings, sellers or {}, [], []

    def search(self, text, category_ids, price_to, page=1, order="newest_first"):
        self.searches.append((text, price_to, page, order))
        return (self.listings if page == 1 else []), 1

    def seller_and_description(self, url):
        self.opened.append(url)
        return self.sellers.get(url, {"description": "", "feedback": None, "reputation": None, "photos": None})


def seller(feedback=30, photos=6, description="Genuine, comes with receipt and dust bag", reputation=0.97):
    return {"description": description, "feedback": feedback, "reputation": reputation, "photos": photos}


def args(**over):
    base = dict(max_price=None, max_ratio=None, brands=None, pages=None, min_reviews=None,
                allow_business=False, no_enrich=False)
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def cfg():
    """A small, self-contained config (not the real targets.yaml) so the tests describe their own world."""
    return {
        "settings": {"domain": "vinted.test", "max_ratio": 0.20, "pages": 1, "delay": [0, 0], "enrich": True,
                     "accept_title_size": True, "min_reviews": 5, "exclude_business": True, "hide_risk": [],
                     "notify": {}, "currency_symbol": "£"},
        "sizes": {"shoes_uk9": {"label": "Shoes UK 9", "field_patterns": [r"^(uk\s*)?9(\s*[|/].*)?$"],
                                "title_patterns": [r"\buk\s*9\b", r"\bsize\s*9\b"], "reject_field": ["child", "kid"]}},
        "targets": {
            "loafers": {"label": "Designer loafers", "size": "shoes_uk9", "category_ids": [2656],
                        "brands": [{"name": "Gucci", "retail": 750, "models": {"jordaan|horsebit": 790}},
                                   {"name": "Prada", "retail": 900}]},
            "boots": {"label": "Boots", "size": "shoes_uk9", "category_ids": [1], "max_price": 40,
                      "queries": ["dr martens boots"], "must_have": [r"dr\.?\s?martens|docs?"],
                      "title_only": False, "sort_price": True},
        },
        "rotation": ["loafers"],
    }
