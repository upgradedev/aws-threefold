"""A workspace root governs only the repositories its include list names.

One `.threefold.json` at the root of a workspace that holds several
repositories, with `include` naming the ones the owner chose. Every call into
a repository left out, at the workspace root itself, or run from either, stays
on the machine as `not-included`; so does a command run from an included
repository whose text names a path outside the list, because the text is what
would be sent. Every test reads what reached the stub service as well as what
the hook printed, since a hold-back that still sent the call looks the same on
stdout.

Most tests use plain sub-directories; the last section makes each repository
its own checkout, as it is on a real machine. A `.git` normally ends the
hook's walk up to a configuration file, so a nested repository is not governed
by its parent's by accident. An include list is not an accident: a checkout
with no file of its own below one is configured from the workspace root, so
the included ones are governed there and the left-out ones stay at home even
when the environment names a project.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import itertools
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from threefold.domain import path_match

AGENTS = ("claude-code", "codex", "antigravity")
INCLUDE = ["repos/acme-alpha/**", "repos/acme-beta/**"]
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z not-included$")
CREDENTIAL = "AKIA" + "ACMEEXAMPLE00000"


@pytest.fixture
def workspace(machine, monkeypatch) -> Path:
    """W/.threefold.json including acme-alpha and acme-beta, and acme-gamma left out."""
    monkeypatch.delenv("THREEFOLD_PROJECT", raising=False)
    root = machine.project
    for relative in ("repos/acme-alpha/src", "repos/acme-beta/src", "repos/acme-gamma/src"):
        (root / relative).mkdir(parents=True)
    configure(root, include=INCLUDE)
    return root


def configure(root: Path, **fields: Any) -> None:
    document: Dict[str, Any] = {"project": "Acme-Workspace", "mode": "observe"}
    document.update(fields)
    (root / ".threefold.json").write_text(json.dumps(document), encoding="utf-8")


def command_in(payloads, agent: str, command: str, directory: Path) -> Dict[str, Any]:
    """A command run from `directory`, said the way each agent says where it runs."""
    if agent == "claude-code":
        payload = payloads.command(agent, command)
        payload["cwd"] = str(directory)
        return payload
    if agent == "codex":
        payload = payloads.command(agent, command)
        payload["tool_input"]["workdir"] = os.path.relpath(directory, payloads.project)
        return payload
    return payloads.antigravity("run_command", {"CommandLine": command, "Cwd": str(directory)})


def sent(stub) -> Dict[str, Any]:
    assert len(stub.requests) == 1, stub.requests
    return stub.requests[0]["body"]


def assert_not_included(run_hook, payload, stub, held_back_lines, agent: str = "claude-code") -> None:
    code, out, _ = run_hook(payload, ["--agent", agent])
    assert (code, out) == (0, ""), "a held-back call prints nothing"
    assert stub.requests == [], "a held-back call must not reach the service"
    lines: List[str] = held_back_lines()
    assert len(lines) == 1 and LOG_LINE.match(lines[0]), lines
    assert "acme" not in lines[0], "the log records a category, never the path"


# --- the example: two repositories in, one out, and the root out -------------------

@pytest.mark.parametrize("agent", AGENTS)
def test_a_write_into_an_included_repository_is_sent(agent, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    code, out, err = run_hook(payloads.write(agent, "repos/acme-alpha/src/x.py"), ["--agent", agent])
    assert (code, out, err) == (0, "", "")
    assert sent(stub)["arguments"]["file_path"] == "repos/acme-alpha/src/x.py"
    assert held_back_lines() == []


@pytest.mark.parametrize("agent", AGENTS)
def test_a_write_into_a_repository_that_is_not_included_is_held_back(agent, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    assert_not_included(run_hook, payloads.write(agent, "repos/acme-gamma/src/y.py"), stub, held_back_lines, agent)


@pytest.mark.parametrize("agent", AGENTS)
def test_a_write_to_a_file_at_the_workspace_root_is_held_back(agent, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    assert_not_included(run_hook, payloads.write(agent, "STATE.md"), stub, held_back_lines, agent)


@pytest.mark.parametrize("agent", AGENTS)
def test_a_command_run_from_inside_a_repository_that_is_not_included_is_held_back(agent, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    payload = command_in(payloads, agent, "npm test", workspace / "repos" / "acme-gamma")
    assert_not_included(run_hook, payload, stub, held_back_lines, agent)


@pytest.mark.parametrize("agent", AGENTS)
def test_a_command_run_from_the_workspace_root_is_held_back(agent, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    assert_not_included(run_hook, command_in(payloads, agent, "npm test", workspace), stub, held_back_lines, agent)


def test_a_command_from_the_root_that_copies_a_left_out_file_into_an_included_one_is_held_back(workspace, payloads, stub, run_hook, held_back_lines) -> None:
    payload = command_in(payloads, "claude-code", "cat repos/acme-gamma/a.py > repos/acme-alpha/b.py", workspace)
    assert_not_included(run_hook, payload, stub, held_back_lines)


@pytest.mark.parametrize(
    "command",
    [
        "cat ../acme-gamma/a.py > src/b.py",
        "cp ../../STATE.md notes.md",
        "diff src/x.py {gamma}/src/x.py",
        "ls ..",
    ],
)
@pytest.mark.parametrize("agent", AGENTS)
def test_a_command_run_inside_an_included_repository_that_names_a_path_outside_the_list_is_held_back(
    agent, command, workspace, payloads, stub, run_hook, held_back_lines
) -> None:
    """Where it runs is included; what its text names is not, and the text is what would be sent."""
    command = command.format(gamma=(workspace / "repos" / "acme-gamma").as_posix())
    payload = command_in(payloads, agent, command, workspace / "repos" / "acme-alpha")
    assert_not_included(run_hook, payload, stub, held_back_lines, agent)


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest -q tests/unit 2>/dev/null",
        "cat src/x.py > ../acme-beta/src/y.py",
        "git log --oneline origin/main",
    ],
)
@pytest.mark.parametrize("agent", AGENTS)
def test_a_command_run_inside_an_included_repository_that_names_only_included_paths_is_sent(
    agent, command, workspace, payloads, stub, run_hook, held_back_lines
) -> None:
    payload = command_in(payloads, agent, command, workspace / "repos" / "acme-alpha")
    code, out, err = run_hook(payload, ["--agent", agent])
    assert (code, out, err) == (0, "", "")
    assert sent(stub)["arguments"]["cwd"] == "repos/acme-alpha"
    assert held_back_lines() == []


def test_a_codex_patch_that_moves_a_file_out_of_a_left_out_repository_is_held_back(workspace, payloads, stub, run_hook, held_back_lines) -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Update File: repos/acme-gamma/src/a.py\n"
        "*** Move to: repos/acme-alpha/src/a.py\n"
        "@@\n"
        "+x = 1\n"
        "*** End Patch\n"
    )
    assert_not_included(run_hook, payloads.codex_patch(patch), stub, held_back_lines, "codex")


def test_a_relative_target_is_read_from_where_the_agent_stands_and_matched_from_the_workspace_root(workspace, payloads, stub, run_hook) -> None:
    payload = payloads.write("claude-code")
    payload["cwd"] = str(workspace / "repos" / "acme-alpha" / "src")
    payload["tool_input"]["file_path"] = "x.py"
    run_hook(payload)
    assert sent(stub)["arguments"]["file_path"] == "repos/acme-alpha/src/x.py"


# --- without a list, nothing changes -------------------------------------------------

@pytest.mark.parametrize("fields", [{}, {"include": []}, {"include": None}], ids=["absent", "empty", "null"])
def test_without_an_include_list_the_whole_root_is_sent_as_before(fields, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    configure(workspace, **fields)
    run_hook(payloads.write("claude-code", "repos/acme-gamma/src/y.py"))
    run_hook(payloads.write("claude-code", "STATE.md"))
    run_hook(command_in(payloads, "claude-code", "npm test", workspace))
    run_hook(command_in(payloads, "claude-code", "cat ../acme-gamma/a.py", workspace / "repos" / "acme-alpha"))
    assert len(stub.requests) == 4
    assert held_back_lines() == []


# --- what still comes first -----------------------------------------------------------

@pytest.mark.parametrize("agent", AGENTS)
def test_a_credential_is_refused_even_where_nothing_is_included(agent, workspace, payloads, stub, run_hook, held_back_lines, verdict) -> None:
    """The refusal is local, so it sends nothing either way, and a key about to
    be written to disk is worth stopping wherever the disk is."""
    payload = payloads.write(agent, "repos/acme-gamma/src/settings.py", f"KEY = '{CREDENTIAL}'\n")
    _, out, _ = run_hook(payload, ["--agent", agent])
    assert verdict.decision(out) == "deny"
    assert "credential" in verdict.reason(out)
    assert stub.requests == []
    assert held_back_lines() == [], "refused before the include list was consulted"


def test_a_credential_in_a_command_run_from_a_left_out_repository_is_refused(workspace, payloads, stub, run_hook, held_back_lines, verdict) -> None:
    payload = command_in(payloads, "claude-code", f"echo {CREDENTIAL} > key.txt", workspace / "repos" / "acme-gamma")
    _, out, _ = run_hook(payload)
    assert verdict.decision(out) == "deny"
    assert stub.requests == [] and held_back_lines() == []


def test_in_enforce_mode_a_write_to_the_hooks_own_files_is_refused_before_the_include_list(workspace, payloads, stub, run_hook, held_back_lines, verdict) -> None:
    configure(workspace, include=INCLUDE, mode="enforce")
    _, out, _ = run_hook(payloads.write("claude-code", "repos/acme-gamma/.claude/settings.local.json", "{}"))
    assert verdict.decision(out) == "deny"
    assert "decides whether the agent's hooks run" in verdict.reason(out)
    assert stub.requests == [] and held_back_lines() == []


# --- a list that cannot be read keeps calls at home -------------------------------------

def test_an_include_that_is_not_a_list_sends_nothing_and_says_so(workspace, payloads, stub, run_hook, held_back_lines) -> None:
    """Written as one string, the owner's list of what to send must not turn
    into sending every repository they left out."""
    configure(workspace, include="repos/acme-alpha/**")
    _, out, err = run_hook(payloads.write("claude-code", "repos/acme-alpha/src/x.py"))
    assert out == "" and stub.requests == []
    assert held_back_lines()[-1].endswith(" not-included")
    assert "must be a list of globs" in err


@pytest.mark.parametrize("bad", ["/repos/acme-alpha/**", "../acme-folder-name/repos/acme-alpha/**", "C:/repos/acme-alpha/**", "", 7])
def test_an_include_glob_that_is_absolute_empty_or_climbs_out_matches_nothing(bad, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    configure(workspace, include=[bad, "repos/acme-beta/**"])
    _, _, err = run_hook(payloads.write("claude-code", "repos/acme-alpha/src/x.py"))
    assert stub.requests == [] and held_back_lines()[-1].endswith(" not-included")
    assert "matches nothing" in err
    run_hook(payloads.write("claude-code", "repos/acme-beta/src/x.py"))
    assert len(stub.requests) == 1, "the globs that can be read still apply"


# --- how a glob is read ---------------------------------------------------------------------

GLOBS = [
    "repos/acme-alpha/**", "repos/*/src/**", "**/domain/**", "repos/acme-?eta/**", "**", "*",
    "repos/acme-alpha", "repos/**/*.py", "./repos/acme-alpha/**", "repos\\acme-beta\\**", "Repos/ACME-Alpha/**",
]
PATHS = [
    "repos/acme-alpha", "repos/acme-alpha/src/x.py", "repos/acme-beta/src/domain/user.py", "repos/acme-gamma/y.py",
    "STATE.md", "repos", "REPOS/Acme-Alpha/src/X.py", "repos/acme-alphabet/x.py", "a/b/c/d/e/f.py",
]


def test_the_hook_reads_an_include_glob_exactly_as_the_rules_read_a_path(hook) -> None:
    """Copied, not imported, because the hook is downloaded alone: this is what keeps the copy honest."""
    for path, glob in itertools.product(PATHS, GLOBS):
        assert hook.glob_matches(path, glob, fold_case=True) == path_match.matches(path, glob), (path, glob)


def test_on_a_case_sensitive_disk_a_directory_spelled_differently_is_not_included(hook) -> None:
    assert hook.glob_matches("repos/acme-alpha/src/x.py", "repos/acme-alpha/**", fold_case=False)
    assert not hook.glob_matches("repos/Acme-Alpha/src/x.py", "repos/acme-alpha/**", fold_case=False)
    assert hook.glob_matches("repos/Acme-Alpha/src/x.py", "repos/acme-alpha/**", fold_case=True)


@pytest.mark.skipif(os.name != "nt", reason="only Windows paths are case-insensitive")
def test_on_windows_a_path_spelled_in_another_case_is_still_included(workspace, payloads, stub, run_hook) -> None:
    run_hook(payloads.write("claude-code", "REPOS/Acme-Alpha/src/x.py"))
    assert len(stub.requests) == 1


# --- repositories that are checkouts of their own ------------------------------------------

@pytest.fixture
def checkouts(workspace) -> Path:
    """Each repository in the workspace is its own checkout, as it is on a real machine."""
    for name in ("acme-alpha", "acme-beta", "acme-gamma"):
        (workspace / "repos" / name / ".git").mkdir()
    return workspace


def test_an_included_repository_that_is_its_own_checkout_is_governed_from_the_workspace_root(checkouts, payloads, stub, run_hook) -> None:
    """Its .git used to end the walk before the workspace file, so the agent
    standing in it was not governed at all, or was governed with no list."""
    payload = payloads.write("claude-code", "repos/acme-alpha/src/x.py")
    payload["cwd"] = str(checkouts / "repos" / "acme-alpha")
    run_hook(payload)
    body = sent(stub)
    assert (body["project_name"], body["arguments"]["file_path"]) == ("Acme-Workspace", "repos/acme-alpha/src/x.py")


def test_antigravity_opened_on_an_included_checkout_is_governed_from_the_workspace_root(checkouts, payloads, stub, run_hook) -> None:
    payload = payloads.antigravity(
        "write_to_file", {"TargetFile": str(checkouts / "repos" / "acme-beta" / "src" / "x.py"), "CodeContent": "x = 1\n"}
    )
    payload["workspacePaths"] = [str(checkouts / "repos" / "acme-beta")]
    run_hook(payload, ["--agent", "antigravity"])
    assert sent(stub)["arguments"]["file_path"] == "repos/acme-beta/src/x.py"


@pytest.mark.parametrize("agent", ["claude-code", "antigravity"])
def test_a_left_out_checkout_is_held_back_even_with_a_project_in_the_environment(agent, checkouts, payloads, stub, run_hook, held_back_lines, monkeypatch) -> None:
    """The case the list exists for. Stopping at the checkout's own .git, the
    hook found no list there, took the project from the environment, and sent
    everything in the repository the owner left out."""
    monkeypatch.setenv("THREEFOLD_PROJECT", "Acme-Env")
    gamma = checkouts / "repos" / "acme-gamma"
    if agent == "claude-code":
        payload = command_in(payloads, agent, "npm test", gamma)
    else:
        payload = payloads.antigravity("write_to_file", {"TargetFile": str(gamma / "src" / "y.py"), "CodeContent": "y = 2\n"})
        payload["workspacePaths"] = [str(gamma)]
    assert_not_included(run_hook, payload, stub, held_back_lines, agent)


def test_a_checkout_under_a_file_without_an_include_list_is_still_its_own(checkouts, hook) -> None:
    configure(checkouts)
    gamma = checkouts / "repos" / "acme-gamma"
    assert hook._canonical(hook.config_root({"cwd": str(gamma)})) == hook._canonical(str(gamma))


def test_a_checkout_with_a_file_of_its_own_keeps_it(checkouts, hook) -> None:
    gamma = checkouts / "repos" / "acme-gamma"
    (gamma / ".threefold.json").write_text(json.dumps({"project": "Acme-Gamma"}), encoding="utf-8")
    assert hook._canonical(hook.config_root({"cwd": str(gamma / "src")})) == hook._canonical(str(gamma))


def test_the_walk_to_a_workspace_stops_at_the_next_checkout(checkouts, hook) -> None:
    """A checkout vendored inside a repository is that repository's business,
    and a .git on the way up is what keeps every walk inside a test's directory."""
    vendored = checkouts / "repos" / "acme-gamma" / "vendor" / "acme-lib"
    (vendored / ".git").mkdir(parents=True)
    assert hook._canonical(hook.config_root({"cwd": str(vendored)})) == hook._canonical(str(vendored))
