"""Append-only price history CSVs and per-itinerary history summaries."""
from __future__ import annotations

import csv
import datetime as dt
import statistics
from collections import defaultdict
from pathlib import Path

PRICE_COLS = ["date", "itinerary_id", "origin", "destination", "return_origin", "return_destination",
              "leave_home_date", "home_arrival_date", "price_czk", "bag_certainty", "separate_tickets", "source"]
ROUTE_COLS = ["date", "origin", "destination", "return_origin", "return_destination", "pair_key",
              "min_total_trip_cost_czk", "itinerary_id"]


def _rewrite_today(path: Path, cols: list[str], today: str, rows: list[dict]) -> None:
    """Append today's rows; a same-day rerun replaces today's rows instead of duplicating them."""
    old = []
    if path.exists():
        with path.open() as f:
            old = [r for r in csv.DictReader(f) if r["date"] != today]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(old + rows)


def route_key(r: dict) -> tuple:
    return (r["origin"], r["destination"], r["return_origin"], r["return_destination"], r["pair_key"])


def write_history(dir_: Path, today: str, recs: list[dict]) -> None:
    """recs = today's freshly fetched records only (no carried-forward ones)."""
    rows = [{
        "date": today, "itinerary_id": r["itinerary_id"], "origin": r["origin"], "destination": r["destination"],
        "return_origin": r["return_origin"], "return_destination": r["return_destination"],
        "leave_home_date": r["calendar"]["leave_home_date"], "home_arrival_date": r["calendar"]["home_arrival_date"],
        "price_czk": r["price"]["price_total_czk"], "bag_certainty": r["price"]["bag_certainty"],
        "separate_tickets": r["price"]["separate_tickets"], "source": r["provenance"]["source"],
    } for r in recs]
    _rewrite_today(dir_ / "price_history.csv", PRICE_COLS, today, rows)
    best: dict[tuple, dict] = {}
    for r in recs:
        if r["scoring"].get("eligible", True) and r["price"]["separate_tickets"] is not True:
            k = route_key(r)
            if k not in best or r["totals"]["total_trip_cost_czk"] < best[k]["totals"]["total_trip_cost_czk"]:
                best[k] = r
    routes = [dict(zip(ROUTE_COLS, [today, *k, r["totals"]["total_trip_cost_czk"], r["itinerary_id"]]))
              for k, r in sorted(best.items())]
    _rewrite_today(dir_ / "route_daily_min.csv", ROUTE_COLS, today, routes)


class History:
    def __init__(self, dir_: Path):
        self.by_id: dict[str, dict[str, int]] = defaultdict(dict)   # id -> {date: min price}
        self.route_min: dict[tuple, dict[str, int]] = defaultdict(dict)
        p = dir_ / "price_history.csv"
        if p.exists():
            with p.open() as f:
                for r in csv.DictReader(f):
                    d, v = self.by_id[r["itinerary_id"]], int(float(r["price_czk"]))
                    d[r["date"]] = min(v, d.get(r["date"], v))
        p = dir_ / "route_daily_min.csv"
        if p.exists():
            with p.open() as f:
                for r in csv.DictReader(f):
                    k = (r["origin"], r["destination"], r["return_origin"], r["return_destination"], r["pair_key"])
                    self.route_min[k][r["date"]] = int(float(r["min_total_trip_cost_czk"]))

    def summary(self, iid: str, today: str) -> dict:
        h = self.by_id.get(iid)
        if not h:
            return {"first_seen": None, "times_seen": 0, "min": None, "median": None, "last": None,
                    "delta_vs_yesterday": None, "trend_7d": None}
        dates = sorted(h)
        last = h[dates[-1]]
        y = str(dt.date.fromisoformat(today) - dt.timedelta(days=1))
        week = [h[d] for d in dates if d >= str(dt.date.fromisoformat(today) - dt.timedelta(days=7))]
        return {"first_seen": dates[0], "times_seen": len(dates), "min": min(h.values()),
                "median": statistics.median(h.values()), "last": last,
                "delta_vs_yesterday": last - h[y] if y in h and dates[-1] == today else None,
                "trend_7d": last - week[0] if len(week) > 1 else None}

    def is_new_low(self, r: dict, today: str) -> str | None:
        """'itinerary' / 'route_pair' when today's price is the lowest ever seen (needs prior data)."""
        h = self.by_id.get(r["itinerary_id"], {})
        prev = [v for d, v in h.items() if d < today]
        if prev and r["price"]["price_total_czk"] < min(prev):
            return "itinerary"
        rm = self.route_min.get(route_key(r), {})
        prev = [v for d, v in rm.items() if d < today]
        if prev and r["totals"]["total_trip_cost_czk"] < min(prev):
            return "route_pair"
        return None
