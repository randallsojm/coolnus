"""What each agent is allowed to see (information hiding). Times are shown as HH:MM.

build_views(sc) -> {"public": ..., "ATC": ..., "PILOT": ..., "GROUND": ...}
An agent's prompt contains only "public" + its own view + the shared message history.
"""
import airside as A
from scenario import DF, fmt_time

# Fields that must never appear outside their owner's view (checked by check_phase4.py).
PRIVATE_KEYS = {
    "PILOT": ["fuel_plan", "fuel_on_board_kg", "crew_report_time", "connections", "alternate_airport"],
    "ATC": ["spare_holding_min", "arrivals_queue", "departures"],
    "GROUND": ["ground_crews", "gates_list", "shift_end"],
}


def build_views(sc):
    f, ap, o = sc["flight"], sc["airport"], sc["other_flights"]
    b = sc["baseline_fcfs"]
    d = sc["disruption"]
    hold = [{"start": fmt_time(h["start"]), "end": fmt_time(h["end"])} for h in A.holds(sc)]
    late = sum(e["delay_min"] for e in sc["effects"] if e["type"] == "late_inbound_aircraft")
    public = {
        "disruption": {
            "row_id": d["row_id"], "callsign": f["callsign"], "airline": f["airline"],
            "route": f"{d['flight']['origin']} -> {ap['iata']}", "date": d["flight"]["date"],
            "cause": d["disruption"]["type"], "phase": sc["phase"],
            "negotiation_starts": fmt_time(sc["start_time"]),
            "weather_hold_no_landings": hold,
            "late_inbound_aircraft_min": late,
        },
        "disrupted_flight": {"id": DF, "aircraft_type": f["aircraft"]["type"],
                             "wake_category": f["aircraft"]["wake_category"],
                             "planned_landing": fmt_time(f["planned_arrival"])},
        "airport_limits": {k: ap[k] for k in ("iata", "size_class", "active_runways", "gates",
                                              "max_runway_ops_per_hour", "min_turnaround_min",
                                              "spare_gates_at_any_time")},
        "rules": {
            "turns": sc["rules"].turns,
            "turn_order": ["ATC", "PILOT", "GROUND"],
            "consensus": "all three agents' latest messages support the same proposal, and it passes every hard rule",
            "final_turn": "in the last turn you may only object by citing a hard rule that fails",
            "fallback": "after the last turn without consensus a deterministic non-AI fallback decides",
            "authority": {"ATC": "atc.priority, atc.fixed_times, atc.departure_moves, df.landing_time",
                          "PILOT": "df.action, df.departure_time, df.reroute_for_turbulence",
                          "GROUND": "ground.gate_changes, df.gate"},
        },
    }
    atc = {
        "runways": [{"id": r["id"], "use": r["use"]} for r in ap["runways"].values()],
        "runway_effects": [e for e in sc["effects"] if e["type"] in ("runway_closure", "capacity_reduction")],
        "arrivals_queue": [
            {"id": k, "callsign": (f["callsign"] if k == DF else o[k]["callsign"]),
             "aircraft": (f["aircraft"] if k == DF else o[k]["aircraft"])["type"],
             "wake": (f["aircraft"] if k == DF else o[k]["aircraft"])["wake_category"],
             "planned": fmt_time(v["planned"]), "first_come_first_served_landing": fmt_time(v["landing"]),
             "order": v["order"],
             "movable": True if k == DF else o[k]["movable"],
             "spare_holding_min": None if k == DF else o[k]["spare_holding_min"],
             "crew_duty_end": None if k == DF else fmt_time(o[k]["crew_duty_end"])}
            for k, v in sorted(b["sequence"].items(), key=lambda kv: kv[1]["order"])],
        "departures": [{"id": k, "callsign": v["callsign"], "time": fmt_time(v["planned_time"]),
                        "runway": v["runway"], "movable": v["movable"],
                        "crew_duty_end": fmt_time(v["crew_duty_end"])}
                       for k, v in sorted(o.items(), key=lambda kv: kv[1]["planned_time"]) if v["kind"] == "departure"],
        "diverted_because_of_weather": b["weather_diversions"],
        "note": "Other flights have no agent. Moving them is allowed (except movable=false) but the "
                "checker rejects any plan that runs one past its spare fuel or crew duty.",
    }
    fp = f["fuel_plan"]
    pilot = {
        "aircraft": f["aircraft"],
        "fuel_on_board_kg": f["fuel_on_board_kg"],
        "fuel_plan": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in fp.items()},
        "alternate_airport": f["alternate"],
        "latest_landing_keeping_alternate": fmt_time(f["planned_arrival"] + fp["holding_before_divert_min"]),
        "latest_landing_if_committed": fmt_time(f["planned_arrival"] + fp["holding_if_committed_min"]),
        "crew_report_time": fmt_time(f["crew_report_time"]),
        "crew_duty_end": fmt_time(f["crew_duty_end"]),
        "connections": [{"to_flight": c["to_flight"], "passengers": c["passengers"],
                         "departs": fmt_time(o[c["to_flight"]]["planned_time"]),
                         "min_connection_min": c["min_connection_min"]} for c in f["connections"]],
        "route_turbulence": f["route_turbulence"],
        "taxi_in_min": f["taxi_in_min"],
        "first_come_first_served": {"landing": fmt_time(b["df_landing"]), "position_in_queue": b["df_order"],
                                    "fuel_status": b["df_fuel"]["status"]},
    }
    if sc["phase"] == "airborne":
        pilot["minutes_to_destination"] = f["minutes_to_destination"]
    else:
        pilot["planned_departure"] = fmt_time(f["planned_departure"])
        pilot["earliest_departure"] = fmt_time(f["planned_departure"] + late)
    ground = {
        "gates_list": [{"id": g["id"], "size": g["size"], "occupied_by": g["occupied_by"],
                        "occupied_until": fmt_time(g["occupied_until"]) if g["occupied_until"] is not None else None}
                       for g in ap["gates"].values()],
        "gate_effects": [e for e in sc["effects"] if e["type"] in ("gate_unavailable", "taxiway_closure")],
        "ground_crews": [{"id": c["id"], "shift_start": fmt_time(c["shift_start"]),
                          "shift_end": fmt_time(c["shift_end"]), "flights": c["flights"]}
                         for c in ap["ground_crews"].values()],
        "df_assigned_gate": f["assigned_gate"],
        "df_gate_size_needed": f["aircraft"]["gate_size"],
        "df_first_come_first_served_in_block": fmt_time(b["df_landing"] + f["taxi_in_min"]),
        "gate_change_notice_min": sc["rules"].ground["gate_change_notice_min"],
        "overtime_multiplier": sc["rules"].ground["overtime_multiplier"],
        "other_arrival_gates": {k: v["gate"] for k, v in o.items() if v["kind"] == "arrival"},
    }
    return {"public": public, "ATC": atc, "PILOT": pilot, "GROUND": ground}
