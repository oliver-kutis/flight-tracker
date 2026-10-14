"""Google Flights via fast-flights (PyPI, primary) and the Mohamed3on fork (fallback).

Both packages are named `fast_flights` and register conflicting protobuf
descriptors, so the fork runs in a subprocess (`fork_worker.py`) with its
install dir first on PYTHONPATH.

The query (`tfs`) is built with fast-flights' own `create_query`, extended by
one field fast-flights does not model: FlightData field 4 = selected segments,
which makes Google list the return options for a chosen outbound (verified
against the Google Flights UI, Oct 2026). Result pages are parsed by
`parse_payload` (both result buckets, flight numbers, layovers); fast-flights'
own parser reads only the "other flights" bucket and drops flight numbers.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import os
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from . import CAPTCHA, CONSENT, EMPTY, ERROR, OK, UNAVAILABLE, Adapter, SearchResult
from ..config import ROOT

DS1 = re.compile(r"<script[^>]*class=\"ds:1\"[^>]*>(.*?)</script>", re.S)
FORK_REPO = "git+https://github.com/Mohamed3on/flights"
FORK_COMMIT = "d1620895da9d770efbb7196b67c95c47dc4a3ab1"


# ------------------------------------------------------------------ tfs query

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(tag: int, payload: bytes | str) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode()
    return bytes([tag]) + _varint(len(payload)) + payload


def selected_bytes(segments: list[dict]) -> bytes:
    """FlightData field 4 (repeated): from(1) date(2) to(3) airline(5) number(6)."""
    return b"".join(
        _field(0x22, _field(0x0A, s["from"]) + _field(0x12, s["dep_local"][:10]) + _field(0x1A, s["to"])
               + _field(0x2A, s["marketing_carrier"]) + _field(0x32, s["flight_number"]))
        for s in segments)


def build_query(cfg, task):
    """fast-flights Query for a task; returns (query, tfs string)."""
    from fast_flights import FlightQuery, Passengers, Query, create_query

    @dataclasses.dataclass
    class TfsQuery(Query):
        selected: bytes = b""  # extra bytes appended to the first FlightData
        gl: str = ""

        def to_bytes(self) -> bytes:
            info = self.pb()
            data = list(info.data)
            del info.data[:]
            fds = b"".join(_field(0x1A, fd.SerializeToString() + (self.selected if i == 0 else b""))
                           for i, fd in enumerate(data))
            return fds + info.SerializeToString()

        def params(self) -> dict[str, str]:
            p = super().params()
            if self.gl:
                p["gl"] = self.gl
            return p

        def url(self) -> str:
            return super().url() + (f"&gl={self.gl}" if self.gl else "")

    fl, o = cfg.flight_limits, task.opts

    def fq(date, a, b):
        return FlightQuery(date=date, from_airport=a, to_airport=b, max_stops=fl.max_stops,
                           max_duration_minutes=fl.max_air_duration_min_per_direction)

    trip = "multi-city" if task.kind == "multicity" or task.ret_dest != task.origin \
        or task.ret_origin != task.dest else "round-trip"
    q = create_query(
        flights=[fq(task.out_date, task.origin, task.dest), fq(task.ret_date, task.ret_origin, task.ret_dest)],
        seat=cfg.traveller.cabin, trip=trip, passengers=Passengers(adults=cfg.traveller.adults),
        language=cfg.sources.google_hl, currency=cfg.sources.currency, max_stops=fl.max_stops,
        carry_on_bags=cfg.traveller.carry_on, checked_bags=o.get("checked_bags", 0),
        hide_separate_and_self_transfer=o.get("hide_separate_and_self_transfer", True),
        exclude_basic_economy=o.get("exclude_basic_economy", False))
    tq = TfsQuery(**{f.name: getattr(q, f.name) for f in dataclasses.fields(Query)},
                  selected=selected_bytes(o.get("selected") or []), gl=cfg.sources.google_gl)
    return tq, tq.to_str()


# ------------------------------------------------------------------ parsing

def classify_html(html: str) -> tuple[str, list | None, str | None]:
    head = html[:20000].lower()
    if "consent.google" in head or "before you continue" in head:
        return CONSENT, None, "EU consent page"
    if "/sorry/" in head or "unusual traffic" in head or "captcha" in head:
        return CAPTCHA, None, "CAPTCHA / unusual-traffic page"
    m = DS1.search(html)
    if not m:
        return ERROR, None, "no ds:1 data in page (layout changed or blocked)"
    js = m.group(1).split("data:", 1)[1].rsplit(",", 1)[0]
    if js.rstrip().endswith("errorHasStatus: true"):
        return EMPTY, None, "Google returned no flights"
    return OK, json.loads(js), None


def _stamp(date, t) -> str:
    t = list(t or []) + [None, None]  # Google omits zero components: [8] = 08:00
    return "%04d-%02d-%02dT%02d:%02d" % (*date, t[0] or 0, t[1] or 0)


def _segment(s) -> dict:
    fn = s[22] if len(s) > 22 and s[22] else [None, None, None, None]
    return {
        "marketing_carrier": fn[0], "operating_carrier": fn[0],
        "carrier_name": fn[3] if len(fn) > 3 else None, "flight_number": fn[1],
        "codeshares": [f"{c[0]}{c[1]}" for c in (s[15] or [])] if len(s) > 15 else [],
        "from": s[3], "to": s[6],
        "dep_local": _stamp(s[20], s[8]), "arr_local": _stamp(s[21], s[10]),
        "duration_min": s[11], "aircraft": s[17] if len(s) > 17 else None,
    }


def parse_payload(p: list) -> list[dict]:
    """Offers from a ds:1 / GetShoppingResults payload: p[2] = top flights, p[3] = other flights."""
    out: dict[tuple, dict] = {}
    for bucket in (2, 3):
        rows = p[bucket][0] if len(p) > bucket and p[bucket] and p[bucket][0] else []
        for k in rows:
            f = k[0]
            fare = k[1][0] if k[1] else None
            price = fare[1] if fare and len(fare) > 1 else None
            if not f[2] or price is None:
                continue
            segs = [_segment(s) for s in f[2]]
            lays = [{"airport": l[1], "airport_dep": l[2], "minutes": l[0]} for l in (f[13] or [])]
            key = tuple((s["marketing_carrier"], s["flight_number"], s["dep_local"][:10]) for s in segs)
            offer = {
                "price": price, "legs": [segs], "layovers": [lays], "air_duration_min": [f[9]],
                "separate_tickets": None, "basic_economy": None, "fare_brand": None, "bag_fee": None,
                "booking_token": k[1][1] if len(k[1]) > 1 else None, "top_flight": bucket == 2,
            }
            if key not in out or price < out[key]["price"]:
                out[key] = offer
    return list(out.values())


def _result(name, version, status, payload, err, tq, tfs, cfg, complete, calls) -> SearchResult:
    offers = parse_payload(payload) if payload is not None else []
    if status == OK and not offers:
        status = EMPTY
    for o in offers:
        o["currency"] = cfg.sources.currency
    return SearchResult(name, status, offers, raw=payload, source_version=version,
                        query_params={"tfs": tfs, **{k: v for k, v in tq.params().items() if k != "tfs"}},
                        booking_hint=tq.url(), complete=complete, error=err, http_calls=calls)


# ------------------------------------------------------------------ adapters

def _ff_caps(create_query_sig, flightquery_fields, trip_values) -> dict:
    params = set(create_query_sig)
    return {
        "checked_bags": "checked_bags" in params, "carry_on_bags": "carry_on_bags" in params,
        "exclude_basic_economy": "exclude_basic_economy" in params,
        "hide_separate_and_self_transfer": "hide_separate_and_self_transfer" in params,
        "currency": "currency" in params, "max_stops": "max_stops" in params,
        "max_duration_minutes": "max_duration_minutes" in flightquery_fields,
        "min_layover_minutes": "min_layover_minutes" in flightquery_fields,
        "max_layover_minutes": "max_layover_minutes" in flightquery_fields,
        "multi_city": "multi-city" in trip_values,
        "return_options": "via tracker tfs extension (FlightData field 4); not native",
        "date_grid": False,
    }


class FastFlights(Adapter):
    name = "fast_flights"

    def available(self):
        try:
            import fast_flights  # noqa: F401
            return True, ""
        except ImportError as e:
            return False, str(e)

    def version(self) -> str:
        return metadata.version("fast-flights")

    def capabilities(self) -> dict:
        import fast_flights
        from fast_flights.querying import TRIP_LOOKUP
        caps = _ff_caps(inspect.signature(fast_flights.create_query).parameters,
                        {f.name for f in dataclasses.fields(fast_flights.FlightQuery)}, TRIP_LOOKUP)
        caps.update(version=self.version(), eu_consent_cookie=False, streams_full_results=False,
                    parser="tracker parse_payload (both buckets, flight numbers)")
        return caps

    def _fetch(self, task, cfg) -> SearchResult:
        from fast_flights import fetch_flights_html
        tq, tfs = build_query(cfg, task)
        try:
            html = fetch_flights_html(tq)
        except Exception as e:  # network errors
            return _result(self.name, self.version(), ERROR, None, f"{type(e).__name__}: {e}"[:300],
                           tq, tfs, cfg, None, 1)
        status, payload, err = classify_html(html)
        return _result(self.name, self.version(), status, payload, err, tq, tfs, cfg, None, 1)

    search_roundtrip = search_multicity = get_return_options = _fetch


class FastFlightsFork(Adapter):
    """Mohamed3on/flights: EU consent cookie + streamed full results (GetShoppingResults)."""
    name = "fast_flights_fork"

    def __init__(self, cfg):
        self.path = (ROOT / cfg.sources.fork_path).resolve()

    def available(self):
        if (self.path / "fast_flights").is_dir():
            return True, ""
        return False, (f"fork not installed at {self.path}; run: uv pip install --target "
                       f"{self.path} {FORK_REPO}@{FORK_COMMIT}")

    def _worker(self, payload: dict, timeout=240) -> dict:
        env = {**os.environ, "PYTHONPATH": str(self.path)}
        p = subprocess.run([sys.executable, str(Path(__file__).with_name("fork_worker.py"))],
                           input=json.dumps(payload), capture_output=True, text=True,
                           env=env, timeout=timeout)
        if p.returncode:
            return {"error": f"fork worker exit {p.returncode}: {p.stderr.strip()[-300:]}"}
        return json.loads(p.stdout)

    def version(self) -> str:
        return f"fork@{FORK_COMMIT[:7]}"

    def capabilities(self) -> dict:
        ok, why = self.available()
        if not ok:
            return {"available": False, "reason": why}
        caps = self._worker({"capabilities": True}, timeout=60)
        if "error" in caps:
            return {"available": False, "reason": caps["error"]}
        out = _ff_caps(caps["create_query"], caps["flightquery"], caps["trips"])
        out.update(version=self.version(), eu_consent_cookie=True, streams_full_results=True,
                   parser="tracker parse_payload", available=True)
        return out

    def _fetch(self, task, cfg) -> SearchResult:
        tq, tfs = build_query(cfg, task)
        try:
            r = self._worker({"tfs": tfs, "hl": cfg.sources.google_hl, "curr": cfg.sources.currency,
                              "gl": cfg.sources.google_gl})
        except subprocess.TimeoutExpired:
            r = {"error": "fork worker timeout"}
        if "error" in r:
            status = CONSENT if "consent" in r["error"].lower() else ERROR
            res = _result(self.name, self.version(), status, None, r["error"], tq, tfs, cfg, None, 2)
            res.retryable = "timeout" in r["error"].lower()  # parse/refusal errors repeat on retry
            return res
        res = _result(self.name, self.version(), OK, r["payload"], None, tq, tfs, cfg,
                      r["complete"], r.get("http_calls", 2))
        if res.status == EMPTY and r["complete"] is False:
            # Page carried no results and Google refused the follow-up stream: not a real "no flights".
            res.status, res.error, res.retryable = ERROR, "truncated: page empty, follow-up results refused", False
        return res

    search_roundtrip = search_multicity = get_return_options = _fetch


def unavailable(name, why) -> SearchResult:
    return SearchResult(name, UNAVAILABLE, error=why, http_calls=0)
