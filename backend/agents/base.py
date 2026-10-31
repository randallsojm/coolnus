"""Agent interface. Every agent (Claude or rule-based) answers one turn with one message."""
from messages import AgentMessage


class AgentFailure(Exception):
    """The agent could not produce a valid message (recorded as NO_VALID_RESPONSE)."""


class Agent:
    kind = "abstract"

    def __init__(self, role):
        self.role = role
        self.stats = {"api_calls": 0, "tool_calls": 0, "format_retries": 0,
                      "input_tokens": 0, "output_tokens": 0}

    def respond(self, ctx) -> AgentMessage:
        """ctx keys: turn, final_turn, public, private, history, proposals, latest_proposal_id, toolbox, sc."""
        raise NotImplementedError
