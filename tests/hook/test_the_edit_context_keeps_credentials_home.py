"""The file an edit changes goes with its credentials left on the machine.

The credential check runs over what the call writes, before the hook reads
the edited file for its before and after. A key elsewhere in that file - one
the edit never touches - used to ride along in the context and leave the
machine, while a Write carrying the same key was refused locally. The context
is there for its imports, so every credential shape in it is replaced by its
label: the imports still reach the introduced-imports check, the key does not
reach the service.
"""
from __future__ import annotations

from conftest import AGENTS  # noqa: F401 - the suite runs every agent dir through these

KEY = "AKIAIOSFODNN7EXAMPLE"


def _edit(path, old: str, new: str):
    return {
        "session_id": "acme-session-1",
        "cwd": str(path.parent),
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": old, "new_string": new},
    }


def _sent(stub):
    assert len(stub.requests) == 1
    return stub.requests[0]["body"]["arguments"]


def test_a_key_the_edit_does_not_touch_stays_home(machine, stub, run_hook) -> None:
    target = machine.project / "src" / "app" / "settings.py"
    target.parent.mkdir(parents=True)
    target.write_text(f"import os\n\nKEY = '{KEY}'\nX = 1\n", encoding="utf-8")
    code, out, _ = run_hook(_edit(target, "X = 1", "X = 2"))
    assert (code, out) == (0, "")
    arguments = _sent(stub)
    assert KEY not in str(stub.requests[0]["body"])
    assert arguments["edit_before"] == "import os\n\nKEY = '<AWS_ACCESS_KEY kept on the machine>'\nX = 1\n"
    assert arguments["edit_after"] == "import os\n\nKEY = '<AWS_ACCESS_KEY kept on the machine>'\nX = 2\n"


def test_the_imports_still_reach_the_check_beside_a_redacted_key(machine, stub, run_hook) -> None:
    target = machine.project / "src" / "domain" / "order.py"
    target.parent.mkdir(parents=True)
    target.write_text(f"import json\nTOKEN = '{KEY}'\n", encoding="utf-8")
    code, out, _ = run_hook(_edit(target, "json", "boto3"))
    assert (code, out) == (0, "")
    arguments = _sent(stub)
    assert KEY not in str(stub.requests[0]["body"])
    assert arguments["edit_before"].startswith("import json\n")
    assert arguments["edit_after"].startswith("import boto3\n")


def test_every_shape_the_hook_knows_is_redacted(hook) -> None:
    samples = {
        "AWS_ACCESS_KEY": KEY,
        "GITHUB_TOKEN": "ghp_" + "a" * 36,
        "ANTHROPIC_KEY": "sk-ant-" + "b" * 24,
        "PRIVATE_KEY_HEADER": "-----BEGIN RSA " + "PRIVATE KEY-----",
    }
    for label, secret in samples.items():
        redacted = hook.redact_credentials(f"x = '{secret}'\n")
        assert secret not in redacted, label
        assert not any(pattern.search(redacted) for _, pattern in hook.SECRET_PATTERNS), label
