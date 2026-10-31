"""Write the consensus log: JSON (complete) and TXT (readable transcript).

    json_path, txt_path = write(result, sc, agents_kind)
"""
import datetime as dt
import json

import config as CFG
from scenario import DF, fmt_time


def _resolution(sc, res, ev):
    r = res["resolved"]
    df = r["df"]
    o = {
        "flight_plan": df["action"],
        "revised_arrival_time": None if df["action"] == "divert" else fmt_time(df["landing"]),
        "revised_in_block_time": None if df["action"] == "divert" else fmt_time(df["in_block"]),
        "runway": df.get("runway"), "gate": df.get("gate"),
        "departure_time": fmt_time(df["departure_time"]) if df.get("departure_time") is not None else None,
        "reroute_for_turbulence": bool(df["reroute_extra_min"]),
    }
    if df["action"] == "divert":
        o.update(divert_to=df["alternate"], arrival_at_alternate=fmt_time(df["arrival_at_alternate"]))
    else:
        o.update(fuel_status=df["fuel_status"]["status"], fuel_at_landing_kg=df["fuel_status"]["fuel_at_landing_kg"])
    return o


def _sequence(res):
    return [{"flight": k, "landing": fmt_time(v["landing"]), "runway": v["runway"],
             "wait_min": round(v["wait_min"], 1)}
            for k, v in sorted(res["resolved"]["sequence"].items(), key=lambda kv: kv[1]["landing"])]


def _round(x):
    if isinstance(x, float):
        return round(x, 2)
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_round(v) for v in x]
    return x


def build(result, sc, agents_kind):
    res, ev = result["check"], result["outcome"]
    d = sc["disruption"]
    limits = {"airport": {k: sc["airport"][k] for k in ("iata", "size_class", "active_runways", "gates",
                                                         "max_runway_ops_per_hour", "min_turnaround_min",
                                                         "spare_gates_at_any_time")},
              **sc["rules"].as_dict()}
    turns = []
    for rec in result["records"]:
        t = dict(rec)
        if rec["new_proposal_id"]:
            pid = rec["new_proposal_id"]
            p = result["proposals"][pid]
            t["new_proposal"] = p["proposal"].model_dump()
            t["constraint_results"] = p["check"]["results"]
            from outcome import evaluate
            t["scores"] = evaluate(sc, p["check"])["scores"]
        turns.append(t)
    return _round({
        "input": {"row_id": d["row_id"], "flights_csv_line": d["file_line"], "scenario": sc.get("source"),
                  "scenario_id": sc["scenario_id"], "phase": sc["phase"], "start_time": fmt_time(sc["start_time"]),
                  "flight": d["flight"]["callsign"], "route": f"{d['flight']['origin']}->{sc['airport']['iata']}",
                  "cause": d["disruption"]["type"], "agents": agents_kind, "model": CFG.MODEL if agents_kind == "claude" else None},
        "limits_used": limits,
        "stated_changes": sc["stated_changes"],
        "assumptions": CFG.ASSUMPTIONS,
        "turns": turns,
        "consensus": result["consensus"],
        "resolution": {
            **_resolution(sc, res, ev),
            "decided_by": result["decided_by"],
            "fallback_reason": result["fallback_reason"],
            "fallback_notes": result["fallback_notes"],
            "final_proposal": result["final_proposal"].model_dump(),
            "constraint_results": res["results"],
            "all_hard_rules_pass": res["ok"],
            "outcome": ev,
            "other_flights_moved_min": ev["others"]["moved"],
            "other_flights_diverted_by_weather": res["resolved"]["weather_diversions"],
            "landing_sequence": _sequence(res),
        },
        "baseline_first_come_first_served": {
            **_resolution(sc, result["baseline"]["check"], result["baseline"]["outcome"]),
            "all_hard_rules_pass": result["baseline"]["check"]["ok"],
            "failed_rules": [x["rule"] for x in result["baseline"]["check"]["results"] if not x["pass"]],
            "outcome": result["baseline"]["outcome"],
        },
        "messages_sent": result["messages_sent"],
        "agent_stats": result["agent_stats"],
        "tool_calls": result["tool_calls"],
        "api_transcripts": result["transcripts"],
    })


def text(log):
    L = []
    i, res = log["input"], log["resolution"]
    L.append(f"CONSENSUS LOG  {i['scenario_id']}  ({i['agents']} agents{', ' + i['model'] if i['model'] else ''})")
    L.append(f"Disruption: flights.csv row {i['row_id']} (line {i['flights_csv_line']}): {i['flight']} {i['route']}, "
             f"{i['cause']}, {i['phase'].replace('_', ' ')}; negotiation starts {i['start_time']}")
    b = log["baseline_first_come_first_served"]
    L.append(f"Without negotiation (first come, first served): "
             + (f"DF lands {b['revised_arrival_time']} ({b.get('fuel_status')})" if b["revised_arrival_time"] else "DF diverts")
             + ("" if b["all_hard_rules_pass"] else f"  BREAKS: {', '.join(b['failed_rules'])}"))
    L.append("")
    turn = None
    for t in log["turns"]:
        if t["turn"] != turn:
            turn = t["turn"]
            L.append(f"--- Turn {turn} ---")
        head = f"{t['sender']:<6} {t['type'] or 'NO MESSAGE':<8}"
        if t["type"] in ("ACCEPT", "REJECT", "COUNTER"):
            head += f" {t['responds_to']}"
        if t["new_proposal_id"]:
            c = t["check_new"]
            head += f" -> {t['new_proposal_id']}: {c['summary']}; hard rules {'pass' if c['ok'] else 'FAIL ' + ', '.join(c['failed'])}"
        L.append(head)
        for o in t["objections"]:
            L.append(f"         objection {o['rule']} ({'hard' if o['hard'] else 'soft'}): {o['detail']}")
        if t["priority_request"]:
            pr = t["priority_request"]
            L.append(f"         PRIORITY REQUEST {pr['level']}: land by {pr['latest_landing_time']}, "
                     f"{pr['holding_remaining_min']} min holding left. {pr['reason']}")
        if t["rationale"]:
            L.append(f"         \"{t['rationale']}\"")
        if not t["valid"]:
            L.append(f"         INVALID: {'; '.join(t['errors'])}")
    L.append("")
    c = log["consensus"]
    L.append(f"Consensus: {'reached in turn ' + str(c['turn']) + ' on ' + c['proposal_id'] if c['reached'] else 'NOT reached'}")
    if res["fallback_reason"]:
        L.append(f"Fallback used. Why: {res['fallback_reason']}")
        L.append(f"Fallback decided: {'; '.join(res['fallback_notes'])}")
    L.append("")
    L.append("RESOLUTION (decided by " + res["decided_by"] + ")")
    if res["flight_plan"] == "divert":
        L.append(f"  Flight plan: DIVERT to {res['divert_to']}, arriving {res['arrival_at_alternate']}")
    else:
        L.append(f"  Revised arrival: lands {res['revised_arrival_time']} on {res['runway']}, gate {res['gate']} "
                 f"at {res['revised_in_block_time']}; fuel {res['fuel_status']} ({res['fuel_at_landing_kg']} kg at landing)")
        L.append(f"  Flight plan: {res['flight_plan']}" + (f", take-off {res['departure_time']}" if res["departure_time"] else "")
                 + (", rerouted around turbulence" if res["reroute_for_turbulence"] else ""))
    ev = res["outcome"]
    L.append(f"  Hard rules: {'all pass' if res['all_hard_rules_pass'] else 'FAIL'}")
    L.append(f"  Other flights: {ev['others']['later_count']} later by {ev['others']['extra_delay_total_min']} min in total "
             f"(max {ev['others']['extra_delay_max_min']} min); {len(ev['others']['weather_diversions'])} diverted by the weather")
    L.append(f"  Connections: {ev['connections']['passengers_kept']} of {ev['connections']['passengers_total']} passengers kept")
    L.append(f"  Ground crew overtime: {ev['ground']['overtime_min']} min (cost-weighted {ev['ground']['overtime_cost_weighted_min']})")
    L.append(f"  Scores: {ev['scores']}")
    L.append("")
    L.append("STATED CHANGES TO WORKBOOK LIMITS")
    for s in log["stated_changes"]:
        L.append(f"  - {s['limit']}: workbook {s['workbook_value']}, used {s['used_value']}" + (f" ({s['note']})" if s.get("note") else ""))
    L.append("ASSUMPTIONS")
    for a in log["assumptions"]:
        L.append(f"  - {a}")
    return "\n".join(L)


def write(result, sc, agents_kind):
    CFG.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = build(result, sc, agents_kind)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = CFG.LOG_DIR / f"{sc['scenario_id']}_{agents_kind}_{stamp}"
    jp, tp = base.with_suffix(".json"), base.with_suffix(".txt")
    jp.write_text(json.dumps(log, indent=1, default=str))
    tp.write_text(text(log))
    return jp, tp, log
