"""The real targets.yaml: catches the mistakes you make when editing it by hand (a typo in the rotation, a regex
that does not compile, a missing size profile) before they crash a run an hour in."""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "targets.yaml").read_text(encoding="utf-8"))
TARGETS = CFG["targets"]


def regexes(target):
    """Every regex string a target uses."""
    out = list(target.get("must_have", []))
    if target.get("always_match"):
        out.append(target["always_match"])
    for b in target.get("brands", []):
        out += b.get("aliases", [])
        out += list((b.get("models") or {}).keys())
    return out


def test_the_rotation_only_names_real_targets():
    assert CFG["rotation"], "the default rotation must not be empty"
    assert not [k for k in CFG["rotation"] if k not in TARGETS]


@pytest.mark.parametrize("key", TARGETS)
def test_target_is_well_formed(key):
    t = TARGETS[key]
    assert t.get("label")
    assert "category_ids" in t
    if t.get("size"):
        assert t["size"] in CFG["sizes"], f"{key} uses an unknown size profile"
    if "queries" in t:                                        # style target
        assert t["queries"] and t.get("must_have"), "a style target needs queries and must_have"
        if "always_requires" in t:
            assert 0 <= t["always_requires"] < len(t["must_have"])
    else:                                                     # retail-ratio target
        assert t.get("brands") and t.get("size"), "a retail-ratio target needs brands and a size"
        for b in t["brands"]:
            assert b.get("name") and b.get("retail", 0) > 0, f"{key}: {b} needs a name and a retail price"


@pytest.mark.parametrize("key", TARGETS)
def test_target_regexes_compile(key):
    for pattern in regexes(TARGETS[key]):
        re.compile(pattern, re.I)


@pytest.mark.parametrize("name", CFG["sizes"])
def test_size_profiles_are_valid(name):
    p = CFG["sizes"][name]
    assert p["label"] and p["field_patterns"] and p["title_patterns"]
    for pattern in p["field_patterns"] + p["title_patterns"]:
        re.compile(pattern)


def test_settings_are_sane():
    s = CFG["settings"]
    assert 0 < s["max_ratio"] < 1
    lo, hi = s["delay"]
    assert 0 < lo <= hi, "keep a pause between requests"
    assert lo >= 1, "do not hammer Vinted: keep the minimum delay at 1 second or more"


def test_no_secrets_are_committed():
    notify = CFG["settings"]["notify"]
    assert not notify.get("discord_webhook"), "use the VINTED_DISCORD_WEBHOOK environment variable instead"
    assert not notify.get("ntfy_topic"), "use the VINTED_NTFY_TOPIC environment variable instead"
