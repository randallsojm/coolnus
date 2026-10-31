"""Settings for the negotiation pipeline. Every assumption that isn't in the workbook lives
here and is copied into each consensus log (ASSUMPTIONS)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "output" / "logs"

# ------------------------------------------------------------------ Claude API
MODEL = os.environ.get("ATM_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = 2000
# Claude Sonnet 5.5 / Opus 5.5 reject `temperature` and forced tool_choice ("any"/"tool"),
# so neither is sent: tool_choice is "auto" and the agent is reminded to call send_message.
TEMPERATURE = None
STRICT_TOOLS = False            # set True to send "strict": true on tool definitions
API_KEY_ENV = "ANTHROPIC_API_KEY"

# ------------------------------------------------------------------ loop caps (no endless loops)
MAX_TOOL_CALLS_PER_MESSAGE = 8  # info-tool calls an agent may make before it must send its message
MAX_FORMAT_RETRIES = 1          # retries when an agent's message is malformed
MAX_MESSAGES = 9                # 3 turns x 3 agents (rules of play)

# ------------------------------------------------------------------ modelling assumptions
DECISION_LATENCY_MIN = 0        # time from the start of the negotiation to the decision
TURBULENCE_REROUTE_EXTRA_MIN = {"none": 0, "light": 3, "moderate": 5, "severe": 10}
DEPARTURE_GATE_RELEASE_MIN = 10 # a departure leaves its gate this long before take-off
PRIORITY_CLAIM_TOLERANCE_MIN = 0.5

ASSUMPTIONS = [
    "Each arriving aircraft occupies its gate from in-block for the airport's minimum turnaround "
    "(workbook min_turnaround_min).",
    f"A departure leaves its gate {DEPARTURE_GATE_RELEASE_MIN} min before its take-off time.",
    "An arrival whose gate is still occupied waits on the apron until it is free (gate wait).",
    f"The decision is taken {DECISION_LATENCY_MIN} min after the negotiation starts "
    "(used for the 20-min gate-change notice).",
    f"Rerouting around turbulence adds {TURBULENCE_REROUTE_EXTRA_MIN} minutes by turbulence level, "
    "flown at cruise burn.",
    "A diversion is decided immediately and needs trip fuel + alternate fuel + final reserve.",
    "Other arrivals that can't wait out the weather on their spare fuel divert (no agent decides).",
    "Other flights' crew-duty and spare-fuel limits only count against a resolution if the resolution "
    "itself causes the breach (compared with first come, first served).",
]
