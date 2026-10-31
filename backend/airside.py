"""Landing queue and fuel urgency. Deterministic, no AI.

Weather model (agreed): while weather is overhead (a `weather_hold` effect), no arrival
can land. Runway capacity itself is never reduced: once the weather clears, the workbook's
movements per hour and wake separation apply as normal, and the aircraft that were held
form a queue. The landing ORDER of that queue is what ATC controls and negotiates.

    seq = landing_sequence(sc)                         # first come, first served
    seq = landing_sequence(sc, priority=["DF"])        # DF moved to the front
    seq["DF"]["landing"], seq["DF"]["wait_min"]
    fuel_status(sc, sc["flight"], landing_time)        # normal / minimum_fuel / mayday_fuel
"""
DF = "DF"
STATUS_ORDER = ("normal", "minimum_fuel", "mayday_fuel")


# ------------------------------------------------------------------ separation
def wake_gap_min(R, leader, follower):
    """Minutes between two landings on the same runway: workbook NM / follower approach speed."""
    nm = R.separation(leader["aircraft"]["wake_category"], follower["aircraft"]["wake_category"])["nm"]
    return nm / follower["aircraft"]["approach_speed_kt"] * 60


def holds(sc):
    return [e for e in sc["effects"] if e["type"] == "weather_hold"]


def ready_time(sc, planned):
    """Earliest landing allowed by the weather: a flight planned inside a hold waits for its end."""
    t = planned
    for h in sorted(holds(sc), key=lambda h: h["start"]):
        if h["start"] <= t < h["end"]:
            t = h["end"]
    return t


def arrivals(sc):
    """Every arrival with its planned landing (DF first)."""
    out = [{"id": DF, "planned": sc["flight"]["planned_arrival"], "aircraft": sc["flight"]["aircraft"],
            "runway": None}]
    for o in sc["other_flights"].values():
        if o["kind"] == "arrival":
            out.append({"id": o["id"], "planned": o["planned_time"], "aircraft": o["aircraft"],
                        "runway": o["runway"]})
    return out


def cap_fn(sc):
    """Movements allowed in the 60-minute window starting at s: the workbook limit, lowered by any
    capacity_reduction effect that overlaps the window (weather never lowers it)."""
    base = sc["airport"]["max_runway_ops_per_hour"]
    reds = [(e["start"], e["end"], e["max_runway_ops_per_hour"]) for e in sc["effects"]
            if e["type"] == "capacity_reduction"]

    def cap(s):
        return min([base] + [c for a, b, c in reds if a < s + 60 and s < b])
    return cap


def window_starts(t, movements):
    starts = {t - 60 + 1e-6}
    starts |= {m for m in movements if t - 60 < m <= t}
    starts |= {m - 60 + 1e-6 for m in movements if t < m < t + 60}
    return starts


def _cap_ok(t, movements, cap):
    """True if adding a movement at t keeps every 60-minute window at or under the cap.
    `cap` is a number or a function of the window start."""
    capf = cap if callable(cap) else (lambda s: cap)
    for s in window_starts(t, movements):
        if sum(1 for m in movements if s <= m < s + 60) + 1 > capf(s):
            return False
    return True


def landing_sequence(sc, priority=(), fixed=None, exclude=()):
    """Assign every arrival a landing time and runway.

    Order: flights in `priority` first (in the order given), then first come, first served
    (earliest allowed time, then planned time). Each flight gets the earliest time on any
    arrival runway that respects wake separation with the landings before and after it,
    keeps any 60-minute window at or under the airport's movements/hour limit (departures
    count too), and keeps a 1-minute gap to departures on a mixed runway.
    `fixed`: {flight_id: landing_time} for flights whose time is already decided.
    `exclude`: flights that are not landing here (for example weather diversions).
    Returns {flight_id: {"landing", "runway", "planned", "ready", "wait_min", "order"}}.
    """
    R, ap = sc["rules"], sc["airport"]
    cap = cap_fn(sc)
    rwys = [r for r, v in ap["runways"].items() if v["use"] in ("arrivals", "mixed")]
    closed = {r: [(e["start"], e["end"]) for e in sc["effects"]
                  if e["type"] == "runway_closure" and e["runway"] == r] for r in rwys}
    dep_gap = R.wake_default["dep_s"] / 60
    departures = [(o["planned_time"], o["runway"]) for o in sc["other_flights"].values()
                  if o["kind"] == "departure"]
    movements = [t for t, _ in departures]
    on_rwy = {r: [] for r in rwys}            # runway -> [(time, flight)]
    flights = {f["id"]: dict(f, ready=ready_time(sc, f["planned"])) for f in arrivals(sc)
               if f["id"] not in set(exclude)}
    pr = [p for p in priority if p in flights]
    rest = sorted((f for f in flights.values() if f["id"] not in pr),
                  key=lambda f: (f["ready"], f["planned"], f["id"]))
    order = [flights[p] for p in pr] + rest

    def fits(f, r, t):
        if any(a <= t < b for a, b in closed[r]):
            return False
        for (u, g) in on_rwy[r]:
            if u <= t and t - u < wake_gap_min(R, g, f) - 1e-9:
                return False
            if u > t and u - t < wake_gap_min(R, f, g) - 1e-9:
                return False
        if ap["runways"][r]["use"] == "mixed":
            if any(rr == r and abs(t - d) < dep_gap for d, rr in departures):
                return False
        return _cap_ok(t, movements, cap)

    def search(f, use_fixed):
        best = None
        for r in rwys:
            if use_fixed:
                cands = [fixed[f["id"]]]
            else:
                cands = {f["ready"]}
                for (u, g) in on_rwy[r]:
                    cands.add(u + wake_gap_min(R, g, f))
                cands |= {m + 60 + 1e-6 for m in movements}      # when an old movement leaves the hour
                cands |= {d + dep_gap for d, rr in departures if rr == r}
                cands |= {b for a, b in closed[r]}                # when a closed runway reopens
                cands = sorted(c for c in cands if c >= f["ready"])
            for t in cands:
                if fits(f, r, t):
                    if best is None or t < best[0]:
                        best = (t, r)
                    break
        return best

    out = {}
    for pos, f in enumerate(order, 1):
        fixed_infeasible = False
        best = None
        if fixed and f["id"] in fixed:
            best = search(f, True)
            fixed_infeasible = best is None    # the pinned time breaks a rule: place it where it fits
        if best is None:
            best = search(f, False)
        if best is None:                      # should not happen; push past everything
            t = max([f["ready"]] + movements) + 60
            best = (t, rwys[0])
        t, r = best
        on_rwy[r].append((t, f))
        movements.append(t)
        # exact times (no rounding): separation checks downstream must see the real gaps
        out[f["id"]] = {"landing": t, "runway": r, "planned": f["planned"], "ready": f["ready"],
                        "wait_min": t - f["planned"], "order": pos, "fixed_infeasible": fixed_infeasible}
    return out


# ------------------------------------------------------------------ fuel urgency
def fuel_status(sc, flight, landing_time):
    """Fuel situation of the disrupted flight if it lands at `landing_time`.

    normal       : it can wait that long and still keep fuel for its alternate.
    minimum_fuel : it can only make it by committing to land here (alternate fuel used up);
                   any further delay is not acceptable. A request, so it is negotiable.
    mayday_fuel  : it would land below the 30-minute final reserve. An emergency:
                   priority is mandatory and not negotiable.
    """
    fp = flight["fuel_plan"]
    planned = flight["planned_arrival"]
    wait = landing_time - planned
    if wait <= fp["holding_before_divert_min"] + 1e-9:
        status = "normal"
    elif wait <= fp["holding_if_committed_min"] + 1e-9:
        status = "minimum_fuel"
    else:
        status = "mayday_fuel"
    fuel_at_landing = fp["on_landing_as_planned_kg"] - max(0.0, wait) * fp["holding_burn_kg_per_min"]
    return {
        "status": status,
        "wait_min": round(wait, 1),
        "fuel_at_landing_kg": round(fuel_at_landing),
        "final_reserve_kg": fp["final_reserve_kg"],
        "latest_landing_keeping_alternate": round(planned + fp["holding_before_divert_min"], 1),
        "latest_landing_if_committed": round(planned + fp["holding_if_committed_min"], 1),
    }


def other_arrival_ok(sc, flight_id, landing_time):
    """Other arrivals have no agent: their spare holding fuel (workbook 45 min) must cover the wait."""
    o = sc["other_flights"][flight_id]
    return landing_time - o["planned_time"] <= o["spare_holding_min"] + 1e-9


def weather_diversions(sc, priority=()):
    """Other arrivals that can't wait out the weather and divert. No agent decides this.

    Repeatedly sequence the queue; if any other arrival would wait longer than its spare
    holding fuel, the one that reaches its limit first (planned landing + spare fuel) diverts
    and leaves the queue. Repeat until everyone left can wait. DF is never removed here:
    its own divert decision belongs to the negotiation.
    Returns (diverted ids in order, final sequence without them).
    """
    diverted = []
    while True:
        seq = landing_sequence(sc, priority=priority, exclude=diverted)
        over = [k for k, v in seq.items() if k != DF and not other_arrival_ok(sc, k, v["landing"])]
        if not over:
            return diverted, seq
        o = sc["other_flights"]
        first = min(over, key=lambda k: (o[k]["planned_time"] + o[k]["spare_holding_min"], k))
        diverted.append(first)


def check_mayday_priority(sc, sequence):
    """Hard rule: a flight in mayday fuel must land at the earliest time it possibly can.

    Returns a list of violations (empty = rule satisfied)."""
    st = fuel_status(sc, sc["flight"], sequence[DF]["landing"])
    if st["status"] != "mayday_fuel":
        return []
    earliest = landing_sequence(sc, priority=[DF])[DF]["landing"]
    if sequence[DF]["landing"] > earliest + 1e-6:
        return [f"MAYDAY_PRIORITY: DF is in mayday fuel and must land at {earliest:.1f}, "
                f"not {sequence[DF]['landing']:.1f}"]
    return []
