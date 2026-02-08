# Kzon's Torn Market Analyzer

**Torn Market Analyzer** is a Python-based analytics tool with a Streamlit web interface designed to analyze the **Item Market** of the online game **Torn**.

The application allows users to paste raw text copied directly from Torn’s **Add Listing** page. From this input, the tool parses the inventory, matches items to Torn IDs, retrieves live market data via Torn’s public API, and computes **robust, market-aware price recommendations** tailored to different market structures.

---

## Core Workflow

1. **Paste Add Listing text** from Torn’s Item Market
2. **Parse & normalize inventory data**
3. **Match items to Torn item IDs** using a local dictionary + fuzzy matching
4. **Fetch live market listings** (up to 100 sell listings per item)
5. **Clean and analyze the market book**
6. **Compute three sale prices per item** based on market structure
7. **Display results and diagnostics directly in the UI**

---

## Features

### Input Processing

* Accepts raw text copied from Torn’s **Add Listing** page.
* Robust parsing of:

  * Item names
  * Quantities
  * UI noise and irrelevant lines
* Automatically ignores:

  * Equipped items
  * Untradable items
* Item matching uses:

  * Normalized item keys
  * Fuzzy matching (`token_set_ratio`)
* Item resolution is backed by a local dictionary:

  * `torn_item_dictionary.csv`

---

### Market Data Retrieval

* Queries Torn’s public `itemmarket` endpoint.
* Fetches **up to 100 sell listings per item**.
* Supports multiple API key injection methods.
* Includes a built-in **token bucket rate limiter** to respect Torn API limits.

---

### Market Cleaning & Structure Detection

Before pricing, the market is cleaned and classified:

* Detection of **suspected price anchors** using:

  * Robust Z-scores (MAD-based)
  * Depth concentration
  * Volume dominance per price level
* Differentiation between:

  * **Bulk markets** (stack-based trading)
  * **Unit-style markets** (single-item listings)
  * **Thin / exclusive markets** (low depth or dominant price levels)

All downstream pricing logic depends on this classification.

---

### Price Recommendations

For each item, the app computes **three prices**, always derived from the **cleaned market**:

#### 1. Fast-sell price

* Designed for quick execution.
* **Applied rules:**

  * **Bulk markets only**:
    - Always **1$ below the relevant bulk wall**
  * **Unit-style or exclusive markets**:
    - No undercut applied; price is left unchanged (float-safe rounded).
* Guarantees that in bulk markets the fast-sell price **never equals the wall price**.

#### 2. Fair price

* Robust estimate of the “true” market value.
* Computed as the **median of the cleaned price distribution**.
* Resistant to outliers and anchors.

#### 3. Greedy price

* Upper-end pricing strategy.
* Computed as the **upper quartile (Q3)** of the cleaned market.

---

## Project Structure

```
torn-market-analyzer/
│
├── app/
│   └── streamlit_app.py         # Streamlit UI and orchestration
│
├── src/
│   └── tma/
│       ├── config.py            # Global constants and thresholds
│       ├── matching.py          # Text parsing and fuzzy item matching
│       ├── http_api.py          # Torn API client (itemmarket)
│       ├── rate_limit.py        # Token bucket rate limiter
│       ├── market_enrichment.py # Market cleaning & pricing logic
│       ├── io_utils.py          # Formatting helpers (UI-focused)
│       └── __init__.py
│
├── data/
│   └── torn_item_dictionary.csv # Local item name - ID mapping
│
├── LICENSE
├── README.md
└── requirements.txt
```

---

## Requirements

* Python **3.10+**
* Streamlit
* Pandas
* NumPy
* Requests
* PyArrow

---

## Running Locally

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Launch the app:

```bash
streamlit run app/streamlit_app.py
```

3. Open the provided local URL (usually `http://localhost:8501`).

---

## Torn API Usage Notes

* Uses **public Torn API keys only**.
* Performs **read-only** requests to the `itemmarket` endpoint.
* API keys are cached locally by Streamlit for convenience.
* No keys or data are transmitted outside the user’s machine.
* All requests respect Torn’s rate limits via controlled throttling.

---

## License

This project is licensed under the **MIT License**.
See the `LICENSE` file for details.
