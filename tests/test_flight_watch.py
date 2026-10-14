"""Run: uv run python tests/test_flight_watch.py"""
import gzip
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flight_watch as fw

CFG = {"max_stops": 2, "max_duration_hours": 20, "adults": 1, "max_price_eur": 600}


def test_tfs_matches_url_verified_in_google_ui():
    # Google Flights rendered this as "Vienna to Los Angeles", 2 stops or fewer, under 20 hr.
    assert fw.gf_tfs(CFG, ["VIE"], ["LAX"], "2026-11-26", "2026-12-12") == (
        "Gh8SCjIwMjYtMTEtMjYoAmCwCWoFEgNWSUVyBRIDTEFYGh8SCjIwMjYtMTItMTIoAmCwCWoFEgNMQVhyBRIDVklFQgEBSAGYAQE=")


def test_parser_reads_both_buckets_of_real_page():
    html = gzip.open(Path(__file__).with_name("multi_page.html.gz"), "rt").read()
    fw._client.cache_clear()
    real = fw._client
    fw._client = lambda: SimpleNamespace(get=lambda *a, **k: SimpleNamespace(text=html))
    try:
        offers = fw.gf_fetch("x")
    finally:
        fw._client = real
    assert len(offers) == 11  # 4 "top" + 7 "other"; fast-flights saw only the 7
    best = min(offers, key=lambda o: o["price"])
    assert (best["origin"], best["dest"], best["price"], best["duration_min"]) == ("PRG", "LAX", 501, 950)
    assert best["segments"][0][:3] == ["PRG", "2026-11-26", "LHR"]
    assert len({o["origin"] for o in offers}) > 1 and len({o["dest"] for o in offers}) > 1


def test_top5_prefers_distinct_date_pairs():
    rows = [({"dep": d, "ret": r, "price": p}, None)
            for d, r, p in [("a", "b", 400), ("a", "b", 410), ("c", "d", 450), ("e", "f", 700)]]
    top, under = fw.top5(CFG, rows)
    assert under and [o["price"] for o, _ in top] == [400, 450, 410]


def test_bag_estimate():
    cfg = {"bag": {"fee_eur": 140, "included_airlines": ["Turkish Airlines"]}}
    assert fw.add_bag({"price": 500, "airlines": ["Lufthansa"]}, cfg)["price"] == 640
    assert fw.add_bag({"price": 500, "airlines": ["Turkish Airlines"]}, cfg)["price"] == 500


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
