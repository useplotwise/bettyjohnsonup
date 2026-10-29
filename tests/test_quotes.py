import pytest

from tradebot.kalshi_client import KalshiError
from tradebot.strategy.quotes import best_yes_quote, request_combo_price, yes_cost


def test_yes_cost_takes_the_worse_reading():
    assert yes_cost({"yes_bid_dollars": "0.0400", "no_bid_dollars": "0.9500"}) == pytest.approx(0.05)
    assert yes_cost({"yes_bid_dollars": "0.0600", "no_bid_dollars": "0.9500"}) == pytest.approx(0.06)
    assert yes_cost({"yes_bid_dollars": "0", "no_bid_dollars": "0.9700"}) == pytest.approx(0.03)
    assert yes_cost({"yes_bid_dollars": "0", "no_bid_dollars": "0"}) is None


def test_best_quote_is_cheapest_open_one():
    quotes = [
        {"id": "a", "status": "open", "yes_bid_dollars": "0.08", "no_bid_dollars": "0"},
        {"id": "b", "status": "open", "yes_bid_dollars": "0.05", "no_bid_dollars": "0"},
        {"id": "c", "status": "cancelled", "yes_bid_dollars": "0.01", "no_bid_dollars": "0"},
    ]
    cost, quote = best_yes_quote(quotes)
    assert quote["id"] == "b" and cost == pytest.approx(0.05)
    assert best_yes_quote([]) is None


class RfqClient:
    def __init__(self, quotes, fail_get=False):
        self.quotes, self.fail_get = quotes, fail_get
        self.created, self.deleted = [], []

    def create_rfq(self, ticker, target_cost):
        self.created.append((ticker, target_cost))
        return "rfq-1"

    def get_rfq_quotes(self, rfq_id):
        if self.fail_get:
            raise KalshiError("boom")
        return self.quotes

    def delete_rfq(self, rfq_id):
        self.deleted.append(rfq_id)


def test_request_combo_price_always_withdraws_the_rfq():
    client = RfqClient([{"status": "open", "yes_bid_dollars": "0.05", "no_bid_dollars": "0"}])
    cost, _ = request_combo_price(client, "KXMVE-1", 5.0, sleep=lambda s: None)
    assert cost == pytest.approx(0.05)
    assert client.created == [("KXMVE-1", 5.0)] and client.deleted == ["rfq-1"]

    failing = RfqClient([], fail_get=True)
    with pytest.raises(KalshiError):
        request_combo_price(failing, "KXMVE-1", 5.0, sleep=lambda s: None)
    assert failing.deleted == ["rfq-1"]


def test_request_combo_price_with_no_quotes():
    assert request_combo_price(RfqClient([]), "KXMVE-1", 5.0, sleep=lambda s: None) is None
