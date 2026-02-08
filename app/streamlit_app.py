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

from tma.config import DICT_PATH, RATE_LIMIT_PER_MIN, FUZZY_THRESHOLD
from tma.matching import load_dict, clean_and_match_from_raw, aggregate_id_quantity
from tma.http_api import session_for_requests, fetch_100
from tma.rate_limit import TokenBucket
from tma.market_enrichment import (
    wide_to_long,
    enrich_all_items,
    build_summary_from_enriched,
)

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

        **What the app does under the hood**

        1. **Clean order book**
           - For each item, up to 100 listings (price + quantity) are loaded.
           - Listings are sorted by price and cumulative quantity is tracked to understand market depth.
           - Volume is also aggregated by exact price level to detect dominant price walls.

        2. **Robust center and spread**
           - A volume-weighted median and quartiles (Q1, Q3) are computed.
           - A robust spread (MAD – median absolute deviation) around the median is used to derive robust z-scores.
           - This helps flag extremely cheap/expensive prices relative to the bulk of the market.

        3. **Suspected anchors**
           - Markets are marked as **normal** or **thin/exclusive** based on total units and dominance of a single price level.
           - In normal markets, suspected anchors are listings with large |z|, shallow depth and small volume.
           - In thin markets, only very high prices far above the median with low volume are flagged.
           - These suspected anchors are removed for pricing (unless that would remove everything).

        4. **Suggested prices**
           - **Fair price**: clean median (volume-weighted in normal markets, per-listing in thin ones).
           - **Greedy price**: clean Q3 (upper quartile) of prices.
           - **Fast-sell price**:
             - Thin markets: around the 3rd cheapest clean listing.
             - Unit-style markets: around the N-th cheapest clean listing.
             - Bulk markets: first price where cumulative clean volume reaches a target units threshold.
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

    # 1) Cleaning & matching
    with st.spinner("Cleaning & matching…"):
        dict_map = load_dict(DICT_PATH)
        clean_rows = clean_and_match_from_raw(raw, dict_map, threshold=FUZZY_THRESHOLD)
        df_clean = pd.DataFrame(clean_rows)
        if df_clean.empty:
            st.warning("No matches found.")
            st.stop()

        parsed_cols = [
            "input_segment",
            "cleaned_name",
            "normalized_key",
            "quantity",
            "id",
        ]
        df_parsed_view = df_clean.reindex(columns=parsed_cols)

    # 2) Aggregate quantities per item_id
    agg = aggregate_id_quantity(clean_rows)
    if not agg:
        st.warning("No valid item IDs after cleaning.")
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
    # MAIN PRICE OVERVIEW (USER-FRIENDLY)
    # -----------------------------------------------------------------

    st.subheader("Price overview")

    overview = df_summary_sorted[
        [
            "item_name",
            "my_quantity",
            "fast_sell_price",
            "fair_price",
            "greedy_price",
        ]
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

    # Format numbers with thousands separator (comma) as strings
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

    st.dataframe(overview_display, width="stretch")

    # -----------------------------------------------------------------
    # DETAILED TABLES (EXPANDERS)
    # -----------------------------------------------------------------

    with st.expander("Parsed segments"):
        st.dataframe(df_parsed_view, width="stretch")

    with st.expander("Raw market data"):
        st.dataframe(df_market, width="stretch")

    with st.expander("Detailed pricing diagnostics"):
        st.dataframe(df_summary_sorted, width="stretch")
