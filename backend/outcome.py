"""Costs of a resolution (soft goals) and each agent's score. Deterministic, no AI.

    ev = evaluate(sc, check_result)

Scores are 0-1, higher is better:
  ATC    = 0.5*(1 - min(1, extra delay pushed onto other flights / 60 min))
         + 0.5*(1 - min(1, average arrival wait / 60 min))
  PILOT  = 0.4*fuel score + 0.4*share of connecting passengers kept + 0.2*turbulence avoided
           (fuel score: normal 1, minimum_fuel 0.5, diverted 0.3, mayday_fuel 0)
  GROUND = 1 - 0.5*min(1, overtime cost-weighted minutes / 60)
             - 0.25*min(1, gate changes / 5) - 0.25*min(1, DF gate delay vs schedule / 60)
"""
import constraints as K
from scenario import DF

FUEL_SCORE = {"normal": 1.0, "minimum_fuel": 0.5, "diverted": 0.3, "mayday_fuel": 0.0}
_base_cache = {}


def _baseline(sc):
    key = id(sc)
    if key not in _base_cache:
        _base_cache[key] = K.baseline(sc)["resolved"]
    return _base_cache[key]


def evaluate(sc, res):
    r = res["resolved"]
    se = res["effective"]
    b = _baseline(sc)
    f, o, R = se["flight"], sc["other_flights"], sc["rules"]
    df = r["df"]
    sched_in = sc["disruption"]["flight"]["times"]["scheduled_arrival"]
    diverted = df["action"] == "divert"

    # ------------------------------------------------------------------ DF
    if diverted:
        df_cost = {"diverted_to": df["alternate"], "delay_vs_schedule_min": None, "holding_min": 0.0,
                   "holding_fuel_kg": 0.0, "fuel_status": "diverted"}
    else:
        df_cost = {"delay_vs_schedule_min": df["in_block"] - sched_in,
                   "holding_min": df["holding_min"],
                   "holding_fuel_kg": df["holding_min"] * f["fuel_plan"]["holding_burn_kg_per_min"],
                   "reroute_fuel_kg": f["reroute_extra_min"] * f["fuel_plan"]["cruise_burn_kg_per_min"],
                   "gate_wait_min": df["gate_wait_min"],
                   "fuel_status": df["fuel_status"]["status"],
                   "fuel_at_landing_kg": df["fuel_status"]["fuel_at_landing_kg"]}

    # ------------------------------------------------------------------ other flights
    moved = {}
    for k, v in r["sequence"].items():
        if k != DF and k in b["sequence"]:
            d = v["landing"] - b["sequence"][k]["landing"]
            if abs(d) > 1e-6:
                moved[k] = d
    for k, d in r["departures"].items():
        if d["moved_min"] > 0:
            moved[k] = d["moved_min"]
    later = {k: d for k, d in moved.items() if d > 0}

    # ------------------------------------------------------------------ connections
    conns, kept, total = [], 0, 0
    for c in f["connections"]:
        dep_t = r["departures"][c["to_flight"]]["time"]
        gap = None if diverted else dep_t - df["in_block"]
        ok = (not diverted) and gap >= c["min_connection_min"]
        total += c["passengers"]
        kept += c["passengers"] if ok else 0
        conns.append({"to_flight": c["to_flight"], "passengers": c["passengers"], "gap_min": gap, "kept": ok})

    # ------------------------------------------------------------------ ground-crew overtime
    turn = sc["airport"]["min_turnaround_min"]

    def overtime(rr):
        tot = {}
        for cid, c in sc["airport"]["ground_crews"].items():
            ends = [rr["arrivals"][fid]["in_block"] + turn for fid in c["flights"] if fid in rr["arrivals"]]
            ends += [rr["departures"][fid]["time"] for fid in c["flights"] if fid in rr["departures"]]
            tot[cid] = max([0.0] + [e - c["shift_end"] for e in ends])
        return tot
    ot, ot_base = overtime(r), overtime(b)
    ot_min = sum(ot.values())
    ot_cost = ot_min * R.ground["overtime_multiplier"]

    gate_changes = sum(1 for k, g in r["gate_of"].items()
                       if k != DF and g != o[k]["gate"]) + (0 if diverted or df["gate"] == sc["flight"]["assigned_gate"] else 1)
    turbulence = "avoided" if f["reroute_extra_min"] else sc["flight"]["route_turbulence"]

    waits = [v["wait_min"] for v in r["sequence"].values()]
    avg_wait = sum(waits) / len(waits) if waits else 0.0
    extra_total = sum(later.values())
    scores = {
        "ATC": 0.5 * (1 - min(1, extra_total / 60)) + 0.5 * (1 - min(1, avg_wait / 60)),
        "PILOT": 0.4 * FUEL_SCORE[df_cost["fuel_status"]] + 0.4 * (kept / total if total else 1.0)
                 + 0.2 * (1.0 if turbulence in ("none", "avoided") else 0.0),
        "GROUND": 1 - 0.5 * min(1, ot_cost / 60) - 0.25 * min(1, gate_changes / 5)
                  - 0.25 * min(1, max(0.0, (df_cost["delay_vs_schedule_min"] or 0)) / 60),
    }
    return {
        "df": df_cost,
        "others": {"moved": moved, "later_count": len(later), "extra_delay_total_min": extra_total,
                   "extra_delay_max_min": max(later.values()) if later else 0.0,
                   "weather_diversions": r["weather_diversions"], "average_arrival_wait_min": avg_wait},
        "connections": {"detail": conns, "passengers_kept": kept, "passengers_total": total,
                        "passengers_missed": total - kept},
        "ground": {"overtime_min_by_crew": ot, "overtime_min": ot_min,
                   "overtime_extra_vs_baseline_min": ot_min - sum(ot_base.values()),
                   "overtime_cost_weighted_min": ot_cost, "gate_changes": gate_changes},
        "turbulence": turbulence,
        "scores": {k: round(v, 3) for k, v in scores.items()},
    }
