#!/usr/bin/env python3
"""Flight watch: round trips Brno area -> California via SerpApi (Google Flights).

Each run picks the date pairs checked least recently (within the daily budget),
searches them, stores results in data/state.json and writes REPORT.md.

Usage:
  SERPAPI_KEY=... python flight_watch.py          # normal run
  python flight_watch.py --dry-run                 # show plan, no API calls
"""
import argparse
import datetime as dt
import json
import os
import sys
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
    back = (f"{fmt_dur(ret['duration_min'])}, {ret['stops']} prest." if ret else "neoverené")
    bag = "; ".join(o["bag_info"]) if o["bag_info"] else "–"
    return (f"| {o['origin']}→{o['dest']} | {fmt_date(o['dep'])} – {fmt_date(o['ret'])} "
            f"| {o['away']} dní / {o['leave']} dov. | **{o['price']} €** | {', '.join(o['airlines'])} "
            f"| {fmt_dur(o['duration_min'])}, {o['stops']} prest. ({'/'.join(o['via']) or 'priamo'}) "
            f"| {back} | {bag} | {o['checked']} |")


HEADER = ("| Trasa | Dátumy | Preč / dovolenka | Cena | Dopravca | Tam | Späť | Batožina (Google) | Overené |\n"
          "|---|---|---|---|---|---|---|---|---|")


def write_report(cfg, state, today, left, changes, errors):
    cap = cfg["max_price_eur"]
    today_iso = today.isoformat()
    all_offers = []
    for u in state["units"].values():
        if u.get("dep", "") <= today_iso:
            continue  # departure already passed
        for o in u.get("offers", []):
            all_offers.append({**o, "checked": u["last_checked"], "url": u.get("url")})
    max_stay = max((o["stay"] for o in all_offers), default=0)
    for o in all_offers:
        o["score"] = score(o, cfg, max_stay)

    L = [f"# ✈️ Brno & okolie → Kalifornia · {fmt_date(today_iso)}{today.year}", "",
         f"Strop {cap} € · {cfg.get('adults', 1)} os. · max {cfg['max_stops']} prestupy · "
         f"max {cfg['max_duration_hours']} h · SerpApi zostáva: {left if left is not None else '?'}", ""]
    if not all_offers:
        L.append("_Zatiaľ žiadne dáta._")
    else:
        under = [o for o in all_offers if o["price"] <= cap]
        pool = under or sorted(all_offers, key=lambda o: o["price"])[:5]
        L += ["## Top 5 (vyvážene)" if under else f"## Nič pod {cap} € – najbližšie k stropu", "", HEADER]
        L += [offer_line(o) for o in sorted(pool, key=lambda o: o["score"])[:5]]
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
    L += ["", "_Ceny sú z Google Flights pre 1 osobu; odbavenú batožinu over pri rezervácii "
              "(stĺpec Batožina ukazuje, čo hlási Google). Cesta vlakom na letisko nie je započítaná._"]
    links = sorted({(o["dep"], o["ret"], o["url"]) for o in all_offers if o.get("url")})
    if links:
        L += ["", "<details><summary>Odkazy na Google Flights</summary>", ""]
        L += [f"- [{fmt_date(d)} – {fmt_date(r)}]({u})" for d, r, u in links] + ["", "</details>"]
    REPORT_FILE.write_text("\n".join(L) + "\n")


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--today", help="override date (YYYY-MM-DD), for testing")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    today = as_date(args.today) if args.today else dt.date.today()
    pairs = candidate_pairs(cfg, today)
    state = load_state()
    b = cfg["budget"]

    if not pairs:
        print("No date pairs left in the window – nothing to do.")
        return

    key = os.environ.get("SERPAPI_KEY")
    if args.dry_run:
        print(f"{len(pairs)} date pairs tracked:")
        for p in pairs:
            print(f"  {fmt_date(p['dep'])} – {fmt_date(p['ret'])}: stay {p['stay']} n, "
                  f"away {p['away']} d, leave {p['leave']}")
        print("Today's plan:")
        for p, gi in plan(cfg, state, pairs, b["max_searches_per_day"]):
            print(f"  {p['dep']} – {p['ret']}  group {gi}: {','.join(cfg['origin_groups'][gi])}")
        return
    if not key:
        sys.exit("SERPAPI_KEY is not set")

    left = searches_left(key)
    n = b["max_searches_per_day"] - (1 if b.get("fetch_return_for_top") else 0)
    if left is not None:
        n = max(0, min(n, left - b["reserve"]))
    DATA.mkdir(exist_ok=True)
    changes, errors, cap = [], [], cfg["max_price_eur"]
    max_stay = pairs[0]["stay"]
    today_iso = today.isoformat()

    for pair, gi in plan(cfg, state, pairs, n):
        origins = cfg["origin_groups"][gi]
        resp = call(base_params(cfg, key, origins, pair["dep"], pair["ret"]))
        k = unit_key(pair, gi)
        prev = state["units"].get(k, {})
        offers = parse_offers(resp, pair)
        with LOG_FILE.open("a") as f:
            f.write(json.dumps({"date": today_iso, "unit": k, "n_offers": len(offers),
                                "min_price": min((o["price"] for o in offers), default=None),
                                "error": resp.get("error")}) + "\n")
        label = f"{fmt_date(pair['dep'])} – {fmt_date(pair['ret'])} ({'/'.join(origins)})"
        if resp.get("error") and not offers:
            msg = resp["error"]
            # "no results" is a normal outcome, not a failure
            if "hasn't returned any results" not in msg:
                errors.append(f"{label}: {msg}")
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

    # Return-leg detail for the overall best offer under the cap.
    if b.get("fetch_return_for_top"):
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

    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    write_report(cfg, state, today, searches_left(key), changes, errors)
    print(REPORT_FILE.read_text())


if __name__ == "__main__":
    main()
