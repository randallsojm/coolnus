"""Deterministic tools the agents call for every number. Each role gets only its own tools,
plus check_proposal and get_my_score (which only show details from the caller's domain).
"""
import json

import airside as A
import constraints as K
import outcome as O
from messages import Proposal
from scenario import DF, GATE_SIZES, fmt_time, parse_time


def _inline(schema):
    """Resolve $ref/$defs so tool schemas are self-contained."""
    defs = schema.get("$defs", {})

    def walk(x):
        if isinstance(x, dict):
            if "$ref" in x:
                return walk(defs[x["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in x.items() if k != "$defs"}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    return walk(schema)


PROPOSAL_SCHEMA = _inline(Proposal.model_json_schema())


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


TIME = {"type": "string", "description": "HH:MM (arrival-airport local time; add +1 for next day)"}
TOOL_SPECS = {
    "check_proposal": ("Run the hard-rule checker on a full proposal. Returns ok, pass/fail for every "
                       "rule (details only for your domain) and the resulting DF landing, gate and in-block time.",
                       _obj({"proposal": PROPOSAL_SCHEMA}, ["proposal"])),
    "get_my_score": ("Your role's 0-1 score and costs for a full proposal (higher is better).",
                     _obj({"proposal": PROPOSAL_SCHEMA}, ["proposal"])),
    "landing_sequence": ("ATC: landing order and times after the weather hold, with optional flights moved "
                         "to the front (priority) and pinned landing times.",
                         _obj({"priority": {"type": "array", "items": {"type": "string"}},
                               "fixed_times": {"type": "object", "additionalProperties": TIME}})),
    "wake_gap": ("ATC: minimum minutes between two landings on one runway (workbook wake rules).",
                 _obj({"leader": {"type": "string"}, "follower": {"type": "string"}}, ["leader", "follower"])),
    "hourly_movements": ("ATC: movements (landings + departures) in the hour starting at a time, first come "
                         "first served, and the limit.", _obj({"time": TIME}, ["time"])),
    "fuel_status_at": ("PILOT: DF fuel status if it lands at this time: normal / minimum_fuel / mayday_fuel, "
                       "fuel at landing and latest landing times.",
                       _obj({"landing_time": TIME, "departure_time": TIME}, ["landing_time"])),
    "connections_at": ("PILOT: which connections are kept if DF reaches its gate (in-block) at this time.",
                       _obj({"in_block_time": TIME}, ["in_block_time"])),
    "crew_duty_status": ("PILOT: whether DF's crew is within the 13 h duty limit at this landing time.",
                         _obj({"landing_time": TIME}, ["landing_time"])),
    "free_gates_at": ("GROUND: gates free for the minimum turnaround starting at this time (first come "
                      "first served traffic), optionally only gates that fit a size.",
                      _obj({"time": TIME, "size": {"type": "string", "enum": list(GATE_SIZES)}}, ["time"])),
    "gate_timeline": ("GROUND: who occupies a gate and when, first come first served.",
                      _obj({"gate": {"type": "string"}}, ["gate"])),
    "overtime_if": ("GROUND: ground-crew overtime if DF reaches its gate at this time.",
                    _obj({"in_block_time": TIME}, ["in_block_time"])),
}
ROLE_TOOLS = {
    "ATC": ["check_proposal", "get_my_score", "landing_sequence", "wake_gap", "hourly_movements"],
    "PILOT": ["check_proposal", "get_my_score", "fuel_status_at", "connections_at", "crew_duty_status"],
    "GROUND": ["check_proposal", "get_my_score", "free_gates_at", "gate_timeline", "overtime_if"],
}


class Toolbox:
    def __init__(self, sc, role):
        self.sc, self.role = sc, role
        self.calls = []

    def definitions(self):
        return [{"name": n, "description": TOOL_SPECS[n][0], "input_schema": TOOL_SPECS[n][1]}
                for n in ROLE_TOOLS[self.role]]

    def call(self, name, args):
        if name not in ROLE_TOOLS[self.role]:
            out = {"error": f"tool {name} is not available to {self.role}"}
        else:
            try:
                out = getattr(self, name)(**(args or {}))
            except Exception as e:          # report to the model, never crash the run
                out = {"error": f"{type(e).__name__}: {e}"}
        self.calls.append({"tool": name, "input": args, "output": out})
        return out

    # ------------------------------------------------------------------ shared
    def check_proposal(self, proposal):
        p = Proposal.model_validate(proposal)
        r = K.check(self.sc, p)
        df = r["resolved"]["df"]
        summary = {"df_action": df["action"],
                   "df_landing": fmt_time(df.get("landing")) if df.get("landing") is not None else None,
                   "df_gate": df.get("gate"),
                   "df_in_block": fmt_time(df.get("in_block")) if df.get("in_block") is not None else None}
        if self.role == "PILOT":
            summary["df_fuel_status"] = df["fuel_status"]["status"]
        return {"ok": r["ok"], "rules": K.visible_to(r, self.role), "summary": summary}

    def get_my_score(self, proposal):
        p = Proposal.model_validate(proposal)
        r = K.check(self.sc, p)
        ev = O.evaluate(self.sc, r)
        mine = {"ATC": {"others_later": ev["others"]["later_count"],
                        "extra_delay_total_min": round(ev["others"]["extra_delay_total_min"], 1),
                        "average_arrival_wait_min": round(ev["others"]["average_arrival_wait_min"], 1)},
                "PILOT": {"fuel_status": ev["df"]["fuel_status"], "connections": ev["connections"],
                          "turbulence": ev["turbulence"]},
                "GROUND": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in ev["ground"].items()}}
        return {"score": ev["scores"][self.role], "hard_rules_ok": r["ok"], "costs": mine[self.role]}

    # ------------------------------------------------------------------ ATC
    def landing_sequence(self, priority=None, fixed_times=None):
        fixed = {k: parse_time(v) for k, v in (fixed_times or {}).items()}
        seq = A.landing_sequence(self.sc, priority=priority or [], fixed=fixed or None,
                                 exclude=self.sc["baseline_fcfs"]["weather_diversions"])
        rows = sorted(seq.items(), key=lambda kv: kv[1]["landing"])
        o = self.sc["other_flights"]
        return {"sequence": [{"id": k, "landing": fmt_time(v["landing"]), "runway": v["runway"],
                              "wait_min": round(v["wait_min"], 1),
                              "over_spare_fuel": k != DF and not A.other_arrival_ok(self.sc, k, v["landing"])}
                             for k, v in rows],
                "flights_over_spare_fuel": [k for k, v in rows if k != DF and not A.other_arrival_ok(self.sc, k, v["landing"])],
                "note": "times are rounded for display; the checker uses exact times"}

    def wake_gap(self, leader, follower):
        fl = {f["id"]: f for f in A.arrivals(self.sc)}
        return {"minutes": round(A.wake_gap_min(self.sc["rules"], fl[leader], fl[follower]), 2)}

    def hourly_movements(self, time):
        s0 = parse_time(time)
        seq = self.sc["baseline_fcfs"]["sequence"]
        moves = [v["landing"] for v in seq.values()] + [v["planned_time"] for v in self.sc["other_flights"].values()
                                                         if v["kind"] == "departure"]
        return {"movements": sum(1 for m in moves if s0 <= m < s0 + 60), "limit": A.cap_fn(self.sc)(s0)}

    # ------------------------------------------------------------------ PILOT
    def _pilot_sc(self, departure_time=None):
        if self.sc["phase"] != "on_ground_at_origin":
            return self.sc
        f = self.sc["flight"]
        ready = f["planned_departure"] + K.late_inbound(self.sc)
        dep = max(ready, parse_time(departure_time)) if departure_time else ready
        return dict(self.sc, flight=dict(f, planned_arrival=f["planned_arrival"] + dep - f["planned_departure"]))

    def fuel_status_at(self, landing_time, departure_time=None):
        se = self._pilot_sc(departure_time)
        st = A.fuel_status(se, se["flight"], parse_time(landing_time))
        st["latest_landing_keeping_alternate"] = fmt_time(st["latest_landing_keeping_alternate"])
        st["latest_landing_if_committed"] = fmt_time(st["latest_landing_if_committed"])
        st["holding_remaining_before_divert_min"] = se["flight"]["fuel_plan"]["holding_before_divert_min"]
        return st

    def connections_at(self, in_block_time):
        t = parse_time(in_block_time)
        o = self.sc["other_flights"]
        out = []
        for c in self.sc["flight"]["connections"]:
            gap = o[c["to_flight"]]["planned_time"] - t
            out.append({"to_flight": c["to_flight"], "passengers": c["passengers"], "gap_min": round(gap, 1),
                        "kept": gap >= c["min_connection_min"]})
        return {"connections": out}

    def crew_duty_status(self, landing_time):
        t = parse_time(landing_time)
        end = self.sc["flight"]["crew_duty_end"]
        return {"within_duty": t <= end, "duty_ends": fmt_time(end), "minutes_left": round(end - t, 1)}

    # ------------------------------------------------------------------ GROUND
    def free_gates_at(self, time, size=None):
        t = parse_time(time)
        turn = self.sc["airport"]["min_turnaround_min"]
        tl = K.baseline(self.sc)["resolved"]["timeline"]
        ua = [e for e in self.sc["effects"] if e["type"] == "gate_unavailable"]
        out = []
        for g in self.sc["airport"]["gates"].values():
            if size and GATE_SIZES.index(g["size"]) < GATE_SIZES.index(size):
                continue
            busy = any(a < t + turn and t < b for a, b, w in tl.get(g["id"], []) if w != DF)
            blocked = any(e["gate"] == g["id"] and e["start"] <= t < e["end"] for e in ua)
            if not busy and not blocked:
                out.append({"gate": g["id"], "size": g["size"]})
        return {"free_gates": out}

    def gate_timeline(self, gate):
        tl = K.baseline(self.sc)["resolved"]["timeline"].get(gate)
        if tl is None:
            return {"error": f"no gate {gate}"}
        return {"gate": gate, "occupied": [{"by": w, "from": None if a < -1e8 else fmt_time(a), "until": fmt_time(b)}
                                           for a, b, w in sorted(tl)]}

    def overtime_if(self, in_block_time):
        t = parse_time(in_block_time)
        turn = self.sc["airport"]["min_turnaround_min"]
        out = []
        for c in self.sc["airport"]["ground_crews"].values():
            if DF in c["flights"]:
                ot = max(0.0, t + turn - c["shift_end"])
                out.append({"crew": c["id"], "shift_end": fmt_time(c["shift_end"]), "overtime_min": round(ot, 1),
                            "cost_weighted_min": round(ot * self.sc["rules"].ground["overtime_multiplier"], 1)})
        return {"crews": out}


def to_json(x):
    return json.dumps(x, default=str)
