#!/usr/bin/env python3
"""Threefold as a Claude Code PreToolUse hook.

This is the difference between a demo and a product. The scenarios on the web
page build their own payloads and then refuse them, which proves the gates run
but intercepts nobody. This script puts Threefold in front of a real agent: it
receives the tool call Claude Code is about to make, asks the deployed service,
and returns the decision that Claude Code obeys.

Take it from the running service, which serves this file:

    curl -O https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/hooks/claude_code_hook.py

Install by adding to .claude/settings.json:

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Write|Edit|Bash|Read",
            "hooks": [
              {"type": "command", "command": "python3 /absolute/path/to/claude_code_hook.py"}
            ]
          }
        ]
      }
    }

Standard library only, so it needs no environment of its own.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod"
TIMEOUT_SECONDS = float(os.environ.get("THREEFOLD_TIMEOUT", "6"))

# Claude Code's tool names, mapped to the vocabulary the service speaks.
ACTION_TYPES = {
    "Read": "FILE_READ",
    "NotebookRead": "FILE_READ",
    "Write": "FILE_WRITE",
    "Edit": "FILE_WRITE",
    "MultiEdit": "FILE_WRITE",
    "NotebookEdit": "FILE_WRITE",
    "Bash": "COMMAND_EXEC",
    "WebFetch": "WEB_SEARCH",
    "WebSearch": "WEB_SEARCH",
}


def _allow(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def evaluate(payload: dict, endpoint: str, session_id: str) -> dict:
    """Asks the deployed service about one tool call and returns a hook decision.

    On any transport failure this allows the call and says so. That is a
    deliberate choice and it is the honest weakness of running the gate out of
    process: a guard that bricks the agent when the network hiccups is a guard
    that gets uninstalled by lunchtime. Set THREEFOLD_FAIL_CLOSED=1 to invert
    it where the risk is worth the interruption.
    """
    tool_name = payload.get("tool_name") or "unknown"
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}

    request_body = json.dumps(
        {
            "session_id": session_id,
            "developer_id": os.environ.get("USER") or os.environ.get("USERNAME") or "local",
            "project_name": os.path.basename(os.getcwd()) or "workspace",
            "tool_name": tool_name,
            "action_type": ACTION_TYPES.get(tool_name, "UNKNOWN"),
            "arguments": tool_input,
            "projected_input_tokens": int(os.environ.get("THREEFOLD_INPUT_TOKENS", "0")),
            "projected_output_tokens": int(os.environ.get("THREEFOLD_OUTPUT_TOKENS", "0")),
            "budget_usd": float(os.environ.get("THREEFOLD_BUDGET_USD", "25")),
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/evaluate-tool-call",
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            verdict = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        if os.environ.get("THREEFOLD_FAIL_CLOSED", "").lower() in ("1", "true", "yes"):
            return _deny(f"Threefold was unreachable and is configured to fail closed: {exc}")
        return _allow(f"Threefold was unreachable, so this call was not checked: {exc}")

    status = verdict.get("status", "APPROVED")
    if status == "APPROVED":
        return _allow("Threefold: every gate passed.")

    reason = verdict.get("reason") or status
    explanation = verdict.get("bedrock_explanation") or ""
    source = verdict.get("explanation_source") or "unknown"
    detail = f"Threefold refused this call. {reason}"
    if explanation:
        label = "Bedrock" if source == "bedrock" else "Deterministic explanation"
        detail = f"{detail}\n{label}: {explanation}"
    return _deny(detail)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        # A hook that cannot read its input must not take the agent down with it.
        print(json.dumps(_allow("Threefold could not read the hook payload.")))
        return 0

    endpoint = os.environ.get("THREEFOLD_ENDPOINT", DEFAULT_ENDPOINT)
    session_id = payload.get("session_id") or os.environ.get("THREEFOLD_SESSION", "claude-code-local")
    print(json.dumps(evaluate(payload, endpoint, str(session_id))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
