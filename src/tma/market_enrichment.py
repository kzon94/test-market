import numpy as np
import pandas as pd
import math

from pathlib import Path

# ---------------------------------------------------------------------
# CONFIG (solo para lógica de precios)
# ---------------------------------------------------------------------

FAST_SELL_UNITS_THRESHOLD = 100.0
FAST_SELL_LISTINGS_THRESHOLD = 10
EXCLUSIVE_TOTAL_UNITS_THRESHOLD = 200.0
EXCLUSIVE_DOMINANCE_SHARE = 0.50
EXCLUSIVE_HIGH_FACTOR = 10.0


# ---------------------------------------------------------------------
# WIDE → LONG
# ---------------------------------------------------------------------

def wide_to_long(df: pd.DataFrame) -> pd.DataFrame:
    price_cols = [c for c in df.columns if c.startswith("price_")]
    amount_cols = [c for c in df.columns if c.startswith("amount_")]

    price_cols = sorted(price_cols, key=lambda x: int(x.split("_")[1]))
    amount_cols = sorted(amount_cols, key=lambda x: int(x.split("_")[1]))

    records: list[dict] = []
    for _, row in df.iterrows():
        base = {
            "item_id": row["item_id"],
            "item_name": row.get("item_name", None),
            "item_type": row.get("item_type", None),
            "average_price": row.get("average_price", None),
            "my_quantity": row.get("my_quantity", None),
        }

        for idx, (p_col, q_col) in enumerate(zip(price_cols, amount_cols), start=1):
            price = row[p_col]
            qty = row[q_col]

            if pd.isna(price) or pd.isna(qty):
                continue

            try:
                price = float(price)
                qty = float(qty)
            except (TypeError, ValueError):
                continue

            if qty <= 0:
                continue

            rec = dict(base)
            rec["listing_rank"] = idx
            rec["price"] = price
            rec["quantity"] = qty
            records.append(rec)

    if not records:
        return pd.DataFrame(
            columns=[
                "item_id",
                "item_name",
                "item_type",
                "average_price",
                "my_quantity",
                "listing_rank",
                "price",
                "quantity",
            ]
        )

    return pd.DataFrame.from_records(records)


# ---------------------------------------------------------------------
# BASIC STATS (WEIGHTED MEDIAN / QUANTILES)
# ---------------------------------------------------------------------

def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    v = values[order]
    w = weights[order]
    cum_w = np.cumsum(w)
    half = 0.5 * w.sum()
    return float(v[cum_w >= half][0])


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    if len(values) == 0:
        return float("nan")
    if q <= 0:
        return float(np.min(values))
    if q >= 1:
        return float(np.max(values))

    order = np.argsort(values)
    v = values[order]
    w = weights[order].astype(float)
    cum_w = np.cumsum(w)
    target = q * w.sum()
    return float(v[cum_w >= target][0])


def _weighted_price_quantile_from_df(df: pd.DataFrame, q: float) -> float:
    if df.empty:
        return float("nan")
    df_sorted = df.sort_values("price")
    prices = df_sorted["price"].to_numpy()
    qty = df_sorted["quantity"].to_numpy().astype(float)
    return _weighted_quantile(prices, qty, q)


def _unweighted_price_quantile_from_df(df: pd.DataFrame, q: float) -> float:
    if df.empty:
        return float("nan")
    prices = df["price"].to_numpy()
    return float(np.quantile(prices, q))


def add_price_stats_for_item(df_item: pd.DataFrame) -> pd.DataFrame:
    df_item = df_item.copy()
    prices = df_item["price"].astype(float).to_numpy()
    qty = df_item["quantity"].astype(float).to_numpy()

    if qty.sum() <= 0:
        median = float(np.median(prices))
        q1 = float(np.quantile(prices, 0.25))
        q3 = float(np.quantile(prices, 0.75))
        mad = float(np.median(np.abs(prices - median)))
    else:
        median = _weighted_median(prices, qty)
        q1 = _weighted_quantile(prices, qty, 0.25)
        q3 = _weighted_quantile(prices, qty, 0.75)
        expanded = np.repeat(prices, qty.astype(int).clip(min=1))
        if len(expanded) > 0:
            mad = float(np.median(np.abs(expanded - median)))
        else:
            mad = 0.0

    iqr = q3 - q1

    df_item["price_median"] = median
    df_item["price_q1"] = q1
    df_item["price_q3"] = q3
    df_item["price_iqr"] = iqr
    df_item["price_mad"] = mad

    if mad > 0:
        df_item["robust_z"] = 0.6745 * (df_item["price"] - median) / mad
    else:
        df_item["robust_z"] = 0.0

    df_item["is_extreme_price"] = df_item["robust_z"].abs() > 3.0

    return df_item


def add_depth_features_for_item(df_item: pd.DataFrame) -> pd.DataFrame:
    df_item = df_item.copy()
    df_item = df_item.sort_values("price").reset_index(drop=True)
    df_item["cum_qty"] = df_item["quantity"].cumsum()
    total_qty = df_item["quantity"].sum()
    if total_qty > 0:
        df_item["cum_qty_pct"] = df_item["cum_qty"] / total_qty
    else:
        df_item["cum_qty_pct"] = 0.0
    return df_item


# ---------------------------------------------------------------------
# ANCHOR DETECTION (NORMAL VS EXCLUSIVE)
# ---------------------------------------------------------------------

def mark_suspected_anchors_for_item(
    df_item: pd.DataFrame,
    z_threshold: float = 5.0,
    front_depth_pct: float = 0.02,
    back_depth_pct: float = 0.02,
    max_level_units_for_anchor: float = 50.0,
) -> pd.DataFrame:
    df_item = df_item.copy()

    if "robust_z" not in df_item.columns or "cum_qty_pct" not in df_item.columns:
        raise ValueError(
            "Missing 'robust_z' or 'cum_qty_pct'. "
            "Call add_price_stats_for_item and add_depth_features_for_item first."
        )

    level = (
        df_item.groupby("price", as_index=False)["quantity"]
        .sum()
        .rename(columns={"quantity": "level_qty"})
    )
    total_qty = level["level_qty"].sum()
    if total_qty > 0:
        level["level_share"] = level["level_qty"] / total_qty
    else:
        level["level_share"] = 0.0

    df_item = df_item.merge(level[["price", "level_share"]], on="price", how="left")

    total_qty_all = float(df_item["quantity"].sum())
    max_level_share = float(df_item["level_share"].max()) if len(df_item) else 0.0
    median_price = float(df_item["price_median"].iloc[0])

    exclusive_mode = (
        (total_qty_all <= EXCLUSIVE_TOTAL_UNITS_THRESHOLD)
        or (max_level_share >= EXCLUSIVE_DOMINANCE_SHARE)
    )

    shallow_front = df_item["cum_qty_pct"] < front_depth_pct
    shallow_back = df_item["cum_qty_pct"] > (1.0 - back_depth_pct)

    if exclusive_mode and median_price > 0:
        extreme_high = df_item["price"] > (median_price * EXCLUSIVE_HIGH_FACTOR)
        small_qty = df_item["quantity"] <= max_level_units_for_anchor
        df_item["is_suspected_anchor"] = extreme_high & (shallow_front | shallow_back | small_qty)
    else:
        extreme_mask = df_item["robust_z"].abs() > z_threshold
        small_qty = df_item["quantity"] < max_level_units_for_anchor
        df_item["is_suspected_anchor"] = extreme_mask & (shallow_front | shallow_back) & small_qty

    return df_item


def enrich_item_orders(df_item: pd.DataFrame) -> pd.DataFrame:
    df_item = add_price_stats_for_item(df_item)
    df_item = add_depth_features_for_item(df_item)
    df_item = mark_suspected_anchors_for_item(df_item)
    return df_item


# ---------------------------------------------------------------------
# PRICE SUGGESTIONS (fast / fair / greedy)
# ---------------------------------------------------------------------

def compute_price_suggestions_for_item(df_item: pd.DataFrame) -> dict:
    item_id = df_item["item_id"].iloc[0]
    item_name = df_item["item_name"].iloc[0] if "item_name" in df_item.columns else None
    item_type = df_item["item_type"].iloc[0] if "item_type" in df_item.columns else None
    average_price = (
        df_item["average_price"].iloc[0]
        if "average_price" in df_item.columns
        else None
    )
    my_quantity = (
        df_item["my_quantity"].iloc[0] if "my_quantity" in df_item.columns else None
    )

    # Remove suspected anchors for pricing
    if "is_suspected_anchor" in df_item.columns:
        df_clean = df_item[~df_item["is_suspected_anchor"]].copy()
        if df_clean.empty:
            df_clean = df_item.copy()
    else:
        df_clean = df_item.copy()

    if df_clean.empty:
        return {
            "item_id": item_id,
            "item_name": item_name,
            "item_type": item_type,
            "average_price_reported": average_price,
            "my_quantity": my_quantity,
            "num_listings": len(df_item),
            "num_suspected_anchors": int(
                df_item["is_suspected_anchor"].sum()
            ) if "is_suspected_anchor" in df_item.columns else 0,
            "fast_sell_price": float("nan"),
            "fair_price": float("nan"),
            "greedy_price": float("nan"),
            "clean_median_price": float("nan"),
            "clean_q1_price": float("nan"),
            "clean_q3_price": float("nan"),
        }

    total_qty_clean = float(df_clean["quantity"].sum())
    total_listings_clean = len(df_clean)
    avg_qty_per_listing = total_qty_clean / total_listings_clean

    level = (
        df_clean.groupby("price", as_index=False)["quantity"]
        .sum()
        .rename(columns={"quantity": "level_qty"})
    )
    max_level_share_clean = (
        float(level["level_qty"].max()) / total_qty_clean if total_qty_clean > 0 else 0.0
    )

    exclusive_mode = (
        (total_qty_clean <= EXCLUSIVE_TOTAL_UNITS_THRESHOLD)
        or (max_level_share_clean >= EXCLUSIVE_DOMINANCE_SHARE)
    )

    # -------- Fair / greedy (before fast-sell) --------
    if exclusive_mode:
        fair_price = _unweighted_price_quantile_from_df(df_clean, 0.5)
        q1_price = _unweighted_price_quantile_from_df(df_clean, 0.25)
        q3_price = _unweighted_price_quantile_from_df(df_clean, 0.75)
    else:
        fair_price = _weighted_price_quantile_from_df(df_clean, 0.5)
        q1_price = _weighted_price_quantile_from_df(df_clean, 0.25)
        q3_price = _weighted_price_quantile_from_df(df_clean, 0.75)

    # -------- Raw fast-sell level (before -1 tweak) --------
    df_clean_sorted = df_clean.sort_values("price").copy()
    df_clean_sorted["cum_qty_clean"] = df_clean_sorted["quantity"].cumsum()

    is_bulk_mode = (not exclusive_mode) and (avg_qty_per_listing > 2.0)

    if exclusive_mode:
        # Thin/exclusive: N-th cheapest clean listing
        exclusive_fast_index = 3
        idx = min(exclusive_fast_index - 1, len(df_clean_sorted) - 1)
        fast_sell_raw = float(df_clean_sorted.iloc[idx]["price"])
    else:
        if avg_qty_per_listing <= 2.0:
            # Unit-style: N-th cheapest listing
            target_listings = min(FAST_SELL_LISTINGS_THRESHOLD, len(df_clean_sorted))
            fast_sell_raw = float(df_clean_sorted.iloc[target_listings - 1]["price"])
        else:
            # Bulk: cumulative units threshold
            target_units = min(FAST_SELL_UNITS_THRESHOLD, total_qty_clean)
            mask = df_clean_sorted["cum_qty_clean"] >= target_units
            if mask.any():
                fast_sell_raw = float(df_clean_sorted.loc[mask, "price"].iloc[0])
            else:
                fast_sell_raw = float(df_clean_sorted["price"].iloc[-1])

    # -------- Final fast-sell tweak: 1$ below bulk wall ONLY --------
    if np.isfinite(fast_sell_raw):
        # Kill any float noise first
        base_price = float(round(fast_sell_raw))

        if is_bulk_mode:
            # Bulk: must be 1$ below the selected wall level
            fast_sell_price = max(math.floor(base_price) - 1, 0.0)
        else:
            # Unit/exclusive: no undercut rule
            fast_sell_price = max(base_price, 0.0)
    else:
        fast_sell_price = float("nan")

    num_listings = len(df_item)
    num_suspected_anchors = int(
        df_item["is_suspected_anchor"].sum()
    ) if "is_suspected_anchor" in df_item.columns else 0

    return {
        "item_id": item_id,
        "item_name": item_name,
        "item_type": item_type,
        "average_price_reported": average_price,
        "my_quantity": my_quantity,
        "num_listings": num_listings,
        "num_suspected_anchors": num_suspected_anchors,
        "fast_sell_price": float(fast_sell_price),
        "fair_price": float(fair_price),
        "greedy_price": float(q3_price),
        "clean_median_price": float(fair_price),
        "clean_q1_price": float(q1_price),
        "clean_q3_price": float(q3_price),
    }


# ---------------------------------------------------------------------
# PIPELINE HELPERS
# ---------------------------------------------------------------------

def enrich_all_items(long_df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "item_id",
        "item_name",
        "item_type",
        "average_price",
        "my_quantity",
        "listing_rank",
        "price",
        "quantity",
    ]

    safe_df = long_df[cols].copy()

    return (
        safe_df
        .groupby("item_id", group_keys=False)[cols]
        .apply(enrich_item_orders)
        .reset_index(drop=True)
    )


def build_summary_from_enriched(enriched_df: pd.DataFrame) -> pd.DataFrame:
    summaries: list[dict] = []
    for _, df_item in enriched_df.groupby("item_id"):
        summary = compute_price_suggestions_for_item(df_item)
        summaries.append(summary)
    return pd.DataFrame(summaries)
