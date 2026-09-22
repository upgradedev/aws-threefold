"""A workspace root governs only the repositories its include list names.

One `.threefold.json` at the root of a workspace that holds several
repositories, with `include` naming the ones the owner chose. Every call into
a repository left out, at the workspace root itself, or run from either, stays
on the machine as `not-included`; so does a command run from an included
repository whose text names a path outside the list, because the text is what
would be sent. Every test reads what reached the stub service as well as what
the hook printed, since a hold-back that still sent the call looks the same on
stdout.

Most tests use plain sub-directories; the later sections make each repository
its own checkout, as it is on a real machine. A `.git` normally ends the
hook's walk up to a configuration file, so a nested repository is not governed
by its parent's by accident. An include list is not an accident: a checkout
with no file of its own below one is configured from the workspace root,
however many checkouts lie between, so the included ones are governed there
and the left-out ones, with their submodules and vendored clones, stay at home
even when the environment names a project.

A command's text is read the way a shell reads it before its paths are
judged: quotes and escapes taken out, the working directory spelled `$PWD` or
`%CD%` read as where it runs, a path written against an option letter, an `@`
or inside a `:` list read on its own, and a cd followed to where it goes. And
a workspace file that cannot be read, but names an include list, sends nothing
rather than everything.

Names are synthetic, as the clean-room rule requires.
"""
from __future__ import annotations

import itertools
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
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


@pytest.mark.parametrize(
    "bad", ["/repos/acme-alpha/**", "../acme-folder-name/repos/acme-alpha/**", "C:/repos/acme-alpha/**", "", 7, ".", "./."]
)
def test_an_include_glob_that_is_absolute_empty_or_climbs_out_matches_nothing(bad, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    configure(workspace, include=[bad, "repos/acme-beta/**"])
    _, _, err = run_hook(payloads.write("claude-code", "repos/acme-alpha/src/x.py"))
    assert stub.requests == [] and held_back_lines()[-1].endswith(" not-included")
    assert "matches nothing" in err
    run_hook(payloads.write("claude-code", "repos/acme-beta/src/x.py"))
    assert len(stub.requests) == 1, "the globs that can be read still apply"


def test_a_dot_segment_inside_an_include_glob_is_read_as_the_glob_without_it(workspace, payloads, stub, run_hook) -> None:
    """No relative path has a `.` segment, so kept, it made the glob match nothing without a word."""
    configure(workspace, include=["repos/./acme-alpha/**"])
    run_hook(payloads.write("claude-code", "repos/acme-alpha/src/x.py"))
    assert sent(stub)["arguments"]["file_path"] == "repos/acme-alpha/src/x.py"


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


# --- the way Git Bash writes a drive --------------------------------------------------------
#
# Claude Code runs Bash through Git Bash on Windows, so /c/... is the spelling
# the owner's own workspace sees. Read as written it resolved to the current
# drive's \c\, which is inside no glob, so the one spelling that actually
# arrives was held back as not-included and ran unjudged, while the relative
# and C:/ spellings of the same file were judged and refused.

def bash_spelling(path, prefix: str = "") -> str:
    drive, rest = os.path.splitdrive(str(path))
    return prefix + "/" + drive[0].lower() + rest.replace("\\", "/")


@pytest.mark.skipif(os.name != "nt", reason="/c/repos names a real directory on a POSIX machine")
@pytest.mark.parametrize("prefix", ["", "/cygdrive", "/mnt"])
def test_a_command_naming_an_included_file_the_git_bash_way_is_sent(prefix, workspace, payloads, stub, run_hook) -> None:
    where = bash_spelling(workspace / "repos" / "acme-alpha" / "src" / "order.py", prefix)
    run_hook(command_in(payloads, "claude-code", f"cat {where}", workspace / "repos" / "acme-alpha"))
    assert sent(stub)["arguments"]["command"] == "cat ../../repos/acme-alpha/src/order.py"


@pytest.mark.skipif(os.name != "nt", reason="/c/repos names a real directory on a POSIX machine")
@pytest.mark.parametrize("prefix", ["", "/cygdrive", "/mnt"])
def test_a_command_naming_a_left_out_file_the_git_bash_way_is_still_held_back(
    prefix, workspace, payloads, stub, run_hook, held_back_lines
) -> None:
    where = bash_spelling(workspace / "repos" / "acme-gamma" / "y.py", prefix)
    payload = command_in(payloads, "claude-code", f"cat {where} > b.py", workspace / "repos" / "acme-alpha")
    assert_not_included(run_hook, payload, stub, held_back_lines)


@pytest.mark.skipif(os.name != "nt", reason="/c/repos names a real directory on a POSIX machine")
def test_a_cd_written_the_git_bash_way_moves_where_the_list_is_read_from(
    workspace, payloads, stub, run_hook, held_back_lines
) -> None:
    where = bash_spelling(workspace / "repos" / "acme-gamma")
    payload = command_in(payloads, "claude-code", f"cd {where} && cat y.py", workspace / "repos" / "acme-alpha")
    assert_not_included(run_hook, payload, stub, held_back_lines)


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


# --- checkouts inside checkouts ----------------------------------------------------------------

@pytest.fixture
def nested(checkouts) -> SimpleNamespace:
    """A submodule and a vendored clone inside the left-out acme-gamma, and a vendored clone inside acme-alpha."""
    gamma = checkouts / "repos" / "acme-gamma"
    submodule = gamma / "libs" / "acme-shared"
    submodule.mkdir(parents=True)
    # How git leaves a submodule's checkout: a .git file pointing into the parent's .git.
    (submodule / ".git").write_text("gitdir: ../../.git/modules/acme-shared\n", encoding="utf-8")
    vendored = gamma / "vendor" / "acme-lib"
    (vendored / ".git").mkdir(parents=True)
    in_alpha = checkouts / "repos" / "acme-alpha" / "vendor" / "acme-lib"
    (in_alpha / ".git").mkdir(parents=True)
    return SimpleNamespace(root=checkouts, submodule=submodule, vendored=vendored, in_alpha=in_alpha)


def call_in(payloads, call: str, checkout: Path):
    """(payload, agent) for one call made by an agent standing in `checkout`."""
    if call == "claude-code write":
        payload = payloads.write("claude-code")
        payload["cwd"] = str(checkout)
        payload["tool_input"]["file_path"] = str(checkout / "y.py")
        return payload, "claude-code"
    if call == "bash command":
        return command_in(payloads, "claude-code", "npm test", checkout), "claude-code"
    payload = payloads.antigravity("write_to_file", {"TargetFile": str(checkout / "z.py"), "CodeContent": "z = 3\n"})
    payload["workspacePaths"] = [str(checkout)]
    return payload, "antigravity"


CALLS = ["claude-code write", "bash command", "antigravity workspace"]


def test_the_walk_to_a_workspace_passes_every_checkout_on_the_way(nested, hook) -> None:
    """It stopped at the first .git above a checkout, so a submodule or a clone
    inside a left-out repository met that repository's .git, never read the
    workspace's list, and was governed as a project of its own."""
    for checkout in (nested.submodule, nested.vendored, nested.in_alpha):
        assert hook._canonical(hook.config_root({"cwd": str(checkout)})) == hook._canonical(str(nested.root)), checkout


@pytest.mark.parametrize("call", CALLS)
@pytest.mark.parametrize("place", ["submodule", "vendored"])
def test_a_checkout_inside_a_left_out_repository_is_held_back_even_with_a_project_in_the_environment(
    place, call, nested, payloads, stub, run_hook, held_back_lines, monkeypatch
) -> None:
    monkeypatch.setenv("THREEFOLD_PROJECT", "Acme-Env")
    payload, agent = call_in(payloads, call, getattr(nested, place))
    assert_not_included(run_hook, payload, stub, held_back_lines, agent)


def test_a_checkout_vendored_inside_an_included_repository_is_sent_under_the_workspace_project(nested, payloads, stub, run_hook) -> None:
    payload, _ = call_in(payloads, "claude-code write", nested.in_alpha)
    run_hook(payload)
    body = sent(stub)
    assert (body["project_name"], body["arguments"]["file_path"]) == ("Acme-Workspace", "repos/acme-alpha/vendor/acme-lib/y.py")


def test_the_first_file_on_the_way_up_decides_and_one_without_a_list_keeps_the_checkout_its_own(nested, hook) -> None:
    (nested.root / "repos" / "acme-gamma" / ".threefold.json").write_text(json.dumps({"project": "Acme-Gamma"}), encoding="utf-8")
    assert hook._canonical(hook.config_root({"cwd": str(nested.vendored)})) == hook._canonical(str(nested.vendored))


def test_no_walk_looks_above_the_ceiling_the_tests_set(nested, hook, machine, monkeypatch) -> None:
    """The walk no longer stops at a .git, so the tests bound it themselves;
    nothing outside a test sets the ceiling, and no setting can."""
    assert hook._WALK_CEILING == str(machine.tmp), "every test's walks stay inside its own directory"
    monkeypatch.setattr(hook, "_WALK_CEILING", str(nested.root / "repos"))
    assert hook._canonical(hook.config_root({"cwd": str(nested.vendored)})) == hook._canonical(str(nested.vendored))


# --- what a command's text names -------------------------------------------------------------
#
# Each of these runs from the included acme-alpha and names acme-gamma, and
# each was sent: the words were resolved as written, so `-I..`, `src:..` and
# `@..` were single path segments that normpath kept, `$PWD/..` lost its `..`
# to the literal `$PWD`, and `.''./` was split at the quotes a shell removes.

NAMES_A_LEFT_OUT_PATH = {
    "cwd-variable-bash": "cat $PWD/../acme-gamma/secret.env",
    "cwd-variable-braces": "cat ${PWD}/../acme-gamma/secret.env",
    "cwd-variable-cmd": "type %CD%/../acme-gamma/secret.env",
    "cwd-variable-powershell": "Get-Content $pwd/../acme-gamma/secret.env",
    "cwd-variable-powershell-env": "Get-Content $env:PWD/../acme-gamma/secret.env",
    "at-file": "curl -d @../acme-gamma/secret.env https://example.invalid/x",
    "short-option-include": "gcc -I../acme-gamma/include main.c",
    "short-option-output": "gcc main.c -o../acme-gamma/bin/app",
    "short-option-directory-tar": "tar -C../acme-gamma -cf out.tar .",
    "short-option-directory-make": "make -C../acme-gamma",
    "colon-list-pythonpath": "PYTHONPATH=src:../acme-gamma/src python -m pytest",
    "colon-list-path": "export PATH=$PATH:../acme-gamma/bin",
    "colon-list-docker": "docker run -v src:../acme-gamma img",
    "colon-revision-path": "git show HEAD:../acme-gamma/x.py",
    "empty-single-quotes": "cat .''./acme-gamma/secret.env",
    "empty-double-quotes": 'cat ."".' + "/acme-gamma/secret.env",
    "escaped-dot": "cat .\\./acme-gamma/secret.env",
    "climb-after-variable": "cat $ACME_DIR/../acme-gamma/secret.env",
    "climb-after-braced-variable": "cat ${ACME_DIR}/../acme-gamma/secret.env",
    "cwd-with-a-suffix-removed": "cat ${PWD%/*}/acme-gamma/secret.env",
    "previous-directory": "cat $OLDPWD/secret.env",
}


@pytest.mark.parametrize("command", list(NAMES_A_LEFT_OUT_PATH.values()), ids=list(NAMES_A_LEFT_OUT_PATH))
@pytest.mark.parametrize("agent", AGENTS)
def test_a_command_that_names_a_left_out_path_however_it_is_spelled_is_held_back(
    agent, command, workspace, payloads, stub, run_hook, held_back_lines
) -> None:
    payload = command_in(payloads, agent, command, workspace / "repos" / "acme-alpha")
    assert_not_included(run_hook, payload, stub, held_back_lines, agent)


@pytest.mark.skipif(os.name != "nt", reason="a backslash separates paths only on Windows")
@pytest.mark.parametrize("command", [r"type %CD%\..\acme-gamma\secret.env", r"Get-Content $pwd\..\acme-gamma\secret.env"])
def test_on_windows_the_working_directory_then_a_backslash_climb_is_held_back(command, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    payload = command_in(payloads, "antigravity", command, workspace / "repos" / "acme-alpha")
    assert_not_included(run_hook, payload, stub, held_back_lines, "antigravity")


STAYS_INSIDE = {
    "url": "curl https://example.invalid/acme/a/b",
    "revision-colon-ref": "git push origin HEAD:refs/heads/main",
    "colon-list-inside": "PYTHONPATH=src:tests python -m pytest -q",
    "short-options-inside": "gcc -Iinclude -obuild/app main.c",
    "revision-range": "git log --oneline HEAD~3..HEAD",
    "cd-then-climb-back": "cd src && python -m pytest -q ../tests",
    "cd-and-back": "cd src && make && cd ..",
    "pushd-popd": "pushd src && make && popd",
    "cd-to-an-included-sibling": "cd ../acme-beta && npm test",
    "cd-in-a-commit-message": "git commit -m 'cd into the right folder first'",
    "cd-in-a-subshell": "(cd src && make) && ls",
}


@pytest.mark.parametrize("command", list(STAYS_INSIDE.values()), ids=list(STAYS_INSIDE))
def test_a_command_that_stays_inside_the_list_is_still_sent(command, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    """Reading the text more closely must not keep ordinary work at home."""
    run_hook(command_in(payloads, "claude-code", command, workspace / "repos" / "acme-alpha"))
    assert sent(stub)["arguments"]["command"] == command
    assert held_back_lines() == []


# --- a command that moves before it names a path ------------------------------------------------

MOVES_SOMEWHERE_UNKNOWN = {
    "cd-to-a-computed-parent": "cd $(dirname $PWD) && cat acme-gamma/secret.env",
    "cd-to-the-previous-directory": "cd $OLDPWD && cat secret.env",
    "bare-cd-goes-home": "cd && cat work/acme-folder-name/repos/acme-gamma/secret.env",
    "cd-dash": "cd - && cat secret.env",
    "cd-tilde-dash": "cd ~-/.. && ls acme-gamma",
    "cd-to-a-variable": 'cd "$ACME_DIR" && cat secret.env',
    "popd-without-pushd": "popd && cat secret.env",
    "pushd-rotating-the-stack": "pushd +1 && cat secret.env",
    "set-location-to-a-variable": "Set-Location $env:ACME_DIR; Get-Content secret.env",
    "cmd-cd-dot-dot": "cd.. && cat acme-gamma/secret.env",
    "cd-on-a-later-line": "echo start\ncd $ACME_DIR\ncat secret.env",
}


@pytest.mark.parametrize("command", list(MOVES_SOMEWHERE_UNKNOWN.values()), ids=list(MOVES_SOMEWHERE_UNKNOWN))
def test_a_command_that_moves_where_the_list_cannot_follow_is_held_back(command, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    """Every relative path after such a move is read from a place nobody can
    check against the list, and the words alone looked included."""
    payload = command_in(payloads, "claude-code", command, workspace / "repos" / "acme-alpha")
    assert_not_included(run_hook, payload, stub, held_back_lines)


def test_after_a_cd_to_a_literal_path_later_paths_are_read_from_there(workspace, payloads, stub, run_hook, held_back_lines) -> None:
    """From acme-alpha/src, `../acme-gamma` is inside acme-alpha; after the cd it is the left-out repository."""
    payload = command_in(payloads, "claude-code", "cd ../../acme-beta && cat ../acme-gamma/secret.env", workspace / "repos" / "acme-alpha" / "src")
    assert_not_included(run_hook, payload, stub, held_back_lines)


def test_a_cd_inside_a_subshell_is_undone_when_it_closes(workspace, payloads, stub, run_hook) -> None:
    src = workspace / "repos" / "acme-alpha" / "src"
    run_hook(command_in(payloads, "claude-code", "(cd ../../acme-beta && npm test); cat ../README.md", src))
    assert len(stub.requests) == 1, "back in acme-alpha/src, ../README.md is acme-alpha's"


def test_after_a_cd_that_may_have_failed_paths_are_read_from_both_places(workspace, payloads, stub, run_hook, held_back_lines) -> None:
    """Without `&&` the next command runs whether the cd worked or not, and from acme-beta ../README.md is the workspace's."""
    payload = command_in(payloads, "claude-code", "cd ../../acme-beta; cat ../README.md", workspace / "repos" / "acme-alpha" / "src")
    assert_not_included(run_hook, payload, stub, held_back_lines)


# --- a workspace file that cannot be read -------------------------------------------------------

GOOD = {"project": "Acme-Workspace", "mode": "observe", "include": INCLUDE}


def damage(root: Path, how: str) -> None:
    """Writes the workspace's file in a way the hook used to read as no file at all, losing its list."""
    path = root / ".threefold.json"
    text = json.dumps(GOOD)
    if how == "trailing-comma":
        path.write_text(text[:-1] + ",}", encoding="utf-8")
    elif how == "larger-than-64-kb":
        path.write_text(text[:-1] + ', "notes": "' + " " * 70_000 + '"}', encoding="utf-8")
    elif how == "not-utf-8":
        path.write_bytes(text.replace("Acme-Workspace", "Acme-Wörkspace").encode("latin-1"))
    elif how == "not-an-object":
        path.write_text(json.dumps([GOOD]), encoding="utf-8")
    else:
        path.write_text(text, encoding=how)


@pytest.mark.parametrize("how", ["trailing-comma", "larger-than-64-kb", "not-utf-8", "not-an-object"])
def test_a_workspace_file_that_names_an_include_list_but_cannot_be_read_sends_nothing(
    how, workspace, payloads, stub, run_hook, held_back_lines, monkeypatch
) -> None:
    """Read as no file, the list was gone and the project in the environment
    sent every repository the owner had left out."""
    monkeypatch.setenv("THREEFOLD_PROJECT", "Acme-Env")
    damage(workspace, how)
    _, out, err = run_hook(payloads.write("claude-code", "repos/acme-gamma/src/y.py"))
    run_hook(command_in(payloads, "claude-code", "npm test", workspace))
    run_hook(payloads.write("claude-code", "repos/acme-alpha/src/x.py"))
    assert out == "" and stub.requests == [], "nothing is sent until the list can be read, the included ones too"
    assert [line.split()[-1] for line in held_back_lines()] == ["not-included"] * 3
    assert "may hold an include list" in err


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be", "utf-32"])
def test_a_workspace_file_windows_powershell_wrote_in_utf_16_is_read(encoding, workspace, payloads, stub, run_hook, held_back_lines) -> None:
    """`>` and Out-File in Windows PowerShell 5.1 write UTF-16 with a BOM."""
    if encoding == "utf-16-be":
        (workspace / ".threefold.json").write_bytes(b"\xfe\xff" + json.dumps(GOOD).encode("utf-16-be"))
    else:
        damage(workspace, encoding)
    run_hook(payloads.write("claude-code", "repos/acme-gamma/src/y.py"))
    assert stub.requests == [] and held_back_lines()[-1].endswith(" not-included")
    run_hook(payloads.write("claude-code", "repos/acme-alpha/src/x.py"))
    assert sent(stub)["project_name"] == "Acme-Workspace", "the project came from the file, so it was decoded"


def test_a_file_that_cannot_be_read_and_names_no_include_list_is_ignored_as_before(workspace, payloads, stub, run_hook, monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_PROJECT", "Acme-Env")
    (workspace / ".threefold.json").write_text('{"project": "Acme-Workspace", "mode": "observe",}', encoding="utf-8")
    run_hook(payloads.write("claude-code", "repos/acme-gamma/src/y.py"))
    assert sent(stub)["project_name"] == "Acme-Env"


@pytest.mark.parametrize("checkout", ["acme-alpha", "acme-gamma", "acme-gamma submodule"])
@pytest.mark.parametrize("state", ["readable", "trailing-comma", "utf-16"])
def test_every_checkout_under_a_workspace_file_follows_what_the_file_can_say(
    state, checkout, nested, payloads, stub, run_hook, held_back_lines, monkeypatch
) -> None:
    """The walk past every checkout and the unreadable file meet here: a file
    the walk reaches but cannot read must hold back even the checkout it found
    from below, or a submodule under a damaged workspace file sends everything."""
    monkeypatch.setenv("THREEFOLD_PROJECT", "Acme-Env")
    if state != "readable":
        damage(nested.root, state)
    directory = nested.submodule if checkout.endswith("submodule") else nested.root / "repos" / checkout
    payload, _ = call_in(payloads, "claude-code write", directory)
    run_hook(payload)
    if checkout == "acme-alpha" and state != "trailing-comma":
        assert sent(stub)["arguments"]["file_path"] == "repos/acme-alpha/y.py"
    else:
        assert stub.requests == [] and held_back_lines()[-1].endswith(" not-included")


def test_a_credential_is_still_refused_first_under_a_workspace_file_that_cannot_be_read(
    workspace, payloads, stub, run_hook, held_back_lines, verdict, monkeypatch
) -> None:
    monkeypatch.setenv("THREEFOLD_PROJECT", "Acme-Env")
    damage(workspace, "trailing-comma")
    _, out, _ = run_hook(payloads.write("claude-code", "repos/acme-gamma/src/settings.py", f"KEY = '{CREDENTIAL}'\n"))
    assert verdict.decision(out) == "deny" and "credential" in verdict.reason(out)
    assert stub.requests == [] and held_back_lines() == []
