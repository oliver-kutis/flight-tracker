"""Daily run: canary -> stage A (coarse round trips) -> B (open-jaw, return legs,
standard-fare requery) -> C (paid validation) -> D (separate-ticket probe) -> outputs.

  uv run tracker --dry-run              # print today's query plan, no requests
  uv run tracker --max-requests 50      # small live run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import random
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from . import storage
from .adapters import CAPTCHA, CONSENT, EMPTY, OK, SearchResult, build_adapters
from .config import load_config
from .fx import Fx
from .history import History, route_key, write_history
from .leave import non_working_days
from .normalize import Ctx, normalize, segment_key
from .planner import (Task, canary_task, date_pairs, openjaw_tasks, plan_counts, shed, stage_a,
                      stage_a_opts, stage_b_reserve)
from .quota import Quota
from .score import eligible, score_all

log = logging.getLogger("tracker")
ALL_STAGES = ["canary", "A", "B", "C", "D"]
STAGE_A_ORDER = {"tier1": 0, "tier2": 1, "sfo_sjc_oak": 2, "tier3": 3}


def utcnow() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Runner:
    def __init__(self, cfg, today: dt.date, budget: int, dry_run: bool, chain: list[str] | None):
        self.cfg, self.today, self.budget, self.dry = cfg, today, budget, dry_run
        self.t = str(today)
        self.quota = Quota(storage.DATA / "quota.json", {
            "searchapi": {"monthly_cap": cfg.sources.searchapi.monthly_cap},
            "serpapi": cfg.sources.serpapi.model_dump()})
        self.adapters = build_adapters(cfg, self.quota)
        self.chain = chain or list(cfg.sources.chain)
        self.free_used = 0
        self.requests = defaultdict(Counter)    # source -> stage -> attempts
        self.outcomes = defaultdict(Counter)    # source -> status -> n
        self.fail_streak = Counter()
        self.breaker_open: set[str] = set()
        self.degraded: dict[str, str] = {}
        self.errors: list[dict] = []
        self.sources_used = Counter()
        self.planned = self.succeeded = 0
        self.truncated_streak = 0

    # ------------------------------------------------------------ execution

    def _sleep(self, s: float) -> None:
        if not self.dry:
            time.sleep(s)

    def _paid_allowed(self, task: Task) -> bool:
        """Paid quota is precious: canary/validation always; Tier 1 only once every free source is down."""
        if task.opts.get("source") or task.group in ("canary", "validate"):
            return True
        free_up = any(ad.free and n not in self.breaker_open and ad.available()[0]
                      for n, ad in self.adapters.items() if n in self.chain)
        return task.group == "tier1" and not free_up

    def execute(self, task: Task) -> SearchResult | None:
        """Try the source chain for one task. None = request budget exhausted."""
        lim = self.cfg.limits
        chain = [task.opts["source"]] if task.opts.get("source") else self.chain
        best: SearchResult | None = None
        for i, name in enumerate(chain):
            ad = self.adapters.get(name)
            if ad is None or name in self.breaker_open:
                continue
            if not ad.free and not self._paid_allowed(task):
                continue
            ok, why = ad.available()
            if not ok:
                continue
            for attempt in range(lim.max_retries):
                if ad.free and self.free_used >= self.budget:
                    return best
                if ad.free:
                    self.free_used += 1
                    self._sleep(random.uniform(*lim.delay_s))
                self.requests[name][task.stage] += 1
                res = ad.run(task, self.cfg)
                self.outcomes[name][res.status] += 1
                if res.status in (OK, EMPTY, CONSENT, CAPTCHA) or not res.retryable:
                    break  # retrying a consent/captcha page from the same IP does not help
                log.warning("%s %s attempt %d: %s", name, task.id, attempt + 1, res.error)
                self._sleep(lim.backoff_base_s * 2 ** attempt)
            if res.status == OK:
                self.fail_streak[name] = 0
            elif res.complete is False:
                pass  # route-specific truncation (slow search, follow-up refused): not a dead source
            else:
                # A consent/CAPTCHA page will not go away within this run from the same IP.
                self.fail_streak[name] += lim.circuit_breaker_consecutive_failures if res.status in (CONSENT, CAPTCHA) else 1
                if self.fail_streak[name] >= lim.circuit_breaker_consecutive_failures:
                    self.breaker_open.add(name)
                    self.degraded[name] = f"circuit breaker open after {self.fail_streak[name]} failures"
                    log.error("circuit breaker open for %s", name)
            if best is None or len(res.offers) > len(best.offers):
                best = res
            last_free = not any(self.adapters[n].free for n in chain[i + 1:] if n in self.adapters)
            if res.status == OK and (len(res.offers) >= lim.min_offers_before_fallback or not ad.free or last_free):
                return res
            log.info("%s %s %s-%s %s: %s %s (%d offers)%s", task.stage, task.group, task.origin, task.dest,
                     task.out_date, name, res.status, len(res.offers), "; trying next source" if i + 1 < len(chain) else "")
        return best

    def run_task(self, task: Task, ctx: Ctx, store: dict, keep: int | None = None) -> list[dict]:
        if task.status == "done":
            return []
        self.planned += 1
        res = self.execute(task)
        if res is None:
            task.status, task.error = "skipped", "request budget exhausted"
            return []
        task.source_used = res.source
        self.sources_used[res.source] += 1
        if res.status not in (OK, EMPTY):
            task.status, task.error = "failed", f"{res.status}: {res.error}"
            self.errors.append({"task": task.id, "stage": task.stage, "group": task.group,
                                "route": f"{task.origin}-{task.dest}/{task.ret_origin}-{task.ret_dest}",
                                "dates": f"{task.out_date}/{task.ret_date}", "source": res.source,
                                "status": res.status, "error": (res.error or "")[:300]})
            return []
        self.succeeded += 1
        task.status, task.n_offers = "done", len(res.offers)
        storage.save_raw(self.t, res.source, res.query_params, res.raw)
        want_to = task.ret_dest if task.kind == "return_options" else task.dest
        offers = [o for o in res.offers if o["legs"][0][-1]["to"] == want_to]
        offers.sort(key=lambda o: o["price"])
        recs, fetched = [], utcnow()
        for o in offers[: keep or self.cfg.limits.top_offers_per_query]:
            try:
                recs.append(normalize(ctx, task, res, o, fetched))
            except Exception as e:  # noqa: BLE001 - one bad offer must not kill the run
                self.errors.append({"task": task.id, "stage": task.stage, "error": f"normalize: {type(e).__name__}: {e}"[:300]})
        for r in recs:
            storage.merge_record(store, r)
        return recs


# ---------------------------------------------------------------- helpers

def gh_issue(title: str, body: str) -> str:
    """Open or comment on an issue titled `title` (needs gh CLI + GH_TOKEN, i.e. in Actions)."""
    if not shutil.which("gh") or not (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")):
        return "skipped (gh CLI or token missing)"
    try:
        found = subprocess.run(["gh", "issue", "list", "--state", "open", "--search", f'"{title}" in:title',
                                "--json", "number", "-q", ".[0].number"], capture_output=True, text=True, timeout=60)
        num = found.stdout.strip()
        cmd = (["gh", "issue", "comment", num, "--body", body] if num
               else ["gh", "issue", "create", "--title", title, "--body", body])
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return "ok" if p.returncode == 0 else f"gh failed: {p.stderr.strip()[:200]}"
    except Exception as e:  # noqa: BLE001
        return f"gh failed: {type(e).__name__}"


def bag_filter_check(with_bag: SearchResult, without: SearchResult | None) -> bool | None:
    """True if Google priced the checked bag: some itinerary got dearer with checked_bags=1."""
    if not without or without.status != OK or with_bag.status != OK:
        return None
    a = {tuple(segment_key(o["legs"][0])): o["price"] for o in with_bag.offers}
    b = {tuple(segment_key(o["legs"][0])): o["price"] for o in without.offers}
    common = set(a) & set(b)
    if not common:
        return None
    return any(a[k] > b[k] for k in common)


def preliminary_rank(cfg, recs: list[dict]) -> list[dict]:
    score_all(cfg, recs)
    return sorted((r for r in recs if eligible(r)), key=lambda r: -r["scoring"]["final_score"])


def _scaled_reserve(cfg, budget: int) -> dict[str, int]:
    full = stage_b_reserve(cfg)
    if budget >= cfg.limits.max_requests_per_run:
        return full
    return {k: math.ceil(v * budget / cfg.limits.max_requests_per_run) for k, v in full.items()}


def order_stage_a(tasks: list[Task], pairs) -> list[Task]:
    """Breadth first: within each group longest pairs first, then round-robin over routes."""
    rank = {p.key: (p.pair_tier != "longest", i) for i, p in enumerate(pairs)}
    return sorted(tasks, key=lambda t: (STAGE_A_ORDER.get(t.group, 9), rank[t.pair_key]))


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tracker")
    ap.add_argument("--stages", default="all", help="comma list of canary,A,B,C,D or 'all'")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-requests", type=int)
    ap.add_argument("--source-override", default="", help="comma list replacing sources.chain")
    ap.add_argument("--today", help="YYYY-MM-DD (testing)")
    ap.add_argument("--config", default=None)
    a = ap.parse_args(argv)

    cfg = load_config(a.config) if a.config else load_config()
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    t = str(today)
    stages = ALL_STAGES if a.stages in ("all", "") else [s.strip() for s in a.stages.split(",")]
    budget = min(a.max_requests or cfg.limits.max_requests_per_run, cfg.limits.max_requests_per_run)
    chain = [s.strip() for s in a.source_override.split(",") if s.strip()] or None

    latest = storage.DATA / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stderr)]
    if not a.dry_run:
        handlers.append(logging.FileHandler(latest / "log.txt", mode="w"))
    # Third-party loggers stay at WARNING (primp logs every URL at INFO).
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers, force=True)
    log.setLevel(logging.INFO)

    pairs = date_pairs(cfg)
    a_tasks = stage_a(cfg, pairs, today)
    reserve = _scaled_reserve(cfg, budget)
    excess_budget = budget - sum(reserve.values())
    kept, reserve_after, shed_counts = shed(cfg, a_tasks, budget)
    if len(kept) > excess_budget:  # small manual runs: runtime budget cuts stage A, B/D keep their share
        kept = order_stage_a(kept, pairs)[:max(0, excess_budget)]
    kept = order_stage_a(kept, pairs)
    plan = {"date": t, "budget": budget, "date_pairs": len(pairs),
            "longest_pairs": sum(p.pair_tier == "longest" for p in pairs),
            "stage_a_full": plan_counts(a_tasks), "stage_a_after_shedding": plan_counts(kept),
            "shed": shed_counts, "reserved_dynamic": reserve if budget < cfg.limits.max_requests_per_run else reserve_after,
            "stages": stages}

    run = Runner(cfg, today, budget, a.dry_run, chain)
    caps = {}
    for name, ad in run.adapters.items():
        ok, why = ad.available()
        try:
            caps[name] = {"available": ok, "reason": why, **(ad.capabilities() if ok else {})}
        except Exception as e:  # noqa: BLE001
            caps[name] = {"available": False, "reason": f"capabilities failed: {type(e).__name__}: {e}"[:200]}
    log.info("capability report: %s", json.dumps(caps))
    log.info("plan: %s", json.dumps(plan))
    if a.dry_run:
        print(json.dumps({"plan": plan, "capabilities": caps}, indent=1))
        return 0

    started = utcnow()
    run_id = f"{t}-{uuid.uuid4().hex[:6]}"
    fx = Fx(storage.DATA / "fx.json", cfg.fx.url)
    hol = non_working_days(cfg)
    queue = storage.load_queue(t)
    known = {d["id"]: Task.from_dict(d) for d in queue["tasks"]}

    def enqueue(ts: list[Task]) -> list[Task]:
        out = []
        for x in ts:
            out.append(known.setdefault(x.id, x))
        queue["tasks"] = [x.to_dict() for x in known.values()]
        storage.save_queue(queue)
        return out

    store = storage.load_itineraries(t)  # same-day rerun merges
    meta_extra: dict = {"bag_filter_effective": None}
    ctx = Ctx(cfg, hol, fx, run_id, None)

    # ---------------- canary
    if "canary" in stages:
        ct = canary_task(cfg, pairs)
        res = run.execute(ct)
        primary = run.chain[0]
        if res is None or res.status != OK or res.source != primary:
            why = f"{res.source if res else '-'}: {res.status if res else 'no budget'} {res.error if res else ''}"
            run.degraded[primary] = f"canary failed on {primary} ({why})"
            fallback = res.source if res and res.status == OK else None
            if fallback:  # promote the source that worked for the rest of the run
                run.chain = [fallback] + [c for c in run.chain if c != fallback]
            meta_extra["issue"] = gh_issue("tracker degraded", f"Run {run_id}: canary MUC-SAN failed on "
                                           f"`{primary}` ({why}). Using `{fallback}` instead." if fallback else
                                           f"Run {run_id}: canary MUC-SAN failed on every source ({why}).")
        if res is not None and res.status == OK:
            ct2 = Task(**{**ct.__dict__, "opts": {**ct.opts, "checked_bags": 0, "source": res.source}})
            res2 = run.execute(ct2)
            ctx.bag_filter_effective = meta_extra["bag_filter_effective"] = bag_filter_check(res, res2)
            log.info("canary ok via %s (%d offers); checked-bag filter effective: %s",
                     res.source, len(res.offers), ctx.bag_filter_effective)
            ct.status = "done"
            enqueue([ct])
            for o in res.offers[: cfg.limits.top_offers_per_query]:
                storage.merge_record(store, normalize(ctx, ct, res, o, utcnow()))

    # ---------------- stage A
    if "A" in stages:
        for i, task in enumerate(enqueue(kept)):
            run.run_task(task, ctx, store)
            if i % 10 == 9:
                queue["tasks"] = [x.to_dict() for x in known.values()]
                storage.save_queue(queue)
                storage.save_itineraries(t, store)
        log.info("stage A done: %d records, %d free requests used", len(store), run.free_used)

    # ---------------- stage B
    sb = cfg.stages.stage_b
    if "B" in stages and store:
        ranked = preliminary_rank(cfg, list(store.values()))
        best_origins = list(dict.fromkeys(r["origin"] for r in ranked))[: sb.openjaw_origins]
        pair_by_key = {p.key: p for p in pairs}
        top_pairs = [pair_by_key[k] for k in dict.fromkeys(r["pair_key"] for r in ranked) if k in pair_by_key][: sb.openjaw_pairs]
        cap = min(sb.openjaw_cap, reserve_after["stage_b_openjaw"] if budget >= cfg.limits.max_requests_per_run
                  else reserve["stage_b_openjaw"])
        for task in enqueue(openjaw_tasks(cfg, best_origins, top_pairs, cap)):
            run.run_task(task, ctx, store)

        ranked = preliminary_rank(cfg, list(store.values()))
        seen, cands = set(), []
        n_top = min(sb.top_candidates, reserve["return_options"])
        for r in ranked:
            k = (r["itinerary_id"], r["provenance"]["source"])
            if not r["inbound_known"] and k not in seen and r["provenance"]["source"].startswith("fast_flights"):
                seen.add(k)
                cands.append(r)
            if len(cands) >= n_top:
                break
        for r in cands:
            out = r["legs"]["outbound"]["segments"]
            slim = [{k: s[k] for k in ("marketing_carrier", "flight_number", "from", "to", "dep_local",
                                       "arr_local", "duration_min", "operating_carrier", "carrier_name",
                                       "codeshares", "aircraft")} for s in out]
            opts = {**stage_a_opts(cfg), "selected": slim, "outbound": slim}
            rt = Task("B", "return_options", "return_options", r["origin"], r["destination"],
                      r["return_origin"], r["return_destination"],
                      r["provenance"]["query_out_date"],
                      _ret_date(r), r["pair_key"], opts)
            recs = run.run_task(enqueue([rt])[0], ctx, store, keep=sb.return_options_keep)
            if recs:
                store.pop(r["itinerary_id"], None)  # replaced by full itineraries

        # standard-fare requery (exclude basic economy) for the routes of the full itineraries
        full = [r for r in preliminary_rank(cfg, list(store.values())) if r["inbound_known"]][: sb.top_candidates]
        groups = defaultdict(list)
        for r in full:
            groups[(r["origin"], r["destination"], r["return_origin"], r["return_destination"],
                    r["legs"]["outbound"]["segments"][0]["dep_local"][:10], _ret_date(r), r["pair_key"])].append(r)
        for (o, d, ro, rd, od, rdt, pk), rs in list(groups.items())[: reserve["exclude_basic"]]:
            kind = "roundtrip" if (ro, rd) == (d, o) else "multicity"
            et = Task("B", "exclude_basic", kind, o, d, ro, rd, od, rdt, pk,
                      {**stage_a_opts(cfg), "exclude_basic_economy": True})
            et = enqueue([et])[0]
            if et.status == "done":
                continue
            res = run.execute(et)
            run.planned += 1
            if not res or res.status != OK:
                et.status = "failed" if res else "skipped"
                continue
            run.succeeded += 1
            et.status, et.source_used = "done", res.source
            std = {tuple(segment_key(of["legs"][0])): of for of in res.offers}
            for r in rs:
                apply_standard_fare(cfg, fx, r, std.get(tuple(segment_key(r["legs"]["outbound"]["segments"]))))

    # ---------------- stage C (paid validation)
    if "C" in stages and store:
        paid = [n for n in ("searchapi", "serpapi") if n in run.adapters and run.adapters[n].available()[0]]
        if paid:
            top = preliminary_rank(cfg, list(store.values()))[: cfg.stages.stage_c.top]
            for r in top:
                src = next((n for n in paid if run.adapters[n].available()[0]), None)
                if not src:
                    break
                vt = Task("C", "validate", "roundtrip" if r["trip_type"] == "round_trip" else "multicity",
                          r["origin"], r["destination"], r["return_origin"], r["return_destination"],
                          r["legs"]["outbound"]["segments"][0]["dep_local"][:10], _ret_date(r), r["pair_key"],
                          {"source": src})
                res = run.execute(vt)
                if res is None:
                    continue
                match = next((o for o in res.offers if segment_key(o["legs"][0]) ==
                              segment_key(r["legs"]["outbound"]["segments"])), None)
                p_czk = round(fx.to_czk(match["price"], match["currency"])) if match else None
                r["provenance"]["validation"] = {
                    "source": src, "status": res.status, "matched": match is not None, "price_czk": p_czk,
                    "discrepancy_czk": p_czk - r["price"]["price_total_czk"] if p_czk is not None else None,
                    "cheapest_listed_czk": round(fx.to_czk(min(o["price"] for o in res.offers), res.offers[0]["currency"])) if res.offers else None,
                    "fetched_at_utc": utcnow()}
                if res.raw is not None:
                    storage.save_raw(t, src, res.query_params, res.raw)
        else:
            log.info("stage C skipped: no paid source available")

    # ---------------- stage D (separate-ticket probe)
    if "D" in stages and store:
        tier1 = {o.iata for o in cfg.enabled_origins if o.tier == 1}
        ranked = [r for r in preliminary_rank(cfg, list(store.values())) if r["origin"] in tier1
                  and r["trip_type"] == "round_trip"]
        routes, best_protected = [], {}
        for r in ranked:
            k = route_key(r)
            best_protected[k] = min(best_protected.get(k, 1e12), r["price"]["price_total_czk"])
            if k not in routes:
                routes.append(k)
        protected_ids = set(store)
        n = min(cfg.stages.stage_d.max_queries, reserve["separate_probe"])
        for k in routes[:n]:
            r0 = next(r for r in ranked if route_key(r) == k)
            dt_ = Task("D", "separate_probe", "roundtrip", k[0], k[1], k[2], k[3],
                       r0["legs"]["outbound"]["segments"][0]["dep_local"][:10], _ret_date(r0), k[4],
                       {**stage_a_opts(cfg), "hide_separate_and_self_transfer": False})
            tmp: dict = {}
            recs = run.run_task(enqueue([dt_])[0], ctx, tmp)
            limit = best_protected[k] * (1 - cfg.stages.stage_d.min_saving_pct / 100)
            for rec in recs:
                if rec["itinerary_id"] in protected_ids or rec["price"]["price_total_czk"] > limit:
                    continue
                rec["price"]["separate_tickets"] = True
                rec["price"]["separate_tickets_basis"] = "absent_from_protected_search"
                rec["price"]["saving_vs_best_protected_czk"] = round(best_protected[k] - rec["price"]["price_total_czk"])
                store[rec["itinerary_id"]] = rec

    queue["tasks"] = [x.to_dict() for x in known.values()]
    storage.save_queue(queue)

    # ---------------- outputs
    fresh = list(store.values())
    score_all(cfg, fresh)
    storage.save_itineraries(t, store)
    write_history(storage.DATA / "history", t, fresh)
    last_seen = storage.load_last_seen()
    fresh_keys = {"|".join(route_key(r)) for r in fresh}
    carried = storage.carried_forward(last_seen, t, fresh_keys, cfg.storage.carry_forward_max_stale_days)
    storage.update_last_seen(last_seen, t, fresh)
    allrecs = fresh + carried
    score_all(cfg, allrecs)
    hist = History(storage.DATA / "history")

    success_rate = run.succeeded / run.planned if run.planned else 0.0
    if run.planned and success_rate < 0.3:
        meta_extra["issue_low_success"] = gh_issue(
            "tracker degraded", f"Run {run_id}: only {run.succeeded}/{run.planned} queries succeeded "
                                f"({success_rate:.0%}). Degraded: {json.dumps(run.degraded)}")
    pruned = storage.prune_raw(today, cfg.storage.raw_retention_days)
    meta = {
        "schema_version": storage.SCHEMA_VERSION, "run_id": run_id, "date": t,
        "started_at_utc": started, "finished_at_utc": utcnow(),
        "stages_run": stages, "dry_run": False, "plan": plan,
        "requests": {"free_budget": budget, "free_used": run.free_used,
                     "by_source_and_stage": {k: dict(v) for k, v in run.requests.items()},
                     "outcomes_by_source": {k: dict(v) for k, v in run.outcomes.items()}},
        "queries": {"planned": run.planned, "succeeded": run.succeeded, "success_rate": round(success_rate, 3)},
        "sources_used_for_results": dict(run.sources_used), "source_chain": run.chain,
        "degraded": run.degraded, "circuit_breakers_open": sorted(run.breaker_open),
        "capability_report": caps, "bag_filter_effective": meta_extra["bag_filter_effective"],
        "quotas": run.quota.report(), "fx": fx.meta(),
        "errors": run.errors[:200], "errors_total": len(run.errors),
        "issues": {k: v for k, v in meta_extra.items() if k.startswith("issue")},
        "raw_dirs_pruned": pruned,
        "assumptions": {
            "esta": cfg.esta_note,
            "estimates": "ground legs, hotel costs, final legs to/from San Diego and bag_fee_estimate_czk are estimates (estimate: true)",
            "inbound_unknown": "records with inbound_known=false use planning assumptions: CA departure "
                               "ret_date 15:00, home arrival next day 20:00",
            "bag": "bag_certainty explains whether the checked bag is in the price; see README",
            "currency": cfg.sources.currency,
        },
    }
    summary = storage.build_summary(cfg, meta, allrecs, hist, t)
    for r in list(store.values())[:200]:
        storage.validate(r, "itinerary.schema.json")
    storage.validate(meta, "run_meta.schema.json")
    storage.validate(summary, "summary.schema.json")
    storage.write_json(latest / "run_meta.json", meta)
    (latest / "summary_for_agent.json").write_text(storage.dumps(summary) + "\n")
    log.info("done: %d/%d queries ok (%.0f%%), %d records, free requests %d/%d",
             run.succeeded, run.planned, 100 * success_rate, len(store), run.free_used, budget)
    return 0


def _ret_date(r: dict) -> str:
    inb = r["legs"]["inbound"]
    return inb["segments"][0]["dep_local"][:10] if inb else r["provenance"]["query_ret_date"]


def apply_standard_fare(cfg, fx, r: dict, std_offer: dict | None) -> None:
    """Final price = min(basic fare + bag fee, standard fare incl. bag)."""
    p = r["price"]
    basic = p["flight_price_incl_bag_czk"]
    p["basic_fare_czk"] = p["price_total_czk"]
    if std_offer is None:
        p["standard_fare_czk"] = None
        p["price_basis"] = "as_listed_no_standard_match"
        return
    std = round(fx.to_czk(std_offer["price"], std_offer["currency"]))
    p["standard_fare_czk"] = std
    p["basic_economy"] = std > p["price_total_czk"]
    if std < basic:
        delta = std - basic
        p.update(price_total_czk=std, flight_price_incl_bag_czk=std,
                 price_total_eur=round(std / fx.rates["EUR"], 2) if fx.rates.get("EUR") else None, bag_fee_estimate_czk=0,
                 price_includes_checked_bag=True, bag_certainty="standard_fare_assumed_included",
                 price_basis="standard_fare")
        r["totals"]["total_trip_cost_czk"] += delta
    else:
        p["price_basis"] = "basic_fare_plus_bag" if p["bag_fee_estimate_czk"] else "listed_fare_incl_bag"


if __name__ == "__main__":
    sys.exit(main())
