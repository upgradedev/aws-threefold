"""The hook sends the file before and after an edit beside it.

A partial-line edit carries no import syntax in its own text, so the service
cannot tell what the edit adds from the fragment alone. The hook reads the
file the edit names and applies each old/new pair in order, as the tool will,
and sends both reconstructions for the introduced-imports check. Any doubt -
a missing, large or undecodable file, an old string that does not occur
exactly once - sends the edit as its new text alone, judged as it always
was. Anything the hook would not send keeps the context home with it.
"""
from __future__ import annotations

from conftest import AGENTS  # noqa: F401 - the suite runs every agent dir through these

import pytest


def _edit(path, old: str, new: str):
    return {
        "session_id": "acme-session-1",
        "transcript_path": str(path.parent / "transcript.jsonl"),
        "cwd": str(path.parent),
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": old, "new_string": new},
    }


def _sent(stub):
    assert len(stub.requests) == 1
    return stub.requests[0]["body"]["arguments"]


def test_an_edit_carries_the_file_before_and_after(machine, stub, run_hook) -> None:
    target = machine.project / "src" / "domain" / "order.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"import json\n\n\ndef total():\n    return 0\n")
    code, out, _ = run_hook(_edit(target, "import json", "import boto3"))
    assert (code, out) == (0, "")
    arguments = _sent(stub)
    assert arguments["content"] == "import boto3"
    assert arguments["edit_before"] == "import json\n\n\ndef total():\n    return 0\n"
    assert arguments["edit_after"] == "import boto3\n\n\ndef total():\n    return 0\n"


def test_a_multiedit_applies_each_pair_in_order(machine, stub, run_hook) -> None:
    target = machine.project / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"aaa\n")
    payload = {
        "session_id": "acme-session-1",
        "cwd": str(machine.project),
        "hook_event_name": "PreToolUse",
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": str(target),
            "edits": [
                {"old_string": "aaa", "new_string": "aba"},
                {"old_string": "aba", "new_string": "aca"},
            ],
        },
    }
    code, out, _ = run_hook(payload)
    assert (code, out) == (0, "")
    arguments = _sent(stub)
    assert arguments["edit_before"] == "aaa\n"
    assert arguments["edit_after"] == "aca\n"


def test_a_missing_file_sends_the_edit_alone(machine, stub, run_hook) -> None:
    target = machine.project / "src" / "absent.py"
    code, out, _ = run_hook(_edit(target, "aaa", "aba"))
    assert (code, out) == (0, "")
    arguments = _sent(stub)
    assert "edit_before" not in arguments and "edit_after" not in arguments


def test_an_old_string_that_matches_twice_sends_the_edit_alone(machine, stub, run_hook) -> None:
    target = machine.project / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"aaa\naaa\n")
    code, out, _ = run_hook(_edit(target, "aaa", "aba"))
    assert (code, out) == (0, "")
    arguments = _sent(stub)
    assert "edit_before" not in arguments and "edit_after" not in arguments


def test_a_large_file_sends_the_edit_alone(machine, stub, run_hook) -> None:
    target = machine.project / "src" / "big.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"# padded\n" + b"x" * 70000)
    code, out, _ = run_hook(_edit(target, "# padded", "# changed"))
    assert (code, out) == (0, "")
    arguments = _sent(stub)
    assert "edit_before" not in arguments and "edit_after" not in arguments


def test_a_file_that_is_not_text_sends_the_edit_alone(machine, stub, run_hook) -> None:
    target = machine.project / "src" / "blob.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xfe\x00bad-bytes")
    code, out, _ = run_hook(_edit(target, "aaa", "aba"))
    assert (code, out) == (0, "")
    arguments = _sent(stub)
    assert "edit_before" not in arguments and "edit_after" not in arguments


def test_a_never_send_term_in_the_surroundings_keeps_the_context_home(
    machine, stub, run_hook
) -> None:
    """The surroundings were never in the payload the hold-back saw, so they are read again here."""
    machine.threefold_home.mkdir(parents=True, exist_ok=True)
    (machine.threefold_home / "never_send.txt").write_text("Acme-Orion\n", encoding="utf-8")
    target = machine.project / "src" / "app.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"CLIENT = 'Acme-Orion'\nvalue = 1\n")
    code, out, _ = run_hook(_edit(target, "value = 1", "value = 2"))
    assert (code, out) == (0, "")
    arguments = _sent(stub)
    assert arguments["content"] == "value = 2"
    assert "edit_before" not in arguments and "edit_after" not in arguments


def test_a_write_sends_no_context(machine, stub, run_hook, payloads) -> None:
    run_hook(payloads.write("claude-code", "src/app.py", "x = 1\n"))
    arguments = _sent(stub)
    assert "edit_before" not in arguments and "edit_after" not in arguments


def test_stripping_a_governance_write_removes_its_context(hook) -> None:
    call = hook.NormalisedCall(
        "FILE_WRITE",
        [{
            "file_path": ".claude/settings.json",
            "content": "{}",
            "edit_before": "{}",
            "edit_after": '{"a": 1}',
        }],
    )
    hook._strip_governance_content(call)
    entry = call.files[0]
    assert entry["content"] == ""
    assert "edit_before" not in entry and "edit_after" not in entry
