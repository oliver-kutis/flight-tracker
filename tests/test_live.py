"""Live smoke test: TRACKER_LIVE=1 uv run pytest tests/test_live.py"""
import os

import pytest

from tracker.adapters.gflights import FastFlights, FastFlightsFork
from tracker.planner import canary_task, date_pairs

pytestmark = [pytest.mark.live, pytest.mark.skipif(not os.environ.get("TRACKER_LIVE"), reason="TRACKER_LIVE not set")]


@pytest.mark.parametrize("adapter", ["upstream", "fork"])
def test_canary_live(cfg, adapter):
    ad = FastFlights() if adapter == "upstream" else FastFlightsFork(cfg)
    ok, why = ad.available()
    if not ok:
        pytest.skip(why)
    res = ad.search_roundtrip(canary_task(cfg, date_pairs(cfg)), cfg)
    assert res.status == "ok", res.error
    assert res.offers and all(o["legs"][0][0]["from"] == "MUC" for o in res.offers)
