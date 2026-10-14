"""Leave-day / calendar arithmetic and the ground-leg rules.

All European origins in the config share Brno's time zone (CET/CEST), so
ground-leg arithmetic is done on naive local datetimes.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

import holidays as holidays_lib

from .config import Buffers, Config, Origin

DAY = dt.timedelta(days=1)
EARLIEST_GROUND_DEPARTURE = dt.time(4, 0)  # nothing leaves the airport for Brno before this
NEXT_DAY_GROUND_DEPARTURE = dt.time(6, 0)  # after a hotel night
EVENING_BEFORE_ARRIVAL = dt.time(21, 0)    # arrive in the airport city the evening before


@lru_cache(maxsize=8)
def _country_holidays(country: str, years: tuple[int, ...]) -> frozenset[dt.date]:
    return frozenset(holidays_lib.country_holidays(country, years=years).keys())


def non_working_days(cfg: Config) -> frozenset[dt.date]:
    w, lv = cfg.window, cfg.leave
    years = tuple(range(w.earliest_leave_home.year - 1, w.latest_home_arrival.year + 2))
    days = set(_country_holidays(lv.country, years)) | set(lv.holiday_overrides_add)
    return frozenset(days - set(lv.holiday_overrides_remove))


def leave_days(start: dt.date, end: dt.date, holidays: frozenset[dt.date]) -> int:
    """CZ working days (Mon-Fri minus holidays) in [start, end] inclusive."""
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            n += 1
        d += DAY
    return n


def calendar_days_away(start: dt.date, end: dt.date) -> int:
    return (end - start).days + 1


def leave_home(flight_dep_local: dt.datetime, first_segment_min: int, origin: Origin,
               buffers: Buffers) -> tuple[dt.datetime, int]:
    """(leave_home_local, hotel_nights) for the outbound ground leg.

    The traveller must be at the airport by departure - check-in buffer -
    positioning slack. If the first ground connection of the flight day
    arrives later, they travel the evening before and sleep near the airport.
    """
    buffer = (buffers.airport_before_longhaul_min
              if first_segment_min > buffers.longhaul_segment_min
              else buffers.airport_before_other_min)
    required = flight_dep_local - dt.timedelta(minutes=buffer + buffers.positioning_slack_min)
    g = origin.ground_out
    first = dt.datetime.combine(flight_dep_local.date(), g.first_airport_arrival_local)
    if required.date() == flight_dep_local.date() and first <= required:
        return required - dt.timedelta(minutes=g.minutes), 0
    eve = dt.datetime.combine(flight_dep_local.date() - DAY, EVENING_BEFORE_ARRIVAL)
    return eve - dt.timedelta(minutes=g.minutes), 1


def arrive_home(landing_local: dt.datetime, origin: Origin,
                buffers: Buffers) -> tuple[dt.datetime, int]:
    """(arrive_home_local, hotel_nights) for the inbound ground leg."""
    g = origin.ground_back
    ready = landing_local + dt.timedelta(minutes=buffers.customs_baggage_min)
    minutes = dt.timedelta(minutes=g.minutes)
    if ready.date() == landing_local.date():
        if ready.time() < EARLIEST_GROUND_DEPARTURE:  # wait at the airport for the first bus
            return dt.datetime.combine(ready.date(), EARLIEST_GROUND_DEPARTURE) + minutes, 0
        if ready.time() <= g.last_departure_from_airport_local and (ready + minutes).date() == ready.date():
            return ready + minutes, 0
    nxt = dt.datetime.combine(landing_local.date() + DAY, NEXT_DAY_GROUND_DEPARTURE)
    return nxt + minutes, 1


def plan_flight_offset(cfg: Config, origin: Origin) -> int:
    """0 = query the outbound flight on the leave-home day D, 1 = on D+1
    (far origins: the traveller leaves home D evening)."""
    return int(origin.ground_out.first_airport_arrival_local > cfg.planning.latest_same_day_airport_arrival)
