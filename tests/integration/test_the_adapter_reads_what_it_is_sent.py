"""The universal adapter judges the payload it is given, or refuses to judge it.

The adapter is offered as the way a team puts Threefold in front of an agent
without writing glue: send the tool call as the agent emits it. It read an
Anthropic `tool_use` object and a `{name, arguments}` pair, and nothing else.
The two shapes OpenAI actually returns — a message-level `function_call` object
and a `tool_calls` array — fell through to a branch that named the call
"unknown_tool" with no arguments at all, and a call with no arguments passes
every gate. So a command exporting an access key came back APPROVED, from the
route whose whole job is to read it, while the same command sent as a
`tool_use` came back BLOCKED_SECRET_DETECTED.

Two guarantees here: every shape the adapter claims to read is judged exactly
as the native route judges it, and a shape it cannot read is refused rather
than approved as an empty call.
"""
from __future__ import annotations

import json

import pytest

from threefold.interfaces.api_handlers import lambda_handler

# Synthetic, and the same key AWS prints in its own documentation, which is what
# the demo's secret scenario uses.
LEAKING_COMMAND = {"command": "export AWS_ACCESS_KEY_ID=" + "AKIA" + "IOSFODNN7EXAMPLE"}
CLEAN_READ = {"path": "README.md"}


def _post(body: dict, path: str = "/adapter/universal-tool-call"):
    response = lambda_handler(
        {
            "rawPath": f"/prod{path}",
            "headers": {"Content-Type": "application/json"},
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
            "body": json.dumps(body),
        }
    )
    return response["statusCode"], json.loads(response["body"])


def _openai_function_call(session_id: str, name: str, arguments: dict) -> dict:
    return {"session_id": session_id, "function_call": {"name": name, "arguments": json.dumps(arguments)}}


def _openai_tool_calls(session_id: str, name: str, arguments: dict) -> dict:
    return {
        "session_id": session_id,
        "tool_calls": [
            {"id": "call_a1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        ],
    }


def _anthropic_tool_use(session_id: str, name: str, arguments: dict) -> dict:
    return {"session_id": session_id, "type": "tool_use", "name": name, "input": arguments}


def _flat(session_id: str, name: str, arguments: dict) -> dict:
    return {"session_id": session_id, "name": name, "arguments": json.dumps(arguments)}


def _nested(session_id: str, name: str, arguments: dict) -> dict:
    """The same call inside the envelope the pages and the template test use."""
    return {"session_id": session_id, "tool_call": {"type": "tool_use", "name": name, "input": arguments}}


SHAPES = {
    "openai function_call": _openai_function_call,
    "openai tool_calls": _openai_tool_calls,
    "anthropic tool_use": _anthropic_tool_use,
    "flat name and arguments": _flat,
    "nested tool_call": _nested,
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_credential_is_refused_in_every_shape_the_adapter_claims_to_read(shape) -> None:
    status, body = _post(SHAPES[shape](f"adapter-secret-{abs(hash(shape)) % 10000}", "run_command", LEAKING_COMMAND))
    assert status == 200, body
    assert body["detected_tool_name"] == "run_command"
    assert body["detected_action_type"] == "COMMAND_EXEC"
    assert body["evaluation"]["status"] == "BLOCKED_SECRET_DETECTED", f"{shape} was not read"


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_ordinary_work_is_still_approved_in_every_shape(shape) -> None:
    status, body = _post(SHAPES[shape](f"adapter-clean-{abs(hash(shape)) % 10000}", "read_file", CLEAN_READ))
    assert status == 200, body
    assert body["detected_tool_name"] == "read_file"
    assert body["evaluation"]["status"] == "APPROVED"


def test_an_openai_tool_call_object_on_its_own_is_read() -> None:
    """One entry of a tool_calls array, sent as the whole body."""
    status, body = _post(
        {
            "session_id": "adapter-single-object",
            "tool_call": {"type": "function", "function": {"name": "run_command", "arguments": json.dumps(LEAKING_COMMAND)}},
        }
    )
    assert status == 200, body
    assert body["evaluation"]["status"] == "BLOCKED_SECRET_DETECTED"


def test_a_body_that_names_no_tool_is_refused_rather_than_approved() -> None:
    """The old fall-through: unknown_tool, no arguments, and every gate satisfied."""
    status, problem = _post({"session_id": "adapter-nothing", "hello": "world"})
    assert status == 400
    assert problem["invalid_params"][0]["name"] == "tool_call"
    assert "tool_use" in problem["detail"]


@pytest.mark.parametrize(
    "arguments",
    ["42", '"a string"', "[1, 2]", "not json at all", json.dumps([{"command": "ls"}])],
)
def test_arguments_the_gates_cannot_walk_are_refused(arguments) -> None:
    """A number or a list holds no strings to scan, so it would be approved unread."""
    status, problem = _post(
        {"session_id": "adapter-args", "function_call": {"name": "run_command", "arguments": arguments}}
    )
    assert status == 400, problem
    assert problem["invalid_params"][0]["name"] == "arguments"


def test_a_tool_call_with_no_name_is_refused() -> None:
    status, problem = _post({"session_id": "adapter-noname", "function_call": {"arguments": "{}"}})
    assert status == 400
    assert problem["invalid_params"][0]["name"] == "name"


@pytest.mark.parametrize(
    "calls",
    [
        [],
        [
            {"function": {"name": "read_file", "arguments": json.dumps(CLEAN_READ)}},
            {"function": {"name": "run_command", "arguments": json.dumps(LEAKING_COMMAND)}},
        ],
        "not a list",
    ],
)
def test_a_tool_calls_array_that_is_not_one_call_is_refused(calls) -> None:
    """Answering a batch with one verdict would leave the rest judged by nothing."""
    status, problem = _post({"session_id": "adapter-batch", "tool_calls": calls})
    assert status == 400, problem
    assert problem["invalid_params"][0]["name"] == "tool_calls"


def test_a_tool_that_takes_no_arguments_is_still_evaluated() -> None:
    status, body = _post({"session_id": "adapter-empty-args", "function_call": {"name": "list_dir", "arguments": "{}"}})
    assert status == 200, body
    assert body["evaluation"]["status"] == "APPROVED"


def test_the_alias_route_reads_the_same_shapes() -> None:
    status, body = _post(_openai_tool_calls("adapter-alias", "run_command", LEAKING_COMMAND), "/universal-eval")
    assert status == 200, body
    assert body["evaluation"]["status"] == "BLOCKED_SECRET_DETECTED"


def test_the_adapter_and_the_native_route_agree_on_the_same_call() -> None:
    """The claim the adapter makes: no glue code, and the same verdict."""
    adapter_status, adapter = _post(_openai_function_call("adapter-parity-1", "run_command", LEAKING_COMMAND))
    native = lambda_handler(
        {
            "rawPath": "/prod/evaluate-tool-call",
            "headers": {"Content-Type": "application/json"},
            "requestContext": {"http": {"method": "POST"}, "stage": "prod"},
            "body": json.dumps(
                {
                    "session_id": "adapter-parity-2",
                    "tool_name": "run_command",
                    "action_type": "COMMAND_EXEC",
                    "arguments": LEAKING_COMMAND,
                }
            ),
        }
    )
    assert adapter_status == 200
    assert adapter["evaluation"]["status"] == json.loads(native["body"])["status"]
