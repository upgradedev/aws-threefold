"""Muse speaks to the hook in its own names, and the hook answers in Claude's.

Muse (Muse Code) writes with write_file and edit_file and runs its shell with
powershell, every path relative to the cwd the payload carries; its reads and
its internal calls are met with silence. A refusal is the same shape Claude
Code reads, which is what stops the write. Shapes and enforcement are in
docs/evidence/ENFORCEMENT_2026-09-28-MUSE.md.
"""
from __future__ import annotations

import json
from typing import Any, Dict


def _muse(tool_name: str, tool_input: Dict[str, Any], cwd: str) -> str:
    return json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": "call_muse1",
        "session_id": "muse-session-1",
        "turn_id": "muse-turn-1",
        "cwd": cwd,
        "transcript_path": None,
        "model": "muse-spark-1.3-contributor",
        "permission_mode": "default",
        "model_provider": "meta",
    })


def test_muse_is_detected_by_its_provider(hook) -> None:
    raw = _muse("write_file", {"path": "src/app.py", "content": "x = 1\n"}, "C:/proj")
    assert hook.detect_agent(json.loads(raw)) == "muse"


def test_muse_is_detected_by_its_tool_names_without_the_provider(hook) -> None:
    for tool_name in ("write_file", "edit_file", "powershell", "read_file", "search", "submit_reminder_decision"):
        payload = json.loads(_muse(tool_name, {"path": "src/app.py"}, "C:/proj"))
        del payload["model_provider"]
        assert hook.detect_agent(payload) == "muse", tool_name


def test_muse_is_detected_by_its_cron_names_without_the_provider(hook) -> None:
    payload = json.loads(_muse("cron_create", {"schedule": "daily"}, "C:/proj"))
    del payload["model_provider"]
    assert hook.detect_agent(payload) == "muse"


def test_the_agent_flag_still_decides_for_muse(hook) -> None:
    forced, problem = hook.parse_agent_flag(["--agent", "muse"])
    assert (forced, problem) == ("muse", None)


def test_a_muse_write_is_sent_as_a_write(hook, tmp_path, monkeypatch) -> None:
    sent: Dict[str, Any] = {}
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: sent.update(body) or (200, {"status": "APPROVED"}, "OK"))
    raw = _muse("write_file", {"path": "src/app.py", "content": "x = 1\n"}, str(tmp_path))
    output, _ = hook.handle(raw, "muse")
    assert output is None
    assert sent["agent"] == "muse"
    assert sent["tool_name"] == "Write"
    assert sent["action_type"] == "FILE_WRITE"
    assert sent["arguments"] == {"file_path": "src/app.py", "content": "x = 1\n"}
    assert sent["session_id"] == "muse-session-1"


def test_a_muse_edit_sends_only_the_replacement(hook, tmp_path, monkeypatch) -> None:
    sent: Dict[str, Any] = {}
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: sent.update(body) or (200, {"status": "APPROVED"}, "OK"))
    raw = _muse("edit_file", {"path": "src/app.py", "find": "x = 1", "replace": "x = 2"}, str(tmp_path))
    output, _ = hook.handle(raw, "muse")
    assert output is None
    assert sent["tool_name"] == "Write"
    assert sent["arguments"] == {"file_path": "src/app.py", "content": "x = 2"}


def test_a_muse_shell_command_is_sent_with_its_workdir(hook, tmp_path, monkeypatch) -> None:
    sent: Dict[str, Any] = {}
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: sent.update(body) or (200, {"status": "APPROVED"}, "OK"))
    raw = _muse(
        "powershell",
        {"command": "Get-ChildItem", "description": "List files", "workdir": str(tmp_path)},
        str(tmp_path),
    )
    output, _ = hook.handle(raw, "muse")
    assert output is None
    assert sent["tool_name"] == "Bash"
    assert sent["action_type"] == "COMMAND_EXEC"
    assert sent["arguments"]["command"] == "Get-ChildItem"


def test_a_muse_workdir_loses_its_win32_prefix(hook, tmp_path) -> None:
    payload = json.loads(_muse(
        "powershell",
        {"command": "echo hi", "description": "hi", "workdir": "\\\\?\\" + str(tmp_path)},
        str(tmp_path),
    ))
    call = hook.normalise(payload, "muse")
    assert call is not None and call.command_base == str(tmp_path)


def test_a_muse_relative_write_is_judged_inside_the_project(hook, tmp_path, monkeypatch) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / ".threefold.json").write_text(json.dumps({"project": "Acme-Muse"}), encoding="utf-8")
    for name in ("THREEFOLD_PROJECT", "THREEFOLD_ENDPOINT", "THREEFOLD_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("THREEFOLD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: (200, {"status": "APPROVED"}, "OK"))
    raw = _muse("write_file", {"path": "src/app.py", "content": "x = 1\n"}, str(tmp_path))
    output, notes = hook.handle(raw, "muse")
    assert output is None
    assert [line for line in notes if "outside the project" in line] == []


def test_a_muse_write_that_escapes_the_project_is_held_back(hook, tmp_path, monkeypatch) -> None:
    (tmp_path / ".threefold.json").write_text(json.dumps({"project": "Acme-Muse"}), encoding="utf-8")
    for name in ("THREEFOLD_PROJECT", "THREEFOLD_ENDPOINT", "THREEFOLD_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("THREEFOLD_HOME", str(tmp_path / "home"))
    calls = []
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: calls.append(body) or (200, {"status": "APPROVED"}, "OK"))
    raw = _muse("write_file", {"path": "../escape.py", "content": "x = 1\n"}, str(tmp_path))
    output, _ = hook.handle(raw, "muse")
    assert output is None
    assert calls == []


def test_a_muse_credential_is_refused_in_claude_s_shape(hook, tmp_path) -> None:
    raw = _muse("write_file", {"path": "src/settings.py", "content": "AKIAIOSFODNN7EXAMPLE\n"}, str(tmp_path))
    output, _ = hook.handle(raw, "muse")
    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": output["hookSpecificOutput"]["permissionDecisionReason"],
        }
    }
    assert "credential" in output["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_muse_write_to_its_own_plugin_bundle_is_refused_without_a_request(hook, tmp_path, monkeypatch) -> None:
    """The bundle decides whether this hook runs, like every other agent's
    settings: a Muse agent neutering its own hook is refused on the machine."""
    calls = []
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: calls.append(body) or (200, {"status": "APPROVED"}, "OK"))
    for relative in (
        ".threefold-muse/hooks/threefold_hook.py",
        ".threefold-muse/.muse-plugin/plugin.json",
        ".threefold-muse/manifest.json",
    ):
        raw = _muse("write_file", {"path": relative, "content": "{}\n"}, str(tmp_path))
        output, _ = hook.handle(raw, "muse")
        assert output is not None, relative
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny", relative
        assert relative in output["hookSpecificOutput"]["permissionDecisionReason"], relative
    assert calls == []


def test_a_muse_refusal_from_the_service_keeps_claude_s_shape(hook, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        hook, "post_evaluation",
        lambda body, endpoint, key=None: (200, {"status": "BLOCKED", "reason": "a domain file must not import boto3."}, "OK"),
    )
    raw = _muse("write_file", {"path": "src/domain/orders.py", "content": "import boto3\n"}, str(tmp_path))
    output, _ = hook.handle(raw, "muse")
    assert output is not None and output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_muse_read_is_met_with_silence(hook, tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: calls.append(body) or (200, {"status": "APPROVED"}, "OK"))
    for tool_name, tool_input in (
        ("read_file", {"path": "src/app.py"}),
        ("search", {"pattern": "^", "output_mode": "files_with_matches", "glob": ["**/*.py"]}),
    ):
        output, notes = hook.handle(_muse(tool_name, tool_input, str(tmp_path)), "muse")
        assert (output, notes) == (None, []), tool_name
    assert calls == []


def test_a_muse_internal_call_is_met_with_silence(hook, tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: calls.append(body) or (200, {"status": "APPROVED"}, "OK"))
    for tool_name in ("submit_reminder_decision", "cron_create", "cron_anything_new"):
        output, notes = hook.handle(_muse(tool_name, {"decision": "none"}, str(tmp_path)), "muse")
        assert (output, notes) == (None, []), tool_name
    assert calls == []


def test_an_unknown_muse_tool_is_met_with_silence(hook, tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: calls.append(body) or (200, {"status": "APPROVED"}, "OK"))
    payload = json.loads(_muse("write_file", {"path": "src/app.py", "content": "x = 1\n"}, str(tmp_path)))
    payload["tool_name"] = "teleport_file"
    output, notes = hook.handle(json.dumps(payload), "muse")
    assert (output, notes) == (None, [])
    assert calls == []


def test_a_muse_write_without_its_shape_is_unjudged_not_sent(hook, tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: calls.append(body) or (200, {"status": "APPROVED"}, "OK"))
    for tool_input in ({"content": "x = 1\n"}, {"path": "  "}, None, "src/app.py"):
        output, notes = hook.handle(_muse("write_file", tool_input, str(tmp_path)), "muse")
        assert output is None
        assert any("cannot read" in line for line in notes), tool_input
    assert calls == []


def test_a_muse_shell_without_a_command_is_unjudged_not_sent(hook, tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: calls.append(body) or (200, {"status": "APPROVED"}, "OK"))
    for tool_input in ({"description": "hi"}, {"command": "  "}, None):
        output, notes = hook.handle(_muse("powershell", tool_input, str(tmp_path)), "muse")
        assert output is None
        assert any("cannot read" in line for line in notes), tool_input
    assert calls == []


def test_muse_fails_open_when_the_service_is_down(hook, tmp_path, monkeypatch) -> None:
    def down(body, endpoint, key=None):
        raise hook.ServiceUnavailable("connection refused")

    monkeypatch.setattr(hook, "post_evaluation", down)
    raw = _muse("write_file", {"path": "src/app.py", "content": "x = 1\n"}, str(tmp_path))
    output, notes = hook.handle(raw, "muse")
    assert output is None
    assert any("connection refused" in line for line in notes)


def test_muse_never_sends_its_credential_findings(hook, tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(hook, "post_evaluation", lambda body, endpoint, key=None: calls.append(body) or (200, {"status": "APPROVED"}, "OK"))
    raw = _muse("write_file", {"path": "src/settings.py", "content": "AKIAIOSFODNN7EXAMPLE\n"}, str(tmp_path))
    output, _ = hook.handle(raw, "muse")
    assert output is not None
    assert calls == []
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(output)


def test_muse_in_observe_mode_sends_a_dry_run(hook, tmp_path, monkeypatch) -> None:
    (tmp_path / ".threefold.json").write_text(
        json.dumps({"project": "Acme-Muse", "mode": "observe"}), encoding="utf-8"
    )
    for name in ("THREEFOLD_PROJECT", "THREEFOLD_ENDPOINT", "THREEFOLD_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("THREEFOLD_HOME", str(tmp_path / "home"))
    sent: Dict[str, Any] = {}
    monkeypatch.setattr(
        hook, "post_evaluation",
        lambda body, endpoint, key=None: sent.update(body) or (200, {"status": "OBSERVED"}, "OK"),
    )
    raw = _muse("write_file", {"path": "src/app.py", "content": "x = 1\n"}, str(tmp_path))
    output, _ = hook.handle(raw, "muse")
    assert output is None
    assert sent["dry_run"] is True
    assert sent["hook_mode"] == "observe"


def test_the_muse_tool_names_stay_the_three_measured(hook) -> None:
    assert hook.MUSE_WRITE_TOOLS == ("write_file", "edit_file")
    assert hook.MUSE_COMMAND_TOOLS == ("powershell",)


def test_muse_joins_the_agent_tuple_last(hook) -> None:
    assert hook.AGENTS == ("claude-code", "codex", "antigravity", "muse")
    muse = json.loads(_muse("write_file", {"path": "a.py", "content": "x"}, "C:/proj"))
    assert hook.normalise(muse, "muse") is not None
