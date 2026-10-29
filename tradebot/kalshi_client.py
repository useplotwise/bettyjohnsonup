"""Minimal Kalshi Trade API v2 client with RSA-PSS request signing.

Public market data (markets, events, order books) needs no key. Portfolio
endpoints and order placement are signed with the account's API key.
"""

import base64
import time
from typing import Any, Iterator
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiError(RuntimeError):
    pass


def load_private_key(path: str) -> rsa.RSAPrivateKey:
    with open(path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise KalshiError("Kalshi private key must be an RSA key")
    return key


def sign_request(key: rsa.RSAPrivateKey, timestamp_ms: str, method: str, path: str) -> str:
    """Signature over timestamp + METHOD + path (path excludes the query string)."""
    message = f"{timestamp_ms}{method.upper()}{path.split('?', 1)[0]}".encode()
    signature = key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        api_key_id: str = "",
        private_key: rsa.RSAPrivateKey | None = None,
        session: requests.Session | None = None,
        timeout: float = 15.0,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self._base_path = urlparse(self.base_url).path
        self.api_key_id = api_key_id
        self.private_key = private_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries

    # -- transport ---------------------------------------------------------

    def _headers(self, method: str, path: str, signed: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if signed:
            if not (self.api_key_id and self.private_key):
                raise KalshiError("This endpoint needs KALSHI_API_KEY_ID and a private key")
            ts = str(int(time.time() * 1000))
            headers.update(
                {
                    "KALSHI-ACCESS-KEY": self.api_key_id,
                    "KALSHI-ACCESS-TIMESTAMP": ts,
                    "KALSHI-ACCESS-SIGNATURE": sign_request(
                        self.private_key, ts, method, self._base_path + path
                    ),
                }
            )
        return headers

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> dict[str, Any]:
        url = self.base_url + path
        for attempt in range(self.max_retries + 1):
            resp = self.session.request(
                method,
                url,
                params={k: v for k, v in (params or {}).items() if v is not None},
                json=json,
                headers=self._headers(method, path, signed),
                timeout=self.timeout,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 10))
                    continue
            if not resp.ok:
                raise KalshiError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else {}
        raise KalshiError(f"{method} {path} failed after retries")  # pragma: no cover

    def _paginate(self, path: str, key: str, params: dict[str, Any]) -> Iterator[dict]:
        cursor = None
        while True:
            data = self.request("GET", path, params={**params, "cursor": cursor})
            yield from data.get(key, [])
            cursor = data.get("cursor")
            if not cursor:
                return

    # -- public market data ------------------------------------------------

    def iter_markets(self, status: str = "open", limit: int = 1000, **filters) -> Iterator[dict]:
        return self._paginate("/markets", "markets", {"status": status, "limit": limit, **filters})

    def iter_events(self, status: str = "open", limit: int = 200, **filters) -> Iterator[dict]:
        return self._paginate("/events", "events", {"status": status, "limit": limit, **filters})

    def get_market(self, ticker: str) -> dict:
        return self.request("GET", f"/markets/{ticker}")["market"]

    def get_orderbook(self, ticker: str, depth: int | None = None) -> dict:
        data = self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        # Older responses use "orderbook" (cents); newer ones "orderbook_fp" (dollar strings).
        book = data.get("orderbook") or data.get("orderbook_fp")
        if book is None:
            raise KalshiError(f"unexpected orderbook response, keys: {sorted(data)}")
        return book

    # -- account (signed) --------------------------------------------------

    def get_balance(self) -> dict:
        return self.request("GET", "/portfolio/balance", signed=True)

    def get_positions(self) -> dict:
        return self.request("GET", "/portfolio/positions", signed=True)

    def get_fills(self, **params) -> dict:
        return self.request("GET", "/portfolio/fills", params=params, signed=True)

    # -- request for quote (signed) -----------------------------------------
    # Combo markets have no resting orders; prices come from market makers'
    # quotes in reply to an RFQ. Creating or deleting an RFQ moves no money.

    def create_rfq(self, market_ticker: str, target_cost_dollars: float) -> str:
        data = self.request(
            "POST",
            "/communications/rfqs",
            json={
                "market_ticker": market_ticker,
                "target_cost_dollars": f"{target_cost_dollars:.2f}",
                "rest_remainder": False,
                "obscure_creator_id": True,
            },
            signed=True,
        )
        return data["id"]

    def get_communications_id(self) -> str:
        if not getattr(self, "_communications_id", None):
            data = self.request("GET", "/communications/id", signed=True)
            self._communications_id = data["communications_id"]
        return self._communications_id

    def get_rfq_quotes(self, rfq_id: str) -> list[dict]:
        """Quotes replying to one of our RFQs.

        Kalshi rejects an unfiltered lookup with 403 "Either creator_user_id or
        rfq_creator_user_id must be filled", so filter to our own RFQs, falling
        back to the older rfq_creator_user_id parameter if the filter is refused.
        """
        params = {"rfq_id": rfq_id, "rfq_user_filter": "self"}
        try:
            data = self.request("GET", "/communications/quotes", params=params, signed=True)
        except KalshiError as exc:
            if "-> 403" not in str(exc):
                raise
            params = {"rfq_id": rfq_id, "rfq_creator_user_id": self.get_communications_id()}
            data = self.request("GET", "/communications/quotes", params=params, signed=True)
        return data.get("quotes", [])

    def delete_rfq(self, rfq_id: str) -> None:
        self.request("DELETE", f"/communications/rfqs/{rfq_id}", signed=True)
