from __future__ import annotations

import math
import numpy as np
import pandas as pd


FAST_SELL_UNITS_THRESHOLD = 100.0
FAST_SELL_LISTINGS_THRESHOLD = 10
EXCLUSIVE_TOTAL_UNITS_THRESHOLD = 200.0
EXCLUSIVE_DOMINANCE_SHARE = 0.50
EXCLUSIVE_HIGH_FACTOR = 10.0


def wide_to_long(df: pd.DataFrame) -> pd.DataFrame:
    price_cols = sorted([c for c in df.columns if c.startswith("price_")], key=lambda x: int(x.split("_")[1]))
    amount_cols = sorted([c for c in df.columns if c.startswith("amount_")], key=lambda x: int(x.split("_")[1]))

    records: list[dict] = []
    for _, row in df.iterrows():
        base = {
            "item_id": row.get("item_id"),
            "item_name": row.get("item_name", None),
            "item_type": row.get("item_type", None),
            "average_price": row.get("average_price", None),
            "my_quantity": row.get("my_quantity", None),
        }

        for idx, (p_col, q_col) in enumerate(zip(price_cols, amount_cols), start=1):
            price = row.get(p_col)
            qty = row.get(q_col)

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

    if not records:
        return pd.DataFrame(columns=cols)

    out = pd.DataFrame.from_records(records)
    return out[cols]


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    if values.size == 0:
        return float("nan")

    q = float(q)
    if q <= 0:
        return float(np.min(values))
    if q >= 1:
        return float(np.max(values))

    order = np.argsort(values)
    v = values[order]
    w = weights[order].astype(float)

    total = float(np.sum(w))
    if total <= 0:
        return float(np.quantile(v, q))

    cum = np.cumsum(w)
    target = q * total
    return float(v[cum >= target][0])


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    return _weighted_quantile(values, weights, 0.5)


def _weighted_price_quantile(df: pd.DataFrame, q: float) -> float:
    if df.empty:
        return float("nan")
    d = df.sort_values("price")
    return _weighted_quantile(d["price"].to_numpy(dtype=float), d["quantity"].to_numpy(dtype=float), q)


def _unweighted_price_quantile(df: pd.DataFrame, q: float) -> float:
    if df.empty:
        return float("nan")
    return float(np.quantile(df["price"].to_numpy(dtype=float), q))


def add_price_stats_for_item(df_item: pd.DataFrame) -> pd.DataFrame:
    df_item = df_item.copy()

    prices = df_item["price"].to_numpy(dtype=float)
    qty = df_item["quantity"].to_numpy(dtype=float)

    total_w = float(np.sum(qty))
    if total_w <= 0:
        median = float(np.median(prices))
        q1 = float(np.quantile(prices, 0.25))
        q3 = float(np.quantile(prices, 0.75))
        mad = float(np.median(np.abs(prices - median)))
    else:
        median = _weighted_median(prices, qty)
        q1 = _weighted_quantile(prices, qty, 0.25)
        q3 = _weighted_quantile(prices, qty, 0.75)
        mad = _weighted_median(np.abs(prices - median), qty)

    df_item["price_median"] = median
    df_item["price_q1"] = q1
    df_item["price_q3"] = q3
    df_item["price_iqr"] = q3 - q1
    df_item["price_mad"] = mad

    if mad > 0:
        df_item["robust_z"] = 0.6745 * (df_item["price"] - median) / mad
    else:
        df_item["robust_z"] = 0.0

    df_item["is_extreme_price"] = df_item["robust_z"].abs() > 3.0
    return df_item


def add_depth_features_for_item(df_item: pd.DataFrame) -> pd.DataFrame:
    df_item = df_item.copy().sort_values("price").reset_index(drop=True)
    df_item["cum_qty"] = df_item["quantity"].cumsum()
    total = float(df_item["quantity"].sum())
    df_item["cum_qty_pct"] = (df_item["cum_qty"] / total) if total > 0 else 0.0
    return df_item


def mark_suspected_anchors_for_item(
    df_item: pd.DataFrame,
    z_threshold: float = 5.0,
    front_depth_pct: float = 0.02,
    back_depth_pct: float = 0.02,
    max_level_units_for_anchor: float = 50.0,
) -> pd.DataFrame:
    df_item = df_item.copy()

    if "robust_z" not in df_item.columns or "cum_qty_pct" not in df_item.columns:
        raise ValueError("Missing required features. Call add_price_stats_for_item and add_depth_features_for_item first.")

    level = df_item.groupby("price", as_index=False)["quantity"].sum().rename(columns={"quantity": "level_qty"})
    total_qty = float(level["level_qty"].sum())
    level["level_share"] = (level["level_qty"] / total_qty) if total_qty > 0 else 0.0

    df_item = df_item.merge(level[["price", "level_share"]], on="price", how="left")

    total_qty_all = float(df_item["quantity"].sum())
    max_level_share = float(df_item["level_share"].max()) if len(df_item) else 0.0
    median_price = float(df_item["price_median"].iloc[0]) if len(df_item) else 0.0

    exclusive_mode = (total_qty_all <= EXCLUSIVE_TOTAL_UNITS_THRESHOLD) or (max_level_share >= EXCLUSIVE_DOMINANCE_SHARE)

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


def compute_price_suggestions_for_item(df_item: pd.DataFrame) -> dict:
    item_id = int(df_item["item_id"].iloc[0])
    item_name = df_item["item_name"].iloc[0] if "item_name" in df_item.columns else None
    item_type = df_item["item_type"].iloc[0] if "item_type" in df_item.columns else None
    average_price = df_item["average_price"].iloc[0] if "average_price" in df_item.columns else None
    my_quantity = df_item["my_quantity"].iloc[0] if "my_quantity" in df_item.columns else None

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
            "num_listings": int(len(df_item)),
            "num_suspected_anchors": int(df_item["is_suspected_anchor"].sum()) if "is_suspected_anchor" in df_item.columns else 0,
            "fast_sell_price": float("nan"),
            "fair_price": float("nan"),
            "greedy_price": float("nan"),
            "clean_median_price": float("nan"),
            "clean_q1_price": float("nan"),
            "clean_q3_price": float("nan"),
        }

    total_qty_clean = float(df_clean["quantity"].sum())
    total_listings_clean = int(len(df_clean))
    avg_qty_per_listing = (total_qty_clean / total_listings_clean) if total_listings_clean > 0 else 0.0

    level = df_clean.groupby("price", as_index=False)["quantity"].sum().rename(columns={"quantity": "level_qty"})
    max_level_share_clean = (float(level["level_qty"].max()) / total_qty_clean) if total_qty_clean > 0 else 0.0

    exclusive_mode = (total_qty_clean <= EXCLUSIVE_TOTAL_UNITS_THRESHOLD) or (max_level_share_clean >= EXCLUSIVE_DOMINANCE_SHARE)

    if exclusive_mode:
        fair_price = _unweighted_price_quantile(df_clean, 0.5)
        q1_price = _unweighted_price_quantile(df_clean, 0.25)
        q3_price = _unweighted_price_quantile(df_clean, 0.75)
    else:
        fair_price = _weighted_price_quantile(df_clean, 0.5)
        q1_price = _weighted_price_quantile(df_clean, 0.25)
        q3_price = _weighted_price_quantile(df_clean, 0.75)

    df_clean_sorted = df_clean.sort_values("price").copy()
    df_clean_sorted["cum_qty_clean"] = df_clean_sorted["quantity"].cumsum()

    is_bulk_mode = (not exclusive_mode) and (avg_qty_per_listing > 2.0)

    if exclusive_mode:
        idx = min(2, len(df_clean_sorted) - 1)
        fast_sell_raw = float(df_clean_sorted.iloc[idx]["price"])
    else:
        if avg_qty_per_listing <= 2.0:
            target_listings = min(FAST_SELL_LISTINGS_THRESHOLD, len(df_clean_sorted))
            fast_sell_raw = float(df_clean_sorted.iloc[target_listings - 1]["price"])
        else:
            target_units = min(FAST_SELL_UNITS_THRESHOLD, total_qty_clean)
            mask = df_clean_sorted["cum_qty_clean"] >= target_units
            fast_sell_raw = float(df_clean_sorted.loc[mask, "price"].iloc[0]) if mask.any() else float(df_clean_sorted["price"].iloc[-1])

    if np.isfinite(fast_sell_raw):
        base_price = float(round(fast_sell_raw))
        if is_bulk_mode:
            fast_sell_price = max(math.floor(base_price) - 1, 0.0)
        else:
            fast_sell_price = max(base_price, 0.0)
    else:
        fast_sell_price = float("nan")

    num_listings = int(len(df_item))
    num_suspected_anchors = int(df_item["is_suspected_anchor"].sum()) if "is_suspected_anchor" in df_item.columns else 0

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


def enrich_all_items(long_df: pd.DataFrame) -> pd.DataFrame:
    required = {
        "item_id",
        "item_name",
        "item_type",
        "average_price",
        "my_quantity",
        "listing_rank",
        "price",
        "quantity",
    }
    missing = required.difference(long_df.columns)
    if missing:
        raise ValueError(f"Missing columns in long_df: {sorted(missing)}")

    safe_df = long_df[list(required)].copy()
    return safe_df.groupby("item_id", group_keys=False).apply(enrich_item_orders).reset_index(drop=True)


def build_summary_from_enriched(enriched_df: pd.DataFrame) -> pd.DataFrame:
    summaries: list[dict] = []
    for _, df_item in enriched_df.groupby("item_id"):
        summaries.append(compute_price_suggestions_for_item(df_item))
    return pd.DataFrame(summaries)
