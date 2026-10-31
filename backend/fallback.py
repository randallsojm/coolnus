"""Deterministic non-AI fallback: decides when the agents don't reach consensus in 3 turns.

    proposal, notes = decide(sc)

Rules (in order):
  1. On the ground with a weather hold: delay the take-off so DF reaches the airport as landings
     resume (ground delay programme), instead of holding in the air.
  2. mayday_fuel  -> DF moved to the front (mandatory).
     minimum_fuel -> DF moved to the front if that breaks no hard rule; otherwise first come, first served.
     normal       -> first come, first served.
  3. Airborne and still mayday even with priority (or the plan breaks crew duty) -> divert to the
     alternate, if there is fuel to get there with the final reserve.
  4. Gate: keep DF's gate unless it would have to wait for it; then the smallest free gate that fits,
     with at least the workbook's gate-change notice. Otherwise keep the gate and wait.
Every candidate is run through the constraint checker; the first fully legal one wins. If none is
legal, the least-bad one is returned and its failing rules are reported.
"""
import airside as A
import constraints as K
from messages import Proposal, default_proposal
from scenario import DF, GATE_SIZES, fmt_time


def _ground_delay(sc, p):
    holds = A.holds(sc)
    if sc["phase"] != "on_ground_at_origin" or not holds:
        return p, None
    f = sc["flight"]
    ready = f["planned_departure"] + K.late_inbound(sc)
    air = f["planned_arrival"] - f["planned_departure"]
    land_ready = ready + air
    for h in sorted(holds, key=lambda h: h["start"]):
        if h["start"] <= land_ready < h["end"]:
            dep = h["end"] - air
            if dep > ready:
                q = p.model_copy(deep=True)
                q.df.action = "delay_departure_then_land"
                q.df.departure_time = fmt_time(dep)
                return q, f"take-off delayed to {fmt_time(dep)} so DF arrives as the weather clears"
    return p, None


def _best_gate(sc, p, res):
    """If DF waits for its gate, move it to the smallest free gate that fits (with notice)."""
    df = res["resolved"]["df"]
    if df["action"] == "divert" or df["gate_wait_min"] <= 1e-6:
        return p, None
    size = sc["flight"]["aircraft"]["gate_size"]
    gates = sorted(sc["airport"]["gates"].values(), key=lambda g: (GATE_SIZES.index(g["size"]), g["id"]))
    for g in gates:
        if GATE_SIZES.index(g["size"]) < GATE_SIZES.index(size) or g["id"] == df["gate"]:
            continue
        q = p.model_copy(deep=True)
        q.df.gate = g["id"]
        r = K.check(sc, q)
        if r["ok"] and r["resolved"]["df"]["gate_wait_min"] <= 1e-6:
            return q, f"gate changed to {g['id']} to avoid a {df['gate_wait_min']:.0f}-min gate wait"
    return p, None


def decide(sc):
    notes = []
    base, why = _ground_delay(sc, default_proposal(sc))
    if why:
        notes.append(why)
    fcfs = K.check(sc, base)
    st = fcfs["resolved"]["df"]["fuel_status"]["status"]
    prio = base.model_copy(deep=True)
    prio.atc.priority = [DF]
    pr = K.check(sc, prio)

    candidates = []
    if st == "mayday_fuel":
        notes.append("DF in mayday fuel: priority is mandatory")
        candidates = [(prio, pr)]
    elif st == "minimum_fuel":
        if pr["ok"]:
            notes.append("DF in minimum fuel: moved to the front (breaks no hard rule)")
            candidates = [(prio, pr), (base, fcfs)]
        else:
            notes.append("DF in minimum fuel but priority breaks " + ", ".join(K.failed(pr)) +
                         "; first come, first served")
            candidates = [(base, fcfs), (prio, pr)]
    else:
        notes.append("DF fuel normal: first come, first served")
        candidates = [(base, fcfs), (prio, pr)]

    if sc["phase"] == "airborne":
        div = base.model_copy(deep=True)
        div.df.action = "divert"
        candidates.append((div, K.check(sc, div)))

    for p, r in candidates:
        if r["ok"]:
            if p.df.action == "divert":
                notes.append(f"no legal landing at {sc['airport']['iata']}: divert to "
                             f"{sc['flight']['alternate']['airport']}")
            p2, why = _best_gate(sc, p, r)
            if why:
                notes.append(why)
            return p2, notes
    # nothing fully legal: least-bad (fewest failing rules), report it
    p, r = min(candidates, key=lambda c: len(K.failed(c[1])))
    notes.append("no fully legal plan exists; least-bad plan chosen, failing: " + ", ".join(K.failed(r)))
    return p, notes
