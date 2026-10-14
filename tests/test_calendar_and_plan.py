import datetime as dt

import pytest

from tracker.leave import arrive_home, calendar_days_away, leave_days, leave_home, non_working_days
from tracker.planner import date_pairs, openjaw_tasks, shed, stage_a

D = dt.date.fromisoformat


@pytest.mark.parametrize("start,end,leave,cal", [
    ("2026-12-03", "2026-12-20", 12, 18),  # Thu -> Sun
    ("2026-11-28", "2026-12-15", 12, 18),  # Sat -> Tue
    ("2026-11-26", "2026-12-13", 12, 18),  # Thu -> Sun
    ("2026-12-01", "2026-12-20", 14, 20),  # Tue -> Sun
    ("2026-11-25", "2026-12-13", 13, 19),  # Wed -> Sun
])
def test_leave_days(cfg, start, end, leave, cal):
    hol = non_working_days(cfg)
    assert leave_days(D(start), D(end), hol) == leave
    assert calendar_days_away(D(start), D(end)) == cal


def test_holiday_override(cfg):
    cfg.leave.holiday_overrides_add = [D("2026-12-07")]
    assert leave_days(D("2026-12-03"), D("2026-12-20"), non_working_days(cfg)) == 11
    cfg.leave.holiday_overrides_add = []
    cfg.leave.holiday_overrides_remove = [D("2026-11-17")]  # a real CZ holiday, worked anyway
    assert leave_days(D("2026-11-16"), D("2026-11-17"), non_working_days(cfg)) == 2


def test_pair_count_and_boundaries(cfg):
    pairs = date_pairs(cfg)
    assert len(pairs) == 67
    keys = {(p.leave_home_date, p.home_arrival_date) for p in pairs}
    assert (D("2026-12-07"), D("2026-12-20")) in keys      # 14 calendar days: included
    assert (D("2026-12-08"), D("2026-12-20")) not in keys  # 13 calendar days: excluded
    assert min(p.leave_home_date for p in pairs) == D("2026-11-25")
    assert max(p.leave_home_date for p in pairs) == D("2026-12-07")
    assert max(p.home_arrival_date for p in pairs) <= D("2026-12-20")
    assert all(p.calendar_days_away >= 14 and p.leave_days <= 14 for p in pairs)
    longest = [p for p in pairs if p.pair_tier == "longest"]
    assert len(longest) == len({p.leave_home_date for p in pairs}) == 13


def test_evening_before_departure(cfg):
    vie = cfg.origin("VIE")
    # 06:00 short-haul: must be at VIE by 01:30, first bus arrives 04:00 -> night before
    leave, hotel = leave_home(dt.datetime(2026, 12, 1, 6, 0), 90, vie, cfg.buffers)
    assert hotel == 1 and leave.date() == D("2026-11-30")
    # 10:00 departure: at VIE by 05:30, same day, leave home 05:30 - 115 min
    leave, hotel = leave_home(dt.datetime(2026, 12, 1, 10, 0), 90, vie, cfg.buffers)
    assert hotel == 0 and leave == dt.datetime(2026, 12, 1, 3, 35)
    # long-haul first segment uses the bigger buffer (150)
    leave, _ = leave_home(dt.datetime(2026, 12, 1, 10, 0), 600, vie, cfg.buffers)
    assert leave == dt.datetime(2026, 12, 1, 3, 5)


def test_next_day_home(cfg):
    vie = cfg.origin("VIE")
    home, hotel = arrive_home(dt.datetime(2026, 12, 14, 20, 0), vie, cfg.buffers)
    assert hotel == 0 and home == dt.datetime(2026, 12, 14, 22, 40)
    home, hotel = arrive_home(dt.datetime(2026, 12, 14, 22, 30), vie, cfg.buffers)  # misses 23:00 bus
    assert hotel == 1 and home.date() == D("2026-12-15")
    home, hotel = arrive_home(dt.datetime(2026, 12, 14, 1, 0), vie, cfg.buffers)  # waits for 04:00
    assert hotel == 0 and home == dt.datetime(2026, 12, 14, 5, 55)


def test_stage_a_counts_and_rotation(cfg):
    pairs = date_pairs(cfg)
    days = [D("2026-10-14") + dt.timedelta(days=i) for i in range(6)]
    plans = [stage_a(cfg, pairs, d) for d in days]
    for p in plans:
        groups = {g: sum(t.group == g for t in p) for g in ("tier1", "tier2", "tier3", "sfo_sjc_oak")}
        assert groups["tier1"] == 3 * 2 * 67
        assert 130 <= groups["tier2"] <= 138
        assert groups["tier3"] == 4 * 2 * 13
        assert 50 <= groups["sfo_sjc_oak"] <= 63
    # tier2: three consecutive days cover every pair exactly once per route
    t2 = [{(t.origin, t.dest, t.pair_key) for t in p if t.group == "tier2"} for p in plans[:3]]
    assert not (t2[0] & t2[1]) and len(t2[0] | t2[1] | t2[2]) == 3 * 2 * 67
    # tier3: each origin every third day
    t3 = [{t.origin for t in p if t.group == "tier3"} for p in plans[:3]]
    assert len(t3[0] | t3[1] | t3[2]) == 12 and not (t3[0] & t3[1])
    # deterministic
    assert [t.id for t in stage_a(cfg, pairs, days[0])] == [t.id for t in plans[0]]


def test_load_shedding_order(cfg):
    tasks = stage_a(cfg, date_pairs(cfg), D("2026-10-14"))
    kept, reserve, shed_counts = shed(cfg, tasks, 950)
    assert list(shed_counts) == ["tier3"] and len(kept) + sum(reserve.values()) == 950
    kept, reserve, shed_counts = shed(cfg, tasks, 500)
    assert list(shed_counts) == ["tier3", "sfo_sjc_oak", "tier2", "stage_b_openjaw"]
    assert {t.group for t in kept} == {"tier1"}
    assert reserve["stage_b_openjaw"] < 200


def test_openjaw_generator(cfg):
    pairs = date_pairs(cfg)[:8]
    tasks = openjaw_tasks(cfg, ["MUC", "VIE", "WAW", "PRG", "BUD"], pairs, 200)
    assert len(tasks) == 200
    assert all(not (t.ret_dest == t.origin and t.ret_origin == t.dest) for t in tasks)
    assert all(t.kind == "multicity" for t in tasks)
    assert len({t.id for t in tasks}) == 200
    assert tasks[0].origin == "MUC" and tasks[0].ret_dest == "MUC"  # best origin first
    assert [t.id for t in openjaw_tasks(cfg, ["MUC", "VIE", "WAW", "PRG", "BUD"], pairs, 200)] == [t.id for t in tasks]
