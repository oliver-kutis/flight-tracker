import base64
import datetime as dt
import json

from conftest import load_json, result, rt_task
from fast_flights.pb.flights_pb2 import Info

from tracker import storage
from tracker.adapters.gflights import build_query, parse_payload
from tracker.adapters.paid import parse_json
from tracker.history import History, write_history
from tracker.normalize import final_legs, normalize
from tracker.quota import Quota
from tracker.score import pareto_ids, score_all


# ------------------------------------------------------------- adapters / query

def test_tfs_matches_query_verified_in_google_ui(cfg):
    # Legacy URL Google rendered as "Vienna to Los Angeles, 2 stops or fewer, under 20 hr".
    legacy = "Gh8SCjIwMjYtMTEtMjYoAmCwCWoFEgNWSUVyBRIDTEFYGh8SCjIwMjYtMTItMTIoAmCwCWoFEgNMQVhyBRIDVklFQgEBSAGYAQE="
    t = rt_task("VIE", "LAX", "2026-11-26", "2026-12-12", checked_bags=0, hide_separate_and_self_transfer=False)
    cfg.traveller.carry_on = 0
    _, tfs = build_query(cfg, t)
    assert Info.FromString(base64.b64decode(tfs)) == Info.FromString(base64.urlsafe_b64decode(legacy))


def test_tfs_bag_flags_and_selected_segments(cfg):
    sel = [{"from": "VIE", "to": "LHR", "dep_local": "2026-11-26T06:30", "marketing_carrier": "BA", "flight_number": "703"}]
    tq, tfs = build_query(cfg, rt_task("VIE", "LAX", selected=sel))
    info = Info.FromString(base64.b64decode(tfs))
    assert info.baggage.checked_bags == 1 and info.baggage.carry_on_bags == 1
    assert info.hide_separate_and_self_transfer and info.trip == 1
    raw = base64.b64decode(tfs)
    assert b"\x22" in raw and b"BA" in raw and b"703" in raw  # field 4 survives inside FlightData
    assert tq.params()["curr"] == "CZK"


def test_google_fixture_parses_both_buckets(google_offers):
    assert len(google_offers) == 11  # 4 "top" + 7 "other"; upstream fast-flights reads only the 7
    best = min(google_offers, key=lambda o: o["price"])
    seg = best["legs"][0][0]
    assert (seg["from"], best["legs"][0][-1]["to"], best["price"]) == ("PRG", "LAX", 501)
    assert (seg["marketing_carrier"], seg["flight_number"], seg["dep_local"]) == ("BA", "865", "2026-11-26T06:35")


def test_normalize_google(ctx, google_offers):
    o = next(o for o in google_offers if o["price"] == 501 and o["legs"][0][0]["from"] == "PRG")
    r = normalize(ctx, rt_task(), result(offers=[o]), o, "2026-10-14T07:00:00Z")
    out = r["legs"]["outbound"]
    assert out["stops"] == 1 and out["layovers"][0]["airport"] == "LHR"
    assert out["segments"][0]["dep_utc"] == "2026-11-26T05:35Z"     # PRG CET -> UTC
    assert out["air_duration_min"] == 950
    assert r["price"]["price_total_czk"] == 501 * 25 and r["price"]["bag_certainty"] == "filter_applied_in_query"
    assert r["price"]["separate_tickets"] is False
    assert r["flags"]["us_entry_airport"] == "LAX" and r["flags"]["us_entry_connection_min"] is None
    assert r["inbound_known"] is False and r["totals"]["estimated_inbound"]
    # PRG 06:35 needs airport by 02:05 -> night before
    assert r["ground"]["ground_out"]["hotel_night_before"] and r["calendar"]["leave_home_date"] == "2026-11-25"
    # LAX arrival Thu 13:25 -> off-peak drive 150 min
    assert r["final_leg_to_san_diego"]["minutes"] == 150
    assert r["totals"]["total_trip_cost_czk"] == 501 * 25 + 500 + 500 + 2000


def test_bag_unknown_when_google_ignores_filter(ctx, google_offers):
    ctx.bag_filter_effective = False
    o = google_offers[0]
    r = normalize(ctx, rt_task(), result(offers=[o]), o, "t")
    assert r["price"]["bag_certainty"] == "unknown" and r["price"]["bag_fee_estimate_czk"] == 3500
    r = normalize(ctx, rt_task(exclude_basic_economy=True), result(offers=[o]), o, "t")
    assert r["price"]["bag_certainty"] == "standard_fare_assumed_included"


def test_normalize_return_options(ctx, google_offers):
    out = next(o for o in google_offers if o["legs"][0][0]["from"] == "PRG")["legs"][0]
    inbound = [{"marketing_carrier": "BA", "operating_carrier": "BA", "carrier_name": "British Airways",
                "flight_number": "282", "from": "LAX", "to": "LHR", "dep_local": "2026-12-12T20:40",
                "arr_local": "2026-12-13T14:50", "duration_min": 610, "aircraft": None},
               {"marketing_carrier": "BA", "operating_carrier": "BA", "carrier_name": "British Airways",
                "flight_number": "860", "from": "LHR", "to": "PRG", "dep_local": "2026-12-13T20:45",
                "arr_local": "2026-12-13T22:55", "duration_min": 130, "aircraft": None}]
    t = rt_task(outbound=out)
    t.kind = "return_options"
    offer = {"price": 520, "currency": "EUR", "legs": [inbound], "separate_tickets": None}
    r = normalize(ctx, t, result(offers=[offer]), offer, "t")
    assert r["inbound_known"] and r["legs"]["inbound"]["air_duration_min"] == 1035
    # 22:55 + 45 min misses the last 20:00 connection from PRG -> hotel, home next day 06:00 + 200 min
    assert r["calendar"]["home_arrival_date"] == "2026-12-14" and r["ground"]["ground_back"]["hotel_night_after"]
    assert r["ground"]["ground_back"]["arrive_home_local"] == "2026-12-14T09:20"


def test_serpapi_fixture(ctx):
    offers = parse_json(load_json("serpapi_roundtrip.json"), "CZK")
    assert len(offers) == 2
    lh = offers[0]
    assert lh["legs"][0][0]["flight_number"] == "466" and lh["departure_token"] == "SANITIZED_DEP_TOKEN"
    t = rt_task("MUC", "SAN", "2026-12-01", "2026-12-16", pair="2026-12-01_2026-12-17")
    r = normalize(ctx, t, result("serpapi", offers), lh, "t")
    assert r["price"]["bag_certainty"] == "standard_fare_assumed_included"
    assert r["final_leg_to_san_diego"]["minutes"] == 0
    assert r["legs"]["outbound"]["air_duration_min"] == 725
    fra = normalize(ctx, t, result("serpapi", offers), offers[1], "t")
    assert fra["flags"]["us_entry_airport"] == "SAN"


def test_searchapi_fixture(ctx):
    offers = parse_json(load_json("searchapi_roundtrip.json"), "CZK")
    t = rt_task("VIE", "LAX", "2026-11-26", "2026-12-12", pair="2026-11-26_2026-12-13")
    r = normalize(ctx, t, result("searchapi", offers), offers[0], "t")
    assert r["price"]["bag_certainty"] == "unknown"  # SearchApi ignores checked_bags in practice
    assert r["legs"]["outbound"]["layovers"][0]["minutes"] == 100
    assert not r["flags"]["tight_connection"]  # 100 min at LHR (non-Schengen threshold 90)


# ------------------------------------------------------------- final legs

def test_lax_peak_and_late_pickup(cfg):
    tue = dt.datetime(2026, 12, 1, 15, 0)
    to_sd, _, sd_arr, _ = final_legs(cfg, "LAX", tue, "SAN", dt.datetime(2026, 12, 15, 12, 0))
    assert to_sd["minutes"] == 210 and to_sd["peak"]
    to_sd, _, _, _ = final_legs(cfg, "LAX", dt.datetime(2026, 12, 5, 15, 0), "SAN", tue)  # Saturday
    assert to_sd["minutes"] == 150
    # Tue 22:00 LAX departure: leave SD 22:00-180-150 = 16:30 -> peak -> 210 min -> 15:30
    _, from_sd, _, sd_dep = final_legs(cfg, "SAN", tue, "LAX", dt.datetime(2026, 12, 15, 22, 0))
    assert from_sd["minutes"] == 210 and sd_dep == dt.datetime(2026, 12, 15, 15, 30)


def test_late_night_pickup_flag(ctx, google_offers):
    o = json.loads(json.dumps(next(o for o in google_offers if o["legs"][0][-1]["to"] == "LAX")))
    o["legs"][0][-1]["arr_local"] = o["legs"][0][-1]["arr_local"][:11] + "22:30"
    r = normalize(ctx, rt_task(dest="LAX", origin=o["legs"][0][0]["from"]), result(offers=[o]), o, "t")
    assert r["flags"]["late_night_pickup"]


# ------------------------------------------------------------- scoring

def _rec(i, cost, stay, time, **kw):
    r = {"itinerary_id": f"{i:016x}", "totals": {"total_trip_cost_czk": cost, "door_to_door_out_min": time,
                                                 "door_to_door_back_min": 0},
         "calendar": {"hours_in_san_diego_area": stay, "leave_days_used": kw.get("leave", 12)},
         "price": {"separate_tickets": kw.get("sep", False), "bag_certainty": kw.get("bag", "filter_applied_in_query")},
         "flags": {"us_entry_connection_risky": False, "tight_connection": False, "airport_change": False,
                   "constraint_violations": kw.get("viol", [])},
         "ground": {"hotel_nights": kw.get("hotel", 0)},
         "legs": {"outbound": {"stops": kw.get("stops", 1)}, "inbound": None}}
    return r


def test_scoring_and_penalties(cfg):
    recs = [_rec(1, 20000, 300, 1000), _rec(2, 30000, 400, 1200), _rec(3, 20000, 300, 1000, leave=14, hotel=2, stops=2),
            _rec(4, 10000, 100, 900, viol=["home_after_window"])]
    score_all(cfg, recs)
    a, b, c, d = (r["scoring"] for r in recs)
    assert a["price_score"] == 1.0 and b["stay_score"] == 1.0 and a["time_score"] == 1.0
    assert c["penalties"] == {"leave_14": 0.10, "hotel_needed": 0.06, "extra_stop": 0.03}
    assert round(a["final_score"] - c["final_score"], 4) == 0.19
    assert d["final_score"] is None and not d["eligible"]


def test_pareto():
    recs = [_rec(1, 100, 10, 10), _rec(2, 200, 20, 10), _rec(3, 200, 10, 10), _rec(4, 100, 10, 5)]
    assert pareto_ids(recs) == {f"{2:016x}", f"{4:016x}"}


# ------------------------------------------------------------- quota

def test_quota_caps(tmp_path):
    now = [dt.datetime(2026, 10, 14, 7, tzinfo=dt.UTC)]
    q = Quota(tmp_path / "q.json", {"serpapi": {"daily_cap": 2, "monthly_cap": 3, "hourly_cap": 50}}, now=lambda: now[0])
    q.consume("serpapi")
    q.consume("serpapi")
    assert not q.allowed("serpapi")
    now[0] += dt.timedelta(days=1)
    assert q.allowed("serpapi")
    q.consume("serpapi")
    assert not q.allowed("serpapi")  # monthly cap
    q2 = Quota(tmp_path / "q.json", q.caps, now=lambda: now[0])  # persisted
    assert q2.used("serpapi")["monthly_cap"] == 3


# ------------------------------------------------------------- outputs + schemas

def test_summary_and_schemas(cfg, ctx, google_offers, tmp_path):
    recs = []
    for o in google_offers:
        t = rt_task(o["legs"][0][0]["from"], o["legs"][0][-1]["to"])
        recs.append(normalize(ctx, t, result(offers=[o]), o, "2026-10-14T07:00:00Z"))
    score_all(cfg, recs)
    for r in recs:
        storage.validate(r, "itinerary.schema.json")
    write_history(tmp_path, "2026-10-13", recs)
    for r in recs:
        r["price"]["price_total_czk"] -= 100
        r["totals"]["total_trip_cost_czk"] -= 100
    write_history(tmp_path, "2026-10-14", recs)
    hist = History(tmp_path)
    meta = {"run_id": "x"}
    s = storage.build_summary(cfg, meta, recs, hist, "2026-10-14")
    storage.validate(s, "summary.schema.json")
    assert s["top_overall"] and s["top_overall"][0]["price_history"]["times_seen"] == 2
    assert s["top_overall"][0]["price_history"]["delta_vs_yesterday"] == -100
    assert s["new_lows_today"]
    assert len(storage.dumps(s)) < cfg.storage.summary_max_bytes
    assert parse_payload([None] * 8) == []


def test_cnb_parse():
    from tracker.fx import parse_cnb
    text = "13 Oct 2026 #197\nCountry|Currency|Amount|Code|Rate\nEMU|euro|1|EUR|24.320\nJapan|yen|100|JPY|15.500\n"
    date, rates = parse_cnb(text)
    assert date == "2026-10-13" and rates["EUR"] == 24.32 and rates["JPY"] == 0.155


def test_fork_worker_lenient_json():
    import importlib.util
    from pathlib import Path
    import tracker.adapters.gflights as g
    spec = importlib.util.spec_from_file_location("fw", Path(g.__file__).with_name("fork_worker.py"))
    fw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fw)
    assert fw.LenientJson.loads('[["wrb.fr",null,"[1]"]]123\n') == [["wrb.fr", None, "[1]"]]


def test_empty_then_failure_is_reported_as_failure(cfg):
    from tracker.adapters import SearchResult
    from tracker.main import Runner

    class Fake:
        def __init__(self, name, status, complete=None):
            self.name, self.status, self.complete, self.free = name, status, complete, True

        def available(self):
            return True, ""

        def run(self, task, cfg):
            return SearchResult(self.name, self.status, complete=self.complete, retryable=False, error="x")

    r = Runner(cfg, __import__("datetime").date(2026, 10, 14), 10, True, ["a", "b"])
    r.adapters = {"a": Fake("a", "empty"), "b": Fake("b", "error", complete=False)}
    res = r.execute(rt_task())
    assert res.source == "b" and res.status == "error"
