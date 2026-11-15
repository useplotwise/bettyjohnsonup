import pytest

from tradebot.kalshi_client import KalshiError
from tradebot.strategy.quotes import YES_ACCEPT_SIDE, best_yes_quote, request_combo_price, yes_cost, yes_size


# Three real quotes from Kalshi (Nov 15 2026), trimmed to the fields that matter.
NO_BID_ONLY = {"id": "q1", "status": "open", "yes_bid_dollars": "0.0000",
               "no_bid_dollars": "0.1480", "no_contracts_fp": "5.80"}
YES_BID_ONLY = {"id": "q2", "status": "open", "yes_bid_dollars": "0.3000", "yes_contracts_fp": "6.99",
                "no_bid_dollars": "0.0000"}
TWO_SIDED = {"id": "q3", "status": "open", "yes_bid_dollars": "0.3020", "yes_contracts_fp": "7.01",
             "no_bid_dollars": "0.1750", "no_contracts_fp": "5.98"}


def test_buying_yes_costs_one_minus_the_makers_no_bid():
    assert YES_ACCEPT_SIDE == "no"
    assert yes_cost(NO_BID_ONLY) == pytest.approx(0.852)
    assert yes_cost(TWO_SIDED) == pytest.approx(0.825)  # never the 0.302 the maker would pay for YES
    assert yes_size(TWO_SIDED) == pytest.approx(5.98) and yes_size(NO_BID_ONLY) == pytest.approx(5.80)


def test_a_maker_who_only_bids_for_yes_is_not_selling_it():
    # This is the quote the old code read as "YES for $0.30" and then bought NO with.
    assert yes_cost(YES_BID_ONLY) is None and yes_size(YES_BID_ONLY) is None
    assert best_yes_quote([YES_BID_ONLY]) is None
    assert yes_cost({"yes_bid_dollars": "0", "no_bid_dollars": "0"}) is None


def test_best_quote_is_cheapest_open_one_that_sells_yes():
    quotes = [
        TWO_SIDED,
        NO_BID_ONLY,
        YES_BID_ONLY,
        {"id": "c", "status": "cancelled", "yes_bid_dollars": "0", "no_bid_dollars": "0.99"},
    ]
    cost, quote = best_yes_quote(quotes)
    assert quote["id"] == "q3" and cost == pytest.approx(0.825)
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
    client = RfqClient([{"status": "open", "yes_bid_dollars": "0", "no_bid_dollars": "0.95"}])
    cost, _ = request_combo_price(client, "KXMVE-1", 5.0, sleep=lambda s: None)
    assert cost == pytest.approx(0.05)
    assert client.created == [("KXMVE-1", 5.0)] and client.deleted == ["rfq-1"]

    failing = RfqClient([], fail_get=True)
    with pytest.raises(KalshiError):
        request_combo_price(failing, "KXMVE-1", 5.0, sleep=lambda s: None)
    assert failing.deleted == ["rfq-1"]


def test_request_combo_price_with_no_quotes():
    assert request_combo_price(RfqClient([]), "KXMVE-1", 5.0, sleep=lambda s: None) is None


def test_quotes_that_do_not_sell_yes_are_still_reported_as_seen():
    seen = []
    assert request_combo_price(RfqClient([YES_BID_ONLY]), "KXMVE-1", 5.0, sleep=lambda s: None, seen=seen) is None
    assert seen == [YES_BID_ONLY]
