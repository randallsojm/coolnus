"""Bridge between the simulator (Node) and the negotiation backend (this Python code).

The simulator sends the flights.csv row the user selected (JSON on stdin):
    {"row": {...raw CSV fields...}, "phase": "airborne" | "on_ground_at_origin",
     "agents": "rule" | "claude", "low_fuel": true | false}
This script builds the situation around that flight, runs the 3-agent negotiation (unchanged backend),
writes the consensus log, and prints one JSON object on stdout:
    {"ok": true, "world": {...what the simulator must show and execute...}, "log_text": ..., ...}
or {"ok": false, "error": "..."}.
"""
import csv
import io
import json
import math
import sys
import traceback

import pandas as pd

import airside as A
import consensus_log as LOG
import disruption as DS
import make_scenario as MS
from orchestrator import negotiate
from rules import load_rules
from scenario import DF, load_scenario

KEYS = ["YEAR", "MONTH", "DAY", "AIRLINE", "FLIGHT_NUMBER", "ORIGIN_AIRPORT", "DESTINATION_AIRPORT",
        "SCHEDULED_DEPARTURE"]
AIRPORT_COLUMNS = ["IATA_CODE", "AIRPORT", "CITY", "STATE", "delayed_departures_in_flights_csv", "size_class",
                   "active_runways", "gates", "max_runway_ops_per_hour", "min_turnaround_min",
                   "spare_gates_at_any_time"]


def find_row(raw):
    """Locate the selected row in data/flights.csv (the backend's copy of the challenge data)."""
    d = DS.flights()
    m = pd.Series(True, index=d.index)
    for k in KEYS:
        v = str(raw.get(k, "")).strip()
        if k in ("AIRLINE", "ORIGIN_AIRPORT", "DESTINATION_AIRPORT"):
            m &= d[k].astype(str).str.strip() == v
        else:
            try:
                m &= pd.to_numeric(d[k], errors="coerce") == float(v)
            except ValueError:
                raise ValueError(f"{k} is not a number: {v!r}")
    hits = list(d.index[m])
    if not hits:
        raise ValueError("This row is not in the backend's data/flights.csv, so it can't be negotiated.")
    return int(hits[0])


def low_fuel(raw_sc):
    """Give DF just enough fuel that waiting its turn forces it to commit (minimum fuel), while
    landing first keeps its alternate fuel. A stated change: below the workbook's 45 min spare."""
    sc = load_scenario(raw_sc)
    if sc["phase"] != "airborne":
        return "Low-fuel option applies to airborne flights only; not applied."
    div, _ = A.weather_diversions(sc)
    fcfs = A.landing_sequence(sc, exclude=div)[DF]["wait_min"]
    prio = A.landing_sequence(sc, priority=[DF], exclude=div)[DF]["wait_min"]
    if fcfs - prio < 0.5:
        return "No queue ahead of DF, so priority would gain nothing; low-fuel option not applied."
    fp = sc["flight"]["fuel_plan"]
    target = prio + (fcfs - prio) / 2
    raw_sc["disrupted_flight"]["fuel_on_board_kg"] = int(math.ceil(
        fp["trip_kg"] + fp["alternate_kg"] + fp["final_reserve_kg"] + target * fp["holding_burn_kg_per_min"]))
    return (f"Low fuel: DF can hold {target:.1f} min before diverting. Waiting its turn needs {fcfs:.1f} min; "
            f"landing first needs {prio:.1f} min.")


def airport_csv(ap):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(AIRPORT_COLUMNS)
    w.writerow([ap["iata"], ap["name"], ap["city"], ap["state"], ap["delayed_departures_in_flights_csv"],
                ap["size_class"], ap["active_runways"], ap["gates"], ap["max_runway_ops_per_hour"],
                ap["min_turnaround_min"], ap["spare_gates_at_any_time"]])
    return buf.getvalue()


def world(sc, result):
    """Everything the simulator needs, in minutes since local midnight (arrival airport, flight date)."""
    chk = result["check"]
    res, se = chk["resolved"], chk["effective"]
    R = sc["rules"]
    reserve_min = R.fuel["final_reserve_min"]
    df = res["df"]
    flights = []

    def spec(a):
        return {"aircraft_type": a["type"], "wake": a["wake_category"], "gate_size": a["gate_size"],
                "cruise_kg_s": a["cruise_burn_kg_h"] / 3600, "hold_kg_s": a["holding_burn_kg_h"] / 3600,
                "reserve_kg": reserve_min * a["holding_burn_kg_h"] / 60}

    f = se["flight"]
    flights.append({
        "id": DF, "callsign": f["callsign"], "kind": "arrival", "disrupted": True, **spec(f["aircraft"]),
        "planned": f["planned_arrival"], "landing": df.get("landing"), "runway": df.get("runway"),
        "gate": df.get("gate"), "in_block": df.get("in_block"), "taxi_in_min": f["taxi_in_min"],
        "fuel_kg": f["fuel_on_board_kg"],
        "airborne_from": df.get("departure_time") if sc["phase"] != "airborne" else None,
        "divert_at": sc["start_time"] if df["action"] == "divert" else None,
        "divert_to": f["alternate"]["airport"]})
    for k, o in sc["other_flights"].items():
        a = o["aircraft"]
        if o["kind"] == "arrival":
            v, g = res["sequence"].get(k), res["arrivals"].get(k)
            trip = max(0.0, o["planned_time"] - sc["start_time"])
            flights.append({
                "id": k, "callsign": o["callsign"], "kind": "arrival", "disrupted": False, **spec(a),
                "planned": o["planned_time"], "landing": v["landing"] if v else None,
                "runway": v["runway"] if v else None, "gate": g["gate"] if g else o["gate"],
                "in_block": g["in_block"] if g else None, "taxi_in_min": o["taxi_in_min"],
                "fuel_kg": trip * a["cruise_burn_kg_h"] / 60 + (o["spare_holding_min"] + reserve_min) * a["holding_burn_kg_h"] / 60,
                "airborne_from": None,
                "divert_at": o["planned_time"] + o["spare_holding_min"] if k in res["weather_diversions"] else None,
                "divert_to": None})
        else:
            d = res["departures"][k]
            flights.append({"id": k, "callsign": o["callsign"], "kind": "departure", "disrupted": False, **spec(a),
                            "planned": o["planned_time"], "takeoff": d["time"], "runway": d["runway"], "gate": o["gate"],
                            "fuel_kg": 0})
    return {
        # the workbook row itself (sc["airport"]["gates"] holds the gate list, not the count)
        "airport_csv": airport_csv(sc["disruption"]["arrival_airport"]), "iata": sc["airport"]["iata"],
        "date": sc["disruption"]["flight"]["date"], "start": sc["start_time"], "phase": sc["phase"],
        "weather_holds": [{"start": h["start"], "end": h["end"]} for h in A.holds(sc)],
        "runways": list(sc["airport"]["runways"].values()),
        "gates": [{"id": g["id"], "size": g["size"], "occupied_by": g["occupied_by"],
                   "occupied_until": g["occupied_until"]} for g in sc["airport"]["gates"].values()],
        "crews": [dict(c) for c in sc["airport"]["ground_crews"].values()],
        "flights": flights,
    }


def main():
    req = json.loads(sys.stdin.read() or "{}")
    R = load_rules()
    row_id = find_row(req.get("row") or {})
    phase = req.get("phase", "airborne")
    raw_sc = MS.make(row_id, phase, R, small=True)
    raw_sc["scenario_id"] = f"sim_row{row_id}_{phase}"
    notes = [low_fuel(raw_sc)] if req.get("low_fuel") else []
    sc = load_scenario(raw_sc)
    if req.get("agents") == "claude":
        from agents.claude_agent import make_claude_agents
        agents = make_claude_agents()
    else:
        from agents.rule_agents import make_rule_agents
        agents = make_rule_agents()
    result = negotiate(sc, agents)
    jp, tp, log = LOG.write(result, sc, req.get("agents", "rule"))
    r = log["resolution"]
    print(json.dumps({
        "ok": True, "row_id": row_id, "notes": notes + sc["warnings"],
        "consensus": log["consensus"], "decided_by": r["decided_by"],
        "resolution": {k: r[k] for k in ("flight_plan", "revised_arrival_time", "revised_in_block_time", "runway",
                                         "gate", "departure_time", "fuel_status", "divert_to", "fallback_reason")
                       if k in r},
        "outcome": {"others_later": r["outcome"]["others"]["later_count"],
                    "others_extra_delay_min": r["outcome"]["others"]["extra_delay_total_min"],
                    "connections_kept": r["outcome"]["connections"]["passengers_kept"],
                    "connections_total": r["outcome"]["connections"]["passengers_total"],
                    "overtime_min": r["outcome"]["ground"]["overtime_min"],
                    "scores": r["outcome"]["scores"]},
        "stated_changes": log["stated_changes"],
        "log_text": LOG.text(log), "log_json": str(jp), "log_txt": str(tp),
        "world": world(sc, result),
    }, default=str))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:                      # e.g. missing API key
        print(json.dumps({"ok": False, "error": str(e)}))
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
