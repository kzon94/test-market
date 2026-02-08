import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------
# PATHS & IMPORTS
# ---------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import tma.config as cfg
from tma.inventory_matcher import match_inventory
from tma.http_api import session_for_requests, fetch_100
from tma.rate_limit import TokenBucket
from tma.market_enrichment import (
    wide_to_long,
    enrich_all_items,
    build_summary_from_enriched,
)

DICT_PATH = cfg.DICT_PATH
if not DICT_PATH.is_absolute():
    DICT_PATH = (ROOT_DIR / DICT_PATH).resolve()

RATE_LIMIT_PER_MIN = cfg.RATE_LIMIT_PER_MIN

# ---------------------------------------------------------------------
# APP CONFIG
# ---------------------------------------------------------------------

st.set_page_config(page_title="Kzon's Torn Market Analyzer", layout="centered")

if "api_key" not in st.session_state:
    st.session_state["api_key"] = ""

# ---------------------------------------------------------------------
# GLOBAL STYLES
# ---------------------------------------------------------------------

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

# ---------------------------------------------------------------------
# HEADER
# ---------------------------------------------------------------------

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
        **How to use the app**

        - Go to the **Add Listing** section of the Item Market.
        - Copy the item list (the part with item names and quantities).
        - Paste it in the text box below; prices and untradable/equipped tags are ignored.
        - Enter your **public Torn API key** (read-only) so the app can call the `itemmarket` endpoint.
        - The app fetches up to the first 100 listings per item and computes suggested prices.
        """
    )

# ---------------------------------------------------------------------
# INPUT FORM
# ---------------------------------------------------------------------

submitted = False
raw = ""
api_key = ""

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

# ---------------------------------------------------------------------
# PIPELINE
# ---------------------------------------------------------------------

if submitted:
    if not DICT_PATH.exists():
        st.error("Dictionary CSV not found (data/torn_item_dictionary.csv).")
        st.stop()
    if not raw or not raw.strip():
        st.error("Listings text is empty.")
        st.stop()
    if not api_key.strip():
        st.error("API key required.")
        st.stop()

    st.session_state["api_key"] = api_key

    # 1) Parse & exact match (inventory_matcher)
    with st.spinner("Parsing & matching…"):
        result = match_inventory(raw, DICT_PATH)

        if not result.matched:
            st.warning("No matches found.")
            st.stop()

        df_parsed_view = pd.DataFrame(
            [
                {
                    "name": it.name,
                    "id": it.item_id,
                    "quantity": it.qty,
                }
                for it in result.matched
            ]
        ).sort_values("name").reset_index(drop=True)

    # 2) Aggregate quantities per item_id (already aggregated in match_inventory)
    agg = [(it.item_id, it.qty) for it in result.matched]
    if not agg:
        st.warning("No valid item IDs after matching.")
        st.stop()

    # 3) Fetch market data (wide format)
    with st.spinner("Fetching market data…"):
        sess = session_for_requests()
        bucket = TokenBucket(RATE_LIMIT_PER_MIN)

        out_rows = [fetch_100(sess, bucket, api_key, iid, qty) for iid, qty in agg]
        df_market = pd.DataFrame(out_rows)

        for i in range(1, 101):
            pcol = f"price_{i}"
            acol = f"amount_{i}"
            if pcol in df_market.columns:
                df_market[pcol] = pd.to_numeric(df_market[pcol], errors="coerce")
            if acol in df_market.columns:
                df_market[acol] = pd.to_numeric(df_market[acol], errors="coerce")

        for c in ["average_price", "my_quantity", "item_id"]:
            if c in df_market.columns:
                df_market[c] = pd.to_numeric(df_market[c], errors="coerce")

    # 4) Anchor-aware price suggestions
    with st.spinner("Computing anchor-aware price suggestions…"):
        df_long = wide_to_long(df_market)

        if df_long.empty:
            st.warning("No valid listings found in the market data.")
            st.stop()

        df_enriched = enrich_all_items(df_long)
        df_summary = build_summary_from_enriched(df_enriched)
        df_summary_sorted = df_summary.sort_values("item_name").reset_index(drop=True)

    # -----------------------------------------------------------------
    # MAIN PRICE OVERVIEW
    # -----------------------------------------------------------------

    st.subheader("Price overview")

    overview = df_summary_sorted[
        ["item_name", "my_quantity", "fast_sell_price", "fair_price", "greedy_price"]
    ].copy()

    overview = overview.rename(
        columns={
            "item_name": "Item",
            "my_quantity": "My quantity",
            "fast_sell_price": "Fast-sell price",
            "fair_price": "Fair price",
            "greedy_price": "Greedy price",
        }
    )

    def fmt_int(x):
        if pd.isna(x):
            return ""
        try:
            return f"{int(x):,}"
        except Exception:
            return str(x)

    overview_display = overview.copy()
    overview_display["My quantity"] = overview_display["My quantity"].apply(fmt_int)
    overview_display["Fast-sell price"] = overview_display["Fast-sell price"].apply(fmt_int)
    overview_display["Fair price"] = overview_display["Fair price"].apply(fmt_int)
    overview_display["Greedy price"] = overview_display["Greedy price"].apply(fmt_int)

    st.dataframe(overview_display, use_container_width=True, hide_index=True)

    # -----------------------------------------------------------------
    # DETAILED TABLES
    # -----------------------------------------------------------------

    with st.expander("Parsed items"):
        st.dataframe(df_parsed_view, use_container_width=True, hide_index=True)

    if result.unmatched:
        with st.expander(f"Unmatched items ({len(result.unmatched)})"):
            df_unmatched = pd.DataFrame(result.unmatched, columns=["name", "qty"]).sort_values("name")
            st.dataframe(df_unmatched, use_container_width=True, hide_index=True)

    with st.expander("Raw market data"):
        st.dataframe(df_market, use_container_width=True, hide_index=True)

    with st.expander("Detailed pricing diagnostics"):
        st.dataframe(df_summary_sorted, use_container_width=True, hide_index=True)
