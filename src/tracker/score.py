"""Balanced scoring (price / stay / time, min-max normalised within today's set),
penalties and the Pareto front over (cost, stay, time)."""
from __future__ import annotations

from .config import Config


def eligible(r: dict) -> bool:
    return not r["flags"]["constraint_violations"]


def _cost(r):
    return r["totals"]["total_trip_cost_czk"]


def _stay(r):
    return r["calendar"]["hours_in_san_diego_area"]


def _time(r):
    return r["totals"]["door_to_door_out_min"] + r["totals"]["door_to_door_back_min"]


def _norm(x, lo, hi, higher_is_better):
    if hi == lo:
        return 1.0
    v = (x - lo) / (hi - lo)
    return v if higher_is_better else 1 - v


def penalties(cfg: Config, r: dict) -> dict[str, float]:
    p, f, out = cfg.scoring.penalties, r["flags"], {}
    ld = r["calendar"]["leave_days_used"]
    if ld == 13:
        out["leave_13"] = p.leave_13
    elif ld >= 14:
        out["leave_14"] = p.leave_14
    if r["price"]["separate_tickets"] is True:
        out["separate_tickets"] = p.separate_tickets
    if r["price"]["bag_certainty"] == "unknown":
        out["bag_unknown"] = p.bag_unknown
    if f["us_entry_connection_risky"]:
        out["us_entry_connection_risky"] = p.us_entry_connection_risky
    if f["tight_connection"]:
        out["tight_connection"] = p.tight_connection
    if r["ground"]["hotel_nights"]:
        out["hotel_needed"] = round(p.hotel_needed_per_night * r["ground"]["hotel_nights"], 4)
    if f["airport_change"]:
        out["airport_change"] = p.airport_change
    extra = sum(max(0, leg["stops"] - 1) for leg in r["legs"].values() if leg)
    if extra:
        out["extra_stop"] = round(p.extra_stop_per_stop * extra, 4)
    return out


def pareto_ids(recs: list[dict]) -> set[str]:
    # ponytail: sort-and-sweep skyline, O(n * front); fine for ~10k records.
    pts = sorted(((_cost(r), -_stay(r), _time(r), r["itinerary_id"]) for r in recs))
    front: list[tuple] = []
    for c, s, t, i in pts:
        if not any(fc <= c and fs <= s and ft <= t for fc, fs, ft, _ in front):
            front.append((c, s, t, i))
    return {i for *_, i in front}


def score_all(cfg: Config, recs: list[dict]) -> None:
    """Fill r['scoring'] in place. Ineligible records (constraint violations) get
    component scores but final_score None."""
    pool = [r for r in recs if eligible(r)] or recs
    w = cfg.scoring.weights
    lo_c, hi_c = min(map(_cost, pool), default=0), max(map(_cost, pool), default=0)
    lo_s, hi_s = min(map(_stay, pool), default=0), max(map(_stay, pool), default=0)
    lo_t, hi_t = min(map(_time, pool), default=0), max(map(_time, pool), default=0)
    front = pareto_ids([r for r in recs if eligible(r)])
    for r in recs:
        ps = _norm(_cost(r), lo_c, hi_c, False)
        ss = _norm(_stay(r), lo_s, hi_s, True)
        ts = _norm(_time(r), lo_t, hi_t, False)
        pen = penalties(cfg, r)
        base = w.price * ps + w.stay * ss + w.time * ts
        r["scoring"] = {
            "price_score": round(ps, 4), "stay_score": round(ss, 4), "time_score": round(ts, 4),
            "penalties": pen, "penalty_total": round(sum(pen.values()), 4),
            "final_score": round(base - sum(pen.values()), 4) if eligible(r) else None,
            "pareto_optimal": r["itinerary_id"] in front, "eligible": eligible(r),
        }
