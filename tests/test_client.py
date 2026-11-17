"""The Vinted client: parsing Vinted's embedded page data, and the polite retry / back-off behaviour.
The network is replaced by a fake session, so none of this talks to Vinted."""
import json

import pytest

import vinted_bot as vb
from conftest import next_page, product_item


class Resp:
    def __init__(self, status=200, text=""):
        self.status_code, self.text = status, text


class FakeHTTP:
    """Returns queued responses in order (an Exception in the queue is raised); then `default` forever."""

    def __init__(self, queue=(), default=None):
        self.queue, self.default, self.calls = list(queue), default or Resp(403), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        item = self.queue.pop(0) if self.queue else self.default
        if isinstance(item, Exception):
            raise item
        return item


def client(http):
    v = object.__new__(vb.Vinted)                    # skip __init__, which would hit the network
    v.base, v.delay, v.http = "https://www.vinted.test", (0, 0), http
    return v


# ---------------------------------------------------------------- parsing
def test_payload_reassembles_the_script_chunks():
    page = next_page('{"hello": "wörld \\"quoted\\""}', pieces=3)
    assert vb.Vinted._payload(page) == '{"hello": "wörld \\"quoted\\""}'


def test_search_parses_listings_and_pagination():
    items = [product_item(1, "Gucci loafers", "80.0", "Gucci", "9 · Very good"),
             product_item(2, "Prada shoes", "55.5", "Prada", "UK 9 · Good", business=True),
             {"somethingElse": {}}]                  # not a product: ignored
    blob = '"items":{"items":' + json.dumps(items) + '},"pagination":{"current_page":1,"total_pages":4}'
    http = FakeHTTP([Resp(200, next_page(blob))])
    v = client(http)
    found, pages = v.search("gucci", [2656], 150)
    assert pages == 4
    assert [l.id for l in found] == [1, 2]
    first, second = found
    assert (first.price, first.total, first.currency) == (80.0, 83.0, "GBP")
    assert (first.size, first.condition, first.brand_field) == ("9", "Very good", "Gucci")
    assert second.business is True and second.size == "UK 9"
    _, params = http.calls[-1]
    assert ("search_text", "gucci") in params and ("price_to", 150) in params and ("catalog[]", 2656) in params


def test_search_with_no_results_block_returns_empty():
    v = client(FakeHTTP([Resp(200, next_page('{"nothing": true}'))]))
    assert v.search("gucci", [1], 10) == ([], 1)


def test_undefined_brand_field_becomes_empty():
    l = vb.Vinted._listing(product_item(5, brand="$undefined")["productItem"])
    assert l.brand_field == ""


def test_seller_and_description():
    blob = ('{"description":"Authentic, with receipt\\nand box","feedback_count":42,"feedback_reputation":0.98,'
            '"photos":[{"id":"ab_1","is_main":true},{"id":"ab_2","is_main":false},{"id":"ab_2","is_main":false}]}')
    v = client(FakeHTTP([Resp(200, next_page(blob))]))
    out = v.seller_and_description("/items/1-x")
    assert out["description"] == "Authentic, with receipt\nand box"
    assert (out["feedback"], out["reputation"]) == (42, 0.98)
    assert out["photos"] == 2                        # photo ids are de-duplicated


def test_seller_details_missing_gives_none_not_a_crash():
    out = client(FakeHTTP([Resp(200, next_page("{}"))])).seller_and_description("/items/1-x")
    assert out == {"description": "", "feedback": None, "reputation": None, "photos": None}


# ---------------------------------------------------------------- being polite: retries and back-off
def test_rate_limit_triggers_a_cooldown_then_a_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(vb.time, "sleep", sleeps.append)
    http = FakeHTTP([Resp(429), Resp(200, "warm again"), Resp(200, "the page")])
    assert client(http)._get("/catalog") == "the page"
    assert 30 in sleeps                              # first cool-down is 30 s


def test_cooldown_grows_with_each_block(monkeypatch):
    sleeps = []
    monkeypatch.setattr(vb.time, "sleep", sleeps.append)
    http = FakeHTTP([Resp(403), Resp(200), Resp(403), Resp(200), Resp(200, "finally")])
    assert client(http)._get("/catalog") == "finally"
    assert [s for s in sleeps if s >= 30] == [30, 60]


def test_gives_up_when_blocked_for_good():
    with pytest.raises(RuntimeError, match="kept failing"):
        client(FakeHTTP(default=Resp(403)))._get("/catalog")


def test_other_http_errors_fail_immediately():
    http = FakeHTTP([Resp(500)])
    with pytest.raises(RuntimeError, match="HTTP 500"):
        client(http)._get("/catalog")
    assert len(http.calls) == 1                      # no hammering the server


def test_network_blips_are_retried():
    http = FakeHTTP([ConnectionError("reset"), Resp(200, "ok")])
    assert client(http)._get("/catalog") == "ok"


def test_every_request_waits_a_random_delay_first(monkeypatch):
    waits = []
    monkeypatch.setattr(vb.time, "sleep", waits.append)
    v = client(FakeHTTP([Resp(200, "ok")]))
    v.delay = (2.0, 4.0)
    v._get("/catalog")
    assert 2.0 <= waits[0] <= 4.0
