"""Generate a scenario file from one flights.csv row, ready for the team to edit.

    python make_scenario.py --row 85405 --phase airborne
    python make_scenario.py --demo late_aircraft_large_hub --phase on_ground_at_origin
    python make_scenario.py --examples          # the two example scenarios in scenarios/

Fields taken from the row: flight identity, origin, destination, schedule, delays,
taxi and air times, time-zone difference. Everything else is EXAMPLE CONTEXT generated
from the workbook limits and the aircraft catalogue, so the file loads straight away.
Replace it with your own data; the "_generated_notes" list says what was assumed.
Same row + phase always gives the same file.
"""
import argparse
import json
import math
import random
from pathlib import Path

import pandas as pd

import disruption as DS
from rules import ROOT, load_rules
from scenario import GATE_SIZES, fmt_time, load_catalogue, load_scenario, summary

AIRPORTS_CSV = ROOT / "data" / "airports.csv"
SCENARIO_DIR = ROOT / "scenarios"
OTHER_WINDOW_MIN = 20          # other traffic within +/- this of the disrupted flight's landing
OTHER_LOAD = 0.8               # share of the workbook's runway movements per hour
CONNECTION_OFFSETS = (50, 75, 110)
MIN_CONNECTION_MIN = 45
WEATHER_LEAD_MIN = 20         # weather hold starts this long before DF's planned landing

RUNWAY_LAYOUT = {   # by workbook size class (runway count must match active_runways)
    "Large hub": [("R1", "arrivals"), ("R2", "arrivals"), ("R3", "departures"), ("R4", "departures")],
    "Medium hub": [("R1", "arrivals"), ("R2", "departures"), ("R3", "mixed")],
    "Small hub": [("R1", "arrivals"), ("R2", "departures")],
    "Non-hub": [("R1", "mixed")],
}
TRAFFIC_MIX = {
    "Large hub": [("CRJ9", 22), ("E175", 20), ("A320", 20), ("B738", 20), ("A321", 8), ("B763", 5), ("B772", 5)],
    "Medium hub": [("CRJ9", 20), ("E175", 20), ("A320", 25), ("B738", 25), ("A321", 10)],
    "Small hub": [("CRJ2", 20), ("CRJ9", 25), ("E175", 20), ("A320", 15), ("B738", 20)],
    "Non-hub": [("B190", 40), ("CRJ2", 40), ("CRJ9", 20)],
}
REGIONAL_AIRLINES = ["OO", "EV", "MQ"]
MAINLINE_AIRLINES = ["UA", "AA", "DL", "WN", "B6", "AS", "NK"]


def df_aircraft_type(airline, distance):
    """Starter rule for the disrupted flight's aircraft; change it in the file if you know better."""
    if airline in ("OO", "EV"):
        return "CRJ9"
    if airline == "MQ":
        return "E175"
    if distance > 2500:
        return {"HA": "A333"}.get(airline, "B772" if airline in ("UA", "AA", "DL") else "A321")
    return "B738" if airline in ("AA", "WN", "DL", "AS", "US") else "A320"


def nearest_alternate(R, dest):
    """Nearest hub airport at least 50 mi away, using airports.csv coordinates (optional file)."""
    if not AIRPORTS_CSV.exists():
        return None
    ap = pd.read_csv(AIRPORTS_CSV).dropna(subset=["LATITUDE", "LONGITUDE"]).set_index("IATA_CODE")
    if dest not in ap.index:
        return None
    lat0, lon0 = math.radians(ap.loc[dest, "LATITUDE"]), math.radians(ap.loc[dest, "LONGITUDE"])
    best = None
    for code, a in R.airports.items():
        if code == dest or a["size_class"] == "Non-hub" or code not in ap.index:
            continue
        lat, lon = math.radians(ap.loc[code, "LATITUDE"]), math.radians(ap.loc[code, "LONGITUDE"])
        h = math.sin((lat - lat0) / 2) ** 2 + math.cos(lat0) * math.cos(lat) * math.sin((lon - lon0) / 2) ** 2
        dist = 2 * 3958.8 * math.asin(math.sqrt(h))
        if dist >= 50 and (best is None or dist < best[1]):
            best = (code, round(dist))
    return {"airport": best[0], "distance_mi": best[1]} if best else None


def fuel_kg(spec, trip_min, R, alternate_mi=None):
    """trip + 5% contingency + alternate + spare holding + final reserve (workbook rules)."""
    cruise, hold = spec["cruise_burn_kg_h"] / 60, spec["holding_burn_kg_h"] / 60
    trip = trip_min * cruise
    alt = 0.0
    if alternate_mi:
        alt = (alternate_mi / 1.15078) / spec["cruise_speed_kt"] * 60 * cruise
    return round(trip * (1 + R.fuel["contingency_pct_of_trip"]) + alt
                 + (R.fuel["spare_holding_min"] + R.fuel["final_reserve_min"]) * hold)


def make(row_id, phase, R=None, small=False):
    """small=True: simulator demo traffic, 4 arrivals just ahead of DF + 2 connecting departures."""
    R = R or load_rules()
    cat = load_catalogue()
    rec = DS.build(row_id, R)
    rng = random.Random(f"{row_id}-{phase}")
    f, ap = rec["flight"], rec["arrival_airport"]
    t = f["times"]
    tz = f["timezone_difference_min"] or 0
    taxi_in = f["taxi_in_min"] or 8
    taxi_out = f["taxi_out_min"] or 15
    air_time = f["air_time_min"] or max(10, (f["scheduled_time_min"] or 60) - taxi_in - taxi_out)
    sched_dep_dest = t["scheduled_departure"] + tz          # origin clock -> arrival-airport clock
    notes = [
        "All times are in the arrival airport's local time; origin times were shifted by the "
        f"row's time-zone difference ({tz:+d} min).",
        "Flight times are runway times (landing / take-off).",
    ]

    df_type = df_aircraft_type(f["airline"], f["distance_mi"])
    spec = cat[df_type]
    notes.append(f"Aircraft {df_type} chosen by a starter rule from airline {f['airline']} and "
                 f"distance {f['distance_mi']} mi; change it if you know the real type.")
    alternate = nearest_alternate(R, f["destination"])
    notes.append("Alternate = nearest hub airport at least 50 mi away (from airports.csv)."
                 if alternate else "No alternate found: fill in disrupted_flight.alternate before loading.")

    dfl = {"aircraft_type": df_type, "taxi_in_min": taxi_in,
           "crew_report_time": fmt_time(sched_dep_dest - 60),
           "alternate": alternate or {"airport": None, "distance_mi": None},
           "route_turbulence": "none"}
    notes.append("Crew report time = scheduled departure - 60 min (assumption).")
    causes = f["recorded_causes_min"]
    weather_min, late_min = causes["weather_delay"], causes["late_aircraft_delay"]
    unmodelled = {k: v for k, v in causes.items() if k not in ("weather_delay", "late_aircraft_delay") and v}
    if phase == "airborne":
        # Late-aircraft delay happened before take-off, so it is already in the plan.
        # Weather comes from the weather_hold effect below (not added here: no double counting).
        landing = t["scheduled_arrival"] + late_min - taxi_in
        mtd = max(5, min(30, air_time))
        start = landing - mtd
        dfl.update(minutes_to_destination=mtd, planned_arrival=fmt_time(landing),
                   fuel_on_board_kg=fuel_kg(spec, mtd, R, alternate and alternate["distance_mi"]))
        notes.append(f"Planned landing = scheduled arrival + late-aircraft delay ({late_min} min) - taxi-in; "
                     f"the negotiation starts {mtd} min before landing.")
    else:
        takeoff = sched_dep_dest + taxi_out
        landing = takeoff + air_time
        start = sched_dep_dest - 30
        dfl.update(planned_departure=fmt_time(takeoff), planned_arrival=fmt_time(landing),
                   fuel_on_board_kg=fuel_kg(spec, air_time, R, alternate and alternate["distance_mi"]))
        notes.append("Planned take-off = scheduled departure + taxi-out; landing = take-off + air time; "
                     "the negotiation starts 30 min before scheduled departure.")
    notes.append(f"Fuel = trip x (1 + {R.fuel['contingency_pct_of_trip']:.0%}) + alternate + "
                 f"{R.fuel['spare_holding_min']:g} min spare holding + {R.fuel['final_reserve_min']:g} min final "
                 "reserve, using the catalogue burn rates.")

    # ------------------------------------------------------------ effects from the row's causes
    # Weather never changes runway capacity. While it is overhead no arrival can land; it lasts the
    # row's WEATHER_DELAY minutes and starts WEATHER_LEAD_MIN before DF's planned landing, so
    # aircraft planned just ahead of DF are held too and a queue forms when it clears.
    effects = []
    if weather_min > 0:
        df_land = landing + (late_min if phase == "on_ground_at_origin" else 0)
        w0 = df_land - WEATHER_LEAD_MIN
        effects.append({"type": "weather_hold", "start": fmt_time(w0), "end": fmt_time(w0 + weather_min),
                        "description": f"Weather over {ap['iata']}: no landings for {weather_min} min "
                                       "(recorded WEATHER_DELAY); runway capacity unchanged"})
        notes.append(f"Weather modelled as a {weather_min}-min hold on all arrivals at {ap['iata']} (the row's "
                     f"WEATHER_DELAY, uncapped), starting {WEATHER_LEAD_MIN} min before DF's planned landing. "
                     "Runway capacity is not reduced; held aircraft queue when it clears.")
    if late_min > 0:
        if phase == "on_ground_at_origin":
            effects.append({"type": "late_inbound_aircraft", "delay_min": late_min,
                            "description": f"Inbound aircraft {late_min} min late (recorded LATE_AIRCRAFT_DELAY)"})
        else:
            notes.append(f"Late-aircraft delay ({late_min} min) already happened before take-off, so it is "
                         "part of the planned landing.")
    if unmodelled:
        notes.append("Recorded delay causes not modelled: " + ", ".join(f"{k} {v} min" for k, v in unmodelled.items())
                     + ". Without negotiation the flight is late by weather + late-aircraft minutes only.")

    # Other traffic is centred on DF's expected landing (on ground this includes the late inbound
    # aircraft), so the queue and the weather hold actually involve DF.
    expected_landing = landing + (late_min if phase == "on_ground_at_origin" else 0)

    # ------------------------------------------------------------ runways
    layout = RUNWAY_LAYOUT[ap["size_class"]]
    assert len(layout) == ap["active_runways"], "runway layout must match the workbook"
    arr_rwys = [r for r, u in layout if u in ("arrivals", "mixed")]
    dep_rwys = [r for r, u in layout if u in ("departures", "mixed")]

    # ------------------------------------------------------------ other flights
    mix = TRAFFIC_MIX[ap["size_class"]]
    types, weights = zip(*mix)
    n = 4 if small else max(2, round(ap["max_runway_ops_per_hour"] * OTHER_LOAD * 2 * OTHER_WINDOW_MIN / 60))
    others = []
    for i in range(n):
        kind = "arrival" if small or i % 2 == 0 else "departure"
        at = rng.choices(types, weights)[0]
        s = cat[at]
        airline = rng.choice(REGIONAL_AIRLINES if s["class"] == "regional" else MAINLINE_AIRLINES)
        if small:   # planned just ahead of DF, so the same weather puts them in front of it in the queue
            tm = round(expected_landing - OTHER_WINDOW_MIN + (i + rng.random()) * (OTHER_WINDOW_MIN - 1) / n)
        else:
            tm = round(expected_landing - OTHER_WINDOW_MIN + (i + rng.random()) * 2 * OTHER_WINDOW_MIN / n)
        o = {"id": f"OF{i + 1:02d}", "callsign": f"{airline}{rng.randint(100, 5999)}", "kind": kind,
             "aircraft_type": at, "planned_time": fmt_time(tm),
             "runway": rng.choice(arr_rwys if kind == "arrival" else dep_rwys),
             "crew_duty_end": fmt_time(tm + rng.randint(60, 360)),
             "connecting_passengers": rng.randint(0, 40), "movable": True}
        if kind == "arrival":
            o["spare_holding_min"] = R.fuel["spare_holding_min"]
        others.append(o)
    conn_out = []
    for k, off in enumerate(CONNECTION_OFFSETS[:2] if small else CONNECTION_OFFSETS):
        tm = landing + taxi_in + off
        at = df_aircraft_type(f["airline"], 800)
        o = {"id": f"CX{k + 1}", "callsign": f"{f['airline']}{rng.randint(100, 5999)}", "kind": "departure",
             "aircraft_type": at, "planned_time": fmt_time(tm), "runway": rng.choice(dep_rwys),
             "crew_duty_end": fmt_time(tm + rng.randint(120, 480)),
             "connecting_passengers": rng.randint(0, 40), "movable": True}
        others.append(o)
        conn_out.append({"to_flight": o["id"], "passengers": rng.randint(10, 40),
                         "min_connection_min": MIN_CONNECTION_MIN})
    dfl["connections"] = conn_out
    notes.append(f"{n} other flights within +/-{OTHER_WINDOW_MIN} min of DF's expected landing at {OTHER_LOAD:.0%} of the "
                 f"workbook's {ap['max_runway_ops_per_hour']} movements/h, plus {len(conn_out)} connecting "
                 f"departures. Other arrivals carry the workbook's {R.fuel['spare_holding_min']:g} min of spare "
                 "holding fuel on arrival.")
    notes.append(f"Minimum connection time {MIN_CONNECTION_MIN} min is our assumption (not in the workbook).")

    # ------------------------------------------------------------ gates
    numbers = rng.sample(range(1, ap["gates"] + 1), k=min(ap["gates"], len(others) + 1 + ap["spare_gates_at_any_time"]))
    ids = iter(f"G{x:03d}" for x in numbers)
    gates = []
    df_gate = next(ids)
    gates.append({"id": df_gate, "size": spec["gate_size"], "occupied_by": None, "occupied_until": None})
    dfl["assigned_gate"] = df_gate
    for o in others:
        gid = next(ids)
        size = cat[o["aircraft_type"]]["gate_size"]
        if o["kind"] == "departure":
            gates.append({"id": gid, "size": size, "occupied_by": o["id"],
                          "occupied_until": fmt_time(_t(o["planned_time"]) - 10)})
        else:
            gates.append({"id": gid, "size": size, "occupied_by": None, "occupied_until": None})
        o["gate"] = gid
    spare_sizes = ["narrowbody", "regional", "widebody", "narrowbody", "regional"]
    for k in range(ap["spare_gates_at_any_time"]):
        gates.append({"id": next(ids), "size": spare_sizes[k % len(spare_sizes)],
                      "occupied_by": None, "occupied_until": None})
    notes.append(f"Gates: one per flight plus exactly {ap['spare_gates_at_any_time']} spare (workbook). "
                 "Gate sizes are our assumption; the workbook only gives the count.")

    # ------------------------------------------------------------ ground crews (8 h shifts)
    shift = int(R.ground["shift_h"] * 60)
    df_in = landing + taxi_in
    gc1_end = df_in + ap["min_turnaround_min"] + 15
    crews = [
        {"id": "GC1", "shift_start": fmt_time(gc1_end - shift), "shift_end": fmt_time(gc1_end),
         "flights": ["DF"]},
        {"id": "GC2", "shift_start": fmt_time(expected_landing - 240), "shift_end": fmt_time(expected_landing - 240 + shift),
         "flights": [o["id"] for o in others if o["kind"] == "arrival"]},
        {"id": "GC3", "shift_start": fmt_time(expected_landing - 240), "shift_end": fmt_time(expected_landing - 240 + shift),
         "flights": [o["id"] for o in others if o["kind"] == "departure"]},
    ]
    notes.append(f"Ground crew GC1 (serving the disrupted flight) ends its {R.ground['shift_h']:g} h shift 15 min "
                 f"after a normal {ap['min_turnaround_min']}-min turnaround would finish, so a delay of more than "
                 "15 min creates overtime. GC2 and GC3 cover the other arrivals and departures.")

    return {
        "scenario_id": f"row{row_id}_{phase}",
        "description": f"{f['callsign']} {f['origin']}->{f['destination']} {f['date']} "
                       f"({rec['disruption']['type']}), {phase.replace('_', ' ')}",
        "disruption_row": row_id,
        "phase": phase,
        "start_time": fmt_time(start),
        "rule_overrides": {},
        "disrupted_flight": dfl,
        "disruption_effects": effects,
        "airport": {"iata": ap["iata"], "runways": [{"id": r, "use": u} for r, u in layout],
                    "gates": gates, "ground_crews": crews},
        "other_flights": others,
        "_generated_notes": notes,
    }


def _t(hhmm):
    from scenario import parse_time
    return parse_time(hhmm)


def write(sc, path=None):
    SCENARIO_DIR.mkdir(exist_ok=True)
    path = Path(path) if path else SCENARIO_DIR / f"{sc['scenario_id']}.json"
    path.write_text(json.dumps(sc, indent=1))
    return path


EXAMPLES = [
    ("example_airborne_weather", "weather_large_hub", "airborne"),
    ("example_on_ground_late_aircraft", "late_aircraft_large_hub", "on_ground_at_origin"),
]

# ------------------------------------------------------------------ pilot-ATC priority demo
PRIORITY_DEMO = {
    "weather_min": (35, 42),       # long enough to build a queue, short enough for others' 45-min spare fuel
    "max_late_aircraft": 20,
    "min_air_time": 40,
    "min_gain_min": 6,             # priority must save at least this much
    "others_margin_min": 3,        # every other arrival keeps this much spare fuel, with or without priority
    "commit_margin_min": 4,        # waiting its turn needs this many minutes more than DF can hold before diverting
}


def make_priority_demo(R=None):
    """Airborne flight caught by a weather hold, low enough on fuel that it must ask ATC for priority.

    The row is the first (lowest row id) airborne large-hub weather row where moving DF to the
    front of the post-weather queue saves at least min_gain_min and no other arrival runs out of
    spare fuel either way. DF's fuel is then set so that:
      first come, first served -> DF must commit to land here   (minimum fuel: negotiable request)
      DF moved to the front    -> DF lands keeping alternate fuel (normal)
    """
    import airside as A
    R = R or load_rules()
    P = PRIORITY_DEMO
    d = DS.flights()
    big = [k for k, a in R.airports.items() if a["size_class"] == "Large hub"]
    cand = d[d.DESTINATION_AIRPORT.isin(big) & d.WEATHER_DELAY.between(*P["weather_min"])
             & (d.LATE_AIRCRAFT_DELAY <= P["max_late_aircraft"]) & (d.AIR_TIME >= P["min_air_time"])
             & (d.CANCELLED == 0) & (d.DIVERTED == 0)].sort_index()
    for rid in cand.index:
        raw = make(int(rid), "airborne", R)
        sc = load_scenario(raw)
        fcfs = A.landing_sequence(sc)
        prio = A.landing_sequence(sc, priority=["DF"])
        gain = fcfs["DF"]["wait_min"] - prio["DF"]["wait_min"]
        limit = R.fuel["spare_holding_min"] - P["others_margin_min"]
        others_ok = all(v["wait_min"] <= limit for seq in (fcfs, prio) for k, v in seq.items() if k != "DF")
        if gain < P["min_gain_min"] or not others_ok:
            continue
        # Fuel: trip + alternate + final reserve + exactly `target` minutes of holding.
        f = sc["flight"]
        fp = f["fuel_plan"]
        target = round(fcfs["DF"]["wait_min"] - P["commit_margin_min"], 1)
        committed = target + fp["alternate_kg"] / fp["holding_burn_kg_per_min"]
        if not (prio["DF"]["wait_min"] < target and fcfs["DF"]["wait_min"] <= committed):
            continue
        raw["disrupted_flight"]["fuel_on_board_kg"] = int(math.ceil(
            fp["trip_kg"] + fp["alternate_kg"] + fp["final_reserve_kg"] + target * fp["holding_burn_kg_per_min"]))
        raw["scenario_id"] = "example_pilot_atc_priority"
        raw["description"] += " - low fuel, pilot must ask ATC for priority after the weather clears"
        raw["_generated_notes"].append(
            f"Pilot-ATC demo: DF fuel set to {raw['disrupted_flight']['fuel_on_board_kg']} kg = trip + alternate + "
            f"final reserve + {target} min of holding (the workbook assumes {R.fuel['spare_holding_min']:g}; "
            "this lower figure is a stated change). Waiting its turn (about "
            f"{fcfs['DF']['wait_min']:.0f} min) would force it to commit to landing here; moved to the front "
            f"(about {prio['DF']['wait_min']:.0f} min) it keeps fuel for its alternate.")
        return raw
    raise LookupError("no row fits the pilot-ATC priority demo rules")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--row", type=int)
    g.add_argument("--demo", choices=sorted(DS.DEMOS))
    g.add_argument("--examples", action="store_true")
    ap.add_argument("--phase", choices=["airborne", "on_ground_at_origin"])
    ap.add_argument("--out")
    args = ap.parse_args()
    R = load_rules()
    jobs = []
    if args.examples:
        jobs = [(DS.demo_row(d, R), ph, SCENARIO_DIR / f"{name}.json", name) for name, d, ph in EXAMPLES]
        pd_raw = make_priority_demo(R)
        path = write(pd_raw, SCENARIO_DIR / "example_pilot_atc_priority.json")
        print(f"wrote {path}")
        print(summary(load_scenario(path)))
    else:
        if not args.phase:
            ap.error("--phase is required with --row or --demo")
        rid = args.row if args.row is not None else DS.demo_row(args.demo, R)
        jobs = [(rid, args.phase, args.out, None)]
    for rid, ph, out, name in jobs:
        sc = make(rid, ph, R)
        if name:
            sc["scenario_id"] = name
        path = write(sc, out)
        print(f"wrote {path}")
        print(summary(load_scenario(path)))
