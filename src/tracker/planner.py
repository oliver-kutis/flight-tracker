"""Date pairs and the deterministic daily query plan."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field

from .config import Config
from .leave import DAY, calendar_days_away, leave_days, non_working_days, plan_flight_offset


@dataclass(frozen=True)
class DatePair:
    leave_home_date: dt.date
    home_arrival_date: dt.date
    leave_days: int
    calendar_days_away: int
    pair_tier: str  # "longest" | "standard"

    @property
    def key(self) -> str:
        return f"{self.leave_home_date}_{self.home_arrival_date}"


def date_pairs(cfg: Config) -> list[DatePair]:
    """Every (D, H) inside the window with >= min calendar days and <= hard leave days."""
    w, hol = cfg.window, non_working_days(cfg)
    out: list[DatePair] = []
    d = w.earliest_leave_home
    while d <= w.latest_home_arrival:
        row = []
        h = d + (w.min_calendar_days_away - 1) * DAY
        while h <= w.latest_home_arrival:
            ld = leave_days(d, h, hol)
            if ld <= cfg.leave.max_leave_hard:
                row.append((h, ld))
            h += DAY
        for h, ld in row:
            tier = "longest" if h == row[-1][0] else "standard"
            out.append(DatePair(d, h, ld, calendar_days_away(d, h), tier))
        d += DAY
    return out


@dataclass
class Task:
    stage: str           # canary | A | B | C | D
    group: str           # tier1 | tier2 | tier3 | sfo_sjc_oak | return_options | exclude_basic | stage_b_openjaw | validate | separate_probe | canary
    kind: str            # roundtrip | multicity | return_options
    origin: str
    dest: str
    ret_origin: str      # where the inbound flight departs (California)
    ret_dest: str        # where the inbound flight lands (Europe)
    out_date: str
    ret_date: str
    pair_key: str
    opts: dict = field(default_factory=dict)  # exclude_basic_economy, hide_separate_and_self_transfer, checked_bags, selected (outbound segments), source
    status: str = "pending"  # pending | done | failed | skipped
    source_used: str | None = None
    n_offers: int = 0
    error: str | None = None

    @property
    def id(self) -> str:
        key = [self.stage, self.kind, self.origin, self.dest, self.ret_origin, self.ret_dest,
               self.out_date, self.ret_date, sorted(self.opts.items())]
        return hashlib.sha1(json.dumps(key, default=str).encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {"id": self.id, **asdict(self)}

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(**{k: v for k, v in d.items() if k != "id"})


def _rt(cfg: Config, stage: str, group: str, origin: str, dest: str, p: DatePair, **opts) -> Task:
    off = plan_flight_offset(cfg, cfg.origin(origin))
    return Task(stage, group, "roundtrip", origin, dest, dest, origin,
                str(p.leave_home_date + off * DAY), str(p.home_arrival_date - DAY), p.key, opts)


def stage_a_opts(cfg: Config) -> dict:
    return {"checked_bags": cfg.traveller.checked_bags, "hide_separate_and_self_transfer": True,
            "exclude_basic_economy": False}


def canary_task(cfg: Config, pairs: list[DatePair]) -> Task:
    """MUC->SAN round trip, longest pair whose outbound is a Tuesday."""
    p = next((p for p in pairs if p.pair_tier == "longest" and p.leave_home_date.weekday() == 1),
             pairs[0])
    return _rt(cfg, "canary", "canary", "MUC", "SAN", p, **stage_a_opts(cfg))


def stage_a(cfg: Config, pairs: list[DatePair], today: dt.date) -> list[Task]:
    sa, doy = cfg.stages.stage_a, today.timetuple().tm_yday
    opts = stage_a_opts(cfg)
    longest = [p for p in pairs if p.pair_tier == "longest"]
    by_tier = {t: [o.iata for o in cfg.enabled_origins if o.tier == t] for t in (1, 2, 3)}
    tasks: list[Task] = []
    for o in by_tier[1]:
        for d in cfg.primary_destinations:
            tasks += [_rt(cfg, "A", "tier1", o, d, p, **opts) for p in pairs]
    sl = doy % sa.tier2_slices
    for o in by_tier[2]:
        for d in cfg.primary_destinations:
            tasks += [_rt(cfg, "A", "tier2", o, d, p, **opts)
                      for i, p in enumerate(pairs) if i % sa.tier2_slices == sl]
    sl = doy % sa.secondary_every_days
    for o in by_tier[1]:
        for d in cfg.secondary_destinations:
            tasks += [_rt(cfg, "A", "sfo_sjc_oak", o, d, p, **opts)
                      for i, p in enumerate(longest) if i % sa.secondary_every_days == sl]
    sl = doy % sa.tier3_every_days
    for i, o in enumerate(by_tier[3]):
        if i % sa.tier3_every_days == sl:
            for d in cfg.primary_destinations:
                tasks += [_rt(cfg, "A", "tier3", o, d, p, **opts) for p in longest]
    return tasks


def stage_b_reserve(cfg: Config) -> dict[str, int]:
    """Requests reserved for the dynamic stages (B/C/D), used for budgeting before they exist."""
    sb = cfg.stages.stage_b
    return {"canary": 2, "return_options": sb.top_candidates, "exclude_basic": sb.top_candidates,
            "stage_b_openjaw": sb.openjaw_cap, "separate_probe": cfg.stages.stage_d.max_queries}


def shed(cfg: Config, tasks: list[Task], budget: int) -> tuple[list[Task], dict[str, int], dict[str, int]]:
    """Drop tasks group by group (load_shedding_order, from the end of each group)
    until stage A + reserved B/D requests fit the budget.
    Returns (kept stage-A tasks, reserve after shedding, shed counts per group)."""
    reserve = stage_b_reserve(cfg)
    excess = len(tasks) + sum(reserve.values()) - budget
    shed_counts: dict[str, int] = {}
    kept = list(tasks)
    for g in cfg.load_shedding_order:
        if excess <= 0:
            break
        if g == "stage_b_openjaw":
            n = min(excess, reserve["stage_b_openjaw"])
            reserve["stage_b_openjaw"] -= n
        else:
            idx = [i for i, t in enumerate(kept) if t.group == g]
            n = min(excess, len(idx))
            drop = set(idx[len(idx) - n:])
            kept = [t for i, t in enumerate(kept) if i not in drop]
        shed_counts[g] = n
        excess -= n
    return kept, reserve, shed_counts


def openjaw_tasks(cfg: Config, best_origins: list[str], top_pairs: list[DatePair], cap: int) -> list[Task]:
    """Multi-city: Europe out/in airports may differ, California in/out may differ.
    Pure round trips (both sides equal) are excluded. Prioritised by origin rank,
    then date-pair rank, then California combination; capped."""
    ca = cfg.primary_destinations
    opts = stage_a_opts(cfg)
    combos = []
    for pi, p in enumerate(top_pairs):
        for i, o_out in enumerate(best_origins):
            for j, o_ret in enumerate(best_origins):
                for k, (c_in, c_out) in enumerate((a, b) for a in ca for b in ca):
                    if o_out == o_ret and c_in == c_out:
                        continue
                    combos.append(((i + j, pi, k), o_out, o_ret, c_in, c_out, p))
    combos.sort(key=lambda c: c[0])
    tasks = []
    for _, o_out, o_ret, c_in, c_out, p in combos[:cap]:
        off = plan_flight_offset(cfg, cfg.origin(o_out))
        tasks.append(Task("B", "stage_b_openjaw", "multicity", o_out, c_in, c_out, o_ret,
                          str(p.leave_home_date + off * DAY), str(p.home_arrival_date - DAY), p.key, dict(opts)))
    return tasks


def plan_counts(tasks: list[Task]) -> dict[str, int]:
    return dict(Counter(f"{t.stage}:{t.group}" for t in tasks))
