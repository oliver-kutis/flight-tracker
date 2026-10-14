"""Optional paid fallbacks: SearchApi.io and SerpApi (engine=google_flights).

Enabled only when their secret exists (SEARCHAPI_KEY / SERPAPI_KEY). Both
return the same JSON shape (best_flights / other_flights), parsed by `parse_json`.
"""
from __future__ import annotations

import json
import os

import requests

from . import EMPTY, ERROR, OK, QUOTA, UNAVAILABLE, Adapter, SearchResult


def _seg_time(a: dict) -> str:
    t = a.get("time", "")
    if len(t) > 5:  # SerpApi: "2026-11-26 06:35"
        return t.replace(" ", "T")[:16]
    return f"{a.get('date', '')}T{t}"  # SearchApi: date + "06:35"


def parse_json(resp: dict, currency: str) -> list[dict]:
    out = []
    for bucket in ("best_flights", "other_flights"):
        for it in resp.get(bucket) or []:
            segs = it.get("flights") or []
            if not segs or not isinstance(it.get("price"), (int, float)):
                continue
            legs = []
            for s in segs:
                carrier, _, num = (s.get("flight_number") or "").partition(" ")
                legs.append({
                    "marketing_carrier": carrier or None, "operating_carrier": carrier or None,
                    "carrier_name": s.get("airline"), "flight_number": num or None, "codeshares": [],
                    "from": s["departure_airport"].get("id"), "to": s["arrival_airport"].get("id"),
                    "dep_local": _seg_time(s["departure_airport"]), "arr_local": _seg_time(s["arrival_airport"]),
                    "duration_min": s.get("duration"), "aircraft": s.get("airplane"),
                })
            exts = [e for s in segs for e in s.get("extensions", [])] + it.get("extensions", [])
            out.append({
                "price": it["price"], "currency": currency, "legs": [legs],
                "layovers": [[{"airport": l.get("id"), "airport_dep": l.get("id"), "minutes": l.get("duration"),
                               "overnight": l.get("overnight", False)} for l in it.get("layovers", [])]],
                "air_duration_min": [it.get("total_duration")],
                "separate_tickets": True if any("separate ticket" in e.lower() for e in exts) else None,
                "basic_economy": None, "fare_brand": None, "bag_fee": None,
                "booking_token": it.get("booking_token"), "departure_token": it.get("departure_token"),
                "extensions": exts, "top_flight": bucket == "best_flights",
            })
    return out


class _Paid(Adapter):
    free = False
    env = ""
    url = ""

    def __init__(self, cfg, quota):
        self.cfg, self.quota = cfg, quota
        self.key = os.environ.get(self.env)

    def available(self):
        if not self.key:
            return False, f"{self.env} not set"
        if not self.quota.allowed(self.name):
            return False, "quota exhausted"
        return True, ""

    def _call(self, params: dict, task) -> SearchResult:
        ok, why = self.available()
        if not ok:
            return SearchResult(self.name, QUOTA if "quota" in why else UNAVAILABLE, error=why, http_calls=0)
        self.quota.consume(self.name)
        shown = {k: v for k, v in params.items() if k != "api_key"}
        try:
            r = requests.get(self.url, params={**params, "api_key": self.key}, timeout=90)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            return SearchResult(self.name, ERROR, error=f"{type(e).__name__}"[:200], query_params=shown)
        if r.status_code != 200 or data.get("error"):
            return SearchResult(self.name, ERROR, raw=data, query_params=shown, retryable=r.status_code >= 500
                                or r.status_code == 429, error=str(data.get("error") or f"HTTP {r.status_code}")[:300])
        offers = parse_json(data, self.cfg.sources.currency)
        meta = data.get("search_metadata") or {}
        return SearchResult(self.name, OK if offers else EMPTY, offers, raw=data, source_version="api",
                            query_params=shown, booking_hint=meta.get("google_flights_url"))

    def _legs(self, task) -> list[dict]:
        return [{"departure_id": task.origin, "arrival_id": task.dest, "date": task.out_date},
                {"departure_id": task.ret_origin, "arrival_id": task.ret_dest, "date": task.ret_date}]


class SerpApi(_Paid):
    """`bags` = carry-on only; exclude_basic=true so the fare is a standard one
    (checked bag assumed included). type=1 lists outbounds; each
    departure_token call (return options) is another billed search."""
    name, env, url = "serpapi", "SERPAPI_KEY", "https://serpapi.com/search.json"

    def capabilities(self):
        return {"checked_bags": False, "carry_on_bags": True, "exclude_basic_economy": True,
                "hide_separate_and_self_transfer": False, "currency": True, "max_stops": True,
                "max_duration_minutes": True, "multi_city": True, "return_options": "departure_token",
                "date_grid": False, "available": self.available()[0]}

    def _base(self, task) -> dict:
        c = self.cfg
        # exclude_basic is only accepted with gl=us and travel_class=1 (economy).
        return {"engine": "google_flights", "currency": c.sources.currency, "hl": "en", "gl": "us",
                "travel_class": 1, "adults": c.traveller.adults, "stops": c.flight_limits.max_stops + 1,
                "max_duration": c.flight_limits.max_air_duration_min_per_direction,
                "bags": c.traveller.carry_on, "exclude_basic": "true"}

    def search_roundtrip(self, task, cfg):
        return self._call({**self._base(task), "type": 1, "departure_id": task.origin,
                           "arrival_id": task.dest, "outbound_date": task.out_date,
                           "return_date": task.ret_date}, task)

    def search_multicity(self, task, cfg):
        return self._call({**self._base(task), "type": 3, "multi_city_json": json.dumps(self._legs(task))}, task)

    def get_return_options(self, task, cfg):
        tok = task.opts.get("departure_token")
        if not tok:
            return SearchResult(self.name, UNAVAILABLE, error="no departure_token for this outbound", http_calls=0)
        return self._call({**self._base(task), "type": 1, "departure_id": task.origin, "arrival_id": task.dest,
                           "outbound_date": task.out_date, "return_date": task.ret_date,
                           "departure_token": tok}, task)


class SearchApi(_Paid):
    """checked_bags=1 is passed but SearchApi ignored it in tests (identical prices with 0/1 bags),
    so its prices are treated as bag status unknown."""
    name, env, url = "searchapi", "SEARCHAPI_KEY", "https://www.searchapi.io/api/v1/search"

    def capabilities(self):
        return {"checked_bags": "accepted but no price effect observed", "carry_on_bags": True, "exclude_basic_economy": False,
                "hide_separate_and_self_transfer": False, "currency": True, "max_stops": True,
                "max_duration_minutes": False, "multi_city": True, "return_options": "departure_token",
                "date_grid": False, "available": self.available()[0]}

    def _base(self) -> dict:
        c = self.cfg
        return {"engine": "google_flights", "currency": c.sources.currency, "hl": "en", "gl": "cz",
                "adults": c.traveller.adults, "stops": "two_stops_or_fewer",
                "checked_bags": c.traveller.checked_bags, "carry_on_bags": c.traveller.carry_on}

    def search_roundtrip(self, task, cfg):
        return self._call({**self._base(), "flight_type": "round_trip", "departure_id": task.origin,
                           "arrival_id": task.dest, "outbound_date": task.out_date,
                           "return_date": task.ret_date}, task)

    def search_multicity(self, task, cfg):
        return self._call({**self._base(), "flight_type": "multi_city",
                           "multi_city_json": json.dumps(self._legs(task))}, task)

    def get_return_options(self, task, cfg):
        tok = task.opts.get("departure_token")
        if not tok:
            return SearchResult(self.name, UNAVAILABLE, error="no departure_token for this outbound", http_calls=0)
        return self._call({**self._base(), "flight_type": "round_trip", "departure_id": task.origin,
                           "arrival_id": task.dest, "outbound_date": task.out_date,
                           "return_date": task.ret_date, "departure_token": tok}, task)
