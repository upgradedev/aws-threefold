"""The hook refuses to be disarmed, on the machine, before anything is sent.

The agents' own directories stay private: memory, sessions and transcripts
are held back silently, and reading the settings is held back the same way.
But a write that decides whether the hooks run - an agent's settings file,
or Threefold's own hook binary - is refused out loud, in every mode, with
nothing sent. A silent hold-back there would let the agent turn the guard
off and keep working as though it were still governed.
"""
from __future__ import annotations

import json

import pytest

SETTINGS_FILES = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".codex/config.toml",
    ".gemini/settings.json",
    ".claude.json",
)

HOOK_BINARIES = (
    "bin/threefold_hook.py",
    "bin/threefold_cli.py",
)


def _write(home, relative: str, content: str = "{}"):
    return {
        "session_id": "s",
        "cwd": str(home),
        "tool_name": "Write",
        "tool_input": {"file_path": str(home / relative), "content": content},
    }


def _denied(stub, run_hook, verdict, payload, argv=None):
    code, out, _ = run_hook(payload, argv)
    assert code == 0, "A refusal is printed, never signalled by exit code"
    assert verdict.decision(out) == "deny"
    assert "hooks" in verdict.reason(out).lower()
    assert stub.requests == [], "A refused call must not reach the service"


@pytest.mark.parametrize("relative", SETTINGS_FILES)
def test_a_write_to_an_agents_settings_is_refused_before_anything_is_sent(
    relative, machine, stub, run_hook, verdict
) -> None:
    _denied(stub, run_hook, verdict, _write(machine.home, relative, '{"disableAllHooks": true}'))


@pytest.mark.parametrize("relative", HOOK_BINARIES)
def test_a_write_to_threefolds_own_binary_is_refused_before_anything_is_sent(
    relative, machine, stub, run_hook, verdict
) -> None:
    _denied(stub, run_hook, verdict, _write(machine.threefold_home, relative, "# replaced"))


def test_an_edit_to_an_agents_settings_is_refused(machine, stub, run_hook, verdict) -> None:
    payload = {
        "session_id": "s",
        "cwd": str(machine.home),
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(machine.home / ".claude" / "settings.json"),
            "old_string": '"hooks": {}',
            "new_string": '"hooks": {}, "disableAllHooks": true',
        },
    }
    _denied(stub, run_hook, verdict, payload)


@pytest.mark.parametrize("mode", ["enforce", "managed", "observe"])
def test_a_disarm_is_refused_in_every_mode(mode, machine, stub, run_hook, verdict, monkeypatch) -> None:
    """Self-protection is not project governance: observe still watches, but no mode lets the agent remove the watch."""
    monkeypatch.setenv("THREEFOLD_MODE", mode)
    _denied(stub, run_hook, verdict, _write(machine.home, ".claude/settings.json", "{}"))


def test_a_refused_disarm_is_not_logged_as_held_back(machine, stub, run_hook, held_back_lines) -> None:
    """Held back means unjudged and allowed; a refusal is judged, so the log keeps no held-back line for it."""
    run_hook(_write(machine.home, ".claude/settings.json", "{}"))
    assert held_back_lines() == []


def test_a_refusal_for_antigravity_is_a_decision_and_a_reason(machine, payloads, stub, run_hook) -> None:
    payload = payloads.antigravity(
        "write_to_file",
        {"TargetFile": str(machine.home / ".claude" / "settings.json"), "CodeContent": "{}"},
    )
    _, out, _ = run_hook(payload, ["--agent", "antigravity"])
    assert json.loads(out) == {"decision": "deny", "reason": json.loads(out)["reason"]}
    assert stub.requests == []


# --- what stays private ----------------------------------------------------------

UNJUDGED_FILES = (
    ".claude/projects/p/memory/MEMORY.md",
    ".claude/todos/session.jsonl",
    ".codex/sessions/019.jsonl",
    ".gemini/history/session.jsonl",
    ".local/share/muse/sessions/01a0e7.jsonl",
    ".local/share/muse/settings.json",
)


@pytest.mark.parametrize("relative", UNJUDGED_FILES)
def test_memory_sessions_and_transcripts_stay_held_back_silently(
    relative, machine, stub, run_hook, held_back_lines
) -> None:
    """Muse's settings format is unknown, so its files stay held back rather than refused by guessed names."""
    code, out, err = run_hook(_write(machine.home, relative, "x"))
    assert (code, out, err) == (0, "", "")
    assert stub.requests == []
    assert len(held_back_lines()) == 1
    assert held_back_lines()[0].endswith(" agent-config")


def test_reading_the_settings_is_still_private(payloads, stub, run_hook, held_back_lines) -> None:
    code, out, err = run_hook(payloads.command("claude-code", "cat ~/.claude/settings.json"))
    assert (code, out, err) == (0, "", "")
    assert stub.requests == []
    assert held_back_lines()[0].endswith(" agent-config")


# --- disarming by command ----------------------------------------------------------

DELETIONS = (
    "rm ~/.threefold/bin/threefold_hook.py",
    "rm -rf ~/.threefold",
    "rm -rf ~/.claude",
    "rm $HOME/.codex/config.toml",
    "del %USERPROFILE%\\.claude\\settings.json",
)

PLUGIN_REMOVALS = (
    "muse plugins remove threefold",
    "Muse plugin uninstall threefold",
    "claude plugins disable threefold",
    "codex plugins remove threefold --force",
    "sudo muse plugins remove threefold",
    "muse plugins remove --all",
)


@pytest.mark.parametrize("command", DELETIONS)
@pytest.mark.parametrize("agent", ("claude-code", "codex", "antigravity", "muse"))
def test_a_command_that_deletes_what_runs_the_hooks_is_refused(agent, command, payloads, stub, run_hook, verdict) -> None:
    _denied(stub, run_hook, verdict, payloads.command(agent, command), ["--agent", agent])


@pytest.mark.parametrize("command", PLUGIN_REMOVALS)
@pytest.mark.parametrize("agent", ("claude-code", "codex", "antigravity", "muse"))
def test_a_command_that_unplugs_threefold_is_refused(agent, command, payloads, stub, run_hook, verdict) -> None:
    _denied(stub, run_hook, verdict, payloads.command(agent, command), ["--agent", agent])


def test_a_redirected_write_into_the_settings_is_refused(payloads, stub, run_hook, verdict) -> None:
    _denied(stub, run_hook, verdict, payloads.command("codex", "echo {} > ~/.claude/settings.json"), ["--agent", "codex"])


def test_a_redirected_read_into_the_settings_is_refused(payloads, stub, run_hook, verdict) -> None:
    """The bytes come from disk, not from the command's text, but the settings are still overwritten."""
    _denied(stub, run_hook, verdict, payloads.command("claude-code", "cat /tmp/evil.json > ~/.claude/settings.json"))


def test_a_pipe_through_tee_into_the_settings_is_refused(payloads, stub, run_hook, verdict) -> None:
    _denied(stub, run_hook, verdict, payloads.command("claude-code", "echo {} | tee ~/.codex/config.toml"))


def test_a_move_over_the_settings_is_refused(payloads, stub, run_hook, verdict) -> None:
    _denied(stub, run_hook, verdict, payloads.command("claude-code", "mv /tmp/evil.json ~/.claude/settings.json"))


def test_a_move_of_the_settings_away_is_refused(payloads, stub, run_hook, verdict) -> None:
    """Moving the settings away removes them, which unconfigures the hooks as surely as deleting them."""
    _denied(stub, run_hook, verdict, payloads.command("claude-code", "mv ~/.claude/settings.json /tmp/settings.bak"))


def test_a_link_over_the_settings_is_refused(payloads, stub, run_hook, verdict) -> None:
    _denied(stub, run_hook, verdict, payloads.command("claude-code", "ln -sf /tmp/evil.json ~/.claude/settings.json"))


NESTED_REMOVALS = (
    "bash -c 'muse plugins remove threefold'",
    'bash -c "rm -rf ~/.threefold"',
    "echo $(muse plugins remove threefold)",
    "X=$(muse plugins remove threefold)",
    "echo `muse plugins remove threefold`",
)


@pytest.mark.parametrize("command", NESTED_REMOVALS)
def test_a_removal_one_level_down_is_refused(command, payloads, stub, run_hook, verdict) -> None:
    _denied(stub, run_hook, verdict, payloads.command("claude-code", command))


def test_a_dd_over_the_hook_binary_is_refused(payloads, stub, run_hook, verdict) -> None:
    _denied(
        stub,
        run_hook,
        verdict,
        payloads.command("claude-code", "dd if=/tmp/evil.py of=~/.threefold/bin/threefold_hook.py"),
    )


def test_a_copy_over_the_hook_binary_is_refused(payloads, stub, run_hook, verdict) -> None:
    _denied(
        stub,
        run_hook,
        verdict,
        payloads.command("claude-code", "cp /tmp/evil.py ~/.threefold/bin/threefold_hook.py"),
    )


def test_copying_the_settings_elsewhere_stays_held_back(payloads, stub, run_hook, held_back_lines) -> None:
    """Reading the settings out is a privacy hold-back, not an overwrite: the refusal must name what the command writes."""
    code, out, err = run_hook(payloads.command("claude-code", "cp ~/.claude/settings.json /tmp/settings.bak"))
    assert (code, out, err) == (0, "", "")
    assert stub.requests == []
    assert held_back_lines()[0].endswith(" agent-config")


def test_deleting_an_unjudged_memory_file_stays_held_back(payloads, stub, run_hook, held_back_lines) -> None:
    """Only what runs the hooks is refused; the rest of the agents' directories keeps its privacy."""
    code, out, err = run_hook(payloads.command("claude-code", "rm ~/.claude/projects/x.jsonl"))
    assert (code, out, err) == (0, "", "")
    assert stub.requests == []
    assert held_back_lines()[0].endswith(" agent-config")


def test_naming_a_removal_without_running_it_is_sent(payloads, stub, run_hook, verdict) -> None:
    """The refusal reads the command, not the words: echoing the removal judges as the echo it is."""
    stub.answer(200, {"status": "APPROVED"})
    code, out, _ = run_hook(payloads.command("claude-code", "echo muse plugins remove threefold"))
    assert (code, out) == (0, "")
    assert len(stub.requests) == 1


def test_listing_plugins_is_sent(payloads, stub, run_hook) -> None:
    stub.answer(200, {"status": "APPROVED"})
    code, out, _ = run_hook(payloads.command("claude-code", "muse plugins list"))
    assert (code, out) == (0, "")
    assert len(stub.requests) == 1
