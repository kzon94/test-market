import io, pandas as pd

def to_csv_bytes(df: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue().encode("utf-8-sig")

def apply_display_formatting(df_display: pd.DataFrame) -> pd.DataFrame:
    from .analytics import fmt_int, fmt_pct
    df = df_display.copy()
    int_cols = ["average_price_api","price_min","price_mean_20u","depth20_total_cost",
                "price_max","weighted_mean_all_units","price_range",
                "suggest_sell_price","gross_revenue","market_fee_5pct","net_revenue","net_per_unit",
                "ref_price_min","ref_price_mean_20u","ref_weighted_mean_all"]
    for c in int_cols:
        if c in df.columns: df[c] = df[c].apply(lambda x: fmt_int(x) if pd.notna(x) else "-")
    if "spread_pct" in df.columns: df["spread_pct"] = df["spread_pct"].apply(lambda x: fmt_pct(x) if pd.notna(x) else "-")
    if "cv_price" in df.columns: df["cv_price"] = df["cv_price"].apply(lambda x: fmt_pct(x) if pd.notna(x) else "-")
    for c in ["amount_at_min","units_used_for_20u","total_stock","my_quantity"]:
        if c in df.columns: df[c] = df[c].fillna(0).astype(int)
    return df