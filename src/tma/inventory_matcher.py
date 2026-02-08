from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


@dataclass(frozen=True)
class MatchedItem:
    name: str
    item_id: int
    qty: int


@dataclass(frozen=True)
class ParseResult:
    matched: List[MatchedItem]
    unmatched: List[Tuple[str, int]]  # (name, qty)


_QTY_LINE_RE = re.compile(r"^x\s*(\d+)$", re.IGNORECASE)
_NAME_QTY_SAME_LINE_RE = re.compile(r"^(.*)\s+x\s*(\d+)$", re.IGNORECASE)


def _clean_line(line: str) -> str:
    return line.replace("\u00a0", " ").strip()


def load_dictionary(dict_csv_path: str | Path) -> Dict[str, int]:
    path = Path(dict_csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Dictionary CSV not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("Dictionary CSV has no header row.")

        headers = {h.strip().lower(): h for h in reader.fieldnames}

        def pick(*candidates: str) -> Optional[str]:
            for c in candidates:
                if c in headers:
                    return headers[c]
            return None

        name_col = pick("key", "name", "item_name", "item", "title")
        id_col = pick("id", "item_id", "itemid")

        if not name_col or not id_col:
            raise ValueError(
                "Dictionary CSV must contain a name column (key/name/item_name/...) "
                "and an id column (id/item_id/itemid)."
            )

        mapping: Dict[str, int] = {}
        for row in reader:
            name = (row.get(name_col) or "").strip()
            raw_id = (row.get(id_col) or "").strip()
            if not name or not raw_id:
                continue
            try:
                mapping[name] = int(raw_id)
            except ValueError:
                continue

    if not mapping:
        raise ValueError("Dictionary CSV loaded but produced an empty mapping (no valid rows).")

    return mapping


def parse_add_listings_text(raw_text: str, valid_item_names: Set[str]) -> List[Tuple[str, int, bool]]:
    """
    Returns a list of tuples: (name, qty, is_omitted)
    - Omitted if block contains 'Equipped' or 'Untradable'
    - Qty defaults to 1
    - Includes items even if there is no price/RRP/Qty/Price section afterwards
    """
    lines = [_clean_line(l) for l in raw_text.splitlines()]
    lines = [l for l in lines if l]

    results: List[Tuple[str, int, bool]] = []

    current_name: Optional[str] = None
    current_qty: int = 1
    current_omitted: bool = False

    def flush() -> None:
        nonlocal current_name, current_qty, current_omitted
        if current_name is not None:
            results.append((current_name, current_qty, current_omitted))
        current_name = None
        current_qty = 1
        current_omitted = False

    for line in lines:
        # Start of a new item block (exact line match)
        if line in valid_item_names:
            flush()
            current_name = line
            current_qty = 1
            current_omitted = False
            continue

        # Start of a new item block with qty on same line: "Name x5"
        m_same = _NAME_QTY_SAME_LINE_RE.match(line)
        if m_same:
            possible_name = m_same.group(1).strip()
            if possible_name in valid_item_names:
                flush()
                current_name = possible_name
                current_qty = int(m_same.group(2))
                current_omitted = False
                continue

        if current_name is None:
            continue

        low = line.lower()
        if low == "equipped" or low == "untradable":
            current_omitted = True
            continue

        m_qty = _QTY_LINE_RE.match(line)
        if m_qty:
            current_qty = int(m_qty.group(1))
            continue

    flush()
    return results


def match_inventory(raw_text: str, dict_csv_path: str | Path) -> ParseResult:
    name_to_id = load_dictionary(dict_csv_path)
    valid_names = set(name_to_id.keys())

    parsed = parse_add_listings_text(raw_text, valid_names)

    aggregated: Dict[int, Tuple[str, int]] = {}
    unmatched: List[Tuple[str, int]] = []

    for name, qty, omitted in parsed:
        if omitted:
            continue

        item_id = name_to_id.get(name)
        if item_id is None:
            unmatched.append((name, qty))
            continue

        if item_id in aggregated:
            prev_name, prev_qty = aggregated[item_id]
            aggregated[item_id] = (prev_name, prev_qty + qty)
        else:
            aggregated[item_id] = (name, qty)

    matched = [MatchedItem(name=n, item_id=i, qty=q) for i, (n, q) in aggregated.items()]
    matched.sort(key=lambda x: x.name)

    return ParseResult(matched=matched, unmatched=unmatched)


def to_csv_rows(result: ParseResult) -> List[List[str]]:
    rows = [["name", "id", "qty"]]
    for it in result.matched:
        rows.append([it.name, str(it.item_id), str(it.qty)])
    return rows


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Parse Torn 'Add listings' inventory and match to item dictionary.")
    parser.add_argument("--dict", default="torn_item_dictionary.csv", help="Path to torn_item_dictionary.csv")
    parser.add_argument("--in", dest="input_path", default=None, help="Path to a text file with copied 'Add listings' content. If omitted, reads stdin.")
    parser.add_argument("--out", dest="output_path", default=None, help="Optional output CSV path. If omitted, prints CSV to stdout.")
    args = parser.parse_args()

    if args.input_path:
        raw_text = Path(args.input_path).read_text(encoding="utf-8", errors="replace")
    else:
        raw_text = sys.stdin.read()

    result = match_inventory(raw_text, args.dict)

    csv_rows = to_csv_rows(result)
    out_text = "\n".join([",".join(r) for r in csv_rows])

    if args.output_path:
        Path(args.output_path).write_text(out_text, encoding="utf-8")
    else:
        print(out_text)

    if result.unmatched:
        sys.stderr.write(f"\nUnmatched items ({len(result.unmatched)}):\n")
        for name, qty in result.unmatched[:200]:
            sys.stderr.write(f"- {name} x{qty}\n")


if __name__ == "__main__":
    main()
