import sys
import time
import threading
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from tma.inventory_matcher import match_inventory
from tma.market_enrichment import wide_to_long, enrich_all_items, build_summary_from_enriched

BASE_URL = "https://api.torn.com/v2"
MAX_WORKERS = 5
RATE_LIMIT_PER_MIN = 90
RETRIES = 3
TIMEOUT = 15

DICT_PATH = (ROOT_DIR / "data" / "torn_item_dictionary.csv").resolve()


class TokenBucket:
    def __init__(self, rate_per_min: int, capacity: int | None = None) -> None:
        self.rate_per_sec = rate_per_min / 60.0
        self.capacity = capacity or rate_per_min
        self.tokens = float(self.capacity)
        self.last = time.perf_counter()
        self.lock = threading.Lock()

    def take(self, tokens: int = 1) -> None:
        while True:
            with self.lock:
                now = time.perf_counter()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate_per_sec)
                self.last = now
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return
            time.sleep(0.01)


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
    resp = session.get(url, headers=headers, params=params, timeout=TIMEOUT)
    return resp.status_code, resp.json()


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

    return {"item_id": item_id, "my_quantity": my_quantity, "error": "Exhausted retries"}


def fmt_int(x) -> str:
    if pd.isna(x):
        return ""
    try:
        return f"{int(x):,}"
    except Exception:
        return str(x)


st.set_page_config(page_title="Kzon's Torn Market Analyzer", layout="centered")

if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""

st.markdown(
    """
    <style>
      .block-container {
        max-width: 900px;
        margin: 0 auto;
        padding-top: 2rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Kzon's Torn Market Analyzer")
st.markdown(
    """
    <p style="margin-top:-12px; font-size:0.95rem; color:#666;">
        Under development by
        <a href="https://www.torn.com/profiles.php?XID=3968250" target="_blank" style="text-decoration:none;">
            Kzon [3968250]
        </a>. Coffee tips are always appreciated :)
    </p>
    """,
    unsafe_allow_html=True,
)

with st.expander("How are prices calculated?"):
    st.markdown(
        """
        - Copy your inventory from the Item Market **Add Listing** page.
        - Paste it below (equipped/untradable items are ignored).
        - Provide a **public Torn API key** so the app can call the `itemmarket` endpoint.
        - The app fetches up to 100 listings per item and computes suggested prices.
        """
    )

with st.form("input_form", clear_on_submit=False):
    st.subheader("Item Market listings")
    st.markdown(
        "[Quick access to your listings](https://www.torn.com/page.php?sid=ItemMarket#/addListing)",
        unsafe_allow_html=False,
    )

    raw = st.text_area(
        "Paste your items",
        height=220,
        placeholder="Paste your full Add Listing items text here…",
    )

    api_key = st.text_input(
        "Enter your public Torn API key",
        value=st.session_state["api_key"],
        key="api_key_input",
    )

    submitted = st.form_submit_button("Run")

if submitted:
    if not DICT_PATH.exists():
        st.error("Dictionary CSV not found.")
        st.stop()
    if not raw or not raw.strip():
        st.error("Listings text is empty.")
        st.stop()
    if not api_key.strip():
        st.error("API key required.")
        st.stop()

    api_key = api_key.strip()
    st.session_state["api_key"] = api_key

    with st.spinner("Parsing & matching…"):
        result = match_inventory(raw, DICT_PATH)
        if not result.matched:
            st.warning("No matches found.")
            st.stop()

        df_parsed = (
            pd.DataFrame([{"name": it.name, "id": it.item_id, "quantity": it.qty} for it in result.matched])
            .sort_values("name")
            .reset_index(drop=True)
        )

        agg = [(it.item_id, it.qty) for it in result.matched]
        if not agg:
            st.warning("No valid item IDs after matching.")
            st.stop()

    with st.spinner("Fetching market data…"):
        sess = session_for_requests()
        bucket = TokenBucket(RATE_LIMIT_PER_MIN)

        rows = [fetch_100(sess, bucket, api_key, iid, qty) for iid, qty in agg]
        df_market = pd.DataFrame(rows)

        for i in range(1, 101):
            pcol = f"price_{i}"
            acol = f"amount_{i}"
            if pcol in df_market.columns:
                df_market[pcol] = pd.to_numeric(df_market[pcol], errors="coerce")
            if acol in df_market.columns:
                df_market[acol] = pd.to_numeric(df_market[acol], errors="coerce")

        for c in ("average_price", "my_quantity", "item_id"):
            if c in df_market.columns:
                df_market[c] = pd.to_numeric(df_market[c], errors="coerce")

    with st.spinner("Computing price suggestions…"):
        df_long = wide_to_long(df_market)
        if df_long.empty:
            st.warning("No valid listings found in the market data.")
            st.stop()

        df_enriched = enrich_all_items(df_long)
        df_summary = build_summary_from_enriched(df_enriched).sort_values("item_name").reset_index(drop=True)

    st.subheader("Price overview")

    overview = df_summary[
        ["item_name", "my_quantity", "fast_sell_price", "fair_price", "greedy_price"]
    ].rename(
        columns={
            "item_name": "Item",
            "my_quantity": "My quantity",
            "fast_sell_price": "Fast-sell price",
            "fair_price": "Fair price",
            "greedy_price": "Greedy price",
        }
    )

    overview_display = overview.copy()
    for col in ["My quantity", "Fast-sell price", "Fair price", "Greedy price"]:
        overview_display[col] = overview_display[col].apply(fmt_int)

    st.dataframe(overview_display, width="stretch", hide_index=True)

    with st.expander("Parsed items"):
        st.dataframe(df_parsed, width="stretch", hide_index=True)

    if result.unmatched:
        with st.expander(f"Unmatched items ({len(result.unmatched)})"):
            df_unmatched = (
                pd.DataFrame(result.unmatched, columns=["name", "qty"])
                .sort_values("name")
                .reset_index(drop=True)
            )
            st.dataframe(df_unmatched, width="stretch", hide_index=True)

    with st.expander("Raw market data"):
        st.dataframe(df_market, width="stretch", hide_index=True)

    with st.expander("Detailed pricing diagnostics"):
        st.dataframe(df_summary, width="stretch", hide_index=True)
