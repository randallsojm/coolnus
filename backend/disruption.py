"""Steps 2-3: pick one row of flights.csv as the disruption and attach the arrival airport's limits.

    python disruption.py --list-demos          # the 5 suggested demo rows
    python disruption.py --demo weather_large_hub
    python disruption.py --row 123456          # any row (0-based data row; file line = row + 2)

Writes output/disruption_<row>.json, the input for the rest of the pipeline.

Decision (step 3): the negotiation happens at the ARRIVAL airport, because the
required output is a revised arrival time, gate or flight plan.
"""
import argparse
import json
from pathlib import Path

import pandas as pd

from rules import ROOT, load_rules

FLIGHTS_CSV = ROOT / "data" / "flights.csv"
AIRLINES_CSV = ROOT / "data" / "airlines.csv"
OUTPUT_DIR = ROOT / "output"
CAUSES = ["AIR_SYSTEM_DELAY", "SECURITY_DELAY", "AIRLINE_DELAY", "LATE_AIRCRAFT_DELAY", "WEATHER_DELAY"]

_flights = None


class NotADisruption(Exception):
    pass


def flights():
    global _flights
    if _flights is None:
        _flights = pd.read_csv(FLIGHTS_CSV, low_memory=False,
                               dtype={"ORIGIN_AIRPORT": str, "DESTINATION_AIRPORT": str})
    return _flights


def hhmm_to_min(v):
    if pd.isna(v):
        return None
    v = int(v)
    return (v // 100) * 60 + v % 100


def min_to_hhmm(m):
    if m is None:
        return None
    day, m = divmod(int(round(m)), 1440)
    return f"{m // 60:02d}:{m % 60:02d}" + (f" (+{day}d)" if day else "")


def arrival_day_and_timezone(sched_dep, sched_arr, sched_time):
    """Put the scheduled arrival on the right day and work out the time-zone difference.

    flights.csv times are local at each airport, so arrival - departure = flight time +
    time-zone difference. We pick the arrival day (same, next or previous) that makes that
    difference smallest, which handles overnight flights and short westbound hops whose
    local arrival clock reads earlier than the departure clock.
    Returns (arrival minutes, tz difference destination minus origin, rounded to the hour, raw).
    """
    best = min((sched_arr + k * 1440 for k in (-1, 0, 1)),
               key=lambda a: abs(a - sched_dep - sched_time))
    raw = best - sched_dep - sched_time
    return best, int(round(raw / 60)) * 60, raw


def airline_names():
    """Optional: full airline names from airlines.csv. Without the file, codes are used."""
    if not AIRLINES_CSV.exists():
        return {}
    df = pd.read_csv(AIRLINES_CSV)
    return dict(zip(df.IATA_CODE, df.AIRLINE))


# ------------------------------------------------------------------ step 2: validate and classify
def qualifying_causes(row, R):
    """Which rules_of_play conditions this row meets, e.g. {'WEATHER_DELAY': 42}."""
    out = {}
    for col, threshold in R.disruption_filter.items():
        v = row.get(col)
        if pd.notna(v) and v > threshold:
            out[col] = int(v)
    return out


def classify(row, R):
    q = qualifying_causes(row, R)
    if not q:
        raise NotADisruption("does not meet the rules_of_play filter "
                             + " or ".join(f"{k} > {v:g}" for k, v in R.disruption_filter.items()))
    if row.get("CANCELLED") == 1 or row.get("DIVERTED") == 1:
        raise NotADisruption("flight was cancelled or diverted; nothing to re-plan")
    if len(q) == 2:
        kind = "weather_and_late_aircraft"
        primary = "weather" if q["WEATHER_DELAY"] >= q["LATE_AIRCRAFT_DELAY"] else "late_aircraft"
    else:
        kind = primary = "weather" if "WEATHER_DELAY" in q else "late_aircraft"
    return {"type": kind, "primary": primary, "qualifying": q}


# ------------------------------------------------------------------ step 3: build the disruption record
def build(row_id, R=None):
    R = R or load_rules()
    df = flights()
    if not 0 <= row_id < len(df):
        raise IndexError(f"row {row_id} is outside the file (0..{len(df) - 1})")
    row = df.iloc[row_id]
    cls = classify(row, R)
    arr_ap = R.airport(row.DESTINATION_AIRPORT)          # negotiation airport
    dep_ap = R.airports.get(row.ORIGIN_AIRPORT)          # for reference only

    sched_dep, sched_arr = hhmm_to_min(row.SCHEDULED_DEPARTURE), hhmm_to_min(row.SCHEDULED_ARRIVAL)
    dep_delay = None if pd.isna(row.DEPARTURE_DELAY) else int(row.DEPARTURE_DELAY)
    arr_delay = None if pd.isna(row.ARRIVAL_DELAY) else int(row.ARRIVAL_DELAY)
    num = lambda c: None if pd.isna(row[c]) else int(row[c])
    tz, tz_raw = None, None
    if sched_arr is not None and sched_dep is not None:
        if num("SCHEDULED_TIME") is not None:
            sched_arr, tz, tz_raw = arrival_day_and_timezone(sched_dep, sched_arr, num("SCHEDULED_TIME"))
        elif sched_arr < sched_dep:
            sched_arr += 1440                              # no flight time given: assume next day

    return {
        "row_id": int(row_id),
        "file_line": int(row_id) + 2,
        "negotiation_airport": row.DESTINATION_AIRPORT,
        "focus": "arrival",
        "disruption": cls,
        "flight": {
            "date": f"{int(row.YEAR)}-{int(row.MONTH):02d}-{int(row.DAY):02d}",
            "day_of_week": int(row.DAY_OF_WEEK),
            "airline": row.AIRLINE,
            "airline_name": airline_names().get(row.AIRLINE, row.AIRLINE),
            "flight_number": int(row.FLIGHT_NUMBER),
            "callsign": f"{row.AIRLINE}{int(row.FLIGHT_NUMBER)}",
            "tail": row.TAIL_NUMBER if isinstance(row.TAIL_NUMBER, str) else None,
            "origin": row.ORIGIN_AIRPORT,
            "destination": row.DESTINATION_AIRPORT,
            "distance_mi": int(row.DISTANCE),
            "scheduled_time_min": num("SCHEDULED_TIME"),
            # destination local time = origin local time + this (minutes)
            "timezone_difference_min": tz,
            "timezone_difference_raw_min": tz_raw,
            "air_time_min": num("AIR_TIME"),
            "times": {   # minutes since local midnight on the flight date (each airport's local time)
                "scheduled_departure": sched_dep,
                "scheduled_arrival": sched_arr,
                "recorded_departure": None if dep_delay is None else sched_dep + dep_delay,
                "recorded_arrival": None if arr_delay is None else sched_arr + arr_delay,
            },
            "times_hhmm": {
                "scheduled_departure": min_to_hhmm(sched_dep),
                "scheduled_arrival": min_to_hhmm(sched_arr),
                "recorded_departure": min_to_hhmm(None if dep_delay is None else sched_dep + dep_delay),
                "recorded_arrival": min_to_hhmm(None if arr_delay is None else sched_arr + arr_delay),
            },
            "departure_delay_min": dep_delay,
            "arrival_delay_min": arr_delay,
            "taxi_out_min": num("TAXI_OUT"),
            "taxi_in_min": num("TAXI_IN"),
            "recorded_causes_min": {c.lower(): (num(c) or 0) for c in CAUSES},
        },
        "arrival_airport": arr_ap,
        "origin_airport": dep_ap,
        "rules_overrides": R.overrides,
    }


# ------------------------------------------------------------------ demo rows
DEMOS = {
    # name: (description, filter on the dataframe, sort columns)
    "weather_large_hub": (
        "Weather delay of 1-3 hours into a large hub",
        lambda d, big: (d.DESTINATION_AIRPORT.isin(big)) & d.WEATHER_DELAY.between(60, 180)
        & (d.LATE_AIRCRAFT_DELAY <= 20),
        ["WEATHER_DELAY"]),
    "late_aircraft_large_hub": (
        "Late inbound aircraft (1-2.5 hours) into a large hub, no weather",
        lambda d, big: (d.DESTINATION_AIRPORT.isin(big)) & d.LATE_AIRCRAFT_DELAY.between(60, 150)
        & (d.WEATHER_DELAY <= 15),
        ["LATE_AIRCRAFT_DELAY"]),
    "weather_and_late_aircraft": (
        "Both causes at once into a large hub",
        lambda d, big: (d.DESTINATION_AIRPORT.isin(big)) & d.WEATHER_DELAY.between(30, 120)
        & d.LATE_AIRCRAFT_DELAY.between(30, 120),
        ["WEATHER_DELAY", "LATE_AIRCRAFT_DELAY"]),
    "widebody_long_haul": (
        "Long flight (over 2,500 mi, likely a widebody) into a large hub with weather",
        lambda d, big: (d.DESTINATION_AIRPORT.isin(big)) & (d.DISTANCE > 2500) & (d.WEATHER_DELAY > 15),
        ["WEATHER_DELAY"]),
    "ord_storm_day": (
        "Biggest weather delay into ORD on 2015-11-21 (the day we used before)",
        lambda d, big: (d.DESTINATION_AIRPORT == "ORD") & (d.MONTH == 11) & (d.DAY == 21)
        & (d.WEATHER_DELAY > 15),
        ["WEATHER_DELAY"]),
}


def demo_row(name, R):
    desc, filt, sort = DEMOS[name]
    d = flights()
    big = [k for k, a in R.airports.items() if a["size_class"] == "Large hub"]
    hits = d[filt(d, big) & (d.CANCELLED == 0) & (d.DIVERTED == 0)]
    if hits.empty:
        raise LookupError(f"no row matches demo {name}")
    # deterministic: largest delay first, then lowest row id
    hits = hits.assign(_rid=hits.index).sort_values(sort + ["_rid"], ascending=[False] * len(sort) + [True])
    return int(hits.index[0])


def save(rec):
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"disruption_{rec['row_id']}.json"
    path.write_text(json.dumps(rec, indent=1, default=str))
    return path


def describe(rec):
    f, d, a = rec["flight"], rec["disruption"], rec["arrival_airport"]
    causes = ", ".join(f"{k.lower()} {v} min" for k, v in d["qualifying"].items())
    return (f"row {rec['row_id']}: {f['callsign']} ({f['airline_name']}) {f['origin']} -> {f['destination']} "
            f"on {f['date']}, scheduled arrival {f['times_hhmm']['scheduled_arrival']}, "
            f"arrived {f['arrival_delay_min']} min late | {d['type']} ({causes}) | "
            f"{a['iata']} is a {a['size_class'].lower()}: {a['active_runways']} runways, {a['gates']} gates, "
            f"{a['max_runway_ops_per_hour']} movements/h, {a['spare_gates_at_any_time']} spare gates")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--row", type=int)
    g.add_argument("--demo", choices=sorted(DEMOS))
    g.add_argument("--list-demos", action="store_true")
    args = ap.parse_args()
    R = load_rules()
    if args.list_demos:
        for name, (desc, _, _) in DEMOS.items():
            rec = build(demo_row(name, R), R)
            print(f"{name}: {desc}\n  {describe(rec)}\n")
    else:
        rid = args.row if args.row is not None else demo_row(args.demo, R)
        rec = build(rid, R)
        print(describe(rec))
        print("saved", save(rec))
