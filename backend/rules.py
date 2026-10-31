"""Step 1: load the challenge workbook as the single source of operational limits.

    from rules import load_rules
    R = load_rules()                       # workbook values
    R = load_rules({"fuel.spare_holding_min": 30})   # with a stated override

Every number the agents, the constraint checker and the fallback use comes from
here. The workbook's rules say any limit a team changes must be stated in the demo,
so overrides are applied through one door and recorded in R.overrides.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
WORKBOOK = ROOT / "data" / "challenge2_operational_constraints.xlsx"
SHEETS = ["airport_capacity", "wake_separation", "fuel_and_crew", "rules_of_play"]
WAKE_CATEGORIES = ["SUPER", "HEAVY", "LARGE", "SMALL"]


class RulesError(Exception):
    pass


# ------------------------------------------------------------------ parsing helpers
def _read_table(path, sheet, first_col):
    """Read a sheet whose header row starts with `first_col` (sheets may have a note row above)."""
    raw = pd.read_excel(path, sheet_name=sheet, header=None)
    hits = raw.index[raw[0].astype(str).str.strip() == first_col]
    if not len(hits):
        raise RulesError(f"sheet {sheet!r}: no header row starting with {first_col!r}")
    h = hits[0]
    note = " ".join(str(v) for v in raw.iloc[:h, 0].dropna()) if h else ""
    df = pd.read_excel(path, sheet_name=sheet, header=h)
    df.columns = [str(c).strip() for c in df.columns]
    return df.dropna(how="all"), note


def _number(text, what):
    m = re.search(r"(\d[\d,]*\.?\d*)", str(text))
    if not m:
        raise RulesError(f"could not read a number for {what!r} from {text!r}")
    return float(m.group(1).replace(",", ""))


def _wake_category(label):
    s = str(label).upper()
    for c in WAKE_CATEGORIES:
        if c in s:
            return c
    return None


# ------------------------------------------------------------------ the rules object
@dataclass
class Rules:
    airports: dict                    # IATA -> limits
    wake: dict                        # (leader, follower) -> {"nm": .., "dep_s": ..}
    wake_default: dict                # any other pair
    fuel: dict
    crew: dict
    ground: dict
    play: dict                        # rules_of_play topic -> text
    turns: int                        # max negotiation turns
    disruption_filter: dict           # {"WEATHER_DELAY": 15, "LATE_AIRCRAFT_DELAY": 20}
    notes: dict = field(default_factory=dict)
    overrides: list = field(default_factory=list)

    def airport(self, iata):
        if iata not in self.airports:
            raise RulesError(f"airport {iata} is not in airport_capacity")
        return self.airports[iata]

    def separation(self, leader, follower):
        return self.wake.get((leader, follower), self.wake_default)

    def as_dict(self):
        return {"fuel": self.fuel, "crew": self.crew, "ground": self.ground, "turns": self.turns,
                "disruption_filter": self.disruption_filter,
                "wake": [{"leader": a, "follower": b, **v} for (a, b), v in self.wake.items()],
                "wake_default": self.wake_default, "overrides": self.overrides}


def load_rules(overrides=None, path=WORKBOOK):
    path = Path(path)
    if not path.exists():
        raise RulesError(f"workbook not found: {path}")
    present = pd.ExcelFile(path).sheet_names
    missing = [s for s in SHEETS if s not in present]
    if missing:
        raise RulesError(f"workbook is missing sheets: {missing}")

    # airport_capacity ---------------------------------------------------------
    cap, cap_note = _read_table(path, "airport_capacity", "IATA_CODE")
    airports = {}
    for r in cap.itertuples(index=False):
        airports[str(r.IATA_CODE).strip()] = {
            "iata": str(r.IATA_CODE).strip(), "name": r.AIRPORT, "city": r.CITY, "state": r.STATE,
            "size_class": r.size_class,
            "active_runways": int(r.active_runways),
            "gates": int(r.gates),
            "max_runway_ops_per_hour": int(r.max_runway_ops_per_hour),
            "min_turnaround_min": int(r.min_turnaround_min),
            "spare_gates_at_any_time": int(r.spare_gates_at_any_time),
            "delayed_departures_in_flights_csv": int(r.delayed_departures_in_flights_csv),
        }

    # wake_separation ----------------------------------------------------------
    wk, wake_note = _read_table(path, "wake_separation", "leader")
    nm_col = next(c for c in wk.columns if "nm" in c.lower())
    s_col = next(c for c in wk.columns if c.lower().startswith("min_time"))
    wake, wake_default = {}, None
    for _, r in wk.iterrows():
        leader, follower = _wake_category(r.iloc[0]), _wake_category(r.iloc[1])
        entry = {"nm": float(r[nm_col]), "dep_s": float(r[s_col])}
        if leader and follower:
            wake[(leader, follower)] = entry
        elif "other" in str(r.iloc[0]).lower():
            wake_default = entry
    if wake_default is None:
        raise RulesError("wake_separation: no 'Any other pair' row")

    # fuel_and_crew ------------------------------------------------------------
    fc, _ = _read_table(path, "fuel_and_crew", "rule")
    v = {str(r.rule).strip().lower(): str(r.value) for r in fc.itertuples(index=False)}

    def get(key_part):
        hits = [val for k, val in v.items() if key_part in k]
        if len(hits) != 1:
            raise RulesError(f"fuel_and_crew: expected one rule containing {key_part!r}, found {len(hits)}")
        return hits[0]

    shift_text = get("ground crew shift")
    ot = re.search(r"(\d+(\.\d+)?)\s*x", shift_text)
    fuel = {
        "final_reserve_min": _number(get("final fuel reserve"), "final reserve"),
        "contingency_pct_of_trip": _number(get("contingency"), "contingency") / 100,
        "holding_burn_kg_h": {
            "narrowbody": _number(get("holding fuel burn (narrowbody)"), "narrowbody burn"),
            "widebody": _number(get("holding fuel burn (widebody)"), "widebody burn"),
        },
        "spare_holding_min": _number(get("assumed spare fuel"), "spare fuel"),
    }
    crew = {"max_duty_h": _number(get("flight crew max duty"), "crew duty")}
    ground = {
        "shift_h": _number(shift_text, "ground crew shift"),
        "overtime_multiplier": float(ot.group(1)) if ot else None,
        "gate_change_notice_min": _number(get("gate change notice"), "gate change notice"),
    }
    if ground["overtime_multiplier"] is None:
        raise RulesError(f"could not read the overtime multiplier from {shift_text!r}")

    # rules_of_play ------------------------------------------------------------
    rp, _ = _read_table(path, "rules_of_play", "topic")
    play = {str(r.topic).strip(): str(r.detail).strip() for r in rp.itertuples(index=False)}
    turns_m = re.search(r"within\s+(\d+)\s+turns", play.get("Turn", ""))
    if not turns_m:
        raise RulesError("rules_of_play: could not read the turn limit")
    filt = dict((k, float(n)) for k, n in re.findall(r"([A-Z_]+)\s*>\s*(\d+)", play.get("Input", "")))
    if not filt:
        raise RulesError("rules_of_play: could not read the disruption filter")

    R = Rules(airports=airports, wake=wake, wake_default=wake_default, fuel=fuel, crew=crew,
              ground=ground, play=play, turns=int(turns_m.group(1)), disruption_filter=filt,
              notes={"airport_capacity": cap_note, "wake_separation": wake_note})
    for key, value in (overrides or {}).items():
        apply_override(R, key, value)
    return R


def apply_override(R, key, value):
    """Change one limit, e.g. 'fuel.spare_holding_min' or 'airport.ORD.gates'. Recorded for the demo."""
    parts = key.split(".")
    if parts[0] == "airport":
        target, leaf = R.airport(parts[1]), parts[2]
    else:
        target = getattr(R, parts[0])
        for p in parts[1:-1]:
            target = target[p]
        leaf = parts[-1]
    if leaf not in target:
        raise RulesError(f"unknown limit {key}")
    old = target[leaf]
    target[leaf] = value
    R.overrides.append({"limit": key, "workbook_value": old, "used_value": value})


if __name__ == "__main__":
    R = load_rules()
    print(f"{len(R.airports)} airports | {len(R.wake)} wake pairs + default | turns: {R.turns}")
    print("ORD:", {k: R.airport("ORD")[k] for k in ("size_class", "active_runways", "gates",
                                                   "max_runway_ops_per_hour", "min_turnaround_min",
                                                   "spare_gates_at_any_time")})
    print("fuel:", R.fuel)
    print("crew:", R.crew, "| ground:", R.ground)
    print("disruption filter:", R.disruption_filter)
