"""Config model (pydantic). Loaded from config/config.yaml."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "config.yaml"


class M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Traveller(M):
    adults: int = 1
    cabin: Literal["economy", "premium-economy", "business", "first"] = "economy"
    checked_bags: int = 1
    carry_on: int = 1


class Window(M):
    earliest_leave_home: dt.date
    latest_home_arrival: dt.date
    min_calendar_days_away: int = 14


class Leave(M):
    country: str = "CZ"
    holiday_overrides_add: list[dt.date] = []
    holiday_overrides_remove: list[dt.date] = []
    max_leave_ideal: int = 12
    max_leave_hard: int = 14


class FlightLimits(M):
    max_stops: int = 2
    max_air_duration_min_per_direction: int = 1200


class Planning(M):
    latest_same_day_airport_arrival: dt.time = dt.time(11, 0)


class GroundOut(M):
    mode: str
    minutes: int
    cost_czk: float
    first_airport_arrival_local: dt.time
    estimate: bool = True


class GroundBack(M):
    mode: str
    minutes: int
    cost_czk: float
    last_departure_from_airport_local: dt.time
    estimate: bool = True


class Origin(M):
    iata: str
    enabled: bool = True
    tier: Literal[1, 2, 3]
    ground_out: GroundOut
    ground_back: GroundBack
    notes: str = ""


class Destination(M):
    mode: str
    minutes: int
    minutes_peak: int | None = None
    peak_window_local: str | None = None  # "HH:MM-HH:MM Mon-Fri"
    cost_czk: float = 0
    estimate: bool = True


class Buffers(M):
    airport_before_longhaul_min: int = 150
    airport_before_other_min: int = 120
    longhaul_segment_min: int = 360
    min_selfconnect_min: int = 240
    min_us_entry_connection_min: int = 120
    positioning_slack_min: int = 150
    customs_baggage_min: int = 45


class Limits(M):
    max_requests_per_run: int = 950
    delay_s: tuple[float, float] = (3, 6)
    max_retries: int = 3
    backoff_base_s: float = 30
    circuit_breaker_consecutive_failures: int = 8
    top_offers_per_query: int = 10
    min_offers_before_fallback: int = 3


class StageA(M):
    tier2_slices: int = 3
    tier3_every_days: int = 3
    secondary_every_days: int = 2


class StageB(M):
    top_candidates: int = 40
    openjaw_origins: int = 5
    openjaw_pairs: int = 8
    openjaw_cap: int = 200
    return_options_keep: int = 3


class StageC(M):
    top: int = 5


class StageD(M):
    max_queries: int = 30
    min_saving_pct: float = 15


class Stages(M):
    stage_a: StageA = StageA()
    stage_b: StageB = StageB()
    stage_c: StageC = StageC()
    stage_d: StageD = StageD()


class SearchApiCfg(M):
    monthly_cap: int = 200


class SerpApiCfg(M):
    daily_cap: int = 8
    monthly_cap: int = 240
    hourly_cap: int = 50


SourceName = Literal["fast_flights", "fast_flights_fork", "searchapi", "serpapi"]


class Sources(M):
    chain: list[SourceName]
    google_hl: str = "en"
    google_gl: str = ""
    currency: str = "CZK"
    fork_path: str = ".vendor/fast_flights_fork"
    searchapi: SearchApiCfg = SearchApiCfg()
    serpapi: SerpApiCfg = SerpApiCfg()


class Weights(M):
    price: float = 1 / 3
    stay: float = 1 / 3
    time: float = 1 / 3


class Penalties(M):
    leave_13: float = 0.05
    leave_14: float = 0.10
    separate_tickets: float = 0.25
    bag_unknown: float = 0.10
    us_entry_connection_risky: float = 0.10
    tight_connection: float = 0.10
    hotel_needed_per_night: float = 0.03
    airport_change: float = 0.10
    extra_stop_per_stop: float = 0.03


class Scoring(M):
    weights: Weights = Weights()
    penalties: Penalties = Penalties()


class Fx(M):
    url: str


class Storage(M):
    raw_retention_days: int = 30
    carry_forward_max_stale_days: int = 6
    summary_max_bytes: int = 2_000_000


ShedGroup = Literal["tier3", "sfo_sjc_oak", "tier2", "stage_b_openjaw"]


class Config(M):
    traveller: Traveller
    window: Window
    leave: Leave
    flight_limits: FlightLimits
    planning: Planning = Planning()
    origins: list[Origin]
    destinations: dict[str, Destination]
    primary_destinations: list[str]
    secondary_destinations: list[str]
    hotel_cost_czk_per_night: float = 2000
    bag_fee_estimate_czk_roundtrip: float = 3500
    ca_departure_lead_min: int = 180
    buffers: Buffers = Buffers()
    limits: Limits = Limits()
    load_shedding_order: list[ShedGroup]
    stages: Stages = Stages()
    sources: Sources
    scoring: Scoring = Scoring()
    fx: Fx
    storage: Storage = Storage()
    esta_note: str = ""

    @model_validator(mode="after")
    def _check(self):
        for d in self.primary_destinations + self.secondary_destinations:
            if d not in self.destinations:
                raise ValueError(f"destination {d} has no final-leg entry in `destinations`")
        if self.window.latest_home_arrival <= self.window.earliest_leave_home:
            raise ValueError("window is empty")
        return self

    def origin(self, iata: str) -> Origin:
        return next(o for o in self.origins if o.iata == iata)

    @property
    def enabled_origins(self) -> list[Origin]:
        return [o for o in self.origins if o.enabled]


def load_config(path: Path | str = CONFIG_PATH) -> Config:
    return Config.model_validate(yaml.safe_load(Path(path).read_text()))
