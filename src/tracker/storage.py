"""Paths, raw/queue/carry-forward persistence, summary building, schema validation."""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import shutil
from collections import defaultdict
from pathlib import Path

import jsonschema

from .config import ROOT, Config
from .history import History, route_key

DATA = ROOT / "data"
SCHEMAS = ROOT / "schemas"
SCHEMA_VERSION = "1.0.0"


def run_dir(today: str) -> Path:
    return DATA / "runs" / today


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"), default=str)


def save_raw(today: str, source: str, params: dict, raw) -> str:
    if raw is None:
        return ""
    h = hashlib.sha1(dumps(params).encode()).hexdigest()[:16]
    p = run_dir(today) / "raw" / f"{source}_{h}.json.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt") as f:
        f.write(dumps({"query_params": params, "response": raw}))
    return str(p.relative_to(ROOT))


def prune_raw(today: dt.date, keep_days: int) -> int:
    n = 0
    for d in (DATA / "runs").glob("*/raw"):
        try:
            day = dt.date.fromisoformat(d.parent.name)
        except ValueError:
            continue
        if (today - day).days > keep_days:
            shutil.rmtree(d)
            n += 1
    return n


# ---------------------------------------------------------------- queue

def load_queue(today: str) -> dict:
    p = DATA / "state" / "queue.json"
    if p.exists():
        q = json.loads(p.read_text())
        if q.get("date") == today:
            return q
    return {"date": today, "tasks": []}


def save_queue(q: dict) -> None:
    p = DATA / "state" / "queue.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(q, indent=0, default=str) + "\n")


# ---------------------------------------------------------------- itineraries

def load_itineraries(today: str) -> dict[str, dict]:
    p = run_dir(today) / "itineraries.jsonl"
    if not p.exists():
        return {}
    return {r["itinerary_id"]: r for r in map(json.loads, p.read_text().splitlines()) if r}


def save_itineraries(today: str, recs: dict[str, dict]) -> None:
    p = run_dir(today) / "itineraries.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(dumps(r) + "\n" for r in recs.values()))


def merge_record(store: dict[str, dict], r: dict) -> None:
    """Same itinerary seen twice today: keep the record with more information
    (inbound known), then the cheaper / later one."""
    old = store.get(r["itinerary_id"])
    if old is None or (r["inbound_known"], -r["price"]["price_total_czk"]) >= \
            (old["inbound_known"], -old["price"]["price_total_czk"]):
        store[r["itinerary_id"]] = r


# ---------------------------------------------------------------- carry-forward

LAST_SEEN = DATA / "state" / "last_seen.json.gz"


def load_last_seen() -> dict:
    if not LAST_SEEN.exists():
        return {}
    with gzip.open(LAST_SEEN, "rt") as f:
        return json.load(f)


def update_last_seen(store: dict, today: str, recs: list[dict], keep: int = 3) -> None:
    by = defaultdict(list)
    for r in recs:
        by["|".join(route_key(r))].append(r)
    for k, rs in by.items():
        rs.sort(key=lambda r: r["totals"]["total_trip_cost_czk"])
        store[k] = {"date": today, "records": rs[:keep]}
    LAST_SEEN.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(LAST_SEEN, "wt") as f:
        f.write(dumps(store))


def carried_forward(store: dict, today: str, fresh_keys: set[str], max_stale: int) -> list[dict]:
    out, t = [], dt.date.fromisoformat(today)
    for k, v in store.items():
        stale = (t - dt.date.fromisoformat(v["date"])).days
        if k in fresh_keys or not 0 < stale <= max_stale:
            continue
        for r in v["records"]:
            r = json.loads(json.dumps(r))
            r["provenance"]["stale_days"] = stale
            out.append(r)
    return out


# ---------------------------------------------------------------- summary

def _item(r: dict, hist: History, today: str) -> dict:
    return {**r, "price_history": hist.summary(r["itinerary_id"], today)}


def build_summary(cfg: Config, meta: dict, recs: list[dict], hist: History, today: str) -> dict:
    ok = [r for r in recs if r["scoring"]["eligible"] and r["price"]["separate_tickets"] is not True]
    by_score = sorted(ok, key=lambda r: -r["scoring"]["final_score"])
    cost = lambda r: r["totals"]["total_trip_cost_czk"]  # noqa: E731
    time = lambda r: r["totals"]["door_to_door_out_min"] + r["totals"]["door_to_door_back_min"]  # noqa: E731
    it = lambda r: _item(r, hist, today)  # noqa: E731

    def best_per(keyf, rows=by_score):
        seen = {}
        for r in rows:
            seen.setdefault(keyf(r), r)
        return {str(k): it(v) for k, v in sorted(seen.items(), key=lambda kv: str(kv[0]))}

    matrix: dict[str, dict] = defaultdict(dict)
    for r in sorted(ok, key=cost):
        row, col = r["origin"], str(r["calendar"]["calendar_days_away"])
        matrix[row].setdefault(col, {"min_total_trip_cost_czk": cost(r), "itinerary_id": r["itinerary_id"]})
    sep = [r for r in recs if r["price"]["separate_tickets"] is True and r["scoring"]["eligible"]]
    lows = []
    for r in sorted(ok, key=cost):
        kind = hist.is_new_low(r, today) if r["provenance"]["stale_days"] == 0 else None
        if kind:
            lows.append({**it(r), "new_low_kind": kind})
    w = cfg.window
    lengths = range(w.min_calendar_days_away, (w.latest_home_arrival - w.earliest_leave_home).days + 2)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_metadata": meta,
        "counts": {"records_total": len(recs), "eligible_protected": len(ok),
                   "fresh_today": sum(r["provenance"]["stale_days"] == 0 for r in recs)},
        "top_overall": [it(r) for r in by_score[:30]],
        "top_by_price": [it(r) for r in sorted(ok, key=cost)[:15]],
        "top_by_stay": [it(r) for r in sorted(ok, key=lambda r: -r["calendar"]["hours_in_san_diego_area"])[:15]],
        "top_by_time": [it(r) for r in sorted(ok, key=time)[:15]],
        "pareto_front": [it(r) for r in sorted((r for r in ok if r["scoring"]["pareto_optimal"]), key=cost)[:40]],
        "best_per_origin": best_per(lambda r: r["origin"]),
        "best_per_date_pair": best_per(lambda r: r["pair_key"]),
        "best_per_trip_length": {k: v for k, v in best_per(lambda r: r["calendar"]["calendar_days_away"]).items()
                                 if int(k) in lengths},
        "best_open_jaw": [it(r) for r in by_score if r["trip_type"] == "open_jaw"][:10],
        "best_per_california_airport": best_per(lambda r: r["destination"]),
        "flagged_cheaper_separate_tickets": [it(r) for r in sorted(sep, key=cost)[:15]],
        "price_vs_length_matrix": dict(sorted(matrix.items())),
        "new_lows_today": lows[:30],
    }
    # Keep the file small: replace the biggest per-key lists with references if needed.
    if len(dumps(summary)) > cfg.storage.summary_max_bytes:
        for k in ("best_per_date_pair", "best_per_trip_length", "best_per_origin"):
            summary[k] = {kk: {"itinerary_id": v["itinerary_id"], "total_trip_cost_czk": cost(v),
                               "final_score": v["scoring"]["final_score"]} for kk, v in summary[k].items()}
            if len(dumps(summary)) <= cfg.storage.summary_max_bytes:
                break
    return summary


# ---------------------------------------------------------------- validation

def validate(obj, schema_name: str) -> None:
    schema = json.loads((SCHEMAS / schema_name).read_text())
    jsonschema.validate(obj, schema)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str) + "\n")
