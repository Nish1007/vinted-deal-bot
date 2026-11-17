# Vinted deal bot

[![tests](https://github.com/Nish1007/vinted-deal-bot/actions/workflows/tests.yml/badge.svg)](https://github.com/Nish1007/vinted-deal-bot/actions/workflows/tests.yml)

A small Python bot that searches Vinted UK for deals and reports the good ones. What it hunts is set in a YAML file, so you can swap items without touching the code.

It does two kinds of hunt:

- **Retail-ratio hunts** (designer loafers, jeans, coats): find listings priced at or below a fraction of retail (default 20%), in your size, and rate each one for fake/scam risk.
- **Style hunts** (Dr. Martens, Clarks Wallabees, Asics, Derschutze, CDs, anime gym tops and more): find listings that match keywords, in your size, under a price cap.

Every result is filtered to sellers with real reviews and no business accounts (both configurable).

## Setup

Needs Python 3.12 or newer (the HTML report code uses f-string syntax from 3.12).

```
python -m venv venv
venv\Scripts\pip install -r requirements.txt     # Windows
venv/bin/pip install -r requirements.txt         # macOS / Linux
```

On Windows keep the venv path short: `curl_cffi` fails to load from very long paths.

## Use

```
python vinted_bot.py --list                          # every target, the sizes, which are in the rotation
python vinted_bot.py                                 # run the current rotation once
python vinted_bot.py --target clarks_wallabees       # run one target
python vinted_bot.py --target dr_martens --max-price 55
python vinted_bot.py --target designer_loafers --max-ratio 0.25
python vinted_bot.py --watch 600 --open              # loop every ~10 minutes, alert on new deals
```

| Flag | What it does |
| --- | --- |
| `--config FILE` | use another YAML config (default `targets.yaml`) |
| `--target NAME` | run one target instead of the rotation |
| `--brands a,b` | only some brands (retail-ratio targets) |
| `--max-ratio 0.25` | loosen or tighten the retail-ratio threshold |
| `--max-price 40` | hard price ceiling in pounds, overrides every cap; `0` disables the ceiling |
| `--min-reviews 20` | only sellers with at least this many reviews |
| `--allow-business` | include business seller accounts |
| `--pages N` | result pages per search (96 listings each) |
| `--no-enrich` | skip opening each listing (faster, but no review or description checks) |
| `--watch SECONDS` | repeat forever, alerting on new deals |
| `--open` | open `reports/latest.html` after each run |

Each run prints a table, writes an HTML report with photos and links to `reports/latest.html`, and remembers what it has shown you in `vinted_seen.db`, so repeat runs mark listings as NEW or PRICE DROP.

`settings.price_ceiling` in `targets.yaml` is applied to every target when you don't pass `--max-price`. It ships set to 10 (a "steals" mode). Set it to `0` or remove it to use each target's own cap.

## Targets

Defined in `targets.yaml`. The ones included:

| Target | Type | What it finds |
| --- | --- | --- |
| `designer_loafers`, `designer_jeans`, `designer_coats` | retail ratio | designer brands at 20% of retail or less |
| `japanese_shoes` | style | Asics, Onitsuka Tiger, Mizuno, Derschutze, Margiela Tabi |
| `dr_martens` | style | Dr. Martens shoes and boots |
| `dr_martens_1461_bex`, `dr_martens_1461_smooth80`, `dr_martens_yellow_stitch` | style | specific Dr. Martens 1461 variants |
| `clarks_wallabees` | style | Clarks Wallabees, desert boots, Clarks Originals |
| `derschutze_everything`, `derschutze_floral_jeans` | style | Derschutze clothing and lookalike embroidered pieces |
| `embroidered_baggy_jeans`, `cool_jumpers` | style | embroidered baggy jeans and statement knitwear |
| `youngla_anime_tops` | style | YoungLA and other oversized anime gym tops |
| `cds_for_you` | style | CDs by chosen artists, cheapest first |

The sizes in `targets.yaml` are example profiles (shoes UK 9, jeans waist 36, tops L/XL). Change them under `sizes:` to fit you.

## Add your own target

Copy a block under `targets:` and edit it.

A **style target** needs:

```yaml
my_target:
  label: "Readable name"
  size: shoes_uk9            # a profile under sizes:, or leave out for size-less goods
  category_ids: [1231]       # the number in a Vinted catalog URL, e.g. /catalog/1231-shoes
  max_price: 40
  pages: 3
  title_only: true           # only open listings whose title already names the look (much faster)
  sort_price: true           # priority matches first, then cheapest
  always_match: 'some|brand' # optional: a regex that skips the must_have check
  queries:
    - search words
    - more search words
  must_have:                 # every regex group must match the title or description
    - 'brand|model'
```

A **retail-ratio target** lists `brands:` with an estimated retail price for each. A listing qualifies when `price <= max_ratio * retail`. Those retail prices are rough estimates, so edit them.

Then add its name to `rotation:` to include it in a default run.

## Size matching

Vinted's size IDs aren't exposed in the page, so sizes are matched on the client side: a regex against the listing's size field, and optionally against the title (`accept_title_size`). Where the title and the size field disagree, the listing is kept but flagged "size from title". Always confirm the size with the seller, especially for US versus UK sizes.

## Fake / scam flags

Designer goods at a fraction of retail are often counterfeit or bait. Retail-ratio deals get a LOW / MEDIUM / HIGH risk rating from how cheap they are, whether the brand field matches, seller review count, photo count, description quality, and authenticity wording. Treat it as a hint, not a guarantee. Check the photos, stitching and labels yourself.

## Alerts

Set `notify.discord_webhook` or `notify.ntfy_topic` in `targets.yaml`, or the environment variables `VINTED_DISCORD_WEBHOOK` / `VINTED_NTFY_TOPIC`. With `--watch`, new deals at or below `min_risk` are pushed to you. Don't commit a real webhook URL to a public repository.

## How it works

Vinted's catalog pages embed their data in Next.js payloads. The bot fetches a catalog search page with a browser-like TLS fingerprint (`curl_cffi`), decodes the embedded listing data, and then opens each candidate listing to read the seller's review count, rating and description. It waits a random 2 to 4 seconds between requests and backs off when Vinted returns 429 or 403. Running two copies at the same time triggers rate limits, so run one at a time.

## Development

```
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite has 113 tests and runs in under a second, **fully offline**: the Vinted client is replaced by a fake that returns canned search pages and seller details, so nothing contacts vinted.co.uk. GitHub Actions runs it on every push.

| File | What it covers |
| --- | --- |
| `tests/test_matching.py` | brand and alias matching, rejecting "replica" and kids' listings, size matching, every fake-risk rule |
| `tests/test_client.py` | parsing Vinted's embedded page data, and the polite behaviour: random delay, growing cool-downs on 403 and 429, no retry on other errors |
| `tests/test_hunt.py` | whole hunts against a fake Vinted: which listings survive the filters, their order, and that rejected listings are never opened |
| `tests/test_persistence.py` | NEW and PRICE DROP tracking in SQLite, alert filtering, HTML escaping in reports |
| `tests/test_config.py` | the real `targets.yaml`: every regex compiles, the rotation names real targets, the delay stays polite, no secrets committed |

### Claude Code setup

The repo is set up for working with [Claude Code](https://claude.com/claude-code):

- `CLAUDE.md` describes the layout, the commands and the rules that must not be broken (read-only, polite, no secrets, offline tests).
- `.claude/agents/` has three subagents: `test-writer` (offline tests, then proves they fail when the code is broken), `code-reviewer` (read-only review against those rules) and `target-tuner` (edits `targets.yaml` and validates it without touching the network).
- `.claude/settings.json` lets the test suite and `--list` run without prompts, and asks before any command that would contact Vinted.

## Notes

- Vinted's terms restrict automated access. Use this for light personal use and keep any `--watch` interval sensible.
- The bot only reads public listing pages. It doesn't log in, buy, or message anyone.
- Prices, sellers and listings change constantly. Results are a starting point, not a guarantee.
