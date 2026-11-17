# Vinted deal bot: notes for Claude Code

A single-file Python bot (`vinted_bot.py`) that searches Vinted UK for under-priced listings and writes a table, an HTML report and optional phone alerts. What it hunts lives in `targets.yaml`, so most changes are config, not code. See `README.md` for what it does and how to use it.

## Commands

```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q                  # the whole suite: offline, under a second
python vinted_bot.py --list          # prints the targets; offline, safe
```

Any other `python vinted_bot.py ...` run **contacts vinted.co.uk**. Do not run one unless the user asked for a live run.

## Layout

| Path | What it is |
| --- | --- |
| `vinted_bot.py` | Everything: `Vinted` client, matching (`size_ok`, `compile_brands`), scoring (`assess`), persistence (`open_db`, `mark_seen`), alerts (`notify`), output (`show`, `write_report`), the two hunts (`hunt`, `hunt_style`) and `main`. |
| `targets.yaml` | Settings, size profiles, targets and the default `rotation`. |
| `tests/` | `conftest.py` holds the fakes: `FakeClient`, `make_listing`, `next_page`, `seller`, `args` and the `cfg` fixture. One file per area: matching, client, persistence, hunt, config. |

## Rules that must not be broken

1. **Read-only.** The bot only reads public listing pages. Never add login, buying, messaging, favouriting or any request that changes something on Vinted.
2. **Stay polite.** Keep the random delay between requests (`settings.delay`, minimum 1 second, enforced by `tests/test_config.py`), keep the cool-down on 403 and 429, and never make requests in parallel. Do not add retries that skip the back-off.
3. **No live traffic from tests or checks.** Tests use `FakeClient` and a fake HTTP session. A test that opens a real connection is a bug.
4. **No secrets in the repo.** Webhook and topic values come from `VINTED_DISCORD_WEBHOOK` and `VINTED_NTFY_TOPIC`; `targets.yaml` must keep them empty. `reports/` and `vinted_seen.db` are git-ignored and stay that way.
5. **Python 3.12 or newer.** `write_report` uses f-string syntax that older versions reject.

## Conventions

- **Config over code.** A new hunt is a new block in `targets.yaml` (README, "Add your own target"), not new Python. `tests/test_config.py` checks every target, size profile and regex, so run it after editing the file.
- **Behaviour changes need tests.** Anything that alters matching, scoring (`assess`), sorting or alert rules gets a test in the matching file for that area. Run `python -m pytest -q` before saying a change is done.
- **Risk scoring is a hint, not a verdict.** Keep flags human-readable (they are shown in the table and the report) and keep `LOW < MEDIUM < HIGH` ordering.
- Retail prices in `targets.yaml` are rough estimates. Say so when changing them; do not present them as facts.
- HTML in reports is built by string formatting: every piece of listing text must go through `html.escape`.
- The tests point `vinted_bot.HERE` at a temp folder, so the database and reports never touch the repo. Reuse that pattern for anything that writes files.

## Subagents (`.claude/agents/`)

| Agent | Use it for |
| --- | --- |
| `test-writer` | Writing offline tests for a change, then checking they fail when the code is broken. |
| `code-reviewer` | Reviewing a diff against the rules above before it is committed. Read-only. |
| `target-tuner` | Adding or adjusting targets, sizes and retail estimates in `targets.yaml` without touching the network. |
