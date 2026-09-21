#!/usr/bin/env python3
"""Threefold in front of a real coding agent: one hook for Claude Code, Codex and Antigravity.

The scenarios on the web page build their own payloads and then refuse them,
which proves the gates run but intercepts nobody. This script is the part that
intercepts. It receives the tool call an agent is about to make, decides locally
what may leave the machine, asks the deployed service about the rest, and prints
a refusal the agent obeys. It never prints an approval: on approval it prints
nothing, so the agent's own permission flow still runs and Threefold can only
ever take permission away.

Take it from the running service, which serves this file:

    curl -O https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/hooks/threefold_hook.py

and register it as the agent's pre-tool-call hook:

    python3 /absolute/path/to/threefold_hook.py --agent claude-code
    python3 /absolute/path/to/threefold_hook.py --agent codex
    python3 /absolute/path/to/threefold_hook.py --agent antigravity

For Claude Code that is `.claude/settings.json`:

    {
      "env": {"THREEFOLD_PROJECT": "Acme-Payments"},
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
            "hooks": [
              {"type": "command",
               "command": "python3 /absolute/path/to/threefold_hook.py --agent claude-code"}
            ]
          }
        ]
      }
    }

Without `--agent` the hook works out which agent called it from the shape of
what arrives on stdin, which is right for Antigravity and for Codex patches but
labels a Codex shell command as Claude Code, so pass the flag.

Configuration is read on every call. The project, the endpoint, the mode and
the API key are each taken from the first of these that sets them:

    1. the environment variable below
    2. `.threefold.json` in the project root: the nearest directory holding a
       `.threefold.json` or a `.git`, walking up from the agent's cwd (or, for
       Antigravity, its first workspace)
    3. `config.json` in THREEFOLD_HOME

Both files hold `project`, `endpoint`, `mode` and `api_key_file`, a path to a
file whose content is the key, never the key itself; a relative path is read
from the directory of the file that names it. The install script writes
`.threefold.json` and lists it in `.git/info/exclude`.

    THREEFOLD_PROJECT     required, here or in a file. Unset, nothing is sent.
    THREEFOLD_ENDPOINT    the service, default the public /prod/ stack
    THREEFOLD_MODE        `enforce` (the default) or `observe`, which sends
                          every call as `dry_run`: recorded, never refused,
                          never able to trip the session
    THREEFOLD_DRY_RUN     1 is the older spelling of THREEFOLD_MODE=observe
    THREEFOLD_API_KEY     sent as X-API-Key when set
    THREEFOLD_API_KEY_FILE a file holding the key, read at call time
    THREEFOLD_DEVELOPER   hashed locally to 12 hex characters; never sent as typed
    THREEFOLD_TIMEOUT     seconds, default 4
    THREEFOLD_FAIL_CLOSED 1 refuses a call the service could not judge
    THREEFOLD_HOME        local state, default ~/.threefold: never_send.txt and
                          config.json are read from it, held_back.log and
                          unknown_shapes.jsonl are written to it

Paths are sent relative to the directory holding `.threefold.json` when there
is one, else to the agent's cwd, and a command carries `cwd`, where it runs
relative to that root, whenever that is not the root itself: a Codex workdir,
an Antigravity Cwd, or Claude Code standing in a subdirectory. Without it the
service would read `> user.py` run in src/domain as a write at the root.

A key is never sent to an endpoint only a repository's `.threefold.json` names,
wherever the key came from, including an api_key_file that same file names:
a cloned repository must not be able to point the hook, and the owner's key
with it, at a server of its choosing. The exception is an endpoint the owner
has paired with that key file in THREEFOLD_HOME/config.json:

    {"trusted_endpoints": [{"endpoint": "https://...", "api_key_file": "owner.key"}]}

which the install script writes when it is given both.

In enforce mode a write to the files that decide whether the hooks run
(`.claude/settings*.json`, `.codex/hooks.json`, `.codex/config.toml`,
`.agents/hooks.json`, `.threefold.json`, `.git/hooks/`, `.git/config`) is refused before
anything is sent, so turning governance off does not depend on the network.
In observe mode the same write is sent as its path with the content left out,
except under `.git`, which is a data directory and is not sent at all.

Standard library only and a single file, because it is downloaded alone and run
by whatever Python the developer already has.
"""
from __future__ import annotations

import datetime
import hashlib
import http.client
import json
import os
import re
import shlex
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

DEFAULT_ENDPOINT = "https://raa131f9dj.execute-api.eu-west-1.amazonaws.com/prod/"
DEFAULT_TIMEOUT_SECONDS = 4.0
MAX_RESPONSE_BYTES = 1_000_000

AGENTS = ("claude-code", "codex", "antigravity")

FILE_WRITE = "FILE_WRITE"
COMMAND_EXEC = "COMMAND_EXEC"

# Claude Code's tools that change a file. Everything else it does (Read, Grep,
# Glob, WebFetch, the MCP tools) is not sent: the service judges writes and
# commands, and shipping every read would put the contents of a working day on
# the wire for nothing.
CLAUDE_CODE_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
CLAUDE_CODE_COMMAND_TOOLS = ("Bash",)

# Codex runs its shell under more than one name depending on version and sandbox.
CODEX_COMMAND_TOOLS = ("bash", "shell", "local_shell", "exec_command", "unified_exec", "container.exec")
CODEX_PATCH_KEYS = ("command", "input", "patch")

ANTIGRAVITY_WRITE_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content")
ANTIGRAVITY_COMMAND_TOOLS = ("run_command",)

# Antigravity's argument names are not fully documented and have been seen in
# more than one spelling, so they are matched case-insensitively and searched
# for inside nested lists as well as at the top. What is deliberately absent:
# TargetContent, which is the text a replacement takes out. Judging it would
# refuse the removal of a forbidden import for containing that import.
ANTIGRAVITY_PATH_KEYS = frozenset(
    ("targetfile", "target_file", "file_path", "filepath", "path", "absolutepath")
)
ANTIGRAVITY_CONTENT_KEYS = frozenset(
    ("codecontent", "content", "replacementcontent", "replacement_content", "newcontent", "new_string")
)
ANTIGRAVITY_COMMAND_KEYS = ("commandline", "command", "cmd")

PATCH_BEGIN = "*** Begin Patch"
PATCH_END = "*** End Patch"
PATCH_END_OF_FILE = "*** End of File"
PATCH_ADD = "*** Add File: "
PATCH_UPDATE = "*** Update File: "
PATCH_DELETE = "*** Delete File: "
PATCH_MOVE = "*** Move to: "

# A file the agent writes that is data rather than code. The rules judge code;
# a dataset, an image or a model checkpoint is exactly what should never be
# uploaded to be looked at.
DATA_EXTENSIONS = (
    ".csv", ".tsv", ".parquet", ".feather", ".npy", ".npz", ".pkl", ".pickle",
    ".h5", ".hdf5", ".dcm", ".nii", ".nii.gz", ".png", ".jpg", ".jpeg", ".gif",
    ".bmp", ".tif", ".tiff", ".zip", ".tar", ".gz", ".7z", ".pt", ".pth", ".ckpt",
    ".onnx", ".safetensors", ".db", ".sqlite",
)
DATA_DIRECTORIES = frozenset(
    ("data", "datasets", "input", "inputs", "output", "outputs", ".venv", "venv", "node_modules", ".git")
)

# Each agent's own configuration and memory. What an agent writes there is about
# the developer, not about the project being governed.
AGENT_CONFIG_DIRECTORIES = (".claude", ".codex", ".gemini")

HELD_BACK_CATEGORIES = ("outside-root", "agent-config", "data-file", "never-send", "no-project")

CONFIG_FILE_NAME = ".threefold.json"
HOME_CONFIG_NAME = "config.json"
MODES = ("enforce", "observe")
MAX_CONFIG_BYTES = 65_536
MAX_KEY_BYTES = 1_024
MAX_WALK_UP = 64

# What a Threefold key looks like: one line of token characters. A file that
# is anything else (a PEM block, a YAML file of tokens, a credentials file) is
# not sent, because api_key_file can be named by a repository's own
# .threefold.json and a header is a way out of the machine.
_API_KEY_SHAPE = re.compile(r"^[A-Za-z0-9._~+/=\-]{8,512}$")

# Copied from threefold.domain.shell_writes, because this file is downloaded
# alone. A test compares the two predicates over the same paths, so the hook
# and the service cannot disagree about which files govern the hooks.
_GOVERNANCE_PAIRS = frozenset(
    (
        (".claude", "settings.json"),
        (".claude", "settings.local.json"),
        (".codex", "hooks.json"),
        (".codex", "config.toml"),
        (".agents", "hooks.json"),
        # Where `git config core.hooksPath` is kept, written directly.
        (".git", "config"),
    )
)
_GOVERNANCE_FILES = frozenset((".threefold.json",))
_GOVERNANCE_DIRECTORIES = frozenset((".claude", ".codex", ".agents", ".git", ".threefold"))

# Copied from threefold.domain.boundary_guard.SecretScanner.PATTERNS, because
# this file is downloaded alone and cannot import it. A test compares the two
# lists pattern by pattern and flag by flag, so a shape added to one and not the
# other fails the suite instead of drifting quietly.
SECRET_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("AWS_ACCESS_KEY", re.compile(r"(?<![A-Z0-9])((?:AKIA|ASIA)[0-9A-Z]{16})(?![A-Z0-9])")),
    ("AWS_SECRET_KEY", re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}")),
    ("GITHUB_TOKEN", re.compile(r"(?<![A-Za-z0-9_])(gh[pousr]_[A-Za-z0-9_]{36,255})(?![A-Za-z0-9_])")),
    ("OPENAI_KEY", re.compile(r"(?<![A-Za-z0-9_])sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("ANTHROPIC_KEY", re.compile(r"(?<![A-Za-z0-9_])sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("SLACK_TOKEN", re.compile(r"(?<![A-Za-z0-9_])xox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("GOOGLE_API_KEY", re.compile(r"(?<![A-Za-z0-9_])AIza[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])")),
    ("JWT", re.compile(r"(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("GENERIC_API_KEY", re.compile(r"(?i)(api[_-]?key|secret[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{20,}['\"]")),
    ("PRIVATE_KEY_HEADER", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


# --- small helpers -----------------------------------------------------------

def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _flag(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes")


def _timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _string_leaves(value: Any) -> Iterator[str]:
    """Every string anywhere in a structure, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _string_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_leaves(item)


def _command_text(value: Any) -> str:
    """A command as one string. Codex sends a shell command as a list of words.

    The words are quoted back into one line, not joined with spaces. Joined,
    `["bash", "-lc", "cp /tmp/acme.py src/domain/x.py"]` reached the service
    as `bash -lc cp /tmp/acme.py src/domain/x.py`, where bash's script is just
    `cp`, and every write in a Codex command went unread.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(word, str) for word in value):
        return shlex.join(value)
    return ""


def _as_path(value: str) -> str:
    """A path from a payload, which Antigravity may send as a file:// URI."""
    if value.lower().startswith("file://"):
        return urllib.request.url2pathname(urllib.parse.urlparse(value).path)
    return value


def _canonical(path: str) -> str:
    """A path as the filesystem sees it, so a symlink cannot walk a target out of the root."""
    try:
        return os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        # A NUL or a name the OS refuses: compare it as written rather than crash.
        return os.path.normcase(os.path.abspath(path.replace("\x00", "")))


def _is_within(child: str, parent: str) -> bool:
    """Whether one canonical path is the other or lies beneath it."""
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        # Different drives on Windows, or a mix of absolute and relative.
        return False


def threefold_home() -> str:
    """Where the owner's local lists and the hook's own logs live."""
    return os.path.abspath(os.path.expanduser(_env("THREEFOLD_HOME") or os.path.join("~", ".threefold")))


def _with_slash(base: str) -> str:
    return base if base.endswith("/") else base + "/"


def endpoint() -> str:
    """The service base URL from the environment alone. It always ends with a slash."""
    return _with_slash(_env("THREEFOLD_ENDPOINT") or DEFAULT_ENDPOINT)


def timeout_seconds() -> float:
    try:
        value = float(_env("THREEFOLD_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def developer_id() -> str:
    """Who is working, as the service may know it: a hash, never a name.

    The previous hook sent $USER, so the public ledger learned the login of
    everyone who installed it. Hashing locally means the name never leaves the
    machine, and the same developer still groups together across sessions.
    """
    developer = _env("THREEFOLD_DEVELOPER")
    if not developer:
        return "anonymous"
    return hashlib.sha256(developer.encode("utf-8")).hexdigest()[:12]


# --- which agent, and what it asked for ----------------------------------------

def parse_agent_flag(argv: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    """Reads `--agent NAME` or `--agent=NAME`. Returns (agent, problem)."""
    arguments = list(argv)
    for index, argument in enumerate(arguments):
        value = None
        if argument == "--agent":
            value = arguments[index + 1] if index + 1 < len(arguments) else ""
        elif argument.startswith("--agent="):
            value = argument.split("=", 1)[1]
        if value is None:
            continue
        name = value.strip().lower()
        if name in AGENTS:
            return name, None
        return None, f"--agent {value!r} is not one of {', '.join(AGENTS)}; working it out from the input instead."
    return None, None


def patch_envelope(text: str) -> Optional[str]:
    """The apply_patch envelope inside a command, if there is one."""
    start = text.find(PATCH_BEGIN)
    if start == -1:
        return None
    end = text.find(PATCH_END, start)
    return text[start:] if end == -1 else text[start:end + len(PATCH_END)]


# Codex feeds a patch to apply_patch through a heredoc, so `apply_patch <<'EOF'`
# on the line before the envelope and the terminator word on the line after it
# belong to the envelope rather than being work of their own.
_HEREDOC_OPENER = re.compile(r"<<-?\s*[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?\s*$")
_HEREDOC_TERMINATOR = re.compile(r"^[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?$")


def patch_is_the_whole_command(command: str, envelope: str) -> bool:
    """Whether the command does nothing but hand this envelope to apply_patch.

    Only then is the call a pure write. A command with anything else in it is a
    shell call that happens to carry a patch: `apply_patch <<'EOF' ... EOF` and
    then `curl -d @.env` on the next line is two actions, and judging only the
    patch left the second one to run unseen. A command that merely quotes the
    marker, such as a commit message or an `echo`, is not a patch at all.
    """
    start = command.find(envelope)
    if start == -1:
        return False
    before = command[:start].strip()
    after = [line.strip() for line in command[start + len(envelope):].splitlines() if line.strip()]
    if before and ("\n" in before or _HEREDOC_OPENER.search(before) is None):
        return False
    if len(after) > 1 or (after and _HEREDOC_TERMINATOR.match(after[0]) is None):
        return False
    return True


def detect_agent(payload: Dict[str, Any], forced: Optional[str] = None) -> str:
    """Which agent sent this. `--agent` wins; otherwise the shape of stdin decides.

    A Codex patch arrives as a tool_input whose only key is `command`, which is
    also what a Claude Code Bash call looks like, so the key shape alone cannot
    tell them apart: a command that is nothing but a patch envelope is what
    does. Finding the marker anywhere in the command is not enough — `git commit
    -m 'see *** Begin Patch in the docs'` is a Bash call, and calling it Codex
    both mislabels it in the ledger and sends it down a path with nothing to parse.
    """
    if forced in AGENTS:
        return forced
    if isinstance(payload.get("toolCall"), dict):
        return "antigravity"
    if payload.get("tool_name") == "apply_patch":
        return "codex"
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict) and set(tool_input) == {"command"}:
        command = _command_text(tool_input["command"])
        envelope = patch_envelope(command)
        if envelope is not None and patch_is_the_whole_command(command, envelope):
            return "codex"
    return "claude-code"


class UnknownShape(Exception):
    """A tool call the hook should govern but cannot read. Carries key names only."""

    def __init__(self, payload: Any):
        super().__init__("unknown tool call shape")
        self.keys = shape_of(payload)


_KEY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,63}$")


def shape_of(payload: Any, max_keys: int = 200, max_depth: int = 6) -> List[str]:
    """The key names of a payload as dotted paths, with every value left out.

    This is what lets an undocumented argument layout be learned from real use
    without recording what anyone wrote. A key that does not look like an
    identifier is replaced by `?`, because a dictionary keyed by file names
    would otherwise put those names in the log.
    """
    keys: List[str] = []

    def walk(node: Any, prefix: str, depth: int) -> None:
        if depth > max_depth or len(keys) >= max_keys:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                name = key if isinstance(key, str) and _KEY_NAME.match(key) else "?"
                path = f"{prefix}.{name}" if prefix else name
                if path not in keys:
                    keys.append(path)
                walk(value, path, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, prefix + "[]", depth + 1)

    walk(payload, "", 0)
    return keys[:max_keys]


class NormalisedCall:
    """One tool call in the shape request v2 carries, whichever agent made it."""

    __slots__ = ("action_type", "files", "command", "command_base", "command_cwd", "touched")

    def __init__(
        self,
        action_type: str,
        files: Optional[List[Dict[str, str]]] = None,
        command: Optional[str] = None,
        command_base: Optional[str] = None,
        touched: Optional[List[str]] = None,
    ):
        self.action_type = action_type
        self.files = files or []
        self.command = command
        self.command_base = command_base
        # Where the command runs, relative to the governed root, once
        # _relative_to_root has worked it out. Empty means the root itself.
        self.command_cwd = ""
        # Paths the call affects without writing content to them, such as the
        # source of a move. They are checked for hold-back like any target.
        self.touched = touched or []

    @property
    def tool_name(self) -> str:
        if self.command is not None:
            return "Bash"
        return "Write" if len(self.files) == 1 else "MultiEdit"

    def arguments(self) -> Dict[str, Any]:
        if self.command is not None:
            # The service reads `echo ... > user.py` run in src/domain as a
            # write to src/domain/user.py only if it is told where it runs.
            return {"command": self.command, "cwd": self.command_cwd} if self.command_cwd else {"command": self.command}
        if len(self.files) == 1:
            return dict(self.files[0])
        return {"edits": [dict(entry) for entry in self.files]}

    def targets(self) -> List[str]:
        return [entry["file_path"] for entry in self.files] + list(self.touched)


def parse_apply_patch(text: str) -> List[Dict[str, Any]]:
    """Reads a Codex apply_patch envelope into one entry per file.

    Each entry has `operation` (add, update or delete), `path`, `added` (the
    lines the patch writes) and `moved_from`. Only added lines are kept: context
    lines are already in the file and removed lines are what the patch takes
    out, and judging either would refuse a patch for code it is deleting.
    """
    files: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        line = line.rstrip("\r")
        if line.startswith("*** "):
            header = line.rstrip()
            if header in (PATCH_BEGIN, PATCH_END, PATCH_END_OF_FILE):
                continue
            for operation, marker in (("add", PATCH_ADD), ("update", PATCH_UPDATE), ("delete", PATCH_DELETE)):
                if line.startswith(marker):
                    current = {
                        "operation": operation,
                        "path": line[len(marker):].strip(),
                        "added": [],
                        "moved_from": None,
                    }
                    files.append(current)
                    break
            else:
                if line.startswith(PATCH_MOVE) and current is not None and current["operation"] == "update":
                    current["moved_from"] = current["path"]
                    current["path"] = line[len(PATCH_MOVE):].strip()
            continue
        if current is not None and line.startswith("+"):
            current["added"].append(line[1:])
    return [entry for entry in files if entry["path"]]


def _patch_call(envelope: str, payload: Any) -> NormalisedCall:
    entries = parse_apply_patch(envelope)
    if not entries:
        raise UnknownShape(payload)
    files: List[Dict[str, str]] = []
    touched: List[str] = []
    for entry in entries:
        if entry["operation"] == "delete":
            files.append({"file_path": entry["path"], "content": "", "note": "file deleted by apply_patch"})
            continue
        record = {"file_path": entry["path"], "content": "\n".join(entry["added"])}
        if entry["moved_from"]:
            record["note"] = f"moved from {entry['moved_from']}"
            touched.append(entry["moved_from"])
        files.append(record)
    return NormalisedCall(FILE_WRITE, files, touched=touched)


def _normalise_claude_code(payload: Dict[str, Any]) -> Optional[NormalisedCall]:
    tool = payload.get("tool_name")
    if tool not in CLAUDE_CODE_WRITE_TOOLS and tool not in CLAUDE_CODE_COMMAND_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise UnknownShape(payload)

    if tool in CLAUDE_CODE_COMMAND_TOOLS:
        command = _command_text(tool_input.get("command"))
        if not command.strip():
            raise UnknownShape(payload)
        return NormalisedCall(COMMAND_EXEC, command=command)

    path = tool_input.get("notebook_path" if tool == "NotebookEdit" else "file_path")
    if not isinstance(path, str) or not path.strip():
        raise UnknownShape(payload)
    if tool == "Write":
        content = tool_input.get("content")
    elif tool == "Edit":
        content = tool_input.get("new_string")
    elif tool == "MultiEdit":
        # The new strings only. old_string is what the edit removes, and an
        # earlier version of the service refused the removal of a forbidden
        # import for containing it.
        edits = tool_input.get("edits")
        pieces = [
            edit.get("new_string")
            for edit in (edits if isinstance(edits, list) else [])
            if isinstance(edit, dict) and isinstance(edit.get("new_string"), str)
        ]
        content = "\n".join(pieces)
    else:
        content = tool_input.get("new_source")
    return NormalisedCall(FILE_WRITE, [{"file_path": path, "content": content if isinstance(content, str) else ""}])


def _normalise_codex(payload: Dict[str, Any]) -> Optional[NormalisedCall]:
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, str):
        tool_input = {"command": tool_input}
    if not isinstance(tool_input, dict):
        if tool == "apply_patch" or tool.lower() in CODEX_COMMAND_TOOLS:
            raise UnknownShape(payload)
        return None

    # A patch can arrive as the apply_patch tool, whose whole argument is the
    # envelope, or as a shell command that pipes one into apply_patch. Only the
    # first is purely a write. A command that also does something else is a
    # shell call carrying a patch, and it has to be judged as the command it is
    # or the half that is not a patch never reaches the service.
    patch_text, envelope = "", None
    for key in CODEX_PATCH_KEYS:
        text = _command_text(tool_input.get(key))
        found = patch_envelope(text)
        if found is not None:
            patch_text, envelope = text, found
            break

    if tool == "apply_patch":
        # This tool does nothing but apply a patch, so an envelope it cannot
        # read is a shape to log: there is no command underneath to fall back to.
        if envelope is None:
            raise UnknownShape(payload)
        return _patch_call(envelope, payload)

    if envelope is not None and patch_is_the_whole_command(patch_text, envelope):
        try:
            return _patch_call(envelope, payload)
        except UnknownShape:
            pass  # not a readable patch after all; judge it as the command it is

    # The command key alone, never `input` or `patch`: those carry a patch to
    # look for a marker in, not shell text anyone is about to run. The tool name
    # decides nothing here, because Codex renames its shell between versions and
    # a payload that arrives without one would otherwise be sent nowhere.
    command = _command_text(tool_input.get("command"))
    if command.strip():
        base = tool_input.get("workdir") or tool_input.get("cwd")
        return NormalisedCall(COMMAND_EXEC, command=command, command_base=base if isinstance(base, str) else None)
    if tool.lower() in CODEX_COMMAND_TOOLS:
        raise UnknownShape(payload)
    return None


def _first_key(mapping: Dict[str, Any], names: Sequence[str]) -> Any:
    """The value of the first key in `names` present in `mapping`, ignoring case."""
    lowered = {key.lower(): value for key, value in mapping.items() if isinstance(key, str)}
    for name in names:
        if name in lowered:
            return lowered[name]
    return None


def _has_any_key(node: Any, names: frozenset) -> bool:
    if isinstance(node, dict):
        return any(
            (isinstance(key, str) and key.lower() in names) or _has_any_key(value, names)
            for key, value in node.items()
        )
    if isinstance(node, list):
        return any(_has_any_key(item, names) for item in node)
    return False


def _antigravity_files(args: Dict[str, Any]) -> List[Dict[str, str]]:
    """Pairs each path in an Antigravity write with the content meant for it.

    A replacement carries its path at the top and its new text inside a list of
    chunks, so content found beneath an object inherits that object's path.
    Content with no path anywhere above it, or a path with no content and no
    EmptyFile flag, means the layout is one this hook does not understand, and
    an empty list is returned so the caller records the shape rather than
    sending a write with nothing in it to judge.
    """
    found: Dict[str, List[str]] = {}
    orphaned = False

    def walk(node: Any, owner: Optional[str]) -> None:
        nonlocal orphaned
        if isinstance(node, dict):
            here = owner
            for key, value in node.items():
                if isinstance(key, str) and key.lower() in ANTIGRAVITY_PATH_KEYS and isinstance(value, str) and value.strip():
                    here = value
                    break
            for key, value in node.items():
                if isinstance(key, str) and key.lower() in ANTIGRAVITY_CONTENT_KEYS and isinstance(value, str):
                    if here is None:
                        orphaned = True
                    else:
                        found.setdefault(here, []).append(value)
                elif isinstance(key, str) and key.lower() == "emptyfile" and value is True and here is not None:
                    found.setdefault(here, [])
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, here)
        elif isinstance(node, list):
            for item in node:
                walk(item, owner)

    walk(args, None)
    if orphaned or not found:
        return []
    return [{"file_path": path, "content": "\n".join(pieces)} for path, pieces in found.items()]


def _normalise_antigravity(payload: Dict[str, Any]) -> Optional[NormalisedCall]:
    call = payload.get("toolCall")
    if not isinstance(call, dict):
        raise UnknownShape(payload)
    name = call.get("name")
    args = call.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            raise UnknownShape(payload) from None
    if not isinstance(name, str) or not isinstance(args, dict):
        raise UnknownShape(payload)
    tool = name.lower()

    if tool in ANTIGRAVITY_COMMAND_TOOLS:
        command = _command_text(_first_key(args, ANTIGRAVITY_COMMAND_KEYS))
        if not command.strip():
            raise UnknownShape(payload)
        base = _first_key(args, ("cwd",))
        return NormalisedCall(COMMAND_EXEC, command=command, command_base=base if isinstance(base, str) else None)

    if tool in ANTIGRAVITY_WRITE_TOOLS:
        files = _antigravity_files(args)
        if not files:
            raise UnknownShape(payload)
        return NormalisedCall(FILE_WRITE, files)

    # A tool this hook has not been taught. One that carries content or a
    # command may be a write or a shell under a new name, so its shape is worth
    # recording; one that does not is a read, and is left alone like any read.
    if _has_any_key(args, ANTIGRAVITY_CONTENT_KEYS | frozenset(ANTIGRAVITY_COMMAND_KEYS)):
        raise UnknownShape(payload)
    return None


def normalise(payload: Dict[str, Any], agent: str) -> Optional[NormalisedCall]:
    """Turns one agent's tool call into request v2's shape.

    Returns None for a call the hook does not govern, such as a read, and raises
    UnknownShape for one it should govern but cannot interpret.
    """
    if agent == "antigravity":
        return _normalise_antigravity(payload)
    if agent == "codex":
        return _normalise_codex(payload)
    return _normalise_claude_code(payload)


# --- what may leave the machine ------------------------------------------------

def project_root(payload: Dict[str, Any]) -> str:
    """The directory a call must stay inside to be sent: the agent's cwd or first workspace."""
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return os.path.abspath(_as_path(cwd))
    workspaces = payload.get("workspacePaths")
    if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], str) and workspaces[0].strip():
        return os.path.abspath(_as_path(workspaces[0]))
    return os.getcwd()


# --- configuration: environment, then the repository, then home -------------------

def config_root(payload: Dict[str, Any]) -> Optional[str]:
    """The directory whose .threefold.json governs this call, or None.

    The nearest directory at or above the agent's working directory that holds
    a `.threefold.json` or a `.git`. A `.git` ends the search even without a
    config file, so a repository nested in a governed one is not governed by
    its parent's settings by accident. This is only where configuration is
    found: what may leave the machine is still decided against project_root.
    """
    current = project_root(payload)
    for _ in range(MAX_WALK_UP):
        if os.path.isfile(os.path.join(current, CONFIG_FILE_NAME)) or os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


def governed_root(payload: Dict[str, Any]) -> str:
    """The directory calls are judged relative to: the one holding .threefold.json, else the agent's cwd.

    An installed repository names its own root by the file the installer put
    there. Measuring from the agent's cwd instead lost the path above it: with
    Claude Code standing in src/domain, a Write to src/domain/user.py was sent
    as `user.py`, which no rule on `**/domain/**` covers. Without a
    .threefold.json the day-one root, the cwd, is kept.
    """
    start = project_root(payload)
    root = config_root(payload)
    if root and os.path.isfile(os.path.join(root, CONFIG_FILE_NAME)):
        return root
    return start


def command_directory(call: "NormalisedCall", payload: Dict[str, Any]) -> str:
    """Where a command runs: the agent's workdir for it, resolved from the cwd, or the cwd itself."""
    cwd = project_root(payload)
    return os.path.abspath(os.path.join(cwd, _as_path(call.command_base))) if call.command_base else cwd


def read_config(path: str) -> Dict[str, Any]:
    """A configuration file as a dictionary, or empty if it is missing, too large or not an object."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        return {}
    if len(raw) > MAX_CONFIG_BYTES:
        return {}
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}
    return document if isinstance(document, dict) else {}


class Settings:
    """What one call is sent under, and where each part came from. Never holds a key in its repr."""

    __slots__ = ("project", "endpoint", "endpoint_source", "mode", "api_key", "notes")

    def __init__(self) -> None:
        self.project = ""
        self.endpoint = DEFAULT_ENDPOINT
        self.endpoint_source = "default"
        self.mode = "enforce"
        self.api_key: Optional[str] = None
        self.notes: List[str] = []

    def __repr__(self) -> str:
        return f"Settings(project={self.project!r}, endpoint={self.endpoint!r}, mode={self.mode!r})"


def _read_key_file(path: str, label: str, notes: List[str]) -> Optional[str]:
    """The key a file holds, read now, stripped, and checked to look like a key.

    Nothing about the content ever reaches a note: not the key, not a line of
    a file that turned out not to be one.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_KEY_BYTES + 1)
    except OSError:
        notes.append(f"the API key file named by {label} could not be read, so no key was sent.")
        return None
    try:
        key = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        key = ""
    if len(raw) > MAX_KEY_BYTES or not _API_KEY_SHAPE.match(key) or any(p.search(key) for _, p in SECRET_PATTERNS):
        notes.append(f"the file named by {label} does not hold a Threefold key, so nothing from it was sent.")
        return None
    return key


def _key_path(value: Any, base: str) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    path = os.path.expanduser(value.strip())
    return path if os.path.isabs(path) else os.path.join(base, path)


def resolve_settings(payload: Dict[str, Any]) -> Settings:
    """The project, endpoint, mode and API key for this call, first source wins.

    The order is the contract: the environment, then the repository's
    .threefold.json, then THREEFOLD_HOME/config.json. A value of the wrong type
    or an unknown mode is skipped with a note rather than trusted, so a typo in
    one layer falls through to the next instead of deciding what is enforced.
    """
    settings = Settings()
    home = threefold_home()
    root = config_root(payload)
    repo_dir = root or ""
    repo = read_config(os.path.join(root, CONFIG_FILE_NAME)) if root else {}
    home_config = read_config(os.path.join(home, HOME_CONFIG_NAME))
    layers = (("repo", CONFIG_FILE_NAME, repo, repo_dir), ("home", "THREEFOLD_HOME/config.json", home_config, home))

    project = _env("THREEFOLD_PROJECT")
    for _, _, document, _ in layers:
        if project:
            break
        value = document.get("project")
        project = value.strip() if isinstance(value, str) else ""
    settings.project = project

    home_endpoint = DEFAULT_ENDPOINT
    value = home_config.get("endpoint")
    if isinstance(value, str) and value.strip().lower().startswith(("https://", "http://")):
        home_endpoint = _with_slash(value.strip())
    if _env("THREEFOLD_ENDPOINT"):
        settings.endpoint, settings.endpoint_source = endpoint(), "env"
    else:
        for source, label, document, _ in layers:
            value = document.get("endpoint")
            if value is None:
                continue
            if isinstance(value, str) and value.strip().lower().startswith(("https://", "http://")):
                settings.endpoint, settings.endpoint_source = _with_slash(value.strip()), source
                break
            settings.notes.append(f"the endpoint in {label} is not an http(s) URL and was ignored.")

    mode = _env("THREEFOLD_MODE").lower()
    if mode and mode not in MODES:
        settings.notes.append(f"THREEFOLD_MODE must be enforce or observe; {mode[:20]!r} was ignored.")
        mode = ""
    if not mode and _flag("THREEFOLD_DRY_RUN"):
        mode = "observe"
    for _, label, document, _ in layers:
        if mode:
            break
        value = document.get("mode")
        if value is None:
            continue
        if isinstance(value, str) and value.strip().lower() in MODES:
            mode = value.strip().lower()
        else:
            settings.notes.append(f"the mode in {label} must be enforce or observe, and was ignored.")
    settings.mode = mode or "enforce"

    key: Optional[str] = None
    key_file: Optional[str] = None
    key_label = ""
    if _env("THREEFOLD_API_KEY"):
        key = _env("THREEFOLD_API_KEY")
    elif _env("THREEFOLD_API_KEY_FILE"):
        key_file, key_label = os.path.abspath(os.path.expanduser(_env("THREEFOLD_API_KEY_FILE"))), "THREEFOLD_API_KEY_FILE"
    else:
        for _, label, document, base in layers:
            if "api_key" in document:
                settings.notes.append(f"{label} holds a key itself; only api_key_file is read, and the key was not sent.")
            path = _key_path(document.get("api_key_file"), base)
            if path:
                key_file, key_label = path, label
                break
    # An endpoint the repository chose, and nobody else did, gets no key unless
    # the owner has paired that endpoint with that key file at home. Checking
    # only keys from the environment or home missed the route a cloned
    # repository actually has: its own .threefold.json naming its own server
    # and, as api_key_file, the owner's key file.
    chosen_by_repository = settings.endpoint_source == "repo" and settings.endpoint != home_endpoint
    if chosen_by_repository and (key or key_file):
        if key_file is None or not _paired(home_config, settings.endpoint, key_file, home):
            settings.notes.append(
                f"{CONFIG_FILE_NAME} names an endpoint that is not yours, and THREEFOLD_HOME/config.json does not "
                "pair it with that key file in trusted_endpoints, so the API key was not sent to it."
            )
            key, key_file = None, None
    if key_file:
        key = _read_key_file(key_file, key_label, settings.notes)
    settings.api_key = key
    return settings


def _paired(home_config: Dict[str, Any], endpoint_url: str, key_file: str, home: str) -> bool:
    """Whether THREEFOLD_HOME/config.json lists this endpoint with this key file in `trusted_endpoints`.

    The installer writes the pair when it is given both, so an owner's own
    second stack keeps working while a repository's choice of server does not
    inherit the owner's key.
    """
    pairs = home_config.get("trusted_endpoints")
    if not isinstance(pairs, list):
        return False
    wanted = _canonical(key_file)
    for entry in pairs:
        if not isinstance(entry, dict):
            continue
        url = entry.get("endpoint")
        path = _key_path(entry.get("api_key_file"), home)
        if isinstance(url, str) and _with_slash(url.strip()) == endpoint_url and path and _canonical(path) == wanted:
            return True
    return False


def is_governance_path(path: str, deletes: bool = False) -> bool:
    """Whether writing (or removing) this project-relative path touches the hooks' own files."""
    parts = [part.lower() for part in re.split(r"[\\/]+", path or "") if part and part != "."]
    if not parts:
        return False
    if parts[-1] in _GOVERNANCE_FILES:
        return True
    if len(parts) >= 2 and (parts[-2], parts[-1]) in _GOVERNANCE_PAIRS:
        return True
    if any(parts[index] == ".git" and parts[index + 1] == "hooks" for index in range(len(parts) - 1)):
        return True
    if ".threefold" in parts:
        return True
    return deletes and parts[-1] in _GOVERNANCE_DIRECTORIES


def protected_directories(home: str) -> List[str]:
    """Each agent's configuration and memory, and Threefold's own home, as written."""
    user_home = os.path.expanduser("~")
    directories = [os.path.join(user_home, name) for name in AGENT_CONFIG_DIRECTORIES]
    directories.append(home)
    return [os.path.abspath(directory) for directory in directories]


def _resolve(path: str, base: str) -> str:
    return _canonical(os.path.join(base, os.path.expanduser(_as_path(path))))


def _is_data_file(target: str, root: str) -> bool:
    """Data by extension, or by a directory segment inside the project.

    Segments are read relative to the root, not from the absolute path: a
    project that happens to live under a folder called `data` is still code.
    """
    parts = [part for part in re.split(r"[\\/]+", os.path.relpath(target, root)) if part]
    if not parts:
        return False
    if parts[-1].lower().endswith(DATA_EXTENSIONS):
        return True
    return any(part.lower() in DATA_DIRECTORIES for part in parts[:-1])


_HOME_PREFIXES = ("${home}", "$home", "%userprofile%", "$env:userprofile", "%home%", "~")
_COMMAND_TOKEN_SPLIT = re.compile(r"[\s'\"`;|&<>(),=]+")


def _expand_home_token(token: str) -> Optional[str]:
    lowered = token.lower()
    for prefix in _HOME_PREFIXES:
        if lowered == prefix or lowered.startswith(prefix + "/") or lowered.startswith(prefix + "\\"):
            rest = token[len(prefix):]
            if os.sep == "/":
                # `%USERPROFILE%\.gemini` names the same place whichever
                # separator it was written with, and missing it is the costly error.
                rest = rest.replace("\\", "/")
            return os.path.expanduser("~") + rest
    return None


def command_reaches(command: str, base: str, protected: Sequence[str]) -> bool:
    """Whether a command names a path inside one of the protected directories.

    Checked two ways: the absolute directory appearing anywhere in the text,
    and each word of the command resolved as a path, with `~`, `$HOME` and
    `%USERPROFILE%` expanded, against where the command runs. A command run
    from inside a protected directory reaches it whatever it names.
    """
    canonical = [_canonical(directory) for directory in protected]
    canonical_base = _canonical(base)
    if any(_is_within(canonical_base, directory) for directory in canonical):
        return True
    text = os.path.normcase(command)
    forms = set(canonical) | {os.path.normcase(directory) for directory in protected}
    for form in forms:
        if re.search(re.escape(form) + r"(?=$|[\\/\s'\"`;|&<>),])", text):
            return True
    for token in _COMMAND_TOKEN_SPLIT.split(command):
        if not token:
            continue
        expanded = _expand_home_token(token)
        if expanded is None:
            if not ("/" in token or "\\" in token or token.startswith(".")):
                continue
            expanded = token
        candidate = _canonical(os.path.join(canonical_base, _as_path(expanded)))
        if any(_is_within(candidate, directory) for directory in canonical):
            return True
    return False


def read_never_send(home: str) -> List[str]:
    """The owner's never-send terms: one per line, blank lines and # comments ignored."""
    try:
        with open(os.path.join(home, "never_send.txt"), "r", encoding="utf-8-sig", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    terms = []
    for line in lines:
        term = line.strip()
        if term and not term.startswith("#"):
            terms.append(term.casefold())
    return terms


def mentions_never_send(raw_text: str, payload: Any, terms: Sequence[str]) -> bool:
    """Whether any term appears anywhere in what the agent sent, ignoring case.

    The raw text alone is not enough: an agent that escapes non-ASCII in its
    JSON writes a Greek term as \\u03b1..., which the term never matches. So the
    decoded strings are searched as well as the text as it arrived.
    """
    if not terms:
        return False
    haystacks = [raw_text.casefold()]
    haystacks.extend(leaf.casefold() for leaf in _string_leaves(payload))
    return any(term in haystack for term in terms for haystack in haystacks)


def held_back_category(call: NormalisedCall, payload: Dict[str, Any], raw_text: str, home: str) -> Optional[str]:
    """Why this call must not leave the machine, or None if it may.

    Only a category is ever reported, never the path, content or term that
    caused it, so the log of what was held back cannot become the leak.
    """
    cwd = project_root(payload)
    canonical_root = _canonical(governed_root(payload))
    protected = protected_directories(home)
    canonical_protected = [_canonical(directory) for directory in protected]

    for target in call.targets():
        resolved = _resolve(target, cwd)
        if any(_is_within(resolved, directory) for directory in canonical_protected):
            return "agent-config"
        if not _is_within(resolved, canonical_root):
            return "outside-root"
        # Everything under .git is held back, the hook scripts and .git/config
        # included. Those two decide whether the hooks run, but .git/config also
        # holds remote URLs, which can carry a token or a repository name, and
        # the day-one contract never sends a data directory. In enforce mode a
        # write to either has already been refused before this is reached.
        if call.action_type == FILE_WRITE and _is_data_file(resolved, canonical_root):
            return "data-file"

    if call.command is not None:
        base = command_directory(call, payload)
        if command_reaches(call.command, base, protected):
            return "agent-config"
        # A command run outside the project writes outside it, relative paths
        # and all, and judging it as though it ran at the root would refuse
        # work on some other checkout for a rule of this one.
        if not _is_within(_canonical(base), canonical_root):
            return "outside-root"

    if mentions_never_send(raw_text, payload, read_never_send(home)):
        return "never-send"
    return None


def governance_target(call: NormalisedCall, payload: Dict[str, Any], home: str) -> Optional[str]:
    """The first project-relative path this call writes or removes that governs the hooks, if any.

    Only targets inside the project count. The agents' own directories under
    the home folder are the developer's, and the day-one contract keeps those
    calls on the machine unjudged rather than refused.
    """
    cwd = project_root(payload)
    canonical_root = _canonical(governed_root(payload))
    canonical_protected = [_canonical(directory) for directory in protected_directories(home)]
    removed = set(call.touched) | {entry["file_path"] for entry in call.files if "deleted" in entry.get("note", "")}
    for target in call.targets():
        resolved = _resolve(target, cwd)
        if not _is_within(resolved, canonical_root) or any(_is_within(resolved, d) for d in canonical_protected):
            continue
        relative = os.path.relpath(resolved, canonical_root).replace(os.sep, "/")
        if is_governance_path(relative, deletes=target in removed):
            return relative
    return None


def find_credential(call: NormalisedCall) -> Optional[str]:
    """The kind of the first credential in anything the call writes or runs."""
    for text in _string_leaves(call.arguments()):
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                return label
    return None


def _shorten(text: str, root: str, shorthand: str = ".") -> str:
    """Replaces the project's own absolute path with `shorthand` and the home directory with `~`.

    The shorthand is the root as seen from where the command runs: `.` when it
    runs at the root, `../..` when it runs two folders down, so a path written
    out in full still names the same file once the service resolves it.

    A command has to be sent as it will run, so it cannot be turned into
    relative paths the way a target can. What can go is the part of it that is
    nobody's business: the path above the project, which on most machines
    begins with the developer's login name. A `cat` of the credentials file
    spelled out in full reaches the service as `cat ~/.aws/credentials`, which
    its protected-path rule refuses exactly as it did before.
    """
    replacements = []
    for base, short in ((root, shorthand), (os.path.expanduser("~"), "~")):
        for candidate in (base, os.path.abspath(base), os.path.realpath(base)):
            if candidate and candidate not in (os.sep, "/"):
                replacements.append((candidate.replace("\\", "/"), short))
                replacements.append((candidate.replace("/", "\\"), short))
    flags = re.IGNORECASE if os.name == "nt" else 0
    for absolute, shorthand in sorted(set(replacements), key=lambda pair: len(pair[0]), reverse=True):
        text = re.sub(re.escape(absolute), shorthand, text, flags=flags)
    return text


def _relative_to_root(call: NormalisedCall, root: str, cwd: Optional[str] = None, command_dir: Optional[str] = None) -> None:
    """Takes the machine out of the call: targets relative to the project, paths shortened.

    By the time this runs every target is inside the root, so nothing is lost,
    and what is gained is that the service never sees the absolute path, which
    on most machines begins with the developer's login name. A relative target
    is read from `cwd`, where the agent stands, and the result is measured from
    `root`; a command carries where it runs, measured from `root` the same way.
    """
    real_root = os.path.realpath(root)
    cwd = cwd or root

    def relative(path: str) -> str:
        absolute = os.path.realpath(os.path.join(cwd, os.path.expanduser(_as_path(path))))
        return os.path.relpath(absolute, real_root).replace(os.sep, "/")

    for entry in call.files:
        entry["file_path"] = relative(entry["file_path"])
        note = entry.get("note", "")
        if note.startswith("moved from "):
            entry["note"] = "moved from " + relative(note[len("moved from "):])
    if call.command is not None:
        runs_in = os.path.realpath(command_dir or cwd)
        where = os.path.relpath(runs_in, real_root).replace(os.sep, "/")
        call.command_cwd = "" if where == "." else where
        call.command = _shorten(call.command, root, os.path.relpath(real_root, runs_in).replace(os.sep, "/"))


def _strip_governance_content(call: NormalisedCall) -> None:
    """In observe mode, a write to the hooks' own files goes as its path alone.

    The service refuses such a write by where it lands, never by what it says,
    so the content adds nothing to the record a rollout reads, and an agent's
    settings file can hold more about the developer than about the project.
    Runs after _relative_to_root, so every path is already relative to the root.
    """
    for entry in call.files:
        if is_governance_path(entry.get("file_path", "")) and entry.get("content"):
            entry["content"] = ""
            entry["note"] = "content not sent: this file decides whether the agent's hooks run"


def record_held_back(home: str, category: str) -> None:
    _append_line(os.path.join(home, "held_back.log"), f"{_timestamp()} {category}")


def record_unknown_shape(home: str, agent: str, keys: List[str]) -> None:
    _append_line(
        os.path.join(home, "unknown_shapes.jsonl"),
        json.dumps({"timestamp": _timestamp(), "agent": agent, "keys": keys}),
    )


def _append_line(path: str, line: str) -> None:
    # A log that cannot be written must not stop the agent.
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


# --- the service ----------------------------------------------------------------

class ServiceUnavailable(Exception):
    """The service could not judge the call: no connection, a timeout, or no usable answer."""


def build_request(
    call: NormalisedCall, payload: Dict[str, Any], agent: str, project: str, dry_run: Optional[bool] = None
) -> Dict[str, Any]:
    """Request v2 for POST /evaluate-tool-call. `dry_run` defaults to what the environment says."""
    if agent == "antigravity":
        session = payload.get("conversationId") or payload.get("session_id")
    else:
        session = payload.get("session_id") or payload.get("conversationId")
    return {
        # A call with no session of its own still belongs to a session, or the
        # loop detector and the cost ceiling have nothing to count against.
        "session_id": str(session or f"{agent}-local"),
        "project_name": project,
        "developer": developer_id(),
        "tool_name": call.tool_name,
        "action_type": call.action_type,
        "arguments": call.arguments(),
        "agent": agent,
        "origin": "hook",
        "explain": False,
        "dry_run": _flag("THREEFOLD_DRY_RUN") if dry_run is None else bool(dry_run),
    }


def _json_or_none(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _describe_failure(error: BaseException, timeout: float) -> str:
    reason = getattr(error, "reason", error)
    if isinstance(reason, (socket.timeout, TimeoutError)) or isinstance(error, (socket.timeout, TimeoutError)):
        return f"timed out after {timeout:g}s"
    if isinstance(reason, ConnectionRefusedError) or isinstance(error, ConnectionRefusedError):
        return "connection refused"
    return type(reason).__name__ if isinstance(reason, BaseException) else str(reason)[:120]


_FROM_ENVIRONMENT = object()


def post_evaluation(body: Dict[str, Any], base: Optional[str] = None, api_key: Any = _FROM_ENVIRONMENT) -> Tuple[int, Any, str]:
    """Sends one request and returns (status code, parsed body or None, reason phrase).

    `base` and `api_key` come from resolve_settings; left out, they are read
    from the environment as they were before configuration files existed.

    HTTPError is caught before URLError on purpose: it is a subclass, and a
    handler written the other way round files every 400 under "unreachable" and
    fails open on requests the service actually refused.
    """
    headers = {"Content-Type": "application/json"}
    if api_key is _FROM_ENVIRONMENT:
        api_key = _env("THREEFOLD_API_KEY")
    if api_key:
        headers["X-API-Key"] = api_key
    timeout = timeout_seconds()
    request = urllib.request.Request(
        (base or endpoint()) + "evaluate-tool-call",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _json_or_none(response.read(MAX_RESPONSE_BYTES)), str(response.reason or "")
    except urllib.error.HTTPError as error:
        try:
            raw = error.read(MAX_RESPONSE_BYTES) or b""
        except (OSError, http.client.HTTPException):
            raw = b""
        finally:
            error.close()
        return error.code, _json_or_none(raw), str(error.reason or "")
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
        raise ServiceUnavailable(_describe_failure(error, timeout)) from None


def refusal_reason(verdict: Dict[str, Any]) -> str:
    """The service's refusal in words, with its explanation attributed to its source."""
    status = str(verdict.get("status") or "BLOCKED")
    reason = verdict.get("reason") or status
    detail = f"Threefold refused this call ({status}). {reason}"
    explanation = verdict.get("bedrock_explanation")
    if explanation:
        label = "Bedrock" if verdict.get("explanation_source") == "bedrock" else "Deterministic explanation"
        detail = f"{detail}\n{label}: {explanation}"
    return detail


def client_error_reason(code: int, problem: Any, phrase: str) -> str:
    """A 4xx in words: the status and the problem's title, which the service always sets."""
    problem = problem if isinstance(problem, dict) else {}
    title = problem.get("title") or problem.get("message") or phrase or "Client Error"
    detail = problem.get("detail")
    text = f"Threefold refused this call: the service answered HTTP {code} {title}."
    if detail and detail != title:
        text = f"{text} {detail}"
    if code in (401, 403):
        text = f"{text} Check THREEFOLD_API_KEY, or api_key_file in .threefold.json."
    return text


# --- the decision ----------------------------------------------------------------

def deny(agent: str, reason: str) -> Dict[str, Any]:
    """The only decision this hook ever prints."""
    if agent == "antigravity":
        return {"decision": "deny", "reason": reason}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _unjudged(agent: str, what: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """The service could not judge the call. Silent by default, a refusal if fail-closed.

    Failing open is deliberate and it is the honest weakness of running the
    gate out of process: a guard that stops the agent whenever the network
    hiccups is a guard that gets uninstalled by lunchtime.
    """
    if _flag("THREEFOLD_FAIL_CLOSED"):
        return deny(agent, f"Threefold could not check this call ({what}), and THREEFOLD_FAIL_CLOSED=1 refuses what it cannot check."), []
    return None, [f"could not check this call ({what}); the agent's own permissions decide. THREEFOLD_FAIL_CLOSED=1 refuses instead."]


def handle(raw_text: Optional[str], forced_agent: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Everything the hook decides about one tool call.

    Returns what to print on stdout (a refusal, or None for nothing) and the
    lines to write to stderr. The order is the contract: nothing is sent until
    every local check has passed.

    The secret scan runs before the hold-back checks, not after. Both happen
    without the network, so neither order sends anything, but hold-back first
    would let a credential written outside the project, or into a data file,
    through without a word, and a key the agent is about to write to disk is
    worth stopping wherever the disk is.
    """
    try:
        payload = json.loads(raw_text) if raw_text is not None else None
    except (ValueError, RecursionError):
        payload = None
    if not isinstance(payload, dict):
        return None, ["the hook input was not a JSON object, so this call was not checked."]

    agent = detect_agent(payload, forced_agent)
    try:
        call: Any = normalise(payload, agent)
    except UnknownShape as unknown:
        call = unknown
    if call is None:
        return None, []

    home = threefold_home()
    settings = resolve_settings(payload)
    notes = list(settings.notes)
    project = settings.project
    if not project:
        # Never a folder name or a login in its place: either would put a name on
        # the public ledger that nobody chose to publish.
        record_held_back(home, "no-project")
        return None, notes + [
            f"THREEFOLD_PROJECT is not set, and no {CONFIG_FILE_NAME} or THREEFOLD_HOME/config.json names a "
            "project, so this project is not governed and nothing was sent."
        ]

    if isinstance(call, UnknownShape):
        record_unknown_shape(home, agent, call.keys)
        return None, notes + [f"this {agent} tool call has a shape the hook cannot read; its key names were logged and nothing was sent."]

    credential = find_credential(call)
    if credential:
        return deny(
            agent,
            f"Threefold refused this call before it left the machine: it contains a credential ({credential}). "
            "Nothing was sent. Read the value from the environment or a secret store instead of writing it.",
        ), notes

    if settings.mode == "enforce":
        # Observe mode sends the call instead, as a dry run, so a rollout sees
        # the attempt without stopping it.
        governing = governance_target(call, payload, home)
        if governing:
            return deny(
                agent,
                f"Threefold refused this call before it left the machine: it changes {governing}, which decides "
                "whether the agent's hooks run. Nothing was sent. A person changes that file, not the agent.",
            ), notes

    category = held_back_category(call, payload, raw_text or "", home)
    if category:
        record_held_back(home, category)
        return None, notes

    _relative_to_root(
        call,
        governed_root(payload),
        project_root(payload),
        command_directory(call, payload) if call.command is not None else None,
    )
    if settings.mode == "observe":
        _strip_governance_content(call)
    body = build_request(call, payload, agent, project, dry_run=settings.mode == "observe")
    try:
        code, document, phrase = post_evaluation(body, settings.endpoint, settings.api_key)
    except ServiceUnavailable as failure:
        output, lines = _unjudged(agent, str(failure))
        return output, notes + lines

    if code == 429 or code >= 500:
        output, lines = _unjudged(agent, f"HTTP {code}")
        return output, notes + lines
    if 400 <= code < 500:
        return deny(agent, client_error_reason(code, document, phrase)), notes
    if code == 200 and isinstance(document, dict):
        status = str(document.get("status") or "")
        if status.startswith("BLOCKED"):
            return deny(agent, refusal_reason(document)), notes
        return None, notes
    output, lines = _unjudged(agent, f"an answer it could not read, HTTP {code}")
    return output, notes + lines


def _agent_hint(raw_text: Optional[str], forced: Optional[str]) -> str:
    """Which agent to answer when handling failed partway, so a refusal is in its format."""
    try:
        payload = json.loads(raw_text or "")
        return detect_agent(payload if isinstance(payload, dict) else {}, forced)
    except Exception:  # noqa: BLE001
        return forced or "claude-code"


def _read_stdin() -> str:
    # Bytes, decoded as UTF-8: on Windows the text layer would decode with the
    # console code page and garble every non-ASCII never-send term.
    return sys.stdin.buffer.read().decode("utf-8", errors="replace")


def main(argv: Optional[Sequence[str]] = None, stdin: Any = None, stdout: Any = None, stderr: Any = None) -> int:
    """Runs the hook once. Always returns 0: a refusal is printed, never signalled by exit code."""
    argv = sys.argv[1:] if argv is None else argv
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    forced, flag_problem = parse_agent_flag(argv)
    notes = [flag_problem] if flag_problem else []
    try:
        raw_text: Optional[str] = stdin.read() if stdin is not None else _read_stdin()
    except (AttributeError, OSError, ValueError):
        raw_text = None

    try:
        output, lines = handle(raw_text, forced)
    except Exception as error:  # noqa: BLE001 - the hook must never take the agent down with it
        # The type only: an exception's message can quote the content it choked on.
        output, lines = _unjudged(_agent_hint(raw_text, forced), f"an internal error, {type(error).__name__}")

    for line in notes + lines:
        print(f"threefold: {line}", file=stderr)
    if output is not None:
        stdout.write(json.dumps(output) + "\n")
        stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
