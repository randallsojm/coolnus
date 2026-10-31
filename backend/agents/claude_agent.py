"""Claude-backed agent. One class, instantiated once per role with that role's prompt, view and tools.

Each message = its own API call(s). The model may call its info tools (capped), then must call
`send_message`. Claude Sonnet 5.5 / Opus 5.5 reject forced tool_choice, so tool_choice stays "auto"
and the model is reminded if it answers without calling send_message (counted as a format retry).
"""
import json
import os
from pathlib import Path

import config as CFG
from agent_tools import PROPOSAL_SCHEMA, _inline
from agents.base import Agent, AgentFailure
from messages import AgentMessage

PROMPTS = Path(__file__).resolve().parent / "prompts"
SEND = "send_message"


def send_message_tool():
    return {"name": SEND,
            "description": "Send your ONE structured message for this turn. Call it exactly once, last.",
            "input_schema": _inline(AgentMessage.model_json_schema())}


def make_client():
    key = os.environ.get(CFG.API_KEY_ENV)
    if not key:
        raise SystemExit(f"Set {CFG.API_KEY_ENV} to use --agents claude (or run with --agents rule).")
    import anthropic                      # imported only when the Claude agents are used
    return anthropic.Anthropic(api_key=key)


def _blocks_to_dicts(blocks):
    out = []
    for b in blocks:
        t = getattr(b, "type", None)
        if t == "text":
            out.append({"type": "text", "text": b.text})
        elif t == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


class ClaudeAgent(Agent):
    kind = "claude"

    def __init__(self, role, client, model=None):
        super().__init__(role)
        self.client = client
        self.model = model or CFG.MODEL
        self.system = (PROMPTS / f"{role.lower()}.md").read_text()
        self.transcript = []                # per-message API summaries for the log

    def _user_prompt(self, ctx):
        return json.dumps({
            "you_are": self.role,
            "turn": ctx["turn"], "final_turn": ctx["final_turn"],
            "public": ctx["public"], "your_private_view": ctx["private"],
            "messages_so_far": ctx["history"],
            "proposals": ctx["proposals"],
            "latest_proposal_id": ctx["latest_proposal_id"],
            "valid_proposal_ids": sorted(ctx["proposals"]),
            "allowed_message_types": (["PROPOSE"] if self.role == "ATC" and not ctx["proposals"]
                                      else ["ACCEPT", "COUNTER", "REJECT"]),
            "proposal_format": PROPOSAL_SCHEMA,
            "instruction": ("Use your tools for every number, then call send_message exactly once. "
                            "responds_to MUST be one of valid_proposal_ids (e.g. 'P1'), never a message id "
                            "like 'T1-ATC'. For COUNTER, copy the proposal you respond to exactly and change "
                            "only the fields you control. Leave df.gate null unless you are GROUND."),
        }, default=str)

    def respond(self, ctx):
        tb = ctx["toolbox"]
        tools = tb.definitions() + [send_message_tool()]
        if CFG.STRICT_TOOLS:
            tools = [dict(t, strict=True) for t in tools]
        messages = [{"role": "user", "content": self._user_prompt(ctx)}]
        tool_calls = retries = 0
        log = {"api_calls": []}
        self.transcript.append(log)
        max_calls = CFG.MAX_TOOL_CALLS_PER_MESSAGE + CFG.MAX_FORMAT_RETRIES + 1
        for _ in range(max_calls):
            kwargs = dict(model=self.model, max_tokens=CFG.MAX_TOKENS, system=self.system,
                          tools=tools, messages=messages)
            if CFG.TEMPERATURE is not None:
                kwargs["temperature"] = CFG.TEMPERATURE
            resp = self.client.messages.create(**kwargs)
            self.stats["api_calls"] += 1
            u = getattr(resp, "usage", None)
            if u is not None:
                self.stats["input_tokens"] += getattr(u, "input_tokens", 0) or 0
                self.stats["output_tokens"] += getattr(u, "output_tokens", 0) or 0
            blocks = _blocks_to_dicts(resp.content)
            uses = [b for b in blocks if b["type"] == "tool_use"]
            log["api_calls"].append({"stop_reason": getattr(resp, "stop_reason", None),
                                     "tools": [b["name"] for b in uses]})
            messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(empty)"}]})
            send = next((b for b in uses if b["name"] == SEND), None)
            if send is not None:
                try:
                    return AgentMessage.model_validate(send["input"])
                except Exception as e:
                    retries += 1
                    self.stats["format_retries"] += 1
                    if retries > CFG.MAX_FORMAT_RETRIES:
                        raise AgentFailure(f"invalid send_message input: {e}")
                    results = [{"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                                "content": f"Invalid message: {e}. Fix it and call send_message again."
                                if b is send else "Ignored: send_message was invalid."} for b in uses]
                    messages.append({"role": "user", "content": results})
                    continue
            if not uses:
                retries += 1
                self.stats["format_retries"] += 1
                if retries > CFG.MAX_FORMAT_RETRIES:
                    raise AgentFailure("did not call send_message")
                messages.append({"role": "user", "content": "You must now call send_message with your one message."})
                continue
            results = []
            for b in uses:
                tool_calls += 1
                self.stats["tool_calls"] += 1
                if tool_calls > CFG.MAX_TOOL_CALLS_PER_MESSAGE:
                    out, err = {"error": "tool budget used up: call send_message now"}, True
                else:
                    out = tb.call(b["name"], b["input"])
                    err = "error" in out
                results.append({"type": "tool_result", "tool_use_id": b["id"],
                                "content": json.dumps(out, default=str), "is_error": err})
            messages.append({"role": "user", "content": results})
        raise AgentFailure("no send_message within the call limit")


def make_claude_agents(client=None, model=None):
    client = client or make_client()
    return {r: ClaudeAgent(r, client, model) for r in ("ATC", "PILOT", "GROUND")}
