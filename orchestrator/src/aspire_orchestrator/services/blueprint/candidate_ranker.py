from __future__ import annotations
import logging
from typing import Any, Literal
logger = logging.getLogger(__name__)
MatchClass = Literal["exact", "functional", "substitute"]
_W_IN_STOCK, _W_DISTANCE, _W_PRICE, _W_BRAND, _W_TARIFF = 0.40, 0.25, 0.15, 0.15, 0.05
_EXACT_T, _FUNC_T = 0.85, 0.70
_MAX_DIST, _MAX_PRICE_PCT = 100.0, 50.0

def _normalize_distance(d):
    if d is None or d < 0: return 0.5
    return min(1.0, d / _MAX_DIST)

def _normalize_price(price, base):
    if price is None or base is None or base <= 0: return 0.5
    return min(1.0, max(0.0, (price - base) / base * 100.0) / _MAX_PRICE_PCT)

def _brand_score(brand, bmap):
    if not brand or not bmap: return 0.0
    bk = brand.strip().lower()
    for k, v in bmap.items():
        if k.strip().lower() == bk: return float(min(1.0, max(0.0, v)))
    return 0.0

def score_candidate(c, *, brand_familiarity_map, baseline_price=None):
    product = c.get("product") or {}
    supplier = c.get("supplier") or {}
    price = c.get("price") or {}
    raw = (
        _W_IN_STOCK * (1.0 if bool(product.get("in_stock")) else 0.0)
        + _W_DISTANCE * (1.0 - _normalize_distance(supplier.get("distance_mi")))
        + _W_PRICE * (1.0 - _normalize_price(price.get("value"), baseline_price))
        + _W_BRAND * _brand_score(product.get("brand"), brand_familiarity_map)
        - _W_TARIFF * (0.5 if c.get("tariff_flag_detected") else 0.0)
    )
    return round(max(0.0, min(1.0, raw)), 4)

def assign_match_class(score, *, in_stock, brand, brand_familiarity_map):
    brand_known = bool(brand and brand_familiarity_map and _brand_score(brand, brand_familiarity_map) > 0.0)
    if score >= _EXACT_T and brand_known and in_stock: return "exact"
    if score >= _FUNC_T and in_stock: return "functional"
    return "substitute"

def rank_candidates(raw_candidates, *, brand_familiarity_map, top_n=3):
    if not raw_candidates: return []
    prices = [float(v) for c in raw_candidates if (v := (c.get("price") or {}).get("value")) is not None and float(v) > 0]
    baseline = min(prices) if prices else None
    annotated = []
    for c in raw_candidates:
        cc = dict(c)
        score = score_candidate(cc, brand_familiarity_map=brand_familiarity_map, baseline_price=baseline)
        product = cc.get("product") or {}
        cc["match_score"] = score
        cc["match_class"] = assign_match_class(score, in_stock=bool(product.get("in_stock")), brand=product.get("brand"), brand_familiarity_map=brand_familiarity_map)
        annotated.append(cc)
    annotated.sort(key=lambda c: c["match_score"], reverse=True)
    return annotated[:top_n]