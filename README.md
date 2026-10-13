# flight-watch

Daily search for round trips from Brno-area airports to California (SerpApi / Google Flights).

- Settings live in `config.yaml`.
- The latest summary is in `REPORT.md`, and history is in `data/`.
- To run it manually, go to **Actions → flight-watch → Run workflow**.
- To preview without spending searches: `python flight_watch.py --dry-run`
- Requires the repository secret `SERPAPI_KEY`.
