"""Raw adapter offer -> normalized itinerary record (see schemas/itinerary.schema.json)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from zoneinfo import ZoneInfo

import airportsdata

from .config import Config
from .leave import arrive_home, calendar_days_away, leave_days, leave_home

HOME_TZ = ZoneInfo("Europe/Prague")
CA_TZ = ZoneInfo("America/Los_Angeles")
SCHENGEN = {"AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IS", "IT",
            "LI", "LT", "LU", "LV", "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK"}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# Planning assumptions when the inbound leg is not fetched yet (Stage A).
EST_CA_DEPARTURE = dt.time(15, 0)
EST_HOME_ARRIVAL = dt.time(20, 0)


@lru_cache(maxsize=1)
def _airports() -> dict:
    return airportsdata.load("IATA")


def airport_tz(code: str) -> ZoneInfo:
    a = _airports().get(code)
    return ZoneInfo(a["tz"]) if a else ZoneInfo("UTC")


def airport_country(code: str) -> str | None:
    a = _airports().get(code)
    return a["country"] if a else None


def _local(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def _utc(local: dt.datetime, tz: ZoneInfo) -> dt.datetime:
    return local.replace(tzinfo=tz).astimezone(dt.UTC)


def _iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M") + ("Z" if d.tzinfo else "")


def _minutes(a: dt.datetime, b: dt.datetime) -> int:
    return round((b - a).total_seconds() / 60)


@dataclass
class Ctx:
    cfg: Config
    holidays: frozenset
    fx: object
    run_id: str
    bag_filter_effective: bool | None  # from the canary bag check; None = unknown


def leg_record(segments: list[dict]) -> dict:
    segs = []
    for s in segments:
        dep, arr = _local(s["dep_local"]), _local(s["arr_local"])
        dep_u, arr_u = _utc(dep, airport_tz(s["from"])), _utc(arr, airport_tz(s["to"]))
        segs.append({**s, "dep_utc": _iso(dep_u), "arr_utc": _iso(arr_u),
                     "duration_min": s.get("duration_min") or _minutes(dep_u, arr_u)})
    lays = []
    for a, b in zip(segs, segs[1:]):
        lays.append({
            "airport": a["to"], "airport_dep": b["from"],
            "minutes": _minutes(dt.datetime.fromisoformat(a["arr_utc"]), dt.datetime.fromisoformat(b["dep_utc"])),
            "overnight": a["arr_local"][:10] != b["dep_local"][:10],
            "airport_change": a["to"] != b["from"],
            "intra_schengen": all(airport_country(x) in SCHENGEN for x in (a["from"], a["to"], b["to"])),
        })
    first, last = dt.datetime.fromisoformat(segs[0]["dep_utc"]), dt.datetime.fromisoformat(segs[-1]["arr_utc"])
    return {"segments": segs, "layovers": lays, "stops": len(segs) - 1,
            "air_duration_min": _minutes(first, last)}


def segment_key(segments: list[dict]) -> list:
    return [f"{s['marketing_carrier']}{s['flight_number']}@{s['dep_local'][:10]}" for s in segments]


def itinerary_id(out_segs: list[dict], in_segs: list[dict] | None, task) -> str:
    inbound = segment_key(in_segs) if in_segs else ["?", task.ret_origin, task.ret_dest, task.ret_date]
    return hashlib.sha1(json.dumps([segment_key(out_segs), inbound]).encode()).hexdigest()[:16]


def _in_peak(t: dt.datetime, window: str | None) -> bool:
    if not window:
        return False
    hours, days = window.split()
    a, b = (dt.time.fromisoformat(x) for x in hours.split("-"))
    d0, d1 = (WEEKDAYS.index(x) for x in days.split("-"))
    return d0 <= t.weekday() <= d1 and a <= t.time() <= b


def final_legs(cfg: Config, arr_airport: str, arr_local: dt.datetime,
               dep_airport: str, dep_local: dt.datetime) -> tuple[dict, dict, dt.datetime, dt.datetime]:
    """(to_san_diego, from_san_diego, sd_arrival_local, sd_departure_local); times in CA local."""
    da = cfg.destinations[arr_airport]
    mins = da.minutes_peak if da.minutes_peak and _in_peak(arr_local, da.peak_window_local) else da.minutes
    to_sd = {"mode": da.mode, "minutes": mins, "cost_czk": da.cost_czk, "estimate": da.estimate,
             "peak": mins != da.minutes}
    dd = cfg.destinations[dep_airport]
    lead = dt.timedelta(minutes=cfg.ca_departure_lead_min)
    sd_dep = dep_local - lead - dt.timedelta(minutes=dd.minutes)
    mins_b = dd.minutes
    if dd.minutes_peak and _in_peak(sd_dep, dd.peak_window_local):
        mins_b = dd.minutes_peak
        sd_dep = dep_local - lead - dt.timedelta(minutes=mins_b)
    from_sd = {"mode": dd.mode, "minutes": mins_b, "cost_czk": dd.cost_czk, "estimate": dd.estimate,
               "peak": mins_b != dd.minutes}
    return to_sd, from_sd, arr_local + dt.timedelta(minutes=mins), sd_dep


def bag_info(ctx: Ctx, source: str, opts: dict, offer: dict) -> tuple[str, object, float]:
    """(bag_certainty, price_includes_checked_bag, bag_fee_estimate_czk)."""
    if offer.get("bag_fee") is not None:
        return "fee_listed_by_source", False, ctx.fx.to_czk(offer["bag_fee"], offer["currency"])
    # SearchApi accepts checked_bags=1 but did not change any price (tested 2026-10-14, gl=cz and us),
    # so only a Google query whose bag filter the canary proved effective counts as bag-inclusive.
    if source.startswith("fast_flights") and opts.get("checked_bags") and ctx.bag_filter_effective:
        return "filter_applied_in_query", True, 0.0
    if source == "serpapi" or opts.get("exclude_basic_economy"):
        return "standard_fare_assumed_included", True, 0.0
    return "unknown", "unknown", ctx.cfg.bag_fee_estimate_czk_roundtrip


def normalize(ctx: Ctx, task, res, offer: dict, fetched_at: str) -> dict:
    """One record per itinerary. For return_options tasks `offer` is an inbound
    option and the outbound comes from task.opts['outbound']."""
    cfg, b = ctx.cfg, ctx.cfg.buffers
    if task.kind == "return_options":
        out_segs, in_segs = task.opts["outbound"], offer["legs"][0]
    else:
        out_segs, in_segs = offer["legs"][0], (offer["legs"][1] if len(offer["legs"]) > 1 else None)
    out, inb = leg_record(out_segs), (leg_record(in_segs) if in_segs else None)

    # price
    cur = offer["currency"]
    certainty, includes, bag_est = bag_info(ctx, res.source, task.opts, offer)
    price_czk = ctx.fx.to_czk(offer["price"], cur)
    flight_czk = price_czk + bag_est

    # ground legs and calendar (European airports share Brno's local time)
    o_cfg, r_cfg = cfg.origin(task.origin), cfg.origin(task.ret_dest)
    first = out["segments"][0]
    leave_local, hotel_out = leave_home(_local(first["dep_local"]), first["duration_min"], o_cfg, b)
    if inb:
        landing = _local(inb["segments"][-1]["arr_local"])
        home_local, hotel_back = arrive_home(landing, r_cfg, b)
        ca_dep_airport, ca_dep_local = inb["segments"][0]["from"], _local(inb["segments"][0]["dep_local"])
    else:
        home_local, hotel_back = dt.datetime.combine(
            dt.date.fromisoformat(task.ret_date) + dt.timedelta(days=1), EST_HOME_ARRIVAL), 0
        ca_dep_airport = task.ret_origin
        ca_dep_local = dt.datetime.combine(dt.date.fromisoformat(task.ret_date), EST_CA_DEPARTURE)
    ca_arr_airport, ca_arr_local = out["segments"][-1]["to"], _local(out["segments"][-1]["arr_local"])
    to_sd, from_sd, sd_arr, sd_dep = final_legs(cfg, ca_arr_airport, ca_arr_local, ca_dep_airport, ca_dep_local)
    hotel_nights = hotel_out + hotel_back
    hotel_cost = hotel_nights * cfg.hotel_cost_czk_per_night

    D, H = leave_local.date(), home_local.date()
    ld, cd = leave_days(D, H, ctx.holidays), calendar_days_away(D, H)
    d2d_out = _minutes(leave_local.replace(tzinfo=HOME_TZ), sd_arr.replace(tzinfo=CA_TZ))
    d2d_back = _minutes(sd_dep.replace(tzinfo=CA_TZ), home_local.replace(tzinfo=HOME_TZ))
    stay_h = (sd_dep - sd_arr).total_seconds() / 3600

    # flags
    us_entry, us_conn = None, None
    for i, s in enumerate(out["segments"]):
        if airport_country(s["to"]) == "US":
            us_entry = s["to"]
            if i < len(out["layovers"]):
                us_conn = out["layovers"][i]["minutes"]
            break
    all_lays = out["layovers"] + (inb["layovers"] if inb else [])
    all_segs = out["segments"] + (inb["segments"] if inb else [])
    tight = any(l["minutes"] < (60 if l["intra_schengen"] else 90) for l in all_lays)
    red_eye = any(_local(s["dep_local"]).hour >= 21 or _local(s["dep_local"]).hour < 5
                  for s in all_segs if s["duration_min"] >= 180)
    late_pickup = (ca_arr_airport == "LAX" and ca_arr_local.time() > dt.time(22, 0)) or \
                  (ca_dep_airport == "LAX" and sd_dep.time() < dt.time(5, 0))

    w = cfg.window
    violations = []
    if D < w.earliest_leave_home:
        violations.append("leaves_home_before_window")
    if H > w.latest_home_arrival:
        violations.append("home_after_window")
    if cd < w.min_calendar_days_away:
        violations.append("too_few_calendar_days")
    if ld > cfg.leave.max_leave_hard:
        violations.append("leave_days_over_hard_limit")
    for leg in filter(None, (out, inb)):
        if leg["stops"] > cfg.flight_limits.max_stops:
            violations.append("too_many_stops")
        if leg["air_duration_min"] > cfg.flight_limits.max_air_duration_min_per_direction:
            violations.append("air_duration_over_limit")

    total = flight_czk + o_cfg.ground_out.cost_czk + r_cfg.ground_back.cost_czk + hotel_cost \
        + to_sd["cost_czk"] + from_sd["cost_czk"]
    eur = ctx.fx.rates.get("EUR")
    return {
        "itinerary_id": itinerary_id(out_segs, in_segs, task),
        "trip_type": "open_jaw" if (task.ret_dest != task.origin or task.ret_origin != task.dest) else "round_trip",
        "origin": task.origin, "destination": task.dest,
        "return_origin": task.ret_origin, "return_destination": task.ret_dest,
        "pair_key": task.pair_key, "inbound_known": inb is not None,
        "price": {
            "price_total_original": offer["price"], "currency_original": cur,
            "price_total_czk": round(price_czk), "price_total_eur": round(price_czk / eur, 2) if eur else None,
            "flight_price_incl_bag_czk": round(flight_czk),
            "fx_rate_eur_czk": eur, "fx_date": ctx.fx.date,
            "price_includes_checked_bag": includes, "bag_fee_estimate_czk": round(bag_est),
            "bag_certainty": certainty, "basic_economy": offer.get("basic_economy") if offer.get("basic_economy") is not None else "unknown",
            "fare_brand": offer.get("fare_brand"), "price_basis": task.opts.get("price_basis", "as_listed"),
            "separate_tickets": offer["separate_tickets"] if offer.get("separate_tickets") is not None else (
                False if task.opts.get("hide_separate_and_self_transfer") else "unknown"),
            "self_transfer": any(l["airport_change"] for l in all_lays) or bool(offer.get("separate_tickets")),
        },
        "legs": {"outbound": out, "inbound": inb},
        "flags": {
            "us_entry_airport": us_entry, "us_entry_connection_min": us_conn,
            "us_entry_connection_risky": us_conn is not None and us_conn < b.min_us_entry_connection_min,
            "tight_connection": tight, "overnight_layover": any(l["overnight"] for l in all_lays),
            "airport_change": any(l["airport_change"] for l in all_lays), "red_eye": red_eye,
            "late_night_pickup": late_pickup, "constraint_violations": violations,
        },
        "ground": {
            "ground_out": {"mode": o_cfg.ground_out.mode, "minutes": o_cfg.ground_out.minutes,
                           "cost_czk": o_cfg.ground_out.cost_czk, "leave_home_local": _iso(leave_local),
                           "hotel_night_before": bool(hotel_out), "estimate": True},
            "ground_back": {"mode": r_cfg.ground_back.mode, "minutes": r_cfg.ground_back.minutes,
                            "cost_czk": r_cfg.ground_back.cost_czk, "arrive_home_local": _iso(home_local),
                            "hotel_night_after": bool(hotel_back), "estimate": True},
            "hotel_nights": hotel_nights, "hotel_cost_czk": hotel_cost,
        },
        "final_leg_to_san_diego": to_sd, "final_leg_from_san_diego": from_sd,
        "totals": {"door_to_door_out_min": d2d_out, "door_to_door_back_min": d2d_back,
                   "total_trip_cost_czk": round(total), "estimated_inbound": inb is None},
        "calendar": {"leave_home_date": str(D), "home_arrival_date": str(H), "leave_days_used": ld,
                     "calendar_days_away": cd, "nights_in_san_diego_area": (sd_dep.date() - sd_arr.date()).days,
                     "hours_in_san_diego_area": round(stay_h, 1),
                     "san_diego_arrival_local": _iso(sd_arr), "san_diego_departure_local": _iso(sd_dep)},
        "scoring": {},
        "provenance": {
            "source": res.source, "source_version": res.source_version, "query_params": res.query_params,
            "fetched_at_utc": fetched_at, "run_id": ctx.run_id, "stage": task.stage, "task_group": task.group,
            "query_out_date": task.out_date, "query_ret_date": task.ret_date,
            "booking_hint": res.booking_hint, "booking_token": offer.get("booking_token"),
            "results_complete": res.complete, "stale_days": 0,
        },
    }
