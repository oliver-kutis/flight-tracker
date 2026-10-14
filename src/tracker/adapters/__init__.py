"""Source adapters behind one interface.

Every adapter returns `SearchResult` with offers in a common raw shape:
  {price, currency, legs: [[segment, ...], ...], layovers: [[{airport, airport_dep, minutes}], ...],
   separate_tickets, basic_economy, fare_brand, bag_fee, price_includes_checked_bag,
   bag_certainty, booking_token, extensions}
segment = {marketing_carrier, operating_carrier, carrier_name, flight_number,
           from, to, dep_local "YYYY-MM-DDTHH:MM", arr_local, duration_min, aircraft}
Google returns one leg per page (outbound with the round-trip total, or the
return options for a selected outbound), so `legs` usually holds one leg.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Response status values; anything but "ok" counts as a failed attempt for that source.
OK, EMPTY, CONSENT, CAPTCHA, ERROR, UNAVAILABLE, QUOTA = (
    "ok", "empty", "consent", "captcha", "error", "unavailable", "quota")


@dataclass
class SearchResult:
    source: str
    status: str
    offers: list[dict] = field(default_factory=list)
    raw: Any = None
    source_version: str = ""
    query_params: dict = field(default_factory=dict)
    booking_hint: str | None = None
    complete: bool | None = None  # False = source says results were truncated
    error: str | None = None
    http_calls: int = 1
    retryable: bool = True  # False for errors a retry cannot fix (bad params, auth)


class Adapter:
    name = "base"
    free = True  # free sources share max_requests_per_run; paid ones have quotas

    def available(self) -> tuple[bool, str]:
        return True, ""

    def capabilities(self) -> dict:
        raise NotImplementedError

    def search_roundtrip(self, task, cfg) -> SearchResult:
        raise NotImplementedError

    def search_multicity(self, task, cfg) -> SearchResult:
        raise NotImplementedError

    def get_return_options(self, task, cfg) -> SearchResult:
        raise NotImplementedError

    def run(self, task, cfg) -> SearchResult:
        fn = {"roundtrip": self.search_roundtrip, "multicity": self.search_multicity,
              "return_options": self.get_return_options}[task.kind]
        return fn(task, cfg)


def build_adapters(cfg, quota) -> dict[str, Adapter]:
    from .gflights import FastFlights, FastFlightsFork
    from .paid import SearchApi, SerpApi
    all_ = {"fast_flights": FastFlights(), "fast_flights_fork": FastFlightsFork(cfg),
            "searchapi": SearchApi(cfg, quota), "serpapi": SerpApi(cfg, quota)}
    return {n: all_[n] for n in cfg.sources.chain}
