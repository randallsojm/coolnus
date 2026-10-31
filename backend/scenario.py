"""Phase 2: the scenario format and its loader.

A scenario is a JSON file the team writes (or generates with make_scenario.py and
then edits). It adds everything flights.csv doesn't have: aircraft, fuel, gates,
crews, other traffic and what the disruption does.

    from scenario import load_scenario
    sc = load_scenario("scenarios/example_airborne_weather.json")

Loading checks the file against the workbook (rules.py) and the aircraft catalogue
(aircraft_types.json). Every problem is collected and reported together, so one run
shows everything that needs fixing.

TIME CONVENTION
  Every time is "HH:MM" in the ARRIVAL airport's local time on the flight date.
  Add "+1" for the next day ("00:40+1") or "-1" for the day before.
  Flight times are RUNWAY times: landing for arrivals, take-off for departures.
  The disrupted flight always has id "DF".
"""
import json
import re
from pathlib import Path

import disruption as DS
from rules import ROOT, RulesError, load_rules

CATALOGUE = ROOT / "aircraft_types.json"
PHASES = ("airborne", "on_ground_at_origin")
GATE_SIZES = ("regional", "narrowbody", "widebody", "super")
RUNWAY_USES = ("arrivals", "departures", "mixed")
TURBULENCE = ("none", "light", "moderate", "severe")
DF = "DF"
EFFECT_FIELDS = {
    "runway_closure": {"runway", "start", "end"},
    "capacity_reduction": {"max_runway_ops_per_hour", "start", "end"},
    "gate_unavailable": {"gate", "start", "end"},
    "taxiway_closure": {"taxiway", "extra_taxi_min", "affected_gates", "start", "end"},
    "late_inbound_aircraft": {"delay_min"},
    # Weather overhead: no arrival can land between start and end. Runway capacity is NOT
    # reduced; when the weather clears, the held arrivals form a queue that ATC sequences.
    "weather_hold": {"start", "end"},
}


class ScenarioError(Exception):
    def __init__(self, errors, path=None):
        self.errors = errors
        head = f"Scenario {path} is invalid" if path else "Scenario is invalid"
        super().__init__(head + f" ({len(errors)} problem{'s' * (len(errors) != 1)}):\n"
                         + "\n".join(f"  {i}. {e}" for i, e in enumerate(errors, 1)))


# ------------------------------------------------------------------ time helpers
_TIME = re.compile(r"^(\d{1,2}):(\d{2})([+-]\d)?$")


def parse_time(value):
    """'14:05' -> 845, '00:40+1' -> 1480. Raises ValueError."""
    m = _TIME.match(str(value).strip())
    if not m:
        raise ValueError(f"{value!r} is not a time like '14:05' or '00:40+1'")
    h, mi, day = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59:
        raise ValueError(f"{value!r} is not a valid clock time")
    return day * 1440 + h * 60 + mi


def fmt_time(minutes):
    if minutes is None:
        return None
    day, m = divmod(int(round(minutes)), 1440)
    return f"{m // 60:02d}:{m % 60:02d}" + (f"{day:+d}" if day else "")


def load_catalogue(path=CATALOGUE):
    return json.loads(Path(path).read_text())["types"]


# ------------------------------------------------------------------ validator
class _Checker:
    def __init__(self, catalogue):
        self.errors, self.warnings, self.cat = [], [], catalogue

    def err(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def need(self, obj, key, where, kind=None):
        if not isinstance(obj, dict) or key not in obj or obj[key] is None:
            self.err(f"{where}: missing required field '{key}'")
            return None
        v = obj[key]
        if kind == "number" and (not isinstance(v, (int, float)) or isinstance(v, bool)):
            self.err(f"{where}.{key}: must be a number, got {v!r}")
            return None
        if kind == "positive" and (not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0):
            self.err(f"{where}.{key}: must be a number above 0, got {v!r}")
            return None
        if kind == "list" and not isinstance(v, list):
            self.err(f"{where}.{key}: must be a list")
            return None
        if kind == "dict" and not isinstance(v, dict):
            self.err(f"{where}.{key}: must be an object")
            return None
        if kind == "str" and not isinstance(v, str):
            self.err(f"{where}.{key}: must be text")
            return None
        return v

    def time(self, obj, key, where, required=True):
        if not required and (not isinstance(obj, dict) or obj.get(key) is None):
            return None
        v = self.need(obj, key, where)
        if v is None:
            return None
        try:
            return parse_time(v)
        except ValueError as e:
            self.err(f"{where}.{key}: {e}")
            return None

    def aircraft(self, type_code, where):
        if type_code is None:
            return None
        if type_code not in self.cat:
            self.err(f"{where}: invalid aircraft '{type_code}': it is not listed in the fuel consumption "
                     f"table (aircraft_types.json). Add it there with its fuel burn, or use one of: "
                     f"{', '.join(sorted(self.cat))}")
            return None
        return dict(self.cat[type_code], type=type_code)


def gate_fits(aircraft_gate_size, gate_size):
    return GATE_SIZES.index(gate_size) >= GATE_SIZES.index(aircraft_gate_size)


def load_scenario(source):
    """Load and validate a scenario (path or dict). Returns the normalised scenario."""
    path = None
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ScenarioError([f"not valid JSON: {e}"], path)
    else:
        raw = source
    C = _Checker(load_catalogue())

    # ---------------------------------------------------------------- top level
    for key, kind in [("scenario_id", "str"), ("disruption_row", "number"), ("phase", "str"),
                      ("start_time", None), ("disrupted_flight", "dict"), ("airport", "dict"),
                      ("other_flights", "list"), ("disruption_effects", "list")]:
        C.need(raw, key, "scenario", kind)
    phase = raw.get("phase")
    if phase is not None and phase not in PHASES:
        C.err(f"scenario.phase: must be one of {PHASES}, got {phase!r}")
    start = C.time(raw, "start_time", "scenario")
    if C.errors:
        raise ScenarioError(C.errors, path)

    try:
        R = load_rules(raw.get("rule_overrides") or {})
    except RulesError as e:
        raise ScenarioError([f"rule_overrides: {e}"], path)
    try:
        row = DS.build(int(raw["disruption_row"]), R)
    except (IndexError, DS.NotADisruption, RulesError) as e:
        raise ScenarioError([f"disruption_row {raw['disruption_row']}: {e}"], path)
    ap = row["arrival_airport"]
    iata = ap["iata"]

    # ---------------------------------------------------------------- airport
    a = raw["airport"]
    if a.get("iata") not in (None, iata):
        C.err(f"airport.iata is {a.get('iata')!r} but the disruption row lands at {iata}")
    runways = {}
    for i, r in enumerate(C.need(a, "runways", "airport", "list") or []):
        w = f"airport.runways[{i}]"
        rid, use = C.need(r, "id", w, "str"), C.need(r, "use", w, "str")
        if use is not None and use not in RUNWAY_USES:
            C.err(f"{w}.use: must be one of {RUNWAY_USES}, got {use!r}")
        if rid in runways:
            C.err(f"{w}: duplicate runway id {rid!r}")
        elif rid:
            runways[rid] = {"id": rid, "use": use}
    if len(runways) > ap["active_runways"]:
        C.err(f"airport.runways: lists {len(runways)} runways but the workbook gives {iata} "
              f"{ap['active_runways']} active runways. Remove runways, or add "
              f"\"airport.{iata}.active_runways\": {len(runways)} to rule_overrides (it will be stated in the demo).")
    if runways and not any(r["use"] in ("arrivals", "mixed") for r in runways.values()):
        C.err("airport.runways: no runway accepts arrivals")

    gates = {}
    for i, g in enumerate(C.need(a, "gates", "airport", "list") or []):
        w = f"airport.gates[{i}]"
        gid, size = C.need(g, "id", w, "str"), C.need(g, "size", w, "str")
        if size is not None and size not in GATE_SIZES:
            C.err(f"{w}.size: must be one of {GATE_SIZES}, got {size!r}")
        if gid in gates:
            C.err(f"{w}: duplicate gate id {gid!r}")
            continue
        gates[gid] = {"id": gid, "size": size, "occupied_by": g.get("occupied_by"),
                      "occupied_until": C.time(g, "occupied_until", w, required=g.get("occupied_by") is not None)}
    if len(gates) > ap["gates"]:
        C.err(f"airport.gates: lists {len(gates)} gates but the workbook gives {iata} {ap['gates']} gates. "
              f"Remove gates, or add \"airport.{iata}.gates\": {len(gates)} to rule_overrides.")

    crews = {}
    shift_min = R.ground["shift_h"] * 60
    for i, c in enumerate(C.need(a, "ground_crews", "airport", "list") or []):
        w = f"airport.ground_crews[{i}]"
        cid = C.need(c, "id", w, "str")
        s, e = C.time(c, "shift_start", w), C.time(c, "shift_end", w)
        flights_ = C.need(c, "flights", w, "list") or []
        if s is not None and e is not None and e - s != shift_min:
            C.err(f"{w}: shift is {(e - s) / 60:g} h but the workbook ground-crew shift is "
                  f"{R.ground['shift_h']:g} h. Fix the times, or add \"ground.shift_h\" to rule_overrides.")
        if cid in crews:
            C.err(f"{w}: duplicate crew id {cid!r}")
        elif cid:
            crews[cid] = {"id": cid, "shift_start": s, "shift_end": e, "flights": flights_}

    # ---------------------------------------------------------------- disrupted flight
    d = raw["disrupted_flight"]
    w = "disrupted_flight"
    ac = C.aircraft(C.need(d, "aircraft_type", w, "str"), w)
    flight = {
        "id": DF, "callsign": row["flight"]["callsign"], "airline": row["flight"]["airline"],
        "kind": "arrival", "aircraft": ac,
        "fuel_on_board_kg": C.need(d, "fuel_on_board_kg", w, "positive"),
        "planned_arrival": C.time(d, "planned_arrival", w),
        "taxi_in_min": d.get("taxi_in_min", row["flight"]["taxi_in_min"] or 8),
        "crew_report_time": C.time(d, "crew_report_time", w),
        "assigned_gate": C.need(d, "assigned_gate", w, "str"),
        "route_turbulence": d.get("route_turbulence", "none"),
        "connections": [],
        "movable": True,
    }
    alt = C.need(d, "alternate", w, "dict")
    if alt:
        flight["alternate"] = {"airport": C.need(alt, "airport", f"{w}.alternate", "str"),
                               "distance_mi": C.need(alt, "distance_mi", f"{w}.alternate", "positive")}
    if flight["route_turbulence"] not in TURBULENCE:
        C.err(f"{w}.route_turbulence: must be one of {TURBULENCE}")
    if phase == "airborne":
        flight["minutes_to_destination"] = C.need(d, "minutes_to_destination", w, "positive")
        if "planned_departure" in d:
            C.err(f"{w}.planned_departure: not used when phase is 'airborne' (the flight has already left)")
    else:
        flight["planned_departure"] = C.time(d, "planned_departure", w)
        if "minutes_to_destination" in d:
            C.err(f"{w}.minutes_to_destination: not used when phase is 'on_ground_at_origin'")

    # ---------------------------------------------------------------- other flights
    others = {}
    for i, o in enumerate(raw["other_flights"]):
        w = f"other_flights[{i}]"
        oid = C.need(o, "id", w, "str")
        if oid == DF:
            C.err(f"{w}.id: 'DF' is reserved for the disrupted flight")
        elif oid in others:
            C.err(f"{w}: duplicate flight id {oid!r}")
        w = f"other_flights[{i}] ({oid})"
        kind = C.need(o, "kind", w, "str")
        if kind is not None and kind not in ("arrival", "departure"):
            C.err(f"{w}.kind: must be 'arrival' or 'departure', got {kind!r}")
        oac = C.aircraft(C.need(o, "aircraft_type", w, "str"), w)
        rec = {"id": oid, "callsign": o.get("callsign", oid), "kind": kind, "aircraft": oac,
               "planned_time": C.time(o, "planned_time", w), "runway": C.need(o, "runway", w, "str"),
               "gate": o.get("gate"), "movable": o.get("movable", True),
               "crew_duty_end": C.time(o, "crew_duty_end", w),
               "connecting_passengers": o.get("connecting_passengers", 0)}
        if not isinstance(rec["movable"], bool):
            C.err(f"{w}.movable: must be true or false")
        if kind == "arrival":
            # Minutes of holding fuel beyond the final reserve when it reaches the airport.
            # Workbook: "assumed spare fuel on arrival: 45 minutes of holding (unless the team states otherwise)".
            spare = o.get("spare_holding_min", R.fuel["spare_holding_min"])
            if not isinstance(spare, (int, float)) or isinstance(spare, bool) or spare < 0:
                C.err(f"{w}.spare_holding_min: must be a number of minutes, 0 or more")
            rec["spare_holding_min"] = spare
            rec["spare_holding_source"] = "scenario" if "spare_holding_min" in o else "workbook default"
            rec["taxi_in_min"] = o.get("taxi_in_min", 8)
        if oid and oid != DF:
            others[oid] = rec

    # ---------------------------------------------------------------- connections (need other flights)
    for i, cn in enumerate(d.get("connections", []) or []):
        w = f"disrupted_flight.connections[{i}]"
        to = C.need(cn, "to_flight", w, "str")
        pax = C.need(cn, "passengers", w, "positive")
        mct = C.need(cn, "min_connection_min", w, "positive")
        if to is not None and (to not in others or others[to]["kind"] != "departure"):
            C.err(f"{w}.to_flight: {to!r} is not a departure in other_flights")
        flight["connections"].append({"to_flight": to, "passengers": pax, "min_connection_min": mct})

    # ---------------------------------------------------------------- disruption effects
    effects = []
    for i, e in enumerate(raw["disruption_effects"]):
        w = f"disruption_effects[{i}]"
        t = C.need(e, "type", w, "str")
        if t not in EFFECT_FIELDS:
            C.err(f"{w}.type: must be one of {sorted(EFFECT_FIELDS)}, got {t!r}")
            continue
        missing = EFFECT_FIELDS[t] - set(e)
        if missing:
            C.err(f"{w} ({t}): missing {sorted(missing)}")
            continue
        eff = dict(e)
        if "start" in EFFECT_FIELDS[t]:
            eff["start"], eff["end"] = C.time(e, "start", w), C.time(e, "end", w)
            if eff["start"] is not None and eff["end"] is not None and eff["end"] <= eff["start"]:
                C.err(f"{w}: end must be after start")
        if t == "runway_closure" and e["runway"] not in runways:
            C.err(f"{w}.runway: {e['runway']!r} is not in airport.runways")
        if t == "capacity_reduction":
            cap = e["max_runway_ops_per_hour"]
            if not isinstance(cap, (int, float)) or cap <= 0:
                C.err(f"{w}.max_runway_ops_per_hour: must be above 0")
            elif cap > ap["max_runway_ops_per_hour"]:
                C.err(f"{w}.max_runway_ops_per_hour: {cap} is above the workbook limit "
                      f"{ap['max_runway_ops_per_hour']} for {iata}; a reduction must be lower")
        if t == "gate_unavailable" and e["gate"] not in gates:
            C.err(f"{w}.gate: {e['gate']!r} is not in airport.gates")
        if t == "taxiway_closure":
            if not isinstance(e["extra_taxi_min"], (int, float)) or e["extra_taxi_min"] <= 0:
                C.err(f"{w}.extra_taxi_min: must be above 0")
            for g in e["affected_gates"]:
                if g not in gates:
                    C.err(f"{w}.affected_gates: {g!r} is not in airport.gates")
        if t == "late_inbound_aircraft":
            if phase != "on_ground_at_origin":
                C.err(f"{w}: late_inbound_aircraft only applies when phase is 'on_ground_at_origin'")
            if not isinstance(e["delay_min"], (int, float)) or e["delay_min"] <= 0:
                C.err(f"{w}.delay_min: must be above 0")
        effects.append(eff)

    # ---------------------------------------------------------------- cross-references
    all_ids = {DF} | set(others)
    for g in gates.values():
        if g["occupied_by"] is not None and g["occupied_by"] not in all_ids:
            C.err(f"gate {g['id']}: occupied_by {g['occupied_by']!r} is not a flight in this scenario")
    for c in crews.values():
        for fid in c["flights"]:
            if fid not in all_ids:
                C.err(f"ground crew {c['id']}: flight {fid!r} is not in this scenario")

    def check_gate(fid, gid, acft, where):
        if gid is None:
            return
        if gid not in gates:
            C.err(f"{where}: gate {gid!r} is not in airport.gates")
        elif acft and gates[gid]["size"] in GATE_SIZES and not gate_fits(acft["gate_size"], gates[gid]["size"]):
            C.err(f"{where}: aircraft {acft['type']} needs a {acft['gate_size']} gate but {gid} is "
                  f"{gates[gid]['size']}")

    check_gate(DF, flight["assigned_gate"], ac, "disrupted_flight.assigned_gate")
    for o in others.values():
        check_gate(o["id"], o["gate"], o["aircraft"], f"other_flights ({o['id']}).gate")
        if o["runway"] is not None and o["runway"] not in runways:
            C.err(f"other_flights ({o['id']}).runway: {o['runway']!r} is not in airport.runways")
        elif o["runway"] and o["kind"]:
            use = runways[o["runway"]]["use"]
            if use != "mixed" and use != o["kind"] + "s":
                C.err(f"other_flights ({o['id']}): an {o['kind']} can't use runway {o['runway']} ({use})")

    if C.errors:
        raise ScenarioError(C.errors, path)

    # ---------------------------------------------------------------- consistency (warnings)
    F = R.fuel
    burn_min = ac["cruise_burn_kg_h"] / 60
    hold_min = ac["holding_burn_kg_h"] / 60
    if phase == "airborne":
        if flight["planned_arrival"] < start + flight["minutes_to_destination"] - 1:
            raise ScenarioError([f"disrupted_flight.planned_arrival {fmt_time(flight['planned_arrival'])} is "
                                 f"earlier than start_time + minutes_to_destination "
                                 f"({fmt_time(start + flight['minutes_to_destination'])})"], path)
        trip_min = flight["planned_arrival"] - start
    else:
        if flight["planned_departure"] >= flight["planned_arrival"]:
            raise ScenarioError(["disrupted_flight.planned_departure must be before planned_arrival"], path)
        if flight["planned_departure"] < start:
            C.warn(f"disrupted_flight.planned_departure {fmt_time(flight['planned_departure'])} is before "
                   f"start_time {fmt_time(start)}")
        trip_min = flight["planned_arrival"] - flight["planned_departure"]
    trip_kg = trip_min * burn_min
    reserve_kg = F["final_reserve_min"] * hold_min
    alt_min = (flight["alternate"]["distance_mi"] / 1.15078) / ac["cruise_speed_kt"] * 60
    alt_kg = alt_min * burn_min
    at_landing_kg = flight["fuel_on_board_kg"] - trip_kg
    flight["fuel_plan"] = {
        "trip_kg": round(trip_kg),
        "final_reserve_kg": round(reserve_kg),
        "alternate_kg": round(alt_kg),
        "alternate_flight_min": round(alt_min, 1),
        "on_landing_as_planned_kg": round(at_landing_kg),
        # holding allowed while keeping enough to fly to the alternate and still have the final reserve
        "holding_before_divert_min": round((at_landing_kg - alt_kg - reserve_kg) / hold_min, 1),
        # holding allowed if committed to land here (alternate fuel may be used)
        "holding_if_committed_min": round((at_landing_kg - reserve_kg) / hold_min, 1),
        "cruise_burn_kg_per_min": round(burn_min, 2),
        "holding_burn_kg_per_min": round(hold_min, 2),
    }
    if at_landing_kg < reserve_kg:
        C.warn(f"DF starts with {flight['fuel_on_board_kg']:.0f} kg; landing on plan would leave less than the "
               f"{F['final_reserve_min']:g}-minute final reserve (needs {trip_kg + reserve_kg:.0f} kg). "
               "This is a fuel emergency.")
    elif at_landing_kg < reserve_kg + alt_kg:
        C.warn("DF can land with its final reserve but cannot also keep fuel for the alternate: "
               "it is committed to landing here.")
    flight["holding_available_min"] = flight["fuel_plan"]["holding_before_divert_min"]
    duty_end = flight["crew_report_time"] + R.crew["max_duty_h"] * 60
    flight["crew_duty_end"] = duty_end
    if flight["planned_arrival"] > duty_end:
        C.warn(f"DF crew would exceed {R.crew['max_duty_h']:g} h duty even on the current plan")
    for cn in flight["connections"]:
        dep = others[cn["to_flight"]]
        gap = dep["planned_time"] - (flight["planned_arrival"] + flight["taxi_in_min"])
        if gap < cn["min_connection_min"]:
            C.warn(f"connection DF -> {dep['id']} already has only {gap:.0f} min "
                   f"(minimum {cn['min_connection_min']}); {cn['passengers']} passengers at risk")

    assigned = {flight["assigned_gate"]} | {o["gate"] for o in others.values() if o["gate"]}
    spare = [g for g in gates.values() if g["id"] not in assigned
             and (g["occupied_by"] is None or (g["occupied_until"] or 0) <= start)]
    if len(spare) > ap["spare_gates_at_any_time"]:
        C.warn(f"{len(spare)} listed gates are free and unassigned at the start, but the workbook says "
               f"{iata} has {ap['spare_gates_at_any_time']} spare gates at any time")

    # stated changes: catalogue burn vs workbook burn
    stated = list(R.overrides)
    used_types = {flight["aircraft"]["type"]} | {o["aircraft"]["type"] for o in others.values()}
    for t in sorted(used_types):
        spec = C.cat[t]
        wb = F["holding_burn_kg_h"].get(spec["class"])
        if wb is None:
            stated.append({"limit": f"holding burn {t}", "workbook_value": None,
                           "used_value": spec["holding_burn_kg_h"],
                           "note": f"workbook has no {spec['class']} value; aircraft catalogue used"})
        elif wb != spec["holding_burn_kg_h"]:
            stated.append({"limit": f"holding burn {t}", "workbook_value": wb,
                           "used_value": spec["holding_burn_kg_h"], "note": "aircraft catalogue value"})

    if flight["fuel_plan"]["holding_before_divert_min"] < F["spare_holding_min"]:
        stated.append({"limit": "DF spare holding fuel (min)", "workbook_value": F["spare_holding_min"],
                       "used_value": flight["fuel_plan"]["holding_before_divert_min"],
                       "note": "scenario fuel; the workbook assumes 45 min of spare holding on arrival"})
    sc = {
        "scenario_id": raw["scenario_id"], "description": raw.get("description", ""),
        "source": str(path) if path else None, "phase": phase, "start_time": start,
        "disruption": row, "rules": R, "airport": {**ap, "runways": runways, "gates": gates,
                                                    "ground_crews": crews},
        "flight": flight, "other_flights": others, "effects": effects,
        "warnings": C.warnings, "stated_changes": stated,
    }
    sc["baseline_fcfs"] = baseline(sc)
    b = sc["baseline_fcfs"]
    if b["df_fuel"]["status"] != "normal":
        C.warn(f"first come, first served: DF waits {b['df_fuel']['wait_min']} min to land at "
               f"{fmt_time(b['df_landing'])} -> {b['df_fuel']['status']}")
    if b["weather_diversions"]:
        C.warn(f"first come, first served: {len(b['weather_diversions'])} other arrivals can't wait out the weather "
               f"on their spare holding fuel and divert: {', '.join(b['weather_diversions'])}")
    return sc


def baseline(sc):
    """What happens if nobody negotiates: first come, first served after the weather clears."""
    import airside as A
    if sc["phase"] != "airborne":
        late = sum(e["delay_min"] for e in sc["effects"] if e["type"] == "late_inbound_aircraft")
        if late:
            sc = dict(sc, flight=dict(sc["flight"], planned_arrival=sc["flight"]["planned_arrival"] + late))
    diverted, seq = A.weather_diversions(sc)
    held = [fid for fid, v in seq.items() if v["ready"] > v["planned"]]
    return {
        "df_landing": seq[DF]["landing"], "df_order": seq[DF]["order"],
        "df_fuel": A.fuel_status(sc, sc["flight"], seq[DF]["landing"]),
        "held_by_weather": len(held) + len(diverted),
        "weather_diversions": diverted,        # other arrivals that can't wait out the weather
        "sequence": seq,
    }


def summary(sc):
    f, ap = sc["flight"], sc["airport"]
    lines = [
        f"{sc['scenario_id']}: {sc['description']}",
        f"  disruption row {sc['disruption']['row_id']}: {f['callsign']} "
        f"{sc['disruption']['flight']['origin']} -> {ap['iata']}, {sc['disruption']['disruption']['type']}",
        f"  phase {sc['phase']}, starts {fmt_time(sc['start_time'])}, planned landing {fmt_time(f['planned_arrival'])}"
        + (f", take-off {fmt_time(f['planned_departure'])}" if sc["phase"] != "airborne" else
           f", {f['minutes_to_destination']} min out"),
        f"  {f['aircraft']['type']} ({f['aircraft']['wake_category']}), {f['fuel_on_board_kg']:.0f} kg fuel = "
        f"{f['fuel_plan']['holding_before_divert_min']} min holding before it must divert to "
        f"{f['alternate']['airport']} ({f['fuel_plan']['holding_if_committed_min']} min if committed to land), "
        f"gate {f['assigned_gate']}",
        f"  airport: {len(ap['runways'])} runways, {len(ap['gates'])} gates listed, "
        f"{len(ap['ground_crews'])} ground crews | {len(sc['other_flights'])} other flights | "
        f"{len(sc['effects'])} disruption effects",
        f"  {len(sc['warnings'])} warnings, {len(sc['stated_changes'])} stated changes",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        try:
            sc = load_scenario(p)
            print(summary(sc))
            for wmsg in sc["warnings"]:
                print("  WARNING:", wmsg)
        except ScenarioError as e:
            print(e)
