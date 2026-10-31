"""The negotiation. Deterministic code, no AI: runs the turns, validates every message, checks
consensus, and calls the fallback after the last turn.

    result = negotiate(sc, agents)          # agents: {"ATC": .., "PILOT": .., "GROUND": ..}

Per turn: ATC, PILOT, GROUND each send exactly one message (fixed order). A message is invalid if:
  - it isn't well formed / the agent failed (NO_VALID_RESPONSE)
  - PROPOSE is not ATC's first message of turn 1; ACCEPT/REJECT/COUNTER don't name an existing proposal
  - a PROPOSE/COUNTER edits fields outside the sender's authority, changes nothing, or repeats a proposal
  - a REJECT has no objection; a hard objection names a rule that doesn't fail; a soft objection isn't
    one of the sender's goals; any soft objection in the final turn
  - a Pilot priority_request doesn't match the fuel calculator (or comes from another role)
An invalid message still uses the agent's turn and supports nothing.
Consensus: every agent's latest message supports the same proposal and that proposal passes every hard rule.
"""
import config as CFG
import constraints as K
import fallback as FB
import outcome as O
from agent_tools import Toolbox
from agents.base import AgentFailure
from messages import (DOMAIN_FIELDS, ROLES, SOFT_RULES, Proposal, canonical, changed_fields,
                      default_proposal)
from scenario import fmt_time, parse_time
from views import build_views


def _summary(res):
    df = res["resolved"]["df"]
    if df["action"] == "divert":
        return f"DF diverts to {df['alternate']}"
    return (f"DF lands {fmt_time(df['landing'])} ({df['fuel_status']['status']}), gate {df['gate']}, "
            f"in-block {fmt_time(df['in_block'])}"
            + (f", departs {fmt_time(df['departure_time'])}" if df.get("departure_time") is not None else ""))


def negotiate(sc, agents):
    R = sc["rules"]
    views = build_views(sc)
    toolboxes = {r: Toolbox(sc, r) for r in ROLES}
    proposals, seen, records, latest = {}, {}, [], {}
    latest_pid, consensus, n_msgs = None, None, 0

    def public_history():
        keys = ("msg_id", "turn", "sender", "type", "responds_to", "new_proposal_id", "valid", "errors",
                "objections", "priority_request", "rationale")
        return [{k: rec.get(k) for k in keys} for rec in records]

    def public_proposals():
        return {pid: {"proposal": v["proposal"].model_dump(), "by": v["by"], "turn": v["turn"],
                      "passes_hard_rules": v["check"]["ok"], "failed_rules": K.failed(v["check"])}
                for pid, v in proposals.items()}

    for turn in range(1, R.turns + 1):
        final = turn == R.turns
        for role in ROLES:
            ctx = {"sc": sc, "turn": turn, "final_turn": final, "public": views["public"],
                   "private": views[role], "history": public_history(), "proposals": public_proposals(),
                   "latest_proposal_id": latest_pid, "toolbox": toolboxes[role]}
            try:
                msg, fail = agents[role].respond(ctx), None
            except AgentFailure as e:
                msg, fail = None, str(e)
            n_msgs += 1
            rec = _validate(sc, role, turn, final, msg, fail, proposals, seen, latest_pid, n_msgs)
            if rec["new_proposal_id"]:
                latest_pid = rec["new_proposal_id"]
            records.append(rec)
            latest[role] = rec
        pid = _consensus(latest, proposals)
        if pid:
            consensus = {"reached": True, "turn": turn, "proposal_id": pid}
            break
    assert n_msgs <= CFG.MAX_MESSAGES, "message cap exceeded"
    api_calls = sum(a.stats["api_calls"] for a in agents.values())
    assert api_calls <= CFG.MAX_MESSAGES * (CFG.MAX_TOOL_CALLS_PER_MESSAGE + CFG.MAX_FORMAT_RETRIES + 1), \
        "API call cap exceeded"

    if consensus:
        final_p, decided_by, reason, notes = proposals[consensus["proposal_id"]]["proposal"], "consensus", None, []
    else:
        consensus = {"reached": False, "turn": None, "proposal_id": None}
        reason = _why_no_consensus(latest, proposals)
        final_p, notes = FB.decide(sc)
        decided_by = "fallback"
    res = K.check(sc, final_p)
    base = K.baseline(sc)
    return {
        "consensus": consensus, "decided_by": decided_by, "fallback_reason": reason, "fallback_notes": notes,
        "final_proposal": final_p, "check": res, "outcome": O.evaluate(sc, res),
        "baseline": {"check": base, "outcome": O.evaluate(sc, base)},
        "records": records, "proposals": proposals, "messages_sent": n_msgs,
        "agent_stats": {r: dict(a.stats, kind=a.kind) for r, a in agents.items()},
        "tool_calls": {r: tb.calls for r, tb in toolboxes.items()},
        "transcripts": {r: getattr(a, "transcript", None) for r, a in agents.items()},
    }


def _validate(sc, role, turn, final, msg, fail, proposals, seen, latest_pid, n):
    rec = {"msg_id": f"T{turn}-{role}", "turn": turn, "sender": role, "type": None, "responds_to": None,
           "new_proposal_id": None, "supports": None, "valid": False, "errors": [], "objections": [],
           "priority_request": None, "rationale": "", "message": None, "check_new": None}
    if msg is None:
        rec["errors"] = [f"NO_VALID_RESPONSE: {fail}"]
        return rec
    rec.update(type=msg.type, responds_to=msg.responds_to, message=msg.model_dump(),
               objections=[o.model_dump() for o in msg.objections],
               priority_request=msg.priority_request.model_dump() if msg.priority_request else None,
               rationale=msg.rationale)
    errs = rec["errors"]
    target = None
    if msg.type == "PROPOSE":
        if not (role == "ATC" and not proposals):
            errs.append("PROPOSE is only allowed for ATC while no valid proposal exists yet")
        base = default_proposal(sc)
    else:
        if msg.responds_to not in proposals:
            errs.append(f"responds_to {msg.responds_to!r} is not an existing proposal")
            base = None
        else:
            base = proposals[msg.responds_to]["proposal"]
            target = proposals[msg.responds_to]["check"]
    new = None
    if msg.type in ("ACCEPT", "REJECT") and msg.proposal is not None:
        msg = msg.model_copy(update={"proposal": None})       # extra copy of a proposal: ignored
        rec["message"] = msg.model_dump()
    if msg.type in ("PROPOSE", "COUNTER") and msg.proposal is not None and msg.proposal.df.gate is None:
        # a blank gate means "keep the gate in the proposal responded to" (not an edit)
        keep = base.df.gate if base is not None else sc["flight"]["assigned_gate"]
        msg = msg.model_copy(update={"proposal": msg.proposal.model_copy(
            update={"df": msg.proposal.df.model_copy(update={"gate": keep})})})
        rec["message"] = msg.model_dump()
    if msg.type in ("PROPOSE", "COUNTER"):
        if msg.proposal is None:
            errs.append(f"{msg.type} needs a proposal")
        elif base is not None:
            new = msg.proposal
            ch = changed_fields(base, new)
            outside = sorted(f"{a}.{b}" for a, b in ch - DOMAIN_FIELDS[role])
            if outside:
                errs.append(f"{role} may not edit {', '.join(outside)}")
            if msg.type == "COUNTER" and not ch:
                errs.append("COUNTER changes nothing (use ACCEPT)")
            if canonical(new) in seen:
                errs.append(f"repeats proposal {seen[canonical(new)]}")
    if msg.type == "REJECT" and not msg.objections:
        errs.append("REJECT needs at least one objection")
    # objections are about the proposal responded to
    for ob in msg.objections:
        if ob.hard:
            if ob.rule not in K.RULES:
                errs.append(f"unknown hard rule {ob.rule}")
            elif target is not None and ob.rule not in K.failed(target):
                errs.append(f"hard objection {ob.rule} but that rule passes for {msg.responds_to}")
        else:
            if final:
                errs.append("final turn: only hard objections are allowed")
            if SOFT_RULES.get(ob.rule) != role:
                errs.append(f"{ob.rule} is not one of {role}'s soft goals")
    if msg.priority_request is not None:
        if role != "PILOT":
            errs.append("only the PILOT may send a priority_request")
        elif target is None:
            errs.append("priority_request must respond to a proposal")
        else:
            errs += _check_priority_claim(target, msg.priority_request)
    if errs:
        return rec
    rec["valid"] = True
    if new is not None:
        pid = f"P{len(proposals) + 1}"
        res = K.check(sc, new)
        proposals[pid] = {"proposal": new, "by": role, "turn": turn, "check": res}
        seen[canonical(new)] = pid
        rec.update(new_proposal_id=pid, supports=pid,
                   check_new={"ok": res["ok"], "failed": K.failed(res), "summary": _summary(res)})
    elif msg.type == "ACCEPT":
        rec["supports"] = msg.responds_to
    return rec


def _check_priority_claim(target, req):
    df = target["resolved"]["df"]
    if df["action"] == "divert":
        return ["PRIORITY_CLAIM_VALID: DF diverts in that proposal"]
    st = df["fuel_status"]
    fp = target["effective"]["flight"]["fuel_plan"]
    errs = []
    if st["status"] != req.level:
        errs.append(f"PRIORITY_CLAIM_VALID: claimed {req.level} but the fuel calculator says {st['status']}")
    tol = CFG.PRIORITY_CLAIM_TOLERANCE_MIN
    try:
        t = parse_time(req.latest_landing_time)
        if abs(t - st["latest_landing_keeping_alternate"]) > tol:
            errs.append(f"PRIORITY_CLAIM_VALID: latest landing {req.latest_landing_time} doesn't match "
                        f"{fmt_time(st['latest_landing_keeping_alternate'])}")
    except ValueError as e:
        errs.append(f"PRIORITY_CLAIM_VALID: {e}")
    if abs(req.holding_remaining_min - fp["holding_before_divert_min"]) > tol:
        errs.append(f"PRIORITY_CLAIM_VALID: holding remaining {req.holding_remaining_min} doesn't match "
                    f"{fp['holding_before_divert_min']:.1f}")
    return errs


def _consensus(latest, proposals):
    if set(latest) != set(ROLES):
        return None
    sup = {latest[r]["supports"] for r in ROLES}
    if len(sup) == 1:
        pid = sup.pop()
        if pid and proposals[pid]["check"]["ok"]:
            return pid
    return None


def _why_no_consensus(latest, proposals):
    parts = []
    for r in ROLES:
        rec = latest.get(r)
        if rec is None:
            continue
        if not rec["valid"]:
            parts.append(f"{r}'s last message was invalid ({'; '.join(rec['errors'])})")
        elif rec["type"] == "REJECT":
            obs = ", ".join(f"{o['rule']}{'' if o['hard'] else ' (soft)'}" for o in rec["objections"])
            parts.append(f"{r} rejected {rec['responds_to']} ({obs})")
        else:
            parts.append(f"{r} supported {rec['supports']}")
    bad = [f"{pid} fails {', '.join(K.failed(v['check']))}" for pid, v in proposals.items() if not v["check"]["ok"]]
    return "No consensus after the last turn: " + "; ".join(parts) + (". " + "; ".join(bad) if bad else "")
