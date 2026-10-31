"""Deterministic agents with the same interface as the Claude agents. Used for tests and offline
runs (no API). Each follows only its own role's goals and authority."""
import constraints as K
import fallback as FB
from agents.base import Agent
from messages import AgentMessage, Objection, PriorityRequest, Proposal, default_proposal
from scenario import DF, fmt_time


def _latest(ctx):
    pid = ctx["latest_proposal_id"]
    return pid, (Proposal.model_validate(ctx["proposals"][pid]["proposal"]) if pid else None)


def _hard_fail(res, domains):
    return [x for x in res["results"] if not x["pass"] and x["domain"] in domains]


def _last_own(ctx, role):
    mine = [m for m in ctx["history"] if m["sender"] == role]
    return mine[-1] if mine else None


class ATCRule(Agent):
    kind = "rule"

    def __init__(self):
        super().__init__("ATC")

    def respond(self, ctx):
        sc = ctx["sc"]
        pid, p = _latest(ctx)
        if p is None:
            why = "First come, first served" + (" after the weather clears." if K.A.holds(sc) else ".")
            return AgentMessage(type="PROPOSE", proposal=default_proposal(sc), rationale=why)
        res = K.check(sc, p)
        wants_priority = any(m["sender"] == "PILOT" and m.get("priority_request") and m["valid"]
                             for m in ctx["history"])
        if wants_priority and DF not in p.atc.priority and not ctx["final_turn"]:
            q = p.model_copy(deep=True)
            q.atc.priority = [DF] + [x for x in q.atc.priority if x != DF]
            r2 = K.check(sc, q)
            if r2["ok"]:
                return AgentMessage(type="COUNTER", responds_to=pid, proposal=q,
                                    rationale="Pilot reports minimum/mayday fuel; DF moved to the front. "
                                              "All other arrivals stay within their spare fuel.")
        bad = _hard_fail(res, ("ATC", "ALL"))
        if bad:
            return AgentMessage(type="REJECT", responds_to=pid,
                                objections=[Objection(rule=x["rule"], hard=True, detail=x["detail"]) for x in bad],
                                rationale="The proposal breaks an ATC rule.")
        return AgentMessage(type="ACCEPT", responds_to=pid, rationale="Sequence is legal.")


class PilotRule(Agent):
    kind = "rule"

    def __init__(self):
        super().__init__("PILOT")

    def respond(self, ctx):
        sc = ctx["sc"]
        pid, p = _latest(ctx)
        res = K.check(sc, p)
        bad = _hard_fail(res, ("PILOT", "ALL"))
        if ctx["final_turn"]:
            if bad:
                return AgentMessage(type="REJECT", responds_to=pid,
                                    objections=[Objection(rule=x["rule"], hard=True, detail=x["detail"]) for x in bad],
                                    rationale="Final turn: a pilot hard rule fails.")
            return AgentMessage(type="ACCEPT", responds_to=pid, rationale="Final turn: no pilot hard rule fails.")
        # on the ground: wait on the ground instead of holding in the air
        if sc["phase"] == "on_ground_at_origin" and p.df.action == "land":
            q, why = FB._ground_delay(sc, p)
            if why and K.check(sc, q)["ok"]:
                return AgentMessage(type="COUNTER", responds_to=pid, proposal=q,
                                    rationale=f"Saves fuel: {why}.")
        df = res["resolved"]["df"]
        status = df["fuel_status"]["status"]
        if status in ("normal", "diverted") and not bad:
            return AgentMessage(type="ACCEPT", responds_to=pid, rationale=f"Fuel status {status}.")
        se = res["effective"]
        fp = se["flight"]["fuel_plan"]
        if status == "minimum_fuel" and p.atc.priority[:1] == [DF] and not bad:
            return AgentMessage(type="ACCEPT", responds_to=pid,
                                rationale="Minimum fuel, but we are already first in line: best available.")
        if status == "mayday_fuel":
            q = p.model_copy(deep=True)
            q.atc.priority = [DF] + [x for x in q.atc.priority if x != DF]
            if K.check(sc, q)["resolved"]["df"]["fuel_status"]["status"] == "mayday_fuel" and sc["phase"] == "airborne":
                d = p.model_copy(deep=True)
                d.df.action = "divert"
                if K.check(sc, d)["ok"]:
                    return AgentMessage(type="COUNTER", responds_to=pid, proposal=d,
                                        rationale="Even first in line we would land below the final reserve: diverting.")
        req = PriorityRequest(level=status if status in ("minimum_fuel", "mayday_fuel") else "minimum_fuel",
                              latest_landing_time=fmt_time(se["flight"]["planned_arrival"] + fp["holding_before_divert_min"]),
                              holding_remaining_min=round(fp["holding_before_divert_min"], 1),
                              reason=f"Landing at {fmt_time(df['landing'])} leaves no fuel for the alternate.")
        objs = [Objection(rule=x["rule"], hard=True, detail=x["detail"]) for x in bad] or \
               [Objection(rule="PILOT_MIN_FUEL", hard=False,
                          detail=f"would have to commit to {sc['airport']['iata']} (no alternate fuel)")]
        return AgentMessage(type="REJECT", responds_to=pid, objections=objs, priority_request=req,
                            rationale="Requesting priority because of fuel.")


class GroundRule(Agent):
    kind = "rule"

    def __init__(self):
        super().__init__("GROUND")

    def respond(self, ctx):
        sc = ctx["sc"]
        pid, p = _latest(ctx)
        res = K.check(sc, p)
        bad = _hard_fail(res, ("GROUND", "ALL"))
        if not ctx["final_turn"] and (res["resolved"]["df"].get("gate_wait_min", 0) > 1e-6 or bad):
            q, why = FB._best_gate(sc, p, res)
            if why:
                return AgentMessage(type="COUNTER", responds_to=pid, proposal=q, rationale=why)
        if bad:
            return AgentMessage(type="REJECT", responds_to=pid,
                                objections=[Objection(rule=x["rule"], hard=True, detail=x["detail"]) for x in bad],
                                rationale="A ground hard rule fails.")
        return AgentMessage(type="ACCEPT", responds_to=pid, rationale="Gate free and fits.")


def make_rule_agents():
    return {"ATC": ATCRule(), "PILOT": PilotRule(), "GROUND": GroundRule()}
