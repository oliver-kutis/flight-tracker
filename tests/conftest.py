import gzip
import json
from pathlib import Path

import pytest

from tracker.adapters import SearchResult
from tracker.adapters.gflights import classify_html, parse_payload
from tracker.config import load_config
from tracker.leave import non_working_days
from tracker.normalize import Ctx
from tracker.planner import Task

FIX = Path(__file__).with_name("fixtures")


class FakeFx:
    date = "2026-10-14"
    rates = {"CZK": 1.0, "EUR": 25.0, "USD": 23.0}

    def to_czk(self, amount, cur):
        return amount * self.rates[cur]


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def ctx(cfg):
    return Ctx(cfg, non_working_days(cfg), FakeFx(), "test-run", True)


@pytest.fixture
def google_payload():
    html = gzip.open(FIX / "google_multi_page.html.gz", "rt").read()
    status, payload, err = classify_html(html)
    assert status == "ok", err
    return payload


@pytest.fixture
def google_offers(google_payload):
    offers = parse_payload(google_payload)
    for o in offers:
        o["currency"] = "EUR"  # the fixture page was fetched with curr=EUR
    return offers


def rt_task(origin="PRG", dest="LAX", out="2026-11-26", ret="2026-12-12", pair="2026-11-26_2026-12-13", **opts):
    return Task("A", "tier2", "roundtrip", origin, dest, dest, origin, out, ret, pair,
                {"checked_bags": 1, "hide_separate_and_self_transfer": True, "exclude_basic_economy": False, **opts})


def result(source="fast_flights", offers=()):
    return SearchResult(source, "ok", list(offers), source_version="test", query_params={"tfs": "x"},
                        booking_hint="https://www.google.com/travel/flights/search?tfs=x")


def load_json(name):
    return json.loads((FIX / name).read_text())
