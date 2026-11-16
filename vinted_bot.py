#!/usr/bin/env python3
"""Vinted deal sniper: finds listings priced under a fraction of retail for the targets in targets.yaml.

    python vinted_bot.py                      run the current rotation once
    python vinted_bot.py --target designer_jeans --brands balmain,amiri
    python vinted_bot.py --watch 600          re-run every ~10 minutes and alert on new deals
    python vinted_bot.py --list               show targets and the active rotation
"""
import argparse
import html
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from curl_cffi import requests
from rich.console import Console
from rich.table import Table

HERE = Path(__file__).resolve().parent
CONSOLE = Console()
CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
CONDITIONS = {"new with tags", "new without tags", "very good", "good", "satisfactory"}
RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

# Titles containing these are not the real thing (or not an adult item), so they are dropped outright.
REJECT_TITLE = re.compile(
    r"\b(replica|inspired|dupe|lookalike|look[- ]alike|fake|copy|knock[- ]?off|unbranded|aaa|1:1|"
    r"child'?s?|children'?s?|kids?|toddler|infant|baby|boys?|girls?)\b|[a-z]-style\b",
    re.I,
)
DOUBT_WORDS = re.compile(r"not authentic|unauthenti|cannot guarantee|can'?t guarantee|unsure|not sure if|no returns", re.I)
PROOF_WORDS = re.compile(r"authentic|receipt|dust ?bag|original box|certificate|proof of purchase|serial|invoice|with tags", re.I)


@dataclass
class Listing:
    id: int
    title: str
    url: str
    price: float
    total: float
    currency: str
    brand_field: str
    size: str
    condition: str
    thumb: str
    favs: int
    brand: str = ""
    retail: float = 0.0
    ratio: float = 1.0
    size_note: str = ""
    risk: str = "LOW"
    flags: list = field(default_factory=list)
    is_new: bool = True
    price_drop: bool = False
    business: bool = False
    hits: int = 0
    feedback: int = None
    reputation: float = None

    @property
    def saving(self):
        return self.retail - self.price


# ───────────────────────────── Vinted client ─────────────────────────────

class Vinted:
    def __init__(self, domain, delay):
        self.base = f"https://www.{domain}"
        self.delay = delay
        self.http = requests.Session(impersonate="chrome")
        self._warm()

    def _warm(self):
        self.http.get(self.base + "/", timeout=30)

    def _get(self, path, params=None):
        for attempt in range(4):
            time.sleep(random.uniform(*self.delay))
            try:
                r = self.http.get(self.base + path, params=params, timeout=30)
            except Exception as e:  # network blip
                CONSOLE.print(f"[yellow]network error ({e}); retrying[/]")
                time.sleep(5)
                continue
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 429):
                wait = 30 * (attempt + 1)
                CONSOLE.print(f"[yellow]Vinted returned {r.status_code}; cooling down {wait}s[/]")
                time.sleep(wait)
                self._warm()
                continue
            raise RuntimeError(f"{path} -> HTTP {r.status_code}")
        raise RuntimeError(f"{path} kept failing (blocked?). Try again later or raise settings.delay")

    @staticmethod
    def _payload(page_html):
        return "".join(json.loads(f'"{c}"') for c in CHUNK.findall(page_html))

    def search(self, text, category_ids, price_to, page=1, order="newest_first"):
        params = [("search_text", text), ("order", order), ("price_to", price_to), ("page", page)]
        params += [("catalog[]", c) for c in category_ids]
        blob = self._payload(self._get("/catalog", params))
        key = '"items":{"items":['
        i = blob.find(key)
        if i < 0:
            return [], 1
        raw, _ = json.JSONDecoder().raw_decode(blob[i + len(key) - 1:])
        pages = re.search(r'"pagination":\{[^}]*"total_pages":(\d+)', blob)
        return [self._listing(r["productItem"]) for r in raw if "productItem" in r], int(pages.group(1)) if pages else 1

    def seller_and_description(self, url):
        """Extra signals from the item page: description, seller review count, photo count."""
        blob = self._payload(self._get(url))
        desc = max(re.findall(r'"description":"((?:[^"\\]|\\.)*)"', blob), key=len, default="")
        fb = re.search(r'"feedback_count":(\d+)', blob)
        rep = re.search(r'"feedback_reputation":([\d.]+)', blob)
        photos = set(re.findall(r'"id":"([0-9a-z_]+)","is_main":(?:true|false)', blob))
        return {
            "description": json.loads(f'"{desc}"') if desc else "",
            "feedback": int(fb.group(1)) if fb else None,
            "reputation": float(rep.group(1)) if rep else None,
            "photos": len(photos) or None,
        }

    @staticmethod
    def _listing(p):
        box = p.get("itemBox") or {}
        parts = [x for x in (box.get("secondLine") or "").split(" · ")]
        condition = parts[-1] if parts and parts[-1].lower() in CONDITIONS else ""
        size = " · ".join(parts[:-1] if condition else parts)
        brand = box.get("firstLine") or ""
        return Listing(
            id=int(p["id"]), title=p.get("title") or "", url=p["url"],
            price=float(p["price"]["amount"]), total=float((p.get("totalItemPrice") or p["price"])["amount"]),
            currency=p["price"]["currencyCode"], brand_field="" if brand == "$undefined" else brand,
            size=size.strip(), condition=condition, thumb=p.get("thumbnailUrl") or "", favs=p.get("favouriteCount") or 0,
            business=bool((p.get("user") or {}).get("isBusiness")),
        )


# ───────────────────────────── matching & scoring ─────────────────────────────

def norm(s):
    return (s or "").replace("’", "'").replace("‘", "'").lower()


def compile_brands(brands):
    out = []
    for b in brands:
        aliases = b.get("aliases") or [re.escape(norm(b["name"]))]
        out.append({
            **b,
            "rx": re.compile(r"(?<![a-z])(" + "|".join(aliases) + r")(?![a-z])", re.I),
            "models": [(re.compile(k, re.I), v) for k, v in (b.get("models") or {}).items()],
        })
    return out


def size_ok(listing, profile, accept_title):
    """Returns (matched, note). Note explains a title-only match."""
    field_text = norm(listing.size)
    if any(w in field_text for w in profile.get("reject_field", [])):
        return False, ""
    if any(re.search(p, field_text) for p in profile["field_patterns"]):
        return True, ""
    if accept_title and any(re.search(p, norm(listing.title)) for p in profile["title_patterns"]):
        return True, f"size from title (listing says '{listing.size or 'none'}')"
    return False, ""


def assess(l, brand, extra, max_ratio):
    """Fills risk + flags. Cheap designer pieces are often fakes, so this is the sanity layer."""
    score, flags = 0, []
    if l.ratio < 0.08:
        score += 40; flags.append(f"only {l.ratio:.0%} of retail: bait/fake territory")
    elif l.ratio < 0.12:
        score += 20; flags.append(f"{l.ratio:.0%} of retail: unusually cheap")
    field_brand = norm(l.brand_field)
    if field_brand and not brand["rx"].search(field_brand):
        score += 30; flags.append(f"brand field says '{l.brand_field}'")
    if not field_brand:
        score += 10; flags.append("no brand set")
    text = f"{l.title} {extra.get('description', '')}"
    if DOUBT_WORDS.search(text):
        score += 25; flags.append("seller hedges on authenticity")
    if PROOF_WORDS.search(text):
        score -= 15; flags.append("mentions proof/box/receipt")
    if extra:
        fb = extra.get("feedback")
        if fb == 0:
            score += 20; flags.append("seller has 0 reviews")
        elif fb is not None and fb < 5:
            score += 8; flags.append(f"seller has {fb} reviews")
        elif fb is not None and fb >= 20:
            score -= 10
        if extra.get("photos") is not None and extra["photos"] < 4:
            score += 15; flags.append(f"only {extra['photos']} photos")
        if len(extra.get("description", "")) < 20:
            score += 10; flags.append("no real description")
    l.risk = "HIGH" if score >= 40 else "MEDIUM" if score >= 15 else "LOW"
    l.flags += flags


# ───────────────────────────── persistence & alerts ─────────────────────────────

def open_db():
    db = sqlite3.connect(HERE / "vinted_seen.db")
    db.execute("CREATE TABLE IF NOT EXISTS seen (id INTEGER, target TEXT, price REAL, first_seen TEXT, PRIMARY KEY (id, target))")
    return db


def mark_seen(db, target, deals):
    for d in deals:
        row = db.execute("SELECT price FROM seen WHERE id=? AND target=?", (d.id, target)).fetchone()
        d.is_new = row is None
        d.price_drop = row is not None and d.price < row[0]
        if row is None:
            db.execute("INSERT INTO seen VALUES (?,?,?,?)", (d.id, target, d.price, datetime.now().isoformat(timespec="seconds")))
        elif d.price_drop:
            db.execute("UPDATE seen SET price=? WHERE id=? AND target=?", (d.price, d.id, target))
    db.commit()


def notify(cfg, target_label, deals, base):
    n = cfg.get("notify") or {}
    hook = os.environ.get("VINTED_DISCORD_WEBHOOK") or n.get("discord_webhook")
    topic = os.environ.get("VINTED_NTFY_TOPIC") or n.get("ntfy_topic")
    ceiling = RISK_ORDER.get(n.get("min_risk", "MEDIUM"), 1)
    fresh = [d for d in deals if (d.is_new or d.price_drop) and RISK_ORDER.get(d.risk, 0) <= ceiling]
    for d in fresh:
        vs = f" vs ~{d.retail:.0f} retail ({d.ratio:.0%}) | risk {d.risk}" if d.retail else ""
        line = f"{d.brand} | {d.title} | {d.currency} {d.price:.0f}{vs} | size {d.size or '?'}"
        link = base + d.url
        try:
            if hook:
                requests.post(hook, json={"content": f"**{target_label}** deal\n{line}\n{link}"}, timeout=15)
            if topic:
                requests.post(f"https://ntfy.sh/{topic}", data=line.encode(), headers={"Title": f"{target_label} deal", "Click": link}, timeout=15)
        except Exception as e:
            CONSOLE.print(f"[yellow]alert failed: {e}[/]")


# ───────────────────────────── output ─────────────────────────────

def seller_label(d):
    if d.feedback is None:
        return "?"
    stars = f" {d.reputation * 5:.1f}★" if d.reputation else ""
    return f"{d.feedback} reviews{stars}"


def show(label, deals, base, max_ratio):
    rule = f"at or under {max_ratio:.0%} of retail" if max_ratio else "matching your search"
    if not deals:
        CONSOLE.print(f"\n[bold]{label}[/]: no listings {rule} in your size right now.")
        return
    t = Table(title=f"{label}: {rule}", show_lines=True)
    for col in ("", "Item", "Size", "Price", "Retail", "%", "Seller", "Risk", "Flags / link"):
        t.add_column(col, overflow="fold")
    colour = {"LOW": "green", "MEDIUM": "yellow", "HIGH": "red"}
    for d in deals:
        tag = "[bold magenta]NEW[/]" if d.is_new else ("[cyan]DROP[/]" if d.price_drop else "")
        flags = "\n".join(d.flags + ([d.size_note] if d.size_note else []))
        t.add_row(tag, f"[bold]{d.brand}[/]\n{d.title[:60]}", d.size or "?", f"{d.currency} {d.price:.0f}",
                  f"~{d.retail:.0f}" if d.retail else "-", f"{d.ratio:.0%}" if d.retail else "-", seller_label(d),
                  f"[{colour.get(d.risk, 'white')}]{d.risk}[/]", f"{flags}\n[blue]{base + d.url}[/]")
    CONSOLE.print(t)


def write_report(label, deals, base, max_ratio, target_key):
    out = HERE / "reports"
    out.mkdir(exist_ok=True)
    cards = []
    for d in deals:
        flags = "".join(f"<li>{html.escape(f)}</li>" for f in d.flags + ([d.size_note] if d.size_note else []))
        badge = "NEW" if d.is_new else ("PRICE DROP" if d.price_drop else "")
        cards.append(
            f'<a class="card" href="{html.escape(base + d.url)}" target="_blank"><img src="{html.escape(d.thumb)}" loading="lazy">'
            f'<div class="body"><div class="top"><b>{html.escape(d.brand)}</b>'
            f'{f" <span class=\'pill {d.risk}\'>{d.risk} risk</span>" if d.retail else ""}'
            f'{f"<span class=new>{badge}</span>" if badge else ""}</div><div class="t">{html.escape(d.title)}</div>'
            f'<div class="p">{d.currency} {d.price:.0f} <small>(inc. fee {d.total:.0f})</small> '
            f'{f"<s>~{d.retail:.0f}</s> <b>{d.ratio:.0%} of retail, save ~{d.saving:.0f}</b>" if d.retail else ""}</div>'
            f'<div class="m">Size {html.escape(d.size or "?")} · {html.escape(d.condition or "?")} · ♥ {d.favs} · seller: {html.escape(seller_label(d))}</div>'
            f'<ul>{flags}</ul></div></a>'
        )
    page = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(label)} deals</title><style>
body{{font:15px system-ui;background:#0f1115;color:#e8e8ea;margin:0;padding:20px;max-width:1000px;margin:auto}}
h1{{font-size:20px}} .sub{{color:#8b8f98;margin-bottom:16px}}
.card{{display:flex;gap:14px;background:#181b22;border:1px solid #262a33;border-radius:12px;padding:12px;margin-bottom:12px;color:inherit;text-decoration:none}}
.card:hover{{border-color:#4a8cff}} img{{width:130px;height:170px;object-fit:cover;border-radius:8px;flex:none}}
.t{{color:#b9bdc6;margin:4px 0}} .p{{font-size:17px;margin:6px 0}} .p s{{color:#8b8f98}} .p b{{color:#5ee08a;font-size:14px}}
.m{{color:#8b8f98;font-size:13px}} ul{{margin:8px 0 0;padding-left:18px;color:#d9a85b;font-size:13px}}
.pill{{font-size:11px;padding:2px 8px;border-radius:99px;margin-left:6px}} .LOW{{background:#17402a;color:#5ee08a}}
.MEDIUM{{background:#46391a;color:#f0c35a}} .HIGH{{background:#4a1d1d;color:#ff7b7b}}
.new{{background:#3b2a66;color:#c9a8ff;font-size:11px;padding:2px 8px;border-radius:99px;margin-left:6px}}
@media(max-width:600px){{img{{width:96px;height:130px}}}}</style>
<h1>{html.escape(label)}: {len(deals)} listing(s) {f"at or under {max_ratio:.0%} of retail" if max_ratio else "matching your search"}</h1>
<div class="sub">{datetime.now():%d %b %Y %H:%M} · retail figures are estimates · cheap designer pieces are often fake, check the flags</div>
{''.join(cards) or '<p>No deals this run.</p>'}"""
    path = out / f"{target_key}_{datetime.now():%Y%m%d_%H%M%S}.html"
    path.write_text(page, encoding="utf-8")
    (out / "latest.html").write_text(page, encoding="utf-8")
    return path


# ───────────────────────────── main flow ─────────────────────────────

def hunt_style(client, db, cfg, key, args):
    """Keyword hunt for a look (no retail price): price cap + size + required wording in title/description."""
    s, tgt = cfg["settings"], cfg["targets"][key]
    cap = args.max_price or tgt.get("max_price") or s.get("max_price")
    profile = cfg["sizes"][tgt["size"]] if tgt.get("size") else None  # no size = size-less goods like CDs
    groups = [re.compile(g, re.I) for g in tgt["must_have"]]
    always = re.compile(tgt["always_match"], re.I) if tgt.get("always_match") else None
    min_reviews = tgt.get("min_reviews", s.get("min_reviews", 0)) if args.min_reviews is None else args.min_reviews
    drop_business = s.get("exclude_business", False) and not args.allow_business
    CONSOLE.rule(f"[bold]{tgt['label']}[/] · {profile['label'] if profile else 'any size'} · max {s.get('currency_symbol', '£')}{cap} · {len(tgt['queries'])} searches")

    found, scanned = {}, 0
    for entry in tgt["queries"]:
        q, qcap = (entry["q"], entry.get("max_price", cap)) if isinstance(entry, dict) else (entry, cap)  # per-search price cap
        if args.max_price:  # --max-price is a hard ceiling over any per-search cap
            qcap = min(qcap, args.max_price)
        for page in range(1, (args.pages or tgt.get("pages", 2)) + 1):
            try:
                items, total_pages = client.search(q, tgt["category_ids"], qcap, page, order="relevance")
            except RuntimeError as e:
                CONSOLE.print(f"[red]{q}: {e}[/]")
                break
            scanned += len(items)
            for l in items:
                if l.id in found or l.price > qcap or REJECT_TITLE.search(norm(l.title)) or (drop_business and l.business):
                    continue
                ok, note = size_ok(l, profile, s["accept_title_size"]) if profile else (True, "")
                if ok:
                    l.size_note = note
                    found[l.id] = l
            if page >= total_pages or not items:
                break
        CONSOLE.print(f"  [dim]{q:<36} done · {len(found)} candidates so far[/]")

    if tgt.get("title_only"):  # only open pages for listings whose title already shows the look
        before = len(found)
        found = {i: l for i, l in found.items()
                 if (always and always.search(f"{l.brand_field} {l.title}")) or all(g.search(l.title) for g in groups)}
        CONSOLE.print(f"title check: {len(found)} of {before} candidates name the look in the title")

    deals = []
    for l in found.values():
        extra = {}
        for attempt in range(2 if tgt.get("enrich", True) else 0):  # one retry so a rate-limit blip doesn't silently drop a listing
            try:
                extra = client.seller_and_description(l.url)
                break
            except RuntimeError:
                extra = None
                time.sleep(20)
        if extra is None:
            CONSOLE.print(f"[yellow]couldn't check {l.url}, skipped[/]")
            continue
        l.feedback, l.reputation = extra.get("feedback"), extra.get("reputation")
        if min_reviews and tgt.get("enrich", True) and (l.feedback is None or l.feedback < min_reviews):
            continue
        text = f"{l.title} {extra.get('description', '')}"
        hits = [{m.group(0).lower() for m in g.finditer(text)} for g in groups]
        favourite = always and always.search(f"{l.brand_field} {text}")
        need = tgt.get("always_requires")  # the priority brand must also match this group (e.g. be anime)
        if favourite and need is not None and not hits[need]:
            favourite = False
        if not all(hits) and not favourite:
            continue
        if favourite:
            hits.append({"favourite brand"})
        l.brand = l.brand_field or "unbranded"
        l.risk = "n/a"
        l.flags = ["matches: " + ", ".join(sorted(set().union(*hits)))] + ([l.size_note] if l.size_note else [])
        l.hits = len(set().union(*hits)) + (10 if favourite else 0)  # the brand itself outranks lookalikes
        deals.append(l)
    if tgt.get("sort_price"):  # priority brand/artist first, then cheapest
        deals.sort(key=lambda d: (-(d.hits >= 10), d.price))
    else:
        deals.sort(key=lambda d: (-d.hits, -min(d.feedback or 0, 50) // 25, -d.favs, d.price))

    mark_seen(db, key, deals)
    CONSOLE.print(f"scanned {scanned} listings, {len(deals)} match")
    show(tgt["label"], deals, client.base, None)
    path = write_report(tgt["label"], deals, client.base, None, key)
    CONSOLE.print(f"report: {path}")
    notify(s, tgt["label"], deals, client.base)
    return path, deals


def hunt(client, db, cfg, key, args):
    if "queries" in cfg["targets"][key]:
        return hunt_style(client, db, cfg, key, args)
    s, tgt = cfg["settings"], cfg["targets"][key]
    max_ratio = args.max_ratio or s["max_ratio"]
    profile = cfg["sizes"][tgt["size"]]
    brands = compile_brands(tgt["brands"])
    if args.brands:
        wanted = [norm(b) for b in args.brands.split(",")]
        brands = [b for b in brands if any(w in norm(b["name"]) for w in wanted)]
    CONSOLE.rule(f"[bold]{tgt['label']}[/] · {profile['label']} · max {max_ratio:.0%} of retail · {len(brands)} brands")

    found, scanned = {}, 0
    for b in brands:
        ceiling = math.ceil(b["retail"] * max_ratio)
        if args.max_price:  # absolute price ceiling on top of the retail ratio
            ceiling = min(ceiling, math.ceil(args.max_price))
        query = f"{b['name']} {tgt.get('search_suffix', '')}".strip()
        for page in range(1, (args.pages or s["pages"]) + 1):
            try:
                items, total_pages = client.search(query, tgt["category_ids"], ceiling, page)
            except RuntimeError as e:
                CONSOLE.print(f"[red]{b['name']}: {e}[/]")
                break
            scanned += len(items)
            for l in items:
                if l.id in found or REJECT_TITLE.search(norm(l.title)):
                    continue
                if not (b["rx"].search(norm(l.brand_field)) or b["rx"].search(norm(l.title))):
                    continue
                ok, note = size_ok(l, profile, s["accept_title_size"])
                if not ok:
                    continue
                l.brand, l.size_note = b["name"], note
                l.retail = next((v for rx, v in b["models"] if rx.search(l.title)), b["retail"])
                l.ratio = l.price / l.retail
                if l.ratio <= max_ratio:
                    found[l.id] = (l, b)
            if page >= total_pages or not items:
                break
        CONSOLE.print(f"  [dim]{b['name']:<20} done · {len(found)} candidates so far[/]")

    min_reviews = s.get("min_reviews", 0) if args.min_reviews is None else args.min_reviews
    drop_business = s.get("exclude_business", False) and not args.allow_business
    candidates = [(l, b) for l, b in found.values() if not (drop_business and l.business)]
    if drop_business:
        CONSOLE.print(f"dropped {len(found) - len(candidates)} business-account listings")

    deals = []
    for l, b in candidates:
        extra = {}
        if s["enrich"] and not args.no_enrich:
            try:
                extra = client.seller_and_description(l.url)
            except RuntimeError:
                pass
        l.feedback, l.reputation = extra.get("feedback"), extra.get("reputation")
        if min_reviews and (l.feedback is None or l.feedback < min_reviews):
            continue
        assess(l, b, extra, max_ratio)
        if l.risk not in s["hide_risk"]:
            deals.append(l)
    if min_reviews:
        CONSOLE.print(f"kept {len(deals)} from sellers with {min_reviews}+ reviews")

    def trust(d):  # established sellers first
        return 0 if (d.feedback or 0) >= 50 else 1 if (d.feedback or 0) >= 10 else 2
    deals.sort(key=lambda d: (RISK_ORDER[d.risk], trust(d), d.ratio))

    mark_seen(db, key, deals)
    CONSOLE.print(f"scanned {scanned} listings, {len(deals)} deal(s)")
    show(tgt["label"], deals, client.base, max_ratio)
    path = write_report(tgt["label"], deals, client.base, max_ratio, key)
    CONSOLE.print(f"report: {path}")
    notify(s, tgt["label"], deals, client.base)
    return path, deals


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "targets.yaml"))
    ap.add_argument("--target", help="run one target instead of the rotation (comma-separate for several)")
    ap.add_argument("--brands", help="only these brands, comma-separated (substring match)")
    ap.add_argument("--max-ratio", type=float, help="override max fraction of retail, e.g. 0.25")
    ap.add_argument("--pages", type=int, help="result pages per brand")
    ap.add_argument("--watch", type=int, metavar="SECONDS", help="loop forever, alerting on new deals")
    ap.add_argument("--max-price", type=float, help="absolute price ceiling for every target, e.g. 10")
    ap.add_argument("--min-reviews", type=int, help="only sellers with at least this many reviews")
    ap.add_argument("--allow-business", action="store_true", help="include business/pro seller accounts")
    ap.add_argument("--no-enrich", action="store_true", help="skip per-item seller/description checks (faster)")
    ap.add_argument("--open", action="store_true", help="open the HTML report when done")
    ap.add_argument("--list", action="store_true", help="list targets and exit")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.list:
        for k, t in cfg["targets"].items():
            on = "*" if k in cfg["rotation"] else " "
            size = cfg["sizes"][t["size"]]["label"] if t.get("size") else "any size"
            scope = f"{len(t['brands'])} brands" if "brands" in t else f"{len(t['queries'])} searches"
            CONSOLE.print(f"{on} {k:<26} {t['label'][:38]:<38} {size:<42} {scope}")
        CONSOLE.print("\n* = in the current rotation")
        return
    if args.max_price is None and cfg["settings"].get("price_ceiling"):
        args.max_price = float(cfg["settings"]["price_ceiling"])  # pass --max-price 0 to switch this off for one run
    keys = args.target.split(",") if args.target else cfg["rotation"]
    for k in keys:
        if k not in cfg["targets"]:
            sys.exit(f"unknown target '{k}'. Available: {', '.join(cfg['targets'])}")

    client = Vinted(cfg["settings"]["domain"], cfg["settings"]["delay"])
    db = open_db()
    while True:
        last = None
        for k in keys:
            last, _ = hunt(client, db, cfg, k, args)
        if args.open and last:
            webbrowser.open(last.as_uri())
        if not args.watch:
            break
        wait = args.watch * random.uniform(0.9, 1.2)
        CONSOLE.print(f"[dim]sleeping {wait / 60:.1f} min (Ctrl+C to stop)[/]")
        time.sleep(wait)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except KeyboardInterrupt:
        pass
