"""Structured messages and proposals exchanged by the agents (pydantic v2).

A Proposal is a complete resolution. Each agent may only edit its own part:
  ATC    -> atc.*  and df.landing_time
  GROUND -> ground.* and df.gate
  PILOT  -> df.action, df.departure_time, df.reroute_for_turbulence
"""
import json
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ROLES = ("ATC", "PILOT", "GROUND")
SOFT_RULES = {
    "PILOT_MIN_FUEL": "PILOT", "PILOT_CONNECTIONS": "PILOT", "PILOT_TURBULENCE": "PILOT",
    "ATC_THROUGHPUT": "ATC", "ATC_OTHERS_DELAY": "ATC",
    "GROUND_OVERTIME": "GROUND", "GROUND_GATE_CHANGES": "GROUND", "GROUND_TURNAROUND_DELAY": "GROUND",
}
DOMAIN_FIELDS = {
    "ATC": {("atc", "priority"), ("atc", "fixed_times"), ("atc", "departure_moves"), ("df", "landing_time")},
    "GROUND": {("ground", "gate_changes"), ("df", "gate")},
    "PILOT": {("df", "action"), ("df", "departure_time"), ("df", "reroute_for_turbulence")},
}


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DFPlan(_M):
    action: Literal["land", "divert", "delay_departure_then_land"] = "land"
    landing_time: Optional[str] = Field(None, description="HH:MM; pins DF's landing. null = computed")
    departure_time: Optional[str] = Field(None, description="HH:MM take-off (on ground only)")
    reroute_for_turbulence: bool = False
    gate: Optional[str] = None


class ATCPart(_M):
    priority: List[str] = Field(default_factory=list, description="flights moved to the front, in order")
    fixed_times: Dict[str, str] = Field(default_factory=dict, description="other arrival -> HH:MM landing")
    departure_moves: Dict[str, str] = Field(default_factory=dict, description="departure -> later HH:MM")


class GroundPart(_M):
    gate_changes: Dict[str, str] = Field(default_factory=dict, description="other arrival -> gate id")


class Proposal(_M):
    df: DFPlan = Field(default_factory=DFPlan)
    atc: ATCPart = Field(default_factory=ATCPart)
    ground: GroundPart = Field(default_factory=GroundPart)


class Objection(_M):
    rule: str
    hard: bool
    detail: str = ""


class PriorityRequest(_M):
    level: Literal["minimum_fuel", "mayday_fuel"]
    latest_landing_time: str = Field(description="HH:MM latest landing that still keeps alternate fuel")
    holding_remaining_min: float = Field(description="minutes DF can still hold before diverting")
    reason: str = ""


class AgentMessage(_M):
    """What an agent sends. The orchestrator adds msg_id, turn and sender."""
    type: Literal["PROPOSE", "ACCEPT", "COUNTER", "REJECT"]
    responds_to: Optional[str] = Field(None, description="proposal id, e.g. P1 (not for PROPOSE)")
    proposal: Optional[Proposal] = Field(None, description="full proposal, only for PROPOSE/COUNTER")
    objections: List[Objection] = Field(default_factory=list)
    priority_request: Optional[PriorityRequest] = None
    rationale: str = ""


def default_proposal(sc):
    """The starting point: land as planned (first come, first served), keep the assigned gate."""
    return Proposal(df=DFPlan(gate=sc["flight"]["assigned_gate"]))


def canonical(p: Proposal) -> str:
    """Stable text form for comparing proposals (repeats are not allowed)."""
    return json.dumps(p.model_dump(), sort_keys=True)


def changed_fields(base: Proposal, new: Proposal):
    """Set of (part, field) pairs that differ."""
    b, n = base.model_dump(), new.model_dump()
    return {(part, k) for part in n for k in n[part] if n[part][k] != b[part][k]}


def message_schema():
    return AgentMessage.model_json_schema()
