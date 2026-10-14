#!/usr/bin/env python3
"""Flight watch: round trips Brno area -> California from Google Flights.

Each run picks the date pairs checked least recently (within the daily budget),
searches them, stores results in data/state.json and writes REPORT.md.

Usage:
  uv run flight_watch.py              # normal run (source from config.yaml)
  uv run flight_watch.py --dry-run    # show plan, no requests
  uv run flight_watch.py --probe      # 2 test searches against Google
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
from base64 import urlsafe_b64encode
from functools import lru_cache
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = DATA / "state.json"
LOG_FILE = DATA / "searches.jsonl"
REPORT_FILE = ROOT / "REPORT.md"
SEARCH_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"
STOPS_PARAM = {0: "1", 1: "2", 2: "3"}  # SerpApi: 1 nonstop, 2 <=1 stop, 3 <=2 stops
DAY = dt.timedelta(days=1)


def as_date(x):
    return x if isinstance(x, dt.date) else dt.date.fromisoformat(str(x))


# ---------------------------------------------------------------- date pairs

def leave_days(dep, ret, holidays, arrival_offset):
    """Working days used from departure day to the day you land back in Europe."""
    end, cur, n = ret + arrival_offset * DAY, dep, 0
    while cur <= end:
        if cur.weekday() < 5 and cur not in holidays:
            n += 1
        cur += DAY
    return n


def candidate_pairs(cfg, today):
    """For every departure day, the latest return that fits the leave budget."""
    w = cfg["window"]
    first = max(as_date(w["earliest_departure"]), today + DAY)
    last_ret = as_date(w["latest_return"])
    hol = {as_date(h) for h in cfg.get("holidays", [])}
    off = cfg.get("return_arrival_offset_days", 1)
    pairs, dep = [], first
    while dep < last_ret:
        best, ret = None, dep + cfg.get("min_stay_days", 7) * DAY
        while ret <= last_ret:
            used = leave_days(dep, ret, hol, off)
            if used > cfg["max_leave_days"]:
                break
            best = (ret, used)
            ret += DAY
        if best:
            ret, used = best
            pairs.append({
                "dep": dep.isoformat(), "ret": ret.isoformat(),
                "stay": (ret - dep).days,
                "away": (ret + off * DAY - dep).days + 1,
                "leave": used,
            })
        dep += DAY
    # Longest stays first; among equal stays, fewer leave days.
    pairs.sort(key=lambda p: (-p["stay"], p["leave"], p["dep"]))
    return pairs[: cfg["budget"]["max_pairs"]]


# ------------------------------------------------------------------- SerpApi

def base_params(cfg, key, origins, dep, ret):
    return {
        "engine": "google_flights",
        "departure_id": ",".join(origins),
        "arrival_id": ",".join(cfg["destinations"]),
        "outbound_date": dep, "return_date": ret,
        "type": "1", "currency": "EUR", "hl": "en", "gl": "cz",
        "adults": cfg.get("adults", 1),
        "stops": STOPS_PARAM[cfg["max_stops"]],
        "max_duration": cfg["max_duration_hours"] * 60,
        "api_key": key,
    }


def call(params):
    r = requests.get(SEARCH_URL, params=params, timeout=90)
    try:
        data = r.json()
    except ValueError:
        return {"error": f"HTTP {r.status_code}: non-JSON response"}
    if r.status_code != 200 and "error" not in data:
        data["error"] = f"HTTP {r.status_code}"
    return data


def searches_left(key):
    try:
        acc = requests.get(ACCOUNT_URL, params={"api_key": key}, timeout=30).json()
        return acc.get("total_searches_left", acc.get("plan_searches_left"))
    except Exception:
        return None


def parse_offers(resp, pair):
    offers = []
    for bucket in ("best_flights", "other_flights"):
        for it in resp.get(bucket) or []:
            segs = it.get("flights") or []
            if not segs or not isinstance(it.get("price"), (int, float)):
                continue
            exts = [e for s in segs for e in s.get("extensions", [])] + it.get("extensions", [])
            offers.append({
                "origin": segs[0]["departure_airport"].get("id"),
                "dest": segs[-1]["arrival_airport"].get("id"),
                "dep_time": segs[0]["departure_airport"].get("time"),
                "arr_time": segs[-1]["arrival_airport"].get("time"),
                "price": it["price"],
                "duration_min": it.get("total_duration"),
                "stops": len(segs) - 1,
                "via": [l.get("id") for l in it.get("layovers", [])],
                "airlines": sorted({s["airline"] for s in segs if s.get("airline")}),
                "bag_info": sorted({e for e in exts if "bag" in e.lower()}),
                "departure_token": it.get("departure_token"),
                **{k: pair[k] for k in ("dep", "ret", "stay", "away", "leave")},
            })
    return offers


def fetch_return(cfg, key, offer, origins):
    p = base_params(cfg, key, origins, offer["dep"], offer["ret"])
    p["departure_token"] = offer["departure_token"]
    resp = call(p)
    rets = parse_offers(resp, offer)
    if not rets:
        return None
    r = min(rets, key=lambda o: o["price"])
    return {k: r[k] for k in ("origin", "dest", "dep_time", "arr_time", "duration_min",
                               "stops", "via", "airlines", "price")}


# -------------------------------------------------------------- Google direct

GF_URL = "https://www.google.com/travel/flights/search"
# EU consent wall: without this cookie Google answers with consent.google.com.
CONSENT = {"SOCS": "CAESHAgBEhJnd3NfMjAyNTA1MDYtMF9SQzEaAmVuIAEaBgiA_LyaBg"}


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(tag_byte, payload):
    if isinstance(payload, str):
        payload = payload.encode()
    return bytes([tag_byte]) + _varint(len(payload)) + payload


def gf_tfs(cfg, origins, dests, dep, ret, selected=()):
    """Google Flights `tfs` protobuf for a round trip, several airports per side.

    Verified against the Google Flights UI (Oct 2026): it shows e.g. "Vienna and
    4 more to Los Angeles and 4 more", "2 stops or fewer", "Under 20 hr".
    FlightData: 2 date, 4 selected segment, 5 max stops, 12 max minutes,
    13 from airport (repeated), 14 to airport (repeated).
    Info: 3 legs, 8 passengers, 9 seat (1 economy), 19 trip (1 round trip).
    `selected` = outbound segments (from, date, to, airline, number); Google
    then lists return flights priced as the round-trip total.
    """
    filt = (b"\x28" + _varint(cfg["max_stops"])
            + b"\x60" + _varint(cfg["max_duration_hours"] * 60))

    def leg(date, fr, to, sel=()):
        b = _field(0x12, date) + filt
        b += b"".join(_field(0x22, _field(0x0A, a) + _field(0x12, d) + _field(0x1A, t)
                             + _field(0x2A, al) + _field(0x32, n)) for a, d, t, al, n in sel)
        b += b"".join(_field(0x6A, _field(0x12, a)) for a in fr)
        b += b"".join(_field(0x72, _field(0x12, a)) for a in to)
        return _field(0x1A, b)

    raw = (leg(dep, origins, dests, selected) + leg(ret, dests, origins)
           + _field(0x42, bytes([1] * cfg.get("adults", 1))) + b"\x48\x01\x98\x01\x01")
    return urlsafe_b64encode(raw).decode()


def gf_url(tfs):
    return f"{GF_URL}?tfs={tfs}&hl=en&curr=EUR"


@lru_cache(maxsize=1)
def _client():
    from primp import Client
    c = Client(impersonate="chrome_145", impersonate_os="macos", referer=True,
               cookie_store=True, timeout=60)
    c.set_cookies("https://www.google.com", CONSENT)
    return c


def _hm(t):
    t = (t or []) + [None, None]  # Google omits zero components: [8] = 08:00
    return t[0] or 0, t[1] or 0


def _stamp(date, t):
    return dt.datetime(*date, *_hm(t)).strftime("%Y-%m-%d %H:%M")


def gf_fetch(tfs):
    """Fetch one results page -> list of itinerary dicts (both result buckets)."""
    html = _client().get(GF_URL, params={"tfs": tfs, "hl": "en", "curr": "EUR"}).text
    m = re.search(r"<script[^>]*class=\"ds:1\"[^>]*>(.*?)</script>", html, re.S)
    if not m:
        raise RuntimeError("consent/captcha page" if "consent" in html[:5000].lower()
                           else "no ds:1 data in page (layout changed or blocked)")
    js = m.group(1).split("data:", 1)[1].rsplit(",", 1)[0]
    if js.rstrip().endswith("errorHasStatus: true"):
        return []
    p = json.loads(js)
    # p[2] = "Top flights", p[3] = "Other flights" (fast-flights only read p[3]).
    recs = [k for b in (2, 3) if p[b] and p[b][0] for k in p[b][0]]
    out = {}
    for k in recs:
        f, price = k[0], (k[1][0] or [None, None])[1]
        segs = f[2]
        if not segs or not price:
            continue
        it = {
            "origin": segs[0][3], "dest": segs[-1][6],
            "dep_time": _stamp(segs[0][20], segs[0][8]),
            "arr_time": _stamp(segs[-1][21], segs[-1][10]),
            "price": price, "duration_min": f[9], "stops": len(segs) - 1,
            "via": [s[6] for s in segs[:-1]], "airlines": list(dict.fromkeys(f[1])),
            "segments": [[s[3], "%04d-%02d-%02d" % tuple(s[20]), s[6], s[22][0], s[22][1]]
                         for s in segs],
        }
        key = tuple(map(tuple, it["segments"]))
        if key not in out or price < out[key]["price"]:
            out[key] = it
    return list(out.values())


def gf_search(cfg, origins, pair):
    tfs = gf_tfs(cfg, origins, cfg["destinations"], pair["dep"], pair["ret"])
    resp = {"search_metadata": {"google_flights_url": gf_url(tfs)}}
    try:
        offers = gf_fetch(tfs)
    except Exception as e:  # network, consent page, changed HTML...
        resp["error"] = f"{type(e).__name__}: {e}"[:300]
        return resp, []
    for o in offers:
        o.update({k: pair[k] for k in ("dep", "ret", "stay", "away", "leave")})
    return resp, offers


def gf_return(cfg, offer, origins):
    """Cheapest (then shortest) return flight for a chosen outbound itinerary."""
    tfs = gf_tfs(cfg, origins, cfg["destinations"], offer["dep"], offer["ret"],
                 selected=offer["segments"])
    rets = gf_fetch(tfs)
    if not rets:
        return None
    r = min(rets, key=lambda o: (o["price"], o["duration_min"] or 9999))
    return {k: r[k] for k in ("origin", "dest", "dep_time", "arr_time", "duration_min",
                               "stops", "via", "airlines", "price")}


def add_bag(o, cfg):
    """Google can no longer price a checked bag (its Bags filter is carry-on only),
    so add a configured estimate unless the airline includes one."""
    bag = cfg.get("bag", {})
    o.setdefault("price_google", o["price"])
    incl = set(o["airlines"]) <= set(bag.get("included_airlines", []))
    fee = 0 if incl else bag.get("fee_eur", 0)
    o["price"] = o["price_google"] + fee
    o["bag_info"] = (["batožina v cene (dopravca)"] if incl else
                     [f"{o['price_google']} € + {fee} € odhad za batožinu"])
    return o


# ------------------------------------------------------------------- scoring

def score(o, cfg, max_stay):
    s = cfg["scoring"]
    hours = (o.get("duration_min") or 0) / 60
    return (o["price"]
            + s["eur_per_lost_day"] * (max_stay - o["stay"])
            + s["eur_per_hour_over_12h"] * max(0.0, hours - 12))


# --------------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"units": {}}


def unit_key(pair, gi):
    return f"{pair['dep']}_{pair['ret']}_g{gi}"


def plan(cfg, state, pairs, n):
    units = []
    for p in pairs:
        for gi in range(len(cfg["origin_groups"])):
            last = state["units"].get(unit_key(p, gi), {}).get("last_checked", "")
            units.append((last, -p["stay"], gi, p))
    units.sort(key=lambda u: (u[0], u[1], u[2]))
    return [(p, gi) for _, _, gi, p in units[:n]]


# -------------------------------------------------------------------- report

def fmt_dur(m):
    return f"{m // 60}h{m % 60:02d}" if m else "?"


def fmt_date(iso):
    d = as_date(iso)
    return f"{['po','ut','st','št','pi','so','ne'][d.weekday()]} {d.day}.{d.month}."


def offer_line(o):
    ret = o.get("return")
    back = "neoverené"
    if ret:
        back = (f"{ret['dep_time'][5:]} {ret['origin']}→{ret['dest']}, {', '.join(ret['airlines'])}, "
                f"{fmt_dur(ret['duration_min'])}, {ret['stops']} prest. "
                f"({'/'.join(ret['via']) or 'priamo'})")
        if ret["price"] != o.get("price_google", o["price"]):
            back += f" ⚠️ spolu {ret['price']} € bez batožiny"
    bag = "; ".join(o["bag_info"]) if o["bag_info"] else "–"
    return (f"| {o['origin']}→{o['dest']} | {fmt_date(o['dep'])} – {fmt_date(o['ret'])} "
            f"| {o['away']} dní / {o['leave']} dov. | **{o['price']} €** | {', '.join(o['airlines'])} "
            f"| {fmt_dur(o['duration_min'])}, {o['stops']} prest. ({'/'.join(o['via']) or 'priamo'}) "
            f"| {back} | {bag} | {o['checked']} |")


HEADER = ("| Trasa | Dátumy | Preč / dovolenka | Cena | Dopravca | Tam | Späť | Batožina | Overené |\n"
          "|---|---|---|---|---|---|---|---|---|")


def ranked(cfg, state, today):
    """(offer, unit) for every stored offer still ahead of us, best score first."""
    rows = [(o, u) for u in state["units"].values() if u.get("dep", "") > today.isoformat()
            for o in u.get("offers", [])]
    max_stay = max((o["stay"] for o, _ in rows), default=0)
    return sorted(rows, key=lambda r: score(r[0], cfg, max_stay))


def top5(cfg, rows):
    """Best 5 under the cap (else the 5 closest to it), one per date pair first."""
    under = [r for r in rows if r[0]["price"] <= cfg["max_price_eur"]]
    pool = under or sorted(rows, key=lambda r: r[0]["price"])
    seen, first, rest = set(), [], []
    for r in pool:
        pair = (r[0]["dep"], r[0]["ret"])
        (rest if pair in seen else first).append(r)
        seen.add(pair)
    return (first + rest)[:5], bool(under)


def write_report(cfg, state, today, left, changes, errors):
    cap = cfg["max_price_eur"]
    today_iso = today.isoformat()
    rows = ranked(cfg, state, today)
    all_offers = [{**o, "checked": u["last_checked"], "url": u.get("url")} for o, u in rows]

    L = [f"# ✈️ Brno & okolie → Kalifornia · {fmt_date(today_iso)}{today.year}", "",
         f"Strop {cap} € · {cfg.get('adults', 1)} os. · max {cfg['max_stops']} prestupy · "
         f"max {cfg['max_duration_hours']} h"
         + (f" · SerpApi zostáva: {left if left is not None else '–'}" if cfg.get("source") == "serpapi" else ""),
         ""]
    if not all_offers:
        L.append("_Zatiaľ žiadne dáta._")
    else:
        top, any_under = top5(cfg, rows)
        under = [o for o in all_offers if o["price"] <= cap]
        L += ["## Top 5 (vyvážene)" if any_under else f"## Nič pod {cap} € – najbližšie k stropu",
              "", HEADER]
        L += [offer_line({**o, "checked": u["last_checked"]}) for o, u in top]
        cheapest = min(all_offers, key=lambda o: o["price"])
        L += ["", f"**Najlacnejšia:** {cheapest['origin']}→{cheapest['dest']}, "
                  f"{fmt_date(cheapest['dep'])} – {fmt_date(cheapest['ret'])}, {cheapest['price']} €"]
        if under:
            longest = max(under, key=lambda o: (o["stay"], -o["price"]))
            L.append(f"**Najdlhší pobyt pod {cap} €:** {longest['away']} dní preč, "
                     f"{longest['origin']}→{longest['dest']}, {fmt_date(longest['dep'])} – "
                     f"{fmt_date(longest['ret'])}, {longest['price']} €")
    L += ["", "## Zmeny od posledného overenia", ""]
    L += [f"- {c}" for c in changes] or ["- žiadne"]
    if errors:
        L += ["", "## Chyby", ""] + [f"- {e}" for e in errors]
    bag = cfg.get("bag", {})
    L += ["", f"_Ceny sú z Google Flights pre 1 osobu, spiatočná letenka spolu. Google už nevie "
              f"započítať odbavenú batožinu, preto je k cene pripočítaný odhad "
              f"{bag.get('fee_eur', 0)} € (okrem dopravcov s batožinou v cene); presnú sumu over "
              f"pri rezervácii. Cesta vlakom na letisko nie je započítaná._"]
    links = sorted({(u["dep"], u["ret"], u.get("url"), "/".join(cfg["origin_groups"][u["group"]]))
                    for u in state["units"].values() if u.get("url") and u["dep"] > today_iso})
    if links:
        L += ["", "<details><summary>Odkazy na Google Flights</summary>", ""]
        L += [f"- [{fmt_date(d)} – {fmt_date(r)} z {g}]({u})" for d, r, u, g in links]
        L += ["", "</details>"]
    REPORT_FILE.write_text("\n".join(L) + "\n")


# ---------------------------------------------------------------------- main

def probe(cfg, pair):
    """Quick connectivity check: one single-airport and one multi-airport search."""
    for label, origins, dests in [("single", ["VIE"], ["LAX"]),
                                  ("multi", cfg["origin_groups"][0], cfg["destinations"])]:
        c = {**cfg, "destinations": dests}
        resp, offers = gf_search(c, origins, pair)
        cheapest = min(offers, key=lambda o: o["price"]) if offers else None
        print(f"[{label}] {','.join(origins)} -> {','.join(dests)} {pair['dep']}..{pair['ret']}: "
              f"{len(offers)} offers, error={resp.get('error')}")
        if cheapest:
            print(f"    cheapest: {cheapest['origin']}->{cheapest['dest']} {cheapest['price']} EUR "
                  f"{', '.join(cheapest['airlines'])}, {fmt_dur(cheapest['duration_min'])}")
            print(f"    origins seen: {sorted({o['origin'] for o in offers})}, "
                  f"dests seen: {sorted({o['dest'] for o in offers})}")
            ret = gf_return(c, cheapest, origins)
            print(f"    return for cheapest: {ret}")
        print(f"    {resp['search_metadata']['google_flights_url']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show plan, no requests")
    ap.add_argument("--probe", action="store_true", help="test Google with 2 searches + 2 return lookups")
    ap.add_argument("--today", help="override date (YYYY-MM-DD), for testing")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    today = as_date(args.today) if args.today else dt.date.today()
    pairs = candidate_pairs(cfg, today)
    state = load_state()
    b = cfg["budget"]
    source = cfg.get("source", "google")

    if not pairs:
        print("No date pairs left in the window – nothing to do.")
        return
    if args.probe:
        probe(cfg, pairs[0])
        return

    key = os.environ.get("SERPAPI_KEY")
    if source == "serpapi":
        n = b["max_searches_per_day"] - (1 if b.get("fetch_return_for_top") else 0)
    else:
        n = cfg["google"]["max_requests_per_day"]

    if args.dry_run:
        print(f"source: {source}; {len(pairs)} date pairs tracked:")
        for p in pairs:
            print(f"  {fmt_date(p['dep'])} – {fmt_date(p['ret'])}: stay {p['stay']} n, "
                  f"away {p['away']} d, leave {p['leave']}")
        print(f"Today's plan ({n} searches max):")
        for p, gi in plan(cfg, state, pairs, n):
            print(f"  {p['dep']} – {p['ret']}  group {gi}: {','.join(cfg['origin_groups'][gi])}")
        return

    left = None
    if source == "serpapi":
        if not key:
            sys.exit("SERPAPI_KEY is not set")
        left = searches_left(key)
        if left is not None:
            n = max(0, min(n, left - b["reserve"]))

    DATA.mkdir(exist_ok=True)
    changes, errors, cap = [], [], cfg["max_price_eur"]
    max_stay = pairs[0]["stay"]
    today_iso = today.isoformat()
    delay = cfg.get("google", {}).get("delay_seconds", [4, 9])
    serp_budget = b["max_searches_per_day"] if source == "google" else 0

    for idx, (pair, gi) in enumerate(plan(cfg, state, pairs, n)):
        origins = cfg["origin_groups"][gi]
        if source == "serpapi":
            resp = call(base_params(cfg, key, origins, pair["dep"], pair["ret"]))
            offers = parse_offers(resp, pair)
        else:
            if idx:
                time.sleep(random.uniform(*delay))  # be gentle with Google
            resp, offers = gf_search(cfg, origins, pair)
            if resp.get("error") and key and serp_budget > 0:
                # Direct Google failed (blocked runner, layout change): same search via SerpApi.
                serp_budget -= 1
                errors.append(f"{fmt_date(pair['dep'])} ({'/'.join(origins)}): Google priamo zlyhal "
                              f"({resp['error'][:80]}), použité SerpApi")
                resp = call(base_params(cfg, key, origins, pair["dep"], pair["ret"]))
                offers = parse_offers(resp, pair)
        offers = [add_bag(o, cfg) for o in offers]
        k = unit_key(pair, gi)
        prev = state["units"].get(k, {})
        with LOG_FILE.open("a") as f:
            f.write(json.dumps({"date": today_iso, "source": source, "unit": k,
                                "n_offers": len(offers),
                                "min_price": min((o["price"] for o in offers), default=None),
                                "error": resp.get("error")}) + "\n")
        label = f"{fmt_date(pair['dep'])} – {fmt_date(pair['ret'])} ({'/'.join(origins)})"
        if resp.get("error") and not offers:
            msg = resp["error"]
            if "hasn't returned any results" not in msg:
                errors.append(f"{label}: {msg}")
            if prev:  # keep last good data, don't overwrite with an error
                prev["last_checked"] = today_iso
                continue
        offers.sort(key=lambda o: score(o, cfg, max_stay))
        best_now = min((o["price"] for o in offers), default=None)
        best_before = prev.get("best_price")
        if best_now is not None:
            if best_before is None:
                if best_now <= cap:
                    changes.append(f"🆕 {label}: {best_now} € (pod stropom)")
            elif best_now != best_before:
                arrow = "📉" if best_now < best_before else "📈"
                changes.append(f"{arrow} {label}: {best_before} € → {best_now} €")
        state["units"][k] = {
            **pair, "group": gi, "last_checked": today_iso,
            "best_price": best_now, "offers": offers[:5],
            "url": (resp.get("search_metadata") or {}).get("google_flights_url"),
            "history": (prev.get("history", []) + [[today_iso, best_now]])[-60:],
        }

    # Return-leg detail (SerpApi only) for the overall best offer under the cap.
    if source == "serpapi" and b.get("fetch_return_for_top"):
        cands = [(score(o, cfg, max_stay), k, i) for k, u in state["units"].items()
                 for i, o in enumerate(u.get("offers", []))
                 if o["price"] <= cap and o.get("departure_token") and not o.get("return")
                 and u.get("last_checked") == today_iso]
        if cands:
            _, k, i = min(cands)
            u = state["units"][k]
            try:
                ret = fetch_return(cfg, key, u["offers"][i], cfg["origin_groups"][u["group"]])
                if ret:
                    u["offers"][i]["return"] = ret
            except requests.RequestException as e:
                errors.append(f"return leg lookup failed: {e}")
    elif source == "google":
        for o, u in top5(cfg, ranked(cfg, state, today))[0]:
            if (u["last_checked"] != today_iso or o.get("return_checked") == today_iso
                    or not o.get("segments")):  # SerpApi fallback offers have no segments
                continue
            time.sleep(random.uniform(*delay))
            try:
                o["return"] = gf_return(cfg, o, cfg["origin_groups"][u["group"]])
                o["return_checked"] = today_iso
            except Exception as e:
                errors.append(f"spiatočný let {o['origin']}→{o['dest']} {o['dep']}: "
                              f"{type(e).__name__}: {e}"[:300])

    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    write_report(cfg, state, today, searches_left(key) if source == "serpapi" else None,
                 changes, errors)
    print(REPORT_FILE.read_text())
    # Fail the Action if every search errored, so GitHub emails you.
    if errors and not any(u.get("last_checked") == today_iso and u.get("best_price")
                          for u in state["units"].values()):
        sys.exit("All searches failed – see REPORT.md")


if __name__ == "__main__":
    main()
