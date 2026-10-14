# flight-tracker

Daily collector of **live Google Flights prices** for Brno-area airports → California
(final destination San Diego), 1 adult, economy, 1 checked bag + 1 carry-on.
Output is machine-readable, pre-sorted data for a downstream LLM agent; there is no human report.

```
config/config.yaml        every tunable value (validated by src/tracker/config.py)
src/tracker/              planner, leave, normalize, score, history, storage, fx, quota, main
src/tracker/adapters/     fast_flights (PyPI), fast_flights_fork (subprocess), searchapi, serpapi
schemas/                  JSON Schemas (schema_version 1.0.0) – every output is validated before writing
data/latest/summary_for_agent.json   <- START HERE (the interpreting agent's input)
data/latest/run_meta.json, data/latest/log.txt
data/runs/YYYY-MM-DD/itineraries.jsonl   all normalized offers fetched that day
data/runs/YYYY-MM-DD/raw/{source}_{hash}.json.gz   raw responses (pruned after 30 days)
data/history/price_history.csv, data/history/route_daily_min.csv
data/state/queue.json (resumable query queue), data/state/last_seen.json.gz (carry-forward)
data/quota.json, data/fx.json
data/legacy/              outputs of the previous flight_watch.py (REPORT.md, state.json) – no longer written
```

## Running

```bash
uv sync
```

```bash
uv pip install --target .vendor/fast_flights_fork "git+https://github.com/Mohamed3on/flights@d1620895da9d770efbb7196b67c95c47dc4a3ab1"
```

```bash
uv run tracker --dry-run
```

```bash
uv run --env-file .env tracker --max-requests 50
```

```bash
uv run pytest -q
```

Options: `--stages all|canary,A,B,C,D`, `--max-requests N`, `--source-override fast_flights_fork,serpapi`,
`--today YYYY-MM-DD`. Live smoke test: `TRACKER_LIVE=1 uv run pytest tests/test_live.py`.

GitHub Actions: `daily.yml` (07:00 UTC + manual dispatch with the same inputs) commits `data/` as
`data: run YYYY-MM-DD`; `tests.yml` runs pytest. Set repo variable `RUNS_ON` to a JSON label list
(e.g. `["self-hosted","residential"]`) to move the daily job to a self-hosted runner with a
residential IP. Secrets `SERPAPI_KEY` / `SEARCHAPI_KEY` are optional. `local/run.sh` + the launchd
plist run the same thing from a Mac.

## Sources and fallbacks (audible)

Per query, `sources.chain` is tried in order: `fast_flights` → `fast_flights_fork` → `searchapi` → `serpapi`.

| source | what it is | notes |
|---|---|---|
| `fast_flights` | PyPI fast-flights 3.1.0 query builder + fetcher | no EU consent cookie; page results only |
| `fast_flights_fork` | Mohamed3on/flights (pinned commit), run in a subprocess | consent cookie + streams the full result set. Same package name and a conflicting protobuf descriptor as upstream, hence the isolated `.vendor/` install |
| `searchapi` | SearchApi.io google_flights, `checked_bags=1` sent | only with `SEARCHAPI_KEY`; monthly cap. In tests (Oct 2026, gl=cz and gl=us) the bag did not change any price, so its bag status is `unknown` |
| `serpapi` | SerpApi google_flights, `exclude_basic=true` (`bags` = carry-on only) | only with `SERPAPI_KEY`; 8/day, 240/month, 50/hour |

A free source's result is accepted when it is `ok` with ≥ `limits.min_offers_before_fallback`
offers; empty results, consent/CAPTCHA pages, errors or suspiciously few offers fall through to the
next source. Paid sources are only used as fallbacks for Tier 1 and the canary, and for Stage C.
Each source has retries with exponential backoff and a circuit breaker. `run_meta.json` records
requests per source and stage, outcomes per source, which source produced results
(`sources_used_for_results`), the final `source_chain`, `degraded` sources and the capability report.
Every itinerary carries `provenance.source` / `source_version`.

Both fast-flights variants are used only for building the `tfs` query and fetching; results are
parsed by `adapters/gflights.parse_payload` (both result buckets, flight numbers, layovers) because
fast-flights' parser drops flight numbers and reads only "other flights". Return options are fetched by
adding the selected outbound segments to the query (FlightData field 4), which fast-flights does not model.

**Canary**: MUC→SAN round trip on a Tuesday. Failure on the primary source marks it degraded, promotes
the source that worked to the front of the chain and opens/updates the GitHub issue "tracker degraded".
The canary is repeated with `checked_bags=0`: if no itinerary gets more expensive with the bag,
Google ignored the checked-bag filter (it does where its UI has no such filter) →
`run_meta.bag_filter_effective=false` and prices are **not** marked bag-inclusive.

## Search plan (deterministic per date)

67 date pairs (leave home D ≥ 25 Nov, home H ≤ 20 Dec, ≥ 14 calendar days, ≤ 14 leave days); `pair_tier="longest"` = latest H per D.
Query dates: outbound = D (D+1 for origins whose first ground arrival is after
`planning.latest_same_day_airport_arrival`), California departure = H − 1.

| stage | what | default volume |
|---|---|---|
| canary | MUC→SAN, with and without checked bag | 2 |
| A tier1 | MUC/VIE/WAW × SAN/LAX × all pairs, daily | 402 |
| A tier2 | PRG/BUD/BTS × SAN/LAX × 1/3 of pairs (`day_of_year % 3`) | ~134 |
| A tier3 | 4 of 12 origins per day × SAN/LAX × longest pairs | 104 |
| A sfo_sjc_oak | Tier 1 × SFO/SJC/OAK × half the longest pairs per day | ~58 |
| B | open-jaw (best 5 origins² × SAN/LAX² × top 8 pairs, cap 200); return legs for top 40; standard-fare requery | ≤ 280 |
| C | paid re-price of top 5 (if a key exists) | ≤ 5 |
| D | separate-ticket probe on Tier 1 routes | ≤ 30 |

When the plan exceeds `limits.max_requests_per_run` (950), groups are shed in
`load_shedding_order` (`tier3`, `sfo_sjc_oak`, `tier2`, `stage_b_openjaw`). The query queue is
persisted (`data/state/queue.json`): a same-day rerun skips finished queries and merges results.
Routes not searched today are carried forward from `data/state/last_seen.json.gz` with
`provenance.stale_days > 0` (max `storage.carry_forward_max_stale_days`).
Google's date grid / price graph is not exposed by either fast-flights variant, so no pre-ranking is
done (`capability_report.*.date_grid=false`).

## summary_for_agent.json

| key | content |
|---|---|
| `run_metadata` | same as `run_meta.json`: timings, requests per source/stage, success rate, errors, degraded sources, capability report, `bag_filter_effective`, quotas, fx, assumptions (ESTA, estimates) |
| `counts` | records total / eligible protected / fresh today |
| `top_overall` (30) | by `scoring.final_score` |
| `top_by_price` / `top_by_stay` / `top_by_time` (15 each) | by total cost / hours in San Diego area / door-to-door minutes |
| `pareto_front` | non-dominated over (cost ↓, stay ↑, time ↓), cheapest first, max 40 |
| `best_per_origin`, `best_per_date_pair`, `best_per_trip_length` (calendar_days_away), `best_per_california_airport` | best final score per key |
| `best_open_jaw` | best open-jaw itineraries |
| `flagged_cheaper_separate_tickets` | Stage D: ≥ 15 % cheaper than the best protected price for the same route and dates; separate tickets, own risk |
| `price_vs_length_matrix` | `{origin: {calendar_days_away: {min_total_trip_cost_czk, itinerary_id}}}` |
| `new_lows_today` | itineraries (`new_low_kind="itinerary"`) or route × date pair (`"route_pair"`) at their lowest observed price |

Lists contain only eligible itineraries (no constraint violation) with `separate_tickets != true`.
Each item is a full itinerary plus `price_history` `{first_seen, times_seen, min, median, last,
delta_vs_yesterday, trend_7d}` (CZK, `price_total_czk`; `trend_7d` = last − first price in the
last 7 days, negative = falling). If the file would exceed 2 MB, per-key maps are reduced to
`{itinerary_id, total_trip_cost_czk, final_score}` references (look them up in `itineraries.jsonl`).

## Itinerary fields

- `itinerary_id` – stable hash of all segments (carrier + flight number + local departure date).
  While the inbound is unknown, the hash uses the planned return route and date instead.
- `trip_type` round_trip | open_jaw; `origin`/`destination` = outbound, `return_origin`/`return_destination` = inbound (California → Europe).
- `pair_key` – planned `D_H`; `inbound_known` – false for Stage A records (Google lists outbounds first, priced as the full round trip).
- `price`: `price_total_original`, `currency_original`, `price_total_czk`, `price_total_eur`, `fx_rate_eur_czk`, `fx_date` (ČNB),
  `flight_price_incl_bag_czk` (= price + `bag_fee_estimate_czk`), `price_includes_checked_bag` (true/false/"unknown"),
  `bag_certainty`:
  - `filter_applied_in_query` – checked_bags=1 was sent and the canary proved Google applies it,
  - `fee_listed_by_source` – the source listed a bag fee,
  - `standard_fare_assumed_included` – basic economy excluded; long-haul standard economy normally includes 1 bag,
  - `unknown` – a bag fee estimate (`bag_fee_estimate_czk_roundtrip`) is added and a penalty applied.
  `basic_economy`, `fare_brand`, `separate_tickets` (true/false/"unknown"), `self_transfer`, `price_basis`
  (`as_listed`, `standard_fare`, `basic_fare_plus_bag`, `listed_fare_incl_bag`, `as_listed_no_standard_match`),
  `basic_fare_czk` / `standard_fare_czk` after Stage B requery. Stage D adds `separate_tickets_basis`, `saving_vs_best_protected_czk`.
- `legs.outbound|inbound`: `segments[]` {marketing_carrier, operating_carrier (Google exposes only the operating flight; codeshares in `codeshares`),
  carrier_name, flight_number, from, to, dep_local, arr_local, dep_utc, arr_utc, duration_min, aircraft},
  `layovers[]` {airport, airport_dep, minutes, overnight, airport_change, intra_schengen}, `stops`, `air_duration_min` (first departure → last arrival).
- `flags`: `us_entry_airport`, `us_entry_connection_min`, `us_entry_connection_risky` (< 120), `tight_connection`
  (< 60 intra-Schengen, < 90 otherwise), `overnight_layover`, `airport_change`, `red_eye`, `late_night_pickup`
  (LAX arrival after 22:00 or leaving San Diego before 05:00 for LAX; info only), `constraint_violations`
  (leaves_home_before_window, home_after_window, too_few_calendar_days, leave_days_over_hard_limit, too_many_stops, air_duration_over_limit).
- `ground`: `ground_out` {mode, minutes, cost_czk, leave_home_local, hotel_night_before}, `ground_back` {…, arrive_home_local, hotel_night_after}, `hotel_nights`, `hotel_cost_czk`. All estimates.
- `final_leg_to_san_diego` / `final_leg_from_san_diego` {mode, minutes, cost_czk, estimate, peak}. LAX uses `minutes_peak` when the arrival
  (or the San Diego departure = flight departure − 180 min − drive) falls in the peak window.
- `totals`: `door_to_door_out_min` (leave home → arrive San Diego), `door_to_door_back_min`, `total_trip_cost_czk`
  (flight incl. bag + ground both ways + hotels + final legs both ways; ESTA not included), `estimated_inbound`.
- `calendar`: `leave_home_date`, `home_arrival_date`, `leave_days_used` (CZ working days incl. both ends), `calendar_days_away`,
  `nights_in_san_diego_area`, `hours_in_san_diego_area`, `san_diego_arrival_local`, `san_diego_departure_local`.
- `scoring`: see below. `provenance`: source, source_version, query_params (no secrets), fetched_at_utc, run_id, stage, task_group,
  query_out_date, query_ret_date, booking_hint (Google Flights URL), booking_token, results_complete, stale_days, `validation` (Stage C:
  paid price, discrepancy_czk).

Calendar rules: the traveller leaves home on the flight day if the first ground connection reaches the airport by
departure − check-in buffer (150 min long-haul first segment, else 120) − positioning slack (150); otherwise the evening
before (+1 hotel night). Home arrival is the landing day if landing + 45 min catches the last connection home and arrives
before midnight; otherwise the next day (+1 hotel night). Records without the inbound leg assume a California departure
on H − 1 at 15:00 and home arrival on H at 20:00 (`totals.estimated_inbound=true`).

## Scoring

Within today's candidate set (fresh + carried forward, eligible only), min-max normalised to 0–1:
`price_score` (lower `total_trip_cost_czk` better), `stay_score` (more `hours_in_san_diego_area`),
`time_score` (lower door-to-door out + back). `final_score = w_price·price + w_stay·stay + w_time·time − penalties`
(weights ⅓ each). Penalties (config `scoring.penalties`): leave_13 0.05, leave_14 0.10, separate_tickets 0.25,
bag_unknown 0.10, us_entry_connection_risky 0.10, tight_connection 0.10, hotel 0.03/night, airport_change 0.10,
extra stop 0.03 per stop beyond 1 per direction. Records with constraint violations get `final_score=null`, `eligible=false`.
`pareto_optimal` marks the (cost, stay, time) front.

## Tuning

Everything lives in `config/config.yaml`: add/disable origins or change tiers and ground estimates, `leave.holiday_overrides_*`,
`stages.*` volumes, `limits.*` (budget, delays, retries, breaker), `load_shedding_order`, `sources.chain` / `google_gl`
(set `US` to try the US UI that has a checked-bag filter), weights and penalties, `bag_fee_estimate_czk_roundtrip`,
`hotel_cost_czk_per_night`, final-leg times. Invalid values fail fast at startup.
