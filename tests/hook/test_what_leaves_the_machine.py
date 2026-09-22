"""What the hook keeps on the developer's machine, decided before any request.

The service is public and its ledger is readable by anyone, so the hook is the
last point where a call can be kept private. Every test here asserts two
things: what the hook printed, and that the stub service received nothing. A
hold-back that still sent the call would pass a test that only looked at stdout.
"""
from __future__ import annotations

import codecs
import json
import os
import re

import pytest

from threefold.domain.boundary_guard import SecretScanner

AGENTS = ("claude-code", "codex", "antigravity")
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z (outside-root|agent-config|data-file|never-send|no-project)$")


def _held_back(stub, run_hook, payload, held_back_lines, category, argv=None) -> None:
    code, out, err = run_hook(payload, argv)
    assert (code, out, err) == (0, "", ""), "A held-back call prints nothing"
    assert stub.requests == [], "A held-back call must not reach the service"
    lines = held_back_lines()
    assert len(lines) == 1
    assert LOG_LINE.match(lines[0]), lines[0]
    assert lines[0].endswith(" " + category)


# --- outside the project -----------------------------------------------------------

@pytest.mark.parametrize("agent", AGENTS)
def test_a_write_outside_the_project_root_is_held_back(agent, machine, payloads, stub, run_hook, held_back_lines) -> None:
    payload = payloads.write(agent, "../acme-other-repo/notes.py")
    _held_back(stub, run_hook, payload, held_back_lines, "outside-root", ["--agent", agent])
    assert "acme-other-repo" not in "\n".join(held_back_lines()), "The log records a category, never the path"


def test_an_absolute_path_elsewhere_is_outside_the_root(machine, stub, run_hook, held_back_lines) -> None:
    payload = {
        "session_id": "s",
        "cwd": str(machine.project),
        "tool_name": "Write",
        "tool_input": {"file_path": str(machine.tmp / "elsewhere" / "a.py"), "content": "x"},
    }
    _held_back(stub, run_hook, payload, held_back_lines, "outside-root")


def test_the_root_falls_back_to_the_first_workspace_for_antigravity(hook, machine) -> None:
    assert hook.project_root({"workspacePaths": [str(machine.project), str(machine.tmp)]}) == str(machine.project)


def test_a_workspace_given_as_a_file_uri_is_read_as_a_path(hook, machine) -> None:
    uri = machine.project.as_uri()
    assert hook._canonical(hook.project_root({"workspacePaths": [uri]})) == hook._canonical(str(machine.project))


def test_without_a_cwd_or_workspace_the_root_is_where_the_hook_runs(hook, machine, monkeypatch) -> None:
    monkeypatch.chdir(machine.project)
    assert hook._canonical(hook.project_root({})) == hook._canonical(str(machine.project))


# --- the agents' own configuration and memory -------------------------------------

@pytest.mark.parametrize("directory", [".claude", ".codex", ".gemini"])
def test_a_write_into_an_agents_own_configuration_is_held_back(directory, machine, stub, run_hook, held_back_lines) -> None:
    """Checked before outside-root, and here the root is the home directory,
    so only the agent-config rule can be what held it back."""
    payload = {
        "session_id": "s",
        "cwd": str(machine.home),
        "tool_name": "Write",
        "tool_input": {"file_path": str(machine.home / directory / "settings.json"), "content": "{}"},
    }
    _held_back(stub, run_hook, payload, held_back_lines, "agent-config")


def test_a_write_into_threefold_home_is_held_back(machine, stub, run_hook, held_back_lines) -> None:
    payload = {
        "session_id": "s",
        "cwd": str(machine.home),
        "tool_name": "Write",
        "tool_input": {"file_path": str(machine.threefold_home / "never_send.txt"), "content": ""},
    }
    _held_back(stub, run_hook, payload, held_back_lines, "agent-config")


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.claude/CLAUDE.md",
        "grep -r token $HOME/.codex",
        'type "%USERPROFILE%\\.gemini\\settings.json"',
        "ls ${HOME}/.gemini/antigravity",
        "cp notes.md ~/.threefold/",
    ],
)
@pytest.mark.parametrize("agent", AGENTS)
def test_a_command_that_reaches_an_agents_configuration_is_held_back(agent, command, payloads, stub, run_hook, held_back_lines) -> None:
    _held_back(stub, run_hook, payloads.command(agent, command), held_back_lines, "agent-config", ["--agent", agent])


def test_a_command_naming_the_configuration_by_its_absolute_path_is_held_back(machine, payloads, stub, run_hook, held_back_lines) -> None:
    command = f"head -n 5 {machine.home / '.claude' / 'projects' / 'x.jsonl'}"
    _held_back(stub, run_hook, payloads.command("claude-code", command), held_back_lines, "agent-config")


def test_a_command_run_from_inside_the_configuration_is_held_back(machine, stub, run_hook, held_back_lines) -> None:
    payload = {"session_id": "s", "cwd": str(machine.home / ".codex"), "tool_name": "Bash", "tool_input": {"command": "ls"}}
    _held_back(stub, run_hook, payload, held_back_lines, "agent-config")


def test_a_patch_piped_with_more_shell_is_still_held_back_by_what_it_names(payloads, stub, run_hook, held_back_lines) -> None:
    """A mixed command is judged as the command it is, and a command is checked word by word.

    The patch inside it is no longer split out into targets, so the protection
    for what it writes has to come from the command text: every word of it is
    resolved as a path, which is what finds the `~/.claude` the patch adds to.
    """
    command = (
        "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: ~/.claude/settings.json\n+{}\n*** End Patch\nEOF\n"
        "echo done"
    )
    _held_back(stub, run_hook, payloads.command("codex", command), held_back_lines, "agent-config", ["--agent", "codex"])


def test_a_command_that_only_resembles_a_configuration_path_is_sent(payloads, stub, run_hook, held_back_lines) -> None:
    """A project's own `.claudette` folder is not ~/.claude."""
    run_hook(payloads.command("claude-code", "ls docs/.claudette ~/.claudette"))
    assert len(stub.requests) == 1
    assert held_back_lines() == []


# --- the same home folder, spelled the way Git Bash and Windows spell it ------------
#
# Claude Code runs Bash through Git Bash on Windows, which writes C:\Users\me
# as /c/Users/me. Resolved as written that lands at the current drive's \c\,
# which is inside nothing, so the developer's login used to reach the ledger
# in the command text while the `~` and C:/ spellings of the same file were
# held back.

BASH_HOME_VARIABLES = [
    'cat "$USERPROFILE/.claude/projects/p/memory/MEMORY.md"',
    "ls ${USERPROFILE}/.codex",
    "cat $HOMEDRIVE$HOMEPATH/.gemini/settings.json",
]


@pytest.mark.parametrize("command", BASH_HOME_VARIABLES)
@pytest.mark.parametrize("agent", AGENTS)
def test_a_command_naming_the_home_folder_by_a_windows_variable_is_held_back(
    agent, command, payloads, stub, run_hook, held_back_lines
) -> None:
    _held_back(stub, run_hook, payloads.command(agent, command), held_back_lines, "agent-config", ["--agent", agent])


def bash_spelling(path, prefix: str = "") -> str:
    """A Windows path as Git Bash, Cygwin and WSL write it: C:\\Users\\me becomes /c/Users/me."""
    drive, rest = os.path.splitdrive(str(path))
    return prefix + "/" + drive[0].lower() + rest.replace("\\", "/")


@pytest.mark.parametrize(
    "token, windows",
    [
        ("/c/Users/acmedev/.claude", "C:/Users/acmedev/.claude"),
        ("/cygdrive/d/work/acme", "D:/work/acme"),
        ("/mnt/e/work/acme", "E:/work/acme"),
        ("/c", "C:/"),
        ("/usr/local/bin/python", "/usr/local/bin/python"),
        ("/carrots/x.py", "/carrots/x.py"),
        ("src/app.py", "src/app.py"),
    ],
)
def test_a_drive_written_the_git_bash_way_is_read_as_a_drive_only_on_windows(hook, token, windows) -> None:
    """A POSIX machine has real /c and /mnt directories, so the reading is Windows' alone."""
    assert hook._drive_path(token, translate=True) == windows
    assert hook._drive_path(token, translate=False) == token


@pytest.mark.skipif(os.name != "nt", reason="/c/Users names a real directory on a POSIX machine")
@pytest.mark.parametrize("prefix", ["", "/cygdrive", "/mnt"])
@pytest.mark.parametrize("directory", [".claude", ".codex", ".gemini", ".threefold"])
def test_a_command_naming_the_agents_configuration_the_git_bash_way_is_held_back(
    prefix, directory, machine, payloads, stub, run_hook, held_back_lines
) -> None:
    where = bash_spelling(machine.home / directory / "projects" / "p" / "memory" / "MEMORY.md", prefix)
    _held_back(stub, run_hook, payloads.command("claude-code", "cat " + where), held_back_lines, "agent-config")


@pytest.mark.skipif(os.name != "nt", reason="/c/Users names a real directory on a POSIX machine")
def test_a_command_run_from_the_configuration_folder_named_the_git_bash_way_is_held_back(
    machine, stub, run_hook, held_back_lines
) -> None:
    command = f"cat {bash_spelling(machine.home / '.claude' / 'settings.json')} > notes.txt"
    payload = {"session_id": "s", "cwd": str(machine.project), "tool_name": "Bash", "tool_input": {"command": command}}
    _held_back(stub, run_hook, payload, held_back_lines, "agent-config")


@pytest.mark.skipif(os.name != "nt", reason="/c/Users names a real directory on a POSIX machine")
def test_the_git_bash_spelling_of_the_project_is_shortened_before_it_is_sent(machine, payloads, stub, run_hook) -> None:
    """The path above the project is what carries the developer's login."""
    command = "python " + bash_spelling(machine.project / "src" / "app.py")
    run_hook(payloads.command("claude-code", command))
    text = stub.requests[0]["body"]["arguments"]["command"]
    assert text == "python ./src/app.py", text


def test_a_command_reaching_the_aws_credentials_is_still_sent_for_the_service_to_refuse(payloads, stub, run_hook, verdict) -> None:
    """~/.aws is not an agent's configuration. It is exactly what the service's
    protected-path rule exists to refuse, so the hook must send it rather than
    quietly let it run ungoverned."""
    stub.answer(200, {"status": "BLOCKED_BOUNDARY_VIOLATION", "reason": "Command reaches a protected path or credential store"})
    _, out, _ = run_hook(payloads.command("claude-code", "cat ~/.aws/credentials"))
    assert stub.requests[0]["body"]["arguments"] == {"command": "cat ~/.aws/credentials"}
    assert verdict.decision(out) == "deny"


# --- data -------------------------------------------------------------------------

@pytest.mark.parametrize(
    "relative",
    [
        "reports/q3.csv",
        "model/weights.safetensors",
        "scans/brain.nii.gz",
        "figures/plot.PNG",
        "cache/features.parquet",
        "store/app.sqlite",
    ],
)
def test_a_data_file_is_held_back_by_its_extension(relative, payloads, stub, run_hook, held_back_lines) -> None:
    _held_back(stub, run_hook, payloads.write("claude-code", relative, "a,b\n1,2\n"), held_back_lines, "data-file")


@pytest.mark.parametrize(
    "relative",
    [
        "data/loader.py", "src/datasets/split.py", "outputs/run1/log.txt", "node_modules/acme/index.js",
        ".git/hooks/pre-commit", ".venv/lib/site.py", ".git/info/attributes", ".git/config",
    ],
)
@pytest.mark.parametrize("agent", AGENTS)
def test_a_file_under_a_data_directory_is_held_back(agent, relative, payloads, stub, run_hook, held_back_lines, monkeypatch) -> None:
    """Run in observe mode, where nothing is refused, so holding back is the
    only thing that can keep a call here. In enforce mode the hook scripts and
    .git/config are refused before this check, and the next test pins that
    they are not sent there either."""
    monkeypatch.setenv("THREEFOLD_MODE", "observe")
    _held_back(stub, run_hook, payloads.write(agent, relative), held_back_lines, "data-file", ["--agent", agent])


@pytest.mark.parametrize("relative", [".git/hooks/pre-commit", ".git/config"])
@pytest.mark.parametrize("agent", AGENTS)
def test_in_enforce_mode_the_files_under_git_that_run_the_hooks_are_refused_and_still_never_sent(agent, relative, payloads, stub, run_hook) -> None:
    code, out, _ = run_hook(payloads.write(agent, relative, "[core]\n\thooksPath = /dev/null\n"), ["--agent", agent])
    assert code == 0
    assert "deny" in out
    assert stub.requests == []


def test_the_data_directories_are_read_inside_the_project_not_above_it(machine, stub, run_hook, held_back_lines) -> None:
    """A project that lives under a folder called `data` is still code."""
    project = machine.tmp / "data" / "acme-service"
    project.mkdir(parents=True)
    payload = {
        "session_id": "s",
        "cwd": str(project),
        "tool_name": "Write",
        "tool_input": {"file_path": str(project / "src" / "app.py"), "content": "x = 1"},
    }
    run_hook(payload)
    assert len(stub.requests) == 1
    assert held_back_lines() == []


def test_a_file_named_like_a_data_directory_is_not_a_directory(payloads, stub, run_hook) -> None:
    run_hook(payloads.write("claude-code", "src/data.py"))
    assert len(stub.requests) == 1


def test_a_move_out_of_a_data_directory_is_held_back(payloads, stub, run_hook, held_back_lines) -> None:
    patch = "*** Begin Patch\n*** Update File: data/raw.py\n*** Move to: src/raw.py\n@@\n+x = 1\n*** End Patch"
    _held_back(stub, run_hook, payloads.codex_patch(patch), held_back_lines, "data-file", ["--agent", "codex"])


# --- a data file written by a shell command ----------------------------------------
#
# The same file, written the other way. A Write of data/train.csv is held back
# and the rows stay here; `cat > data/train.csv <<EOF` carried the same rows to
# the service inside arguments.command, because the data check only ever looked
# at a call's targets and a command has none.

WRITES_A_DATA_FILE = [
    "cat > data/train.csv <<'EOF'\npatient_id,diagnosis\n1001,synthetic-a\n1002,synthetic-b\nEOF",
    "printf 'a\\tb\\n' > outputs/report.tsv",
    "echo 1001,synthetic-a >> reports/q3.csv",
    "sort rows.txt | tee reports/q3.csv",
    "cp src/app.py data/app.py",
    "mv notes.txt outputs/notes.txt",
    "dd if=/dev/zero of=model/weights.safetensors",
    "rsync -a src/ node_modules/acme/",
]


@pytest.mark.parametrize("command", WRITES_A_DATA_FILE)
@pytest.mark.parametrize("agent", AGENTS)
def test_a_command_that_writes_a_data_file_is_held_back(
    agent, command, payloads, stub, run_hook, held_back_lines, monkeypatch
) -> None:
    monkeypatch.setenv("THREEFOLD_MODE", "observe")
    _held_back(stub, run_hook, payloads.command(agent, command), held_back_lines, "data-file", ["--agent", agent])


WRITES_CODE = [
    "cat > src/app.py <<'EOF'\nx = 1\nEOF",
    "printf 'x = 1\\n' > src/app.py",
    "python -m pytest -q > /dev/null 2>&1",
    "cp src/app.py src/copy.py",
    "cat data/train.csv | head -n 5",
    "ls data",
]


@pytest.mark.parametrize("command", WRITES_CODE)
def test_a_command_that_writes_no_data_file_is_still_sent(command, payloads, stub, run_hook, held_back_lines) -> None:
    """Reading a data file is not writing one, and /dev/null is nobody's data."""
    code, out, err = run_hook(payloads.command("claude-code", command))
    assert (code, out, err) == (0, "", "")
    assert len(stub.requests) == 1
    assert held_back_lines() == []


@pytest.mark.parametrize("relative", ["reports/q3.csv", "data/app.py", "outputs/run1/log.txt"])
def test_a_write_and_the_shell_command_that_does_the_same_thing_agree(
    relative, payloads, stub, run_hook, held_back_lines, monkeypatch
) -> None:
    """The contract is about the file, not about which tool wrote it."""
    monkeypatch.setenv("THREEFOLD_MODE", "observe")
    _held_back(stub, run_hook, payloads.write("claude-code", relative, "a,b\n"), held_back_lines, "data-file")
    (machine_log := held_back_lines())  # noqa: F841 - read once so the next run appends to it
    code, out, err = run_hook(payloads.command("claude-code", f"printf 'a,b\\n' > {relative}"))
    assert (code, out, err) == (0, "", "")
    assert stub.requests == []
    assert held_back_lines()[-1].endswith(" data-file")


# --- the never-send list ----------------------------------------------------------

def _never_send(machine, text: str) -> None:
    machine.threefold_home.mkdir(parents=True, exist_ok=True)
    (machine.threefold_home / "never_send.txt").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("agent", AGENTS)
def test_a_call_mentioning_a_never_send_term_is_held_back(agent, machine, payloads, stub, run_hook, held_back_lines) -> None:
    _never_send(machine, "# the owner's list\n\nAcme-Orion\n")
    payload = payloads.write(agent, "src/app.py", "CLIENT = 'acme-orion'\n")
    _held_back(stub, run_hook, payload, held_back_lines, "never-send", ["--agent", agent])
    assert "orion" not in "\n".join(held_back_lines()).lower(), "The log records a category, never the term"


def test_comments_and_blank_lines_in_the_never_send_list_are_not_terms(machine, payloads, stub, run_hook) -> None:
    _never_send(machine, "# Acme-Vega\n\n   \n")
    run_hook(payloads.write("claude-code", "src/app.py", "NAME = 'Acme-Vega'\n"))
    assert len(stub.requests) == 1


def test_a_never_send_term_is_found_even_when_the_agent_escapes_it(machine, payloads, stub, run_hook, held_back_lines) -> None:
    """An agent that escapes non-ASCII writes a Greek term as \\u03b1..., which
    the raw text never matches, so the decoded strings are searched too."""
    _never_send(machine, "Ακμή-Ωρίων\n")
    payload = payloads.write("claude-code", "src/app.py", "CLIENT = 'ακμή-ωρίων'\n")
    raw = json.dumps(payload, ensure_ascii=True)
    assert "ακμή" not in raw
    code, out, _ = run_hook(None, raw=raw)
    assert (code, out) == (0, "")
    assert stub.requests == []
    assert held_back_lines()[0].endswith(" never-send")


def test_a_never_send_term_anywhere_in_the_input_holds_the_call_back(machine, payloads, stub, run_hook) -> None:
    """Anywhere means anywhere: here it is only in the session id, which is sent."""
    _never_send(machine, "acme-session\n")
    run_hook(payloads.write("claude-code"))
    assert stub.requests == []


def test_a_never_send_list_saved_with_a_byte_order_mark_still_works(machine, payloads, stub, run_hook) -> None:
    machine.threefold_home.mkdir(parents=True, exist_ok=True)
    (machine.threefold_home / "never_send.txt").write_text("Acme-Orion\n", encoding="utf-8-sig")
    run_hook(payloads.write("claude-code", "src/app.py", "x = 'Acme-Orion'"))
    assert stub.requests == []


def _never_send_bytes(machine, raw: bytes) -> None:
    machine.threefold_home.mkdir(parents=True, exist_ok=True)
    (machine.threefold_home / "never_send.txt").write_bytes(raw)


@pytest.mark.parametrize(
    "encoding",
    ["utf-16", codecs.BOM_UTF16_BE + "# the owner's list\nAcme-Orion\n".encode("utf-16-be"), "utf-32"],
    ids=["utf-16-le-bom", "utf-16-be-bom", "utf-32"],
)
def test_a_never_send_list_written_by_windows_powershell_is_read(
    encoding, machine, payloads, stub, run_hook, held_back_lines
) -> None:
    """`Add-Content`, `Out-File` and `>` in Windows PowerShell 5.1 write UTF-16 with a BOM.

    Read as UTF-8 every term became NULs and replacement characters, so every
    term stopped matching and the calls that named them were sent, silently.
    The one file that keeps a name at home must not fail that way.
    """
    raw = encoding if isinstance(encoding, bytes) else "# the owner's list\nAcme-Orion\n".encode(encoding)
    _never_send_bytes(machine, raw)
    payload = payloads.write("claude-code", "src/app.py", "CLIENT = 'acme-orion'\n")
    _held_back(stub, run_hook, payload, held_back_lines, "never-send")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"Acme-\xe9\xffOrion\n", id="not-text-at-all"),
        pytest.param("Acme-Orion\n".encode("utf-16-le"), id="utf-16-without-a-bom"),
    ],
)
def test_a_never_send_list_that_cannot_be_read_holds_every_call_back(
    raw, machine, payloads, stub, run_hook, held_back_lines
) -> None:
    """A list nobody can read is not an empty list.

    Guessing at it is how the term it holds reaches the service. The call
    stays here and the developer is told, without a word of the file in the
    note: the file is the one place a name is kept.
    """
    _never_send_bytes(machine, raw)
    code, out, err = run_hook(payloads.write("claude-code", "src/app.py", "x = 1\n"))
    assert (code, out, stub.requests) == (0, "", [])
    assert held_back_lines()[0].endswith(" never-send")
    assert "never_send.txt" in err and "could not be read" in err
    assert "Orion" not in err and "Acme" not in err


def test_a_never_send_list_that_cannot_be_read_does_not_stop_the_agent(machine, payloads, stub, run_hook) -> None:
    """Held back, never refused: the hook still only ever takes permission away."""
    _never_send_bytes(machine, b"Acme-\xe9\xffOrion\n")
    _, out, _ = run_hook(payloads.write("claude-code", "src/app.py", "x = 1\n"))
    assert out == ""


def test_an_empty_never_send_list_is_still_an_empty_list(machine, payloads, stub, run_hook) -> None:
    _never_send_bytes(machine, b"")
    run_hook(payloads.write("claude-code", "src/app.py", "x = 1\n"))
    assert len(stub.requests) == 1


# --- configuration ----------------------------------------------------------------

@pytest.mark.parametrize("agent", AGENTS)
def test_without_a_project_nothing_is_sent_and_stderr_says_why(agent, payloads, stub, run_hook, held_back_lines, monkeypatch) -> None:
    monkeypatch.delenv("THREEFOLD_PROJECT")
    code, out, err = run_hook(payloads.write(agent), ["--agent", agent])
    assert (code, out) == (0, "")
    assert len(err.strip().splitlines()) == 1
    assert "THREEFOLD_PROJECT" in err and "not governed" in err
    assert stub.requests == [], "No folder name or login is sent in place of a project"
    assert held_back_lines()[0].endswith(" no-project")


def test_a_blank_project_is_no_project(payloads, stub, run_hook, monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_PROJECT", "   ")
    run_hook(payloads.write("claude-code"))
    assert stub.requests == []


def test_threefold_home_defaults_to_the_home_directory(machine, payloads, stub, run_hook, monkeypatch) -> None:
    monkeypatch.delenv("THREEFOLD_HOME")
    monkeypatch.delenv("THREEFOLD_PROJECT")
    run_hook(payloads.write("claude-code"))
    assert (machine.home / ".threefold" / "held_back.log").exists()


def test_a_log_that_cannot_be_written_does_not_stop_the_agent(machine, payloads, stub, run_hook, monkeypatch) -> None:
    blocker = machine.tmp / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("THREEFOLD_HOME", str(blocker))
    monkeypatch.delenv("THREEFOLD_PROJECT")
    code, out, _ = run_hook(payloads.write("claude-code"))
    assert (code, out) == (0, "")


# --- credentials ------------------------------------------------------------------

@pytest.mark.parametrize("agent", AGENTS)
def test_a_credential_in_a_write_is_refused_locally_without_a_request(agent, payloads, stub, run_hook, verdict) -> None:
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    code, out, _ = run_hook(payloads.write(agent, "src/settings.py", f"AWS_KEY = '{key}'\n"), ["--agent", agent])
    assert code == 0
    assert verdict.decision(out) == "deny"
    assert "AWS_ACCESS_KEY" in verdict.reason(out)
    assert key not in out, "The reason names the kind of credential, never the value"
    assert stub.requests == []


@pytest.mark.parametrize("agent", AGENTS)
def test_a_credential_in_a_command_is_refused_locally_without_a_request(agent, payloads, stub, run_hook, verdict) -> None:
    token = "ghp_" + "a" * 36
    _, out, _ = run_hook(payloads.command(agent, f"git remote set-url origin https://{token}@example.invalid/acme.git"), ["--agent", agent])
    assert verdict.decision(out) == "deny"
    assert "GITHUB_TOKEN" in verdict.reason(out)
    assert token not in out
    assert stub.requests == []


def test_a_credential_is_refused_even_where_the_call_would_be_held_back(payloads, stub, run_hook, verdict, held_back_lines) -> None:
    """Both checks run before the network, so neither order sends anything. The
    scan runs first so a key written into a data file or outside the project is
    stopped rather than let through silently as not Threefold's business."""
    _, out, _ = run_hook(payloads.write("claude-code", "data/keys.csv", "TOKEN = 'ghp_" + "c" * 36 + "'"))
    assert verdict.decision(out) == "deny"
    assert "GITHUB_TOKEN" in verdict.reason(out)
    assert stub.requests == []
    assert held_back_lines() == []


def test_removing_a_credential_is_not_refused_for_containing_it(machine, stub, run_hook) -> None:
    payload = {
        "session_id": "s",
        "cwd": str(machine.project),
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(machine.project / "src" / "settings.py"),
            "old_string": "KEY = 'AKIAABCDEFGHIJKLMNOP'",
            "new_string": "KEY = os.environ['ACME_KEY']",
        },
    }
    code, out, _ = run_hook(payload)
    assert (code, out) == (0, "")
    assert len(stub.requests) == 1


def test_the_local_credential_shapes_are_the_services_shapes(hook) -> None:
    """Copied because the hook is downloaded alone; compared so they cannot drift.

    Flags are compared as well as the text. Every pattern uses an inline (?i)
    today, so the flags agree trivially, and comparing them is what catches a
    later change to re.IGNORECASE on one side only.
    """
    local = [(label, pattern.pattern, pattern.flags) for label, pattern in hook.SECRET_PATTERNS]
    service = [(label, pattern.pattern, pattern.flags) for label, pattern in SecretScanner.PATTERNS]
    assert local == service


# A short term is a word, not a substring. A three-letter acronym matched as a
# substring inside ordinary identifiers held back calls that mentioned nothing;
# the terms here are synthetic, as the clean-room rule requires.


@pytest.mark.parametrize(
    "text",
    ["QZX", "the qzx api", "qzx-billing", "QzxBilling", "acme_qzx", '"qzx"', "qzxbilling", "path/qzx/x.py", "\nQZX"],
)
def test_a_short_term_that_starts_a_word_is_a_mention(hook, text: str) -> None:
    assert hook.term_occurs(text, "qzx")


@pytest.mark.parametrize("text", ["aqzxb", "isqzxpace", "classqzx", "reqzx"])
def test_a_short_term_inside_another_word_is_not_a_mention(hook, text: str) -> None:
    assert not hook.term_occurs(text, "qzx")


def test_a_longer_term_is_still_matched_anywhere(hook) -> None:
    assert hook.term_occurs("myacmecorpthing", "acmecorp")
    assert hook.term_occurs("ACMECORP", "acmecorp")
