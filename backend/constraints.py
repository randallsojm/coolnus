"""Constraint checker: turns a proposal into concrete times, then checks every hard rule.

    res = check(sc, proposal)
    res["ok"]          True only if every hard rule passes
    res["results"]     [{"rule", "domain", "hard": True, "pass", "detail"}]
    res["resolved"]    concrete times: DF landing/gate/in-block, the landing sequence, gates, departures

Deterministic, no AI. Uses only workbook limits (via sc["rules"]) and config.py assumptions.
"""
import airside as A
import config as CFG
from messages import Proposal, default_proposal
from scenario import DF, GATE_SIZES, fmt_time, parse_time

RULES = {
    "PROPOSAL_VALID": "ALL",
    "ATC_WEATHER_HOLD": "ATC", "ATC_WAKE_SEPARATION": "ATC", "ATC_RUNWAY_CAPACITY": "ATC",
    "ATC_RUNWAY_USE": "ATC", "ATC_FIXED_TIMES": "ATC", "ATC_IMMOVABLE": "ATC", "OTHERS_SPARE_FUEL": "ATC",
    "PILOT_FINAL_RESERVE": "PILOT", "PILOT_MAYDAY_PRIORITY": "PILOT", "PILOT_DIVERT_FUEL": "PILOT",
    "PILOT_CREW_DUTY": "PILOT",
    "GROUND_GATE_FIT": "GROUND", "GROUND_GATE_CONFLICT": "GROUND", "GROUND_GATE_UNAVAILABLE": "GROUND",
    "GROUND_GATE_CHANGE_NOTICE": "GROUND",
}
EPS = 1e-6


def _t(v, what, problems):
    if v is None:
        return None
    try:
        return parse_time(v)
    except ValueError as e:
        problems.append(f"{what}: {e}")
        return None


def late_inbound(sc):
    return sum(e["delay_min"] for e in sc["effects"] if e["type"] == "late_inbound_aircraft")


def effective_scenario(sc, p: Proposal, problems):
    """Apply the Pilot's and ATC's departure choices: DF's effective planned landing and fuel,
    and moved departures. Returns a shallow-copied scenario."""
    f = sc["flight"]
    phase = sc["phase"]
    dep = None
    if phase == "airborne":
        shift = 0
        if p.df.action == "delay_departure_then_land":
            problems.append("df.action 'delay_departure_then_land' only applies on the ground")
        if p.df.departure_time is not None:
            problems.append("df.departure_time only applies on the ground")
    else:
        if p.df.action == "divert":
            problems.append("df.action 'divert' only applies to an airborne flight")
        ready = f["planned_departure"] + late_inbound(sc)
        dep = ready
        if p.df.action == "delay_departure_then_land":
            want = _t(p.df.departure_time, "df.departure_time", problems)
            if want is None:
                problems.append("df.departure_time is required for 'delay_departure_then_land'")
            elif want < ready - EPS:
                problems.append(f"df.departure_time {fmt_time(want)} is before the aircraft is ready "
                                f"({fmt_time(ready)})")
            else:
                dep = want
        elif p.df.departure_time is not None:
            problems.append("df.departure_time is only used with action 'delay_departure_then_land'")
        shift = dep - f["planned_departure"]
    extra = CFG.TURBULENCE_REROUTE_EXTRA_MIN.get(f["route_turbulence"], 0) if p.df.reroute_for_turbulence else 0
    fp = dict(f["fuel_plan"])
    if extra:
        dk = extra * fp["cruise_burn_kg_per_min"]
        hold = fp["holding_burn_kg_per_min"]
        fp["on_landing_as_planned_kg"] -= dk
        fp["holding_before_divert_min"] -= dk / hold
        fp["holding_if_committed_min"] -= dk / hold
    flight = dict(f, planned_arrival=f["planned_arrival"] + shift + extra, fuel_plan=fp,
                  effective_departure=dep, reroute_extra_min=extra)

    others = dict(sc["other_flights"])
    for fid, tv in p.atc.departure_moves.items():
        o = others.get(fid)
        if o is None or o["kind"] != "departure":
            problems.append(f"atc.departure_moves: {fid} is not a departure")
            continue
        t = _t(tv, f"atc.departure_moves[{fid}]", problems)
        if t is None:
            continue
        if t < o["planned_time"] - EPS:
            problems.append(f"atc.departure_moves: {fid} can only move later than {fmt_time(o['planned_time'])}")
            continue
        others[fid] = dict(o, planned_time=t, moved_from=o["planned_time"])
    return dict(sc, flight=flight, other_flights=others)


def resolve(sc, p: Proposal):
    problems = []
    o = sc["other_flights"]
    diverted_wx = list(sc["baseline_fcfs"]["weather_diversions"])
    arrivals_ids = {DF} | {k for k, v in o.items() if v["kind"] == "arrival"}
    for fid in p.atc.priority:
        if fid not in arrivals_ids:
            problems.append(f"atc.priority: {fid} is not an arrival")
    if len(set(p.atc.priority)) != len(p.atc.priority):
        problems.append("atc.priority lists a flight twice")
    for fid in list(p.atc.fixed_times) + list(p.ground.gate_changes):
        if fid == DF or fid not in o or o[fid]["kind"] != "arrival":
            problems.append(f"{fid}: fixed_times and gate_changes are for other arrivals (DF uses df.*)")
    for fid in set(p.atc.priority) | set(p.atc.fixed_times) | set(p.ground.gate_changes):
        if fid in diverted_wx:
            problems.append(f"{fid} has already diverted because of the weather")
    gates = sc["airport"]["gates"]
    for fid, g in p.ground.gate_changes.items():
        if g not in gates:
            problems.append(f"ground.gate_changes: gate {g} does not exist")
    df_gate = p.df.gate or sc["flight"]["assigned_gate"]
    if df_gate not in gates:
        problems.append(f"df.gate {df_gate} does not exist")
        df_gate = sc["flight"]["assigned_gate"]

    se = effective_scenario(sc, p, problems)
    divert = p.df.action == "divert" and sc["phase"] == "airborne"
    fixed = {}
    for fid, tv in p.atc.fixed_times.items():
        t = _t(tv, f"atc.fixed_times[{fid}]", problems)
        if t is not None and fid in o:
            fixed[fid] = t
    if p.df.landing_time is not None and not divert:
        t = _t(p.df.landing_time, "df.landing_time", problems)
        if t is not None:
            fixed[DF] = t
    exclude = diverted_wx + ([DF] if divert else [])
    priority = [x for x in p.atc.priority if x in arrivals_ids and x not in exclude]
    seq = A.landing_sequence(se, priority=priority, fixed=fixed or None, exclude=exclude)

    # ---------------------------------------------------------------- gates and in-block times
    ap = sc["airport"]
    turn = ap["min_turnaround_min"]
    decision = sc["start_time"] + CFG.DECISION_LATENCY_MIN
    timeline = {g: [] for g in gates}                 # gate -> [(start, end, who)]
    so = se["other_flights"]
    for g in gates.values():
        who = g["occupied_by"]
        if who is None:
            continue
        end = g["occupied_until"]
        if who in so and so[who]["kind"] == "departure" and "moved_from" in so[who]:
            end = so[who]["planned_time"] - CFG.DEPARTURE_GATE_RELEASE_MIN
        if end is not None and end > sc["start_time"] - 1e5:
            timeline[g["id"]].append((-1e9, end, who))
    unavailable = {}
    for e in sc["effects"]:
        if e["type"] == "gate_unavailable":
            unavailable.setdefault(e["gate"], []).append((e["start"], e["end"]))
    taxi_extra = [e for e in sc["effects"] if e["type"] == "taxiway_closure"]

    gate_of = {fid: (p.ground.gate_changes.get(fid) or v["gate"]) for fid, v in o.items() if v["kind"] == "arrival"}
    gate_of[DF] = df_gate
    arr_info = {}
    for fid, v in sorted(seq.items(), key=lambda kv: (kv[1]["landing"], kv[0])):
        taxi = se["flight"]["taxi_in_min"] if fid == DF else o[fid]["taxi_in_min"]
        g = gate_of[fid]
        extra = sum(e["extra_taxi_min"] for e in taxi_extra
                    if g in e["affected_gates"] and e["start"] <= v["landing"] < e["end"])
        ib0 = v["landing"] + taxi + extra
        cands = sorted({ib0} | {b for a, b, _ in timeline.get(g, []) if b > ib0}
                       | {b for a, b in unavailable.get(g, []) if b > ib0})
        t_in = None
        for t in cands:
            clash = any(a < t + turn - EPS and t < b - EPS for a, b, _ in timeline.get(g, []))
            blocked = any(a <= t < b for a, b in unavailable.get(g, []))
            if not clash and not blocked:
                t_in = t
                break
        if t_in is None:
            t_in = max([ib0] + [b for a, b, _ in timeline.get(g, [])])
        timeline.setdefault(g, []).append((t_in, t_in + turn, fid))
        arr_info[fid] = {"gate": g, "in_block": t_in, "gate_wait_min": t_in - ib0, "taxi_extra_min": extra}

    f = se["flight"]
    df = {"action": "divert" if divert else p.df.action, "gate": None if divert else df_gate,
          "planned_landing_effective": f["planned_arrival"], "reroute_extra_min": f["reroute_extra_min"],
          "departure_time": f.get("effective_departure")}
    if divert:
        t_alt = sc["start_time"] + f["minutes_to_destination"] + f["fuel_plan"]["alternate_flight_min"]
        df.update(alternate=f["alternate"]["airport"], arrival_at_alternate=t_alt,
                  fuel_status={"status": "diverted"})
    else:
        df.update(landing=seq[DF]["landing"], runway=seq[DF]["runway"], in_block=arr_info[DF]["in_block"],
                  gate_wait_min=arr_info[DF]["gate_wait_min"],
                  holding_min=max(0.0, seq[DF]["landing"] - f["planned_arrival"]),
                  fuel_status=A.fuel_status(se, f, seq[DF]["landing"]))
    departures = {k: {"time": v["planned_time"], "runway": v["runway"],
                      "moved_min": v["planned_time"] - v.get("moved_from", v["planned_time"])}
                  for k, v in so.items() if v["kind"] == "departure"}
    return {"problems": problems, "df": df, "sequence": seq, "arrivals": arr_info, "departures": departures,
            "timeline": timeline, "weather_diversions": diverted_wx, "decision_time": decision,
            "fixed": fixed, "priority": priority, "gate_of": gate_of}, se


def check(sc, p: Proposal):
    if not isinstance(p, Proposal):
        p = Proposal.model_validate(p)
    r, se = resolve(sc, p)
    R, ap, o = sc["rules"], sc["airport"], sc["other_flights"]
    seq, base = r["sequence"], sc["baseline_fcfs"]["sequence"]
    out = []

    def add(rule, bad, ok_detail=""):
        out.append({"rule": rule, "domain": RULES[rule], "hard": True, "pass": not bad,
                    "detail": "; ".join(bad[:6]) + (f" (+{len(bad) - 6} more)" if len(bad) > 6 else "")
                    if bad else ok_detail})

    add("PROPOSAL_VALID", r["problems"])

    # ---------------------------------------------------------------- ATC
    holds = A.holds(sc)
    add("ATC_WEATHER_HOLD", [f"{k} lands {fmt_time(v['landing'])} during the weather hold "
                             f"{fmt_time(h['start'])}-{fmt_time(h['end'])}"
                             for k, v in seq.items() for h in holds if h["start"] <= v["landing"] < h["end"]])
    fl = {x["id"]: x for x in A.arrivals(se)}
    bad = []
    for rw in ap["runways"]:
        lands = sorted((v["landing"], k) for k, v in seq.items() if v["runway"] == rw)
        for (t1, a), (t2, b) in zip(lands, lands[1:]):
            need = A.wake_gap_min(R, fl[a], fl[b])
            if t2 - t1 < need - EPS:
                bad.append(f"{a}->{b} on {rw}: {t2 - t1:.2f} min, needs {need:.2f}")
    add("ATC_WAKE_SEPARATION", bad)
    moves = [v["landing"] for v in seq.values()] + [d["time"] for d in r["departures"].values()]
    capf = A.cap_fn(sc)
    bad = []
    for s0 in sorted(set(moves)):
        n = sum(1 for m in moves if s0 <= m < s0 + 60)
        if n > capf(s0):
            bad.append(f"{n} movements in the hour from {fmt_time(s0)} (limit {capf(s0)})")
    add("ATC_RUNWAY_CAPACITY", bad)
    bad = []
    closures = [e for e in sc["effects"] if e["type"] == "runway_closure"]
    dep_gap = R.wake_default["dep_s"] / 60
    for k, v in seq.items():
        use = ap["runways"][v["runway"]]["use"]
        if use not in ("arrivals", "mixed"):
            bad.append(f"{k} lands on {v['runway']} ({use})")
        for e in closures:
            if e["runway"] == v["runway"] and e["start"] <= v["landing"] < e["end"]:
                bad.append(f"{k} lands on closed runway {v['runway']}")
        if use == "mixed":
            for dk, d in r["departures"].items():
                if d["runway"] == v["runway"] and abs(d["time"] - v["landing"]) < dep_gap - EPS:
                    bad.append(f"{k} lands within {dep_gap:g} min of departure {dk} on {v['runway']}")
    for dk, d in r["departures"].items():
        if d["moved_min"] > 0:
            for e in closures:
                if e["runway"] == d["runway"] and e["start"] <= d["time"] < e["end"]:
                    bad.append(f"departure {dk} moved into the closure of {d['runway']}")
    add("ATC_RUNWAY_USE", bad)
    add("ATC_FIXED_TIMES", [f"{k} can't land at {fmt_time(t)} (earliest legal {fmt_time(seq[k]['landing'])})"
                            for k, t in r["fixed"].items()
                            if k in seq and (seq[k]["fixed_infeasible"] or abs(seq[k]["landing"] - t) > EPS)])
    bad = []
    for k, v in o.items():
        if v["movable"]:
            continue
        if k in r["priority"] or k in r["fixed"] or k in r["departures"] and r["departures"][k]["moved_min"] > 0:
            bad.append(f"{k} is not movable but the proposal moves it")
        if k in seq and k in base and seq[k]["landing"] > base[k]["landing"] + EPS:
            bad.append(f"{k} is not movable but would land {seq[k]['landing'] - base[k]['landing']:.1f} min later")
    add("ATC_IMMOVABLE", bad)
    add("OTHERS_SPARE_FUEL", [f"{k} would wait {v['wait_min']:.1f} min, spare fuel {o[k]['spare_holding_min']:g} min"
                              for k, v in seq.items() if k != DF and not A.other_arrival_ok(sc, k, v["landing"])])

    # ---------------------------------------------------------------- PILOT
    df, f = r["df"], se["flight"]
    if df["action"] == "divert":
        add("PILOT_FINAL_RESERVE", [])
        add("PILOT_MAYDAY_PRIORITY", [])
        hb = f["fuel_plan"]["holding_before_divert_min"]
        add("PILOT_DIVERT_FUEL", [] if hb >= -EPS else
            [f"not enough fuel to reach {f['alternate']['airport']} with the final reserve ({hb:.1f} min short)"])
        t_end = df["arrival_at_alternate"]
    else:
        st = df["fuel_status"]
        add("PILOT_FINAL_RESERVE", [] if st["status"] != "mayday_fuel" else
            [f"DF lands with {st['fuel_at_landing_kg']} kg, below the {st['final_reserve_kg']} kg final reserve"])
        bad = []
        if st["status"] == "mayday_fuel":
            earliest = A.landing_sequence(se, priority=[DF], exclude=r["weather_diversions"])[DF]["landing"]
            if df["landing"] > earliest + EPS:
                bad.append(f"DF is in mayday fuel and must land at {fmt_time(earliest)}, not {fmt_time(df['landing'])}")
        add("PILOT_MAYDAY_PRIORITY", bad)
        add("PILOT_DIVERT_FUEL", [])
        t_end = df["landing"]
    bad = []
    if t_end > f["crew_duty_end"] + EPS:
        bad.append(f"DF crew exceeds {R.crew['max_duty_h']:g} h duty ({fmt_time(t_end)} > {fmt_time(f['crew_duty_end'])})")
    for k, v in seq.items():
        if k != DF and v["landing"] > o[k]["crew_duty_end"] + EPS and \
                not (k in base and base[k]["landing"] > o[k]["crew_duty_end"] + EPS):
            bad.append(f"{k} crew exceeds duty because of this resolution")
    for k, d in r["departures"].items():
        if d["moved_min"] > 0 and d["time"] > o[k]["crew_duty_end"] + EPS:
            bad.append(f"departure {k} moved past its crew duty end")
    add("PILOT_CREW_DUTY", bad)

    # ---------------------------------------------------------------- GROUND
    bad = []
    for k, a in r["arrivals"].items():
        acft = f["aircraft"] if k == DF else o[k]["aircraft"]
        gs = sc["airport"]["gates"][a["gate"]]["size"]
        if GATE_SIZES.index(gs) < GATE_SIZES.index(acft["gate_size"]):
            bad.append(f"{k} ({acft['type']}) needs a {acft['gate_size']} gate; {a['gate']} is {gs}")
    add("GROUND_GATE_FIT", bad)
    bad = []
    for g, ivs in r["timeline"].items():
        ivs = sorted(ivs)
        for (a1, b1, w1), (a2, b2, w2) in zip(ivs, ivs[1:]):
            if a2 < b1 - EPS:
                bad.append(f"{w1} and {w2} overlap at {g}")
    add("GROUND_GATE_CONFLICT", bad)
    bad = []
    for e in sc["effects"]:
        if e["type"] == "gate_unavailable":
            for k, a in r["arrivals"].items():
                if a["gate"] == e["gate"] and e["start"] <= a["in_block"] < e["end"]:
                    bad.append(f"{k} parks at unavailable gate {e['gate']}")
    add("GROUND_GATE_UNAVAILABLE", bad)
    notice = R.ground["gate_change_notice_min"]
    changed = {k: g for k, g in p.ground.gate_changes.items() if g != o.get(k, {}).get("gate")}
    if df["gate"] and df["gate"] != sc["flight"]["assigned_gate"]:
        changed[DF] = df["gate"]
    add("GROUND_GATE_CHANGE_NOTICE",
        [f"{k}: gate change to {g} with {r['arrivals'][k]['in_block'] - r['decision_time']:.0f} min notice "
         f"(needs {notice:g})" for k, g in changed.items()
         if k in r["arrivals"] and r["arrivals"][k]["in_block"] - r["decision_time"] < notice - EPS])

    for x in out:
        assert x["rule"] in RULES
    return {"ok": all(x["pass"] for x in out), "results": out, "resolved": r, "effective": se}


def failed(res):
    return [x["rule"] for x in res["results"] if not x["pass"]]


def visible_to(res, role):
    """Constraint results an agent may see: pass/fail for every rule, details only for its own domain."""
    return [{"rule": x["rule"], "pass": x["pass"],
             "detail": x["detail"] if x["domain"] in ("ALL", role) else ""} for x in res["results"]]


def baseline(sc):
    return check(sc, default_proposal(sc))
