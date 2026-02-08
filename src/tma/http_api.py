import time
import requests

from .config import BASE_URL, MAX_WORKERS, RETRIES, TIMEOUT
from .rate_limit import TokenBucket


def session_for_requests() -> requests.Session:
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=MAX_WORKERS * 10,
        pool_maxsize=MAX_WORKERS * 10,
    )
    session.mount("https://", adapter)
    session.headers.update(
        {
            "accept": "application/json",
            "User-Agent": "torn-itemmarket-web/1.1",
        }
    )
    return session


def attempt_call(
    session: requests.Session,
    bucket: TokenBucket,
    api_key: str,
    item_id: int,
    mode: int,
) -> tuple[int | None, dict]:
    url = f"{BASE_URL}/market/{item_id}/itemmarket"
    headers: dict[str, str] = {}
    params: dict[str, int | str] = {"limit": 100, "offset": 0}

    if mode == 1:
        headers["Authorization"] = f"Apikey {api_key}"
    elif mode == 2:
        headers["Authorization"] = f"ApiKey {api_key}"
    else:
        params["key"] = api_key

    bucket.take(1)
    response = session.get(url, headers=headers, params=params, timeout=TIMEOUT)
    return response.status_code, response.json()


def fetch_100(
    session: requests.Session,
    bucket: TokenBucket,
    api_key: str,
    item_id: int,
    my_quantity: int,
) -> dict:
    backoff = 0.8

    for _ in range(1, RETRIES + 1):
        for mode in (1, 2, 3):
            try:
                status, data = attempt_call(session, bucket, api_key, item_id, mode)
            except Exception as exc:
                status, data = None, {"error": {"code": -1, "error": str(exc)}}

            if isinstance(data, dict) and "error" in data:
                code = data["error"].get("code")
                msg = data["error"].get("error")

                if code == 2 and mode != 3:
                    continue

                if code in (0, 10) or status in (429, 500, 502, 503, 504):
                    time.sleep(backoff)
                    backoff *= 1.6
                    break

                return {
                    "item_id": item_id,
                    "my_quantity": my_quantity,
                    "error": f"API error {code}: {msg}",
                }

            itemmarket = (data or {}).get("itemmarket", {}) or {}
            item = itemmarket.get("item", {}) or {}
            listings = itemmarket.get("listings", []) or []

            n = min(len(listings), 100)

            row: dict[str, object] = {
                "item_id": item.get("id", item_id),
                "item_name": item.get("name"),
                "item_type": item.get("type"),
                "average_price": item.get("average_price"),
                "my_quantity": my_quantity,
            }

            for i, listing in enumerate(listings[:n], start=1):
                row[f"price_{i}"] = listing.get("price")
                row[f"amount_{i}"] = listing.get("amount")

            for i in range(n + 1, 101):
                row[f"price_{i}"] = None
                row[f"amount_{i}"] = None

            return row

        time.sleep(backoff)
        backoff *= 1.6

    return {
        "item_id": item_id,
        "my_quantity": my_quantity,
        "error": "Exhausted retries",
    }