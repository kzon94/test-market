import re, pandas as pd
from typing import Dict, Tuple, Optional, List
from difflib import SequenceMatcher
from pathlib import Path
from .cleaning import to_key, drop_color_prefix, extract_quantity, strip_noise_keep_name, split_raw_into_segments
from .config import FUZZY_THRESHOLD

def load_dict(dict_path: Path) -> Dict[str, int]:
    df = pd.read_csv(dict_path, dtype=str)
    cols = {c.lower(): c for c in df.columns}
    if "key" not in cols or "id" not in cols:
        raise ValueError("Dictionary missing 'key' or 'id' columns")
    df["key_norm"] = df[cols["key"]].map(to_key)
    df = df[df[cols["id"]].str.fullmatch(r"\d+")]
    return {k:int(v) for k,v in zip(df["key_norm"], df[cols["id"]])}

def token_set_ratio(a: str, b: str) -> int:
    ta = set(re.findall(r'[a-z0-9]+', a.lower()))
    tb = set(re.findall(r'[a-z0-9]+', b.lower()))
    if not ta or not tb: return 0
    inter = ' '.join(sorted(ta & tb))
    a_only = ' '.join(sorted(ta - tb))
    b_only = ' '.join(sorted(tb - ta))
    def r(x, y): return int(round(100 * SequenceMatcher(None, x, y).ratio()))
    return max(r(' '.join(sorted(ta)), ' '.join(sorted(tb))),
               r(inter, inter),
               r(inter + ' ' + a_only, inter + ' ' + b_only))

def best_match(key_to_id: Dict[str,int], candidate: str, threshold: int = FUZZY_THRESHOLD) -> Tuple[Optional[str], Optional[int], int]:
    norm = to_key(candidate)
    if norm in key_to_id:
        return norm, key_to_id[norm], 100
    core = re.sub(r'[^a-z0-9]', '', norm)
    if len(core) <= 2:
        return None, None, -1
    best_key, best_id, best_score = None, None, -1
    for k, idv in key_to_id.items():
        sc = token_set_ratio(norm, k)
        if sc > best_score:
            best_key, best_id, best_score = k, idv, sc
    if best_score >= threshold:
        return best_key, best_id, best_score
    return None, None, best_score

def clean_and_match_from_raw(raw_text: str, dict_map: Dict[str,int], threshold: int = FUZZY_THRESHOLD):
    segments = split_raw_into_segments(raw_text)
    rows, seen = [], set()
    for seg in segments:
        base = drop_color_prefix(seg)
        qty  = extract_quantity(base)
        cleaned = strip_noise_keep_name(base)
        if not cleaned: continue
        mkey, mid, score = best_match(dict_map, cleaned, threshold=threshold)
        if mkey is not None and mkey in seen: continue
        if mkey is not None: seen.add(mkey)
        rows.append({
            "input_segment": seg,
            "cleaned_name": cleaned,
            "normalized_key": mkey or "",
            "id": mid or "",
            "confidence": score,
            "quantity": qty if qty is not None else 1,
        })
    return rows

def aggregate_id_quantity(clean_rows: List[dict]) -> List[Tuple[int,int]]:
    agg = {}
    for r in clean_rows:
        v = str(r.get("id") or "").strip()
        if not v.isdigit(): continue
        iid = int(v)
        q = int(r.get("quantity") or 1)
        agg[iid] = agg.get(iid, 0) + q
    return sorted(agg.items())