"""Whole run (canary -> A -> B -> C -> D -> outputs) against a fake source, no network.

Catches orchestration bugs (stage order, breakers, budget, failure reporting)
locally instead of on GitHub Actions.
"""
import datetime as dt
import json

import pytest

from tracker import main as tm
from tracker import storage
from tracker.adapters import Adapter, SearchResult


def _seg(fr, to, date, dep, arr, num, nextday=False):
    arr_date = (dt.date.fromisoformat(date) + dt.timedelta(days=nextday)).isoformat()
    return {"marketing_carrier": "LH", "operating_carrier": "LH", "carrier_name": "Lufthansa",
            "flight_number": str(num), "codeshares": [], "from": fr, "to": to,
            "dep_local": f"{date}T{dep}", "arr_local": f"{arr_date}T{arr}", "duration_min": None, "aircraft": None}


class FakeGoogle(Adapter):
    """Round trips and return options work; multi-city always fails like the fork does on GitHub."""
    name = "fake"

    def __init__(self):
        self.calls = []

    def capabilities(self):
        return {}

    def run(self, task, cfg):
        self.calls.append(task.kind)
        if task.kind == "multicity":
            return SearchResult(self.name, "error", error="TypeError: 'int' object is not subscriptable",
                                retryable=False)
        if task.kind == "return_options":
            legs = [[_seg(task.ret_origin, "FRA", task.ret_date, "15:00", "10:30", 455, True),
                     _seg("FRA", task.ret_dest, (dt.date.fromisoformat(task.ret_date) + dt.timedelta(days=1)).isoformat(),
                          "12:00", "13:15", 1400)]]
            price = 21000
        else:
            legs = [[_seg(task.origin, "FRA", task.out_date, "11:00", "12:15", 1401),
                     _seg("FRA", task.dest, task.out_date, "14:00", "17:00", 456)]]
            price = 20000 + 500 * (task.opts.get("exclude_basic_economy") is True)
        offers = [{"price": price + i * 300, "currency": "CZK", "legs": [[{**s, "flight_number": f"{s['flight_number']}{i}"}
                                                                          for s in legs[0]]],
                   "separate_tickets": None, "basic_economy": None, "fare_brand": None, "bag_fee": None}
                  for i in range(4)]
        return SearchResult(self.name, "ok", offers, raw={"fake": True}, source_version="test",
                            query_params={"kind": task.kind}, booking_hint="https://example.invalid")


class FakeFx:
    date, rates, source, error = "2026-10-14", {"CZK": 1.0, "EUR": 25.0}, "test", None

    def __init__(self, *a, **k):
        pass

    def to_czk(self, amount, cur):
        return amount * self.rates[cur]

    def meta(self):
        return {"source": "test"}


@pytest.fixture
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA", tmp_path)
    monkeypatch.setattr(storage, "LAST_SEEN", tmp_path / "state" / "last_seen.json.gz")
    fake = FakeGoogle()
    monkeypatch.setattr(tm, "build_adapters", lambda cfg, quota: {"fake": fake})
    monkeypatch.setattr(tm, "Fx", FakeFx)
    monkeypatch.setattr(tm.time, "sleep", lambda s: None)
    return tmp_path, fake


def test_full_run_offline(offline):
    data, fake = offline
    assert tm.main(["--today", "2026-10-14", "--max-requests", "120", "--source-override", "fake"]) == 0
    meta = json.loads((data / "latest" / "run_meta.json").read_text())
    q = json.loads((data / "state" / "queue.json").read_text())
    by = {}
    for t in q["tasks"]:
        by.setdefault((t["group"], t["status"]), 0)
        by[(t["group"], t["status"])] += 1
    # open-jaw failures are reported, trip only the multi-city breaker, and do not block the other stages
    assert by.get(("stage_b_openjaw", "failed"), 0) > 0
    assert "fake:multicity" in meta["circuit_breakers_open"] and "fake" not in meta["circuit_breakers_open"]
    assert by.get(("return_options", "done"), 0) > 0
    assert by.get(("separate_probe", "done"), 0) > 0
    assert fake.calls.index("return_options") < fake.calls.index("multicity")
    assert meta["errors_total"] > 0 and meta["queries"]["success_rate"] < 1
    summary = json.loads((data / "latest" / "summary_for_agent.json").read_text())
    storage.validate(summary, "summary.schema.json")
    assert any(r["inbound_known"] for r in summary["top_overall"])
    assert meta["requests"]["free_used"] <= 120


def test_same_day_rerun_resumes(offline):
    data, fake = offline
    tm.main(["--today", "2026-10-14", "--max-requests", "40", "--stages", "canary,A", "--source-override", "fake"])
    first = len(fake.calls)
    tm.main(["--today", "2026-10-14", "--max-requests", "40", "--stages", "A", "--source-override", "fake"])
    q = json.loads((data / "state" / "queue.json").read_text())
    done = [t for t in q["tasks"] if t["group"] == "tier1" and t["status"] == "done"]
    assert len(done) == len({t["id"] for t in done}) > 38  # second run continued, no duplicates
    assert len(fake.calls) - first <= 40
