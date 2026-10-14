# flight-watch

Daily search for round trips from Brno-area airports to California, straight from Google Flights (no API key; SerpApi optional via `source: serpapi`).

- Settings live in `config.yaml` (incl. the checked-bag estimate: Google no longer prices checked bags).
- The latest summary is in `REPORT.md`, and history is in `data/`.
- `uv run flight_watch.py --dry-run` previews the plan, `--probe` runs 2 test searches.
- Tests: `uv run python tests/test_flight_watch.py`
- Runs daily via GitHub Actions; local fallback (launchd on macOS): see `local/io.zaitra.flight-watch.plist`.
