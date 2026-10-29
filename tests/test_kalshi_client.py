import base64

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tradebot.kalshi_client import KalshiClient, KalshiError, sign_request


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_signature_verifies_and_ignores_query(key):
    sig = sign_request(key, "1700000000000", "get", "/trade-api/v2/portfolio/balance?x=1")
    key.public_key().verify(
        base64.b64decode(sig),
        b"1700000000000GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.ok = 200 <= status < 300
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers})
        return self.responses.pop(0)


def test_paginates_markets():
    session = FakeSession(
        [
            FakeResponse(200, {"markets": [{"ticker": "A"}], "cursor": "c1"}),
            FakeResponse(200, {"markets": [{"ticker": "B"}], "cursor": ""}),
        ]
    )
    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", session=session)
    assert [m["ticker"] for m in client.iter_markets()] == ["A", "B"]
    assert session.calls[1]["params"]["cursor"] == "c1"
    assert "cursor" not in session.calls[0]["params"]


def test_signed_request_sets_headers(key):
    session = FakeSession([FakeResponse(200, {"balance": 1234})])
    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", "key-id", key, session=session)
    assert client.get_balance()["balance"] == 1234
    headers = session.calls[0]["headers"]
    assert headers["KALSHI-ACCESS-KEY"] == "key-id"
    assert headers["KALSHI-ACCESS-SIGNATURE"]


def test_signed_request_without_key_fails():
    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", session=FakeSession([]))
    with pytest.raises(KalshiError):
        client.get_balance()


def test_error_status_raises(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    session = FakeSession([FakeResponse(404, {"error": "nope"})])
    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", session=session)
    with pytest.raises(KalshiError):
        client.get_market("MISSING")


def test_orderbook_accepts_fixed_point_key():
    session = FakeSession([FakeResponse(200, {"orderbook_fp": {"yes_dollars": []}})])
    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", session=session)
    assert client.get_orderbook("T") == {"yes_dollars": []}


def test_orderbook_unknown_shape_raises():
    session = FakeSession([FakeResponse(200, {"something": {}})])
    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", session=session)
    with pytest.raises(KalshiError):
        client.get_orderbook("T")


def test_rfq_quotes_filter_to_own_rfqs(key):
    session = FakeSession([FakeResponse(200, {"quotes": [{"id": "q"}]})])
    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", "k", key, session=session)
    assert client.get_rfq_quotes("r1") == [{"id": "q"}]
    assert session.calls[0]["params"] == {"rfq_id": "r1", "rfq_user_filter": "self"}


def test_rfq_quotes_fall_back_to_creator_id(key):
    session = FakeSession(
        [
            FakeResponse(403, {"error": {"code": "Either_creator_user_id_or_rfq_creator_user_id_must_be_filled."}}),
            FakeResponse(200, {"communications_id": "me123"}),
            FakeResponse(200, {"quotes": []}),
        ]
    )
    client = KalshiClient("https://demo-api.kalshi.co/trade-api/v2", "k", key, session=session)
    assert client.get_rfq_quotes("r1") == []
    assert session.calls[2]["params"] == {"rfq_id": "r1", "rfq_creator_user_id": "me123"}
