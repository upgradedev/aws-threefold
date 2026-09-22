#!/usr/bin/env python3
"""Puts Threefold in front of the coding agents of one repository, and takes it out again.

    threefold_install.py --repo PATH --project Acme-Payments
        [--agents claude-code,codex,antigravity] [--mode observe|enforce]
        [--endpoint URL] [--api-key-file PATH] [--include GLOB ...]
        [--uninstall] [--dry-run]

What it does, in order:

1. Copies the hook, the pre-commit check and the engine that check runs on into
   THREEFOLD_HOME (`~/.threefold` unless set): `bin/threefold_hook.py`,
   `bin/threefold_cli.py` and `lib/threefold/`. One shared copy serves every
   repository, so updating Threefold is one install, not one per repository.
2. Writes `<repo>/.threefold.json` with the project, the mode, and the endpoint
   and key file when given. Never the key itself.
3. Merges one PreToolUse entry per agent into `.claude/settings.local.json`,
   `.codex/hooks.json` and `.agents/hooks.json`, with the matchers the hook was
   measured against on day one. Merged, never replaced: every other key and
   every other hook in those files is kept, and a second install adds nothing.
4. Installs a pre-commit hook that runs `threefold_cli.py check`. A pre-commit
   hook already there is kept and run first. If `core.hooksPath` is set, git
   does not read `.git/hooks`, so nothing is installed there and the output
   says where the check has to be added by hand.
5. Lists every working-tree file it wrote in `.git/info/exclude`, never in
   `.gitignore`: the repository's own ignore rules are its owners' to change.

A file git already tracks is never written. Codex and Antigravity project hook
files are usually committed, and the entry names this machine's Python and
home folder, so writing it would put those paths in the next `git commit -a`;
.git/info/exclude cannot keep a tracked file out. The output says which file
was left alone and what to add by hand instead. With `--endpoint` and
`--api-key-file` together, the pair is also written to THREEFOLD_HOME/config.json,
which is what lets the hook send the key to an endpoint a repository names.

The mode defaults to observe. A rule set introduced to a team for the first
time should show what it would stop for a week before it stops anything.

`--uninstall` removes exactly what an install added, from a record kept in the
git directory. A file that was there before is put back byte for byte, from
the copy the record keeps, as long as it still holds exactly what the install
wrote; one changed by hand since keeps the change and loses only the install's
entry. The shared copies in THREEFOLD_HOME stay, because other repositories
may be using them. `--dry-run` prints every step and writes nothing at all.

`--include GLOB`, repeatable, writes an `include` list into `.threefold.json`:
globs relative to that directory, and the hook then sends only calls inside
them. A glob that is empty, only `.`, absolute or contains `..` is refused,
because it could name nothing inside the directory. So is `--include` with a
`--repo` below the top of a repository: the file goes to the top, where the
globs would be read relative to a directory other than the one they were
written for.

A directory git does not recognise as a repository, such as a workspace root
that holds several repositories, is installed in workspace mode instead of
being refused. Only `.threefold.json` and the three agents' settings files are
written there, merged as above; there is no pre-commit hook, so nothing checks
a commit, and nothing is written under any `.git`, including a `.git` folder
git itself does not accept. The install record is kept in
`THREEFOLD_HOME/installs/<16 hex of the directory's path>.json`, so `--uninstall`
still removes exactly what was added. With `--include` this is how a
workspace governs only the repositories the owner chose.

Codex reads a project's hooks only when that project is trusted in Codex. This
script does not edit `~/.codex/config.toml` to trust it: that is a decision
about the whole machine, and it is the developer's.

Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# In a checkout this file is src/threefold/tools/threefold_install.py, beside
# the CLI, one folder from the hook and two from the engine's package root.
# The stack serves it from there too, so these are the only paths it derives.
HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
HOOK_SOURCE = SOURCE / "threefold" / "hooks" / "threefold_hook.py"
CLI_SOURCE = HERE / "threefold_cli.py"

PROJECT_PATTERN = re.compile(r"^Acme-[A-Za-z0-9-]{1,40}$")
AGENTS = ("claude-code", "codex", "antigravity")

# The file each agent reads its project hooks from, and the tools that reach
# the hook. The same matchers the enforcement measurement of 2026-09-21 used.
AGENT_SETTINGS = {
    "claude-code": (".claude/settings.local.json", "Write|Edit|MultiEdit|NotebookEdit|Bash"),
    "codex": (".codex/hooks.json", "apply_patch|Edit|Write|Bash"),
    "antigravity": (".agents/hooks.json", "write_to_file|replace_file_content|multi_replace_file_content|run_command"),
}

CONFIG_FILE = ".threefold.json"
HOME_CONFIG = "config.json"
EXCLUDE_KEY = "git:info/exclude"
MANIFEST_NAME = "threefold-install.json"
INSTALLS_DIR = "installs"
PRE_COMMIT_MARKER = "# threefold pre-commit: installed by threefold_install.py"
CHAINED_NAME = "pre-commit.before-threefold"
EXCLUDE_MARKER = "# threefold: files written by threefold_install.py"

CODEX_TRUST_NOTE = (
    "Codex loads a project's .codex/hooks.json only when the project is trusted in Codex. "
    "This installer does not edit ~/.codex/config.toml; trust the project from Codex itself."
)


class InstallError(Exception):
    """Something that stops the install before anything is written."""


# --- small helpers -------------------------------------------------------------------

def threefold_home() -> Path:
    configured = (os.environ.get("THREEFOLD_HOME") or "").strip()
    return Path(os.path.expanduser(configured or os.path.join("~", ".threefold"))).resolve()


def forward(path: Any) -> str:
    return str(path).replace("\\", "/")


def quoted(path: Any) -> str:
    """A path for a command line: forward slashes, and quotes only when a space needs them."""
    text = forward(path)
    return f'"{text}"' if " " in text else text


def hook_command(home: Path, agent: str) -> str:
    return f"{quoted(sys.executable)} {quoted(home / 'bin' / 'threefold_hook.py')} --agent {agent}"


def git(repo: Path, *args: str, check: bool = True) -> Tuple[int, str]:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise InstallError(f"git {' '.join(args)}: {result.stderr.strip() or 'failed'}")
    return result.returncode, result.stdout.strip()


def git_path(root: Path, name: str) -> Path:
    """Where git keeps `name` for this checkout, which for a worktree is not under .git."""
    _, value = git(root, "rev-parse", "--git-path", name)
    path = Path(value)
    return path if path.is_absolute() else (root / path)


def locate(repo: Path) -> Tuple[Path, bool]:
    """The directory to install in, and whether it is a workspace rather than a repository.

    A repository is whatever git says the top of the checkout is. Anything git
    does not recognise is a workspace, installed where it was named. That
    includes a directory holding a `.git` folder git does not accept, such as
    an empty one: git passes over such a folder and keeps looking upwards, so
    inside some other checkout it would answer with that checkout's top, and
    the install would land in a repository nobody named. A `.git` of any kind
    between the named directory and the top git reports is therefore read as
    git not recognising the named directory.
    """
    try:
        code, top = git(repo, "rev-parse", "--show-toplevel", check=False)
    except OSError:
        code, top = 1, ""  # no git at all: nothing here is a repository git recognises
    if code == 0 and top:
        root = Path(top).resolve()
        if not _passed_over_git(repo.resolve(), root):
            return root, False
    if not repo.is_dir():
        raise InstallError(f"{repo} is not a directory")
    return repo.resolve(), True


def _passed_over_git(start: Path, top: Path) -> bool:
    """Whether a `.git` sits at or above `start` but below the top git reported, so git skipped it."""
    current = start
    while current != top and top in current.parents:
        if (current / ".git").exists():
            return True
        current = current.parent
    return False


def workspace_record(home: Path, root: Path) -> Path:
    """Where a workspace's install record lives: in THREEFOLD_HOME, never in the workspace.

    Keyed by the directory's absolute path, case-folded where the file system
    folds it, so `--uninstall` finds the record however the path was typed.
    """
    digest = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:16]
    return home / INSTALLS_DIR / f"{digest}.json"


def record_path(root: Path, home: Path, workspace: bool) -> Path:
    return workspace_record(home, root) if workspace else git_path(root, MANIFEST_NAME)


def include_globs(values: Optional[Sequence[str]]) -> List[str]:
    """The --include globs as they are written to .threefold.json, or an InstallError.

    Each is relative to the directory being installed, so one that is empty,
    names only that directory (`.`), is absolute or climbs out with `..` could
    only mean a mistake, and a mistake in the list of what may be sent is
    refused rather than written: `.` was accepted, matched nothing, and held
    back every call. A `.` segment inside a glob is dropped, since no path the
    hook compares has one. The hook reads the same way, so a list written here
    is read in full there.
    """
    globs: List[str] = []
    for value in values or ():
        text = (value or "").strip()
        if text.startswith(("/", "\\", "~")) or re.match(r"^[A-Za-z]:", text) or os.path.isabs(text):
            raise InstallError(
                f"--include {text} is absolute; give a glob relative to the directory, such as repos/acme-billing/**"
            )
        if ".." in text:
            raise InstallError(f"--include {text} contains '..'; a glob relative to the directory cannot leave it")
        if not text:
            raise InstallError("--include was given an empty glob, which would match nothing")
        text = "/".join(segment for segment in text.replace("\\", "/").split("/") if segment not in ("", "."))
        if not text:
            raise InstallError(
                f"--include {value.strip()} names only the directory itself, which no glob matches; name what is "
                "inside it, such as repos/acme-billing/**"
            )
        if text not in globs:
            globs.append(text)
    return globs


def read_json_object(path: Path) -> Dict[str, Any]:
    """A settings file as an object, or an InstallError: a file that cannot be read is never overwritten."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise InstallError(f"{path} could not be read ({type(error).__name__}), so it was left alone") from None
    if not text.strip():
        return {}
    try:
        document = json.loads(text)
    except ValueError:
        raise InstallError(f"{path} is not valid JSON, so it was left alone; fix it and install again") from None
    if not isinstance(document, dict):
        raise InstallError(f"{path} is not a JSON object, so it was left alone")
    return document


def dump_json(document: Dict[str, Any]) -> str:
    return json.dumps(document, indent=2) + "\n"


def _text_or_none(path: Path) -> Optional[str]:
    """A file's text as UTF-8, or None when it is not, such as a .threefold.json Windows PowerShell wrote in UTF-16."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


# --- the plan ---------------------------------------------------------------------------

class Plan:
    """Steps described first and carried out second, so --dry-run is the same code minus the writes."""

    def __init__(self, dry_run: bool, out: Any) -> None:
        self.dry_run = dry_run
        self.out = out
        self.steps: List[Tuple[str, Optional[Callable[[], None]]]] = []

    def add(self, description: str, action: Optional[Callable[[], None]] = None) -> None:
        self.steps.append((description, action))

    def run(self) -> None:
        prefix = "would " if self.dry_run else ""
        for description, action in self.steps:
            print(f"threefold: {prefix}{description}" if action else f"threefold: {description}", file=self.out)
            if action and not self.dry_run:
                action()


def _write_text(path: Path, text: str) -> Callable[[], None]:
    def action() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return action


def _write_bytes(path: Path, data: bytes) -> Callable[[], None]:
    def action() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return action


def _copy(source: Path, target: Path) -> Callable[[], None]:
    def action() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return action


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest(path: Path) -> Dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _merged_manifest(old: Dict[str, Any]) -> Dict[str, Any]:
    originals = dict(old.get("originals") or {})
    # A record written before originals were kept had the old config as text.
    if old.get("previous_config") is not None and CONFIG_FILE not in originals:
        originals[CONFIG_FILE] = base64.b64encode(str(old["previous_config"]).encode("utf-8")).decode("ascii")
    return {
        "version": 2,
        "created_files": list(old.get("created_files") or []),
        "created_dirs": list(old.get("created_dirs") or []),
        "entries": list(old.get("entries") or []),
        "pre_commit": old.get("pre_commit") or {"action": "none"},
        "exclude_lines": list(old.get("exclude_lines") or []),
        # The bytes of each file that was there before the install changed it,
        # and a digest of what the install last wrote there. An uninstall puts
        # the old bytes back when the file still holds exactly the install's.
        "originals": originals,
        "installed": dict(old.get("installed") or {}),
    }


def _planned_write(manifest: Dict[str, Any], key: str, path: Path, data: bytes) -> Callable[[], None]:
    """A write the manifest remembers: the bytes before the first install touched it, and what went in."""
    if path.is_file() and key not in manifest["originals"] and key not in manifest["created_files"]:
        manifest["originals"][key] = base64.b64encode(path.read_bytes()).decode("ascii")
    manifest["installed"][key] = _digest(data)
    return _write_bytes(path, data)


def is_tracked(root: Path, relative: str) -> bool:
    """Whether git tracks this file. An install writes the developer's own paths, which must not be committed."""
    code, _ = git(root, "ls-files", "--error-unmatch", "--", relative, check=False)
    return code == 0


# --- install ------------------------------------------------------------------------------

def shared_copies(plan: Plan, home: Path) -> None:
    targets = [(HOOK_SOURCE, home / "bin" / "threefold_hook.py"), (CLI_SOURCE, home / "bin" / "threefold_cli.py")]
    engine = SOURCE / "threefold"
    targets.append((engine / "__init__.py", home / "lib" / "threefold" / "__init__.py"))
    targets.extend((module, home / "lib" / "threefold" / "domain" / module.name) for module in sorted((engine / "domain").glob("*.py")))
    changed = [(source, target) for source, target in targets if not target.is_file() or target.read_bytes() != source.read_bytes()]
    if not changed:
        plan.add(f"the shared copies in {forward(home)} are current")
        return

    def action() -> None:
        for source, target in changed:
            _copy(source, target)()

    plan.add(f"copy the hook, the pre-commit check and the engine into {forward(home)} ({len(changed)} file(s))", action)


def _created_dirs(root: Path, relative: str) -> List[str]:
    """The directories above a repository file that do not exist yet, outermost first."""
    missing = []
    parent = (root / relative).parent
    while parent != root and not parent.exists():
        missing.append(forward(parent.relative_to(root)))
        parent = parent.parent
    return list(reversed(missing))


def workspace_note(root: Path) -> str:
    return (
        f"workspace mode: git does not recognise {forward(root)} as a repository, so only {CONFIG_FILE} and the agents' "
        "hook settings are written there and nothing under .git. No pre-commit hook is installed, so no commit-time "
        "check runs for work committed from inside it; a repository within it gets one from its own install"
    )


def install(
    args: argparse.Namespace, root: Path, home: Path, out: Any, workspace: bool = False, includes: Sequence[str] = ()
) -> int:
    agents = [name.strip() for name in args.agents.split(",") if name.strip()]
    unknown = [name for name in agents if name not in AGENTS]
    if unknown or not agents:
        raise InstallError(f"--agents takes a comma-separated list of {', '.join(AGENTS)}")
    if args.endpoint and not args.endpoint.lower().startswith(("https://", "http://")):
        raise InstallError("--endpoint must be an http(s) URL")

    manifest_path = record_path(root, home, workspace)
    manifest = _merged_manifest(load_manifest(manifest_path))
    plan = Plan(args.dry_run, out)
    written: List[str] = []
    if workspace:
        manifest["workspace"] = forward(root)
        plan.add(workspace_note(root))

    def tracked(relative: str) -> bool:
        # Outside a repository git recognises there is nothing to be tracked
        # by, and nothing to ask: git is not run against a workspace at all.
        return False if workspace else is_tracked(root, relative)

    shared_copies(plan, home)

    # The repository's configuration. Never the key: only where to read it.
    config: Dict[str, Any] = {"project": args.project, "mode": args.mode}
    endpoint = ""
    key_file: Optional[Path] = None
    if args.endpoint:
        endpoint = args.endpoint if args.endpoint.endswith("/") else args.endpoint + "/"
        config["endpoint"] = endpoint
    if args.api_key_file:
        key_file = Path(os.path.expanduser(args.api_key_file)).resolve()
        if not key_file.is_file():
            plan.add(f"note: {forward(key_file)} does not exist yet; the hook sends no key until it does")
        config["api_key_file"] = forward(key_file)
    # Only when asked for: a file without the key is read exactly as before
    # include existed, and an empty list would say the same thing less plainly.
    scope = ""
    if includes:
        config["include"] = list(includes)
        scope = f", sending only calls inside {', '.join(includes)}"
    config_path = root / CONFIG_FILE
    config_text = dump_json(config)
    if tracked(CONFIG_FILE):
        # Writing it would put this machine's key path in a committed file,
        # and an uninstall could never tell the team's version from ours.
        plan.add(
            f"{CONFIG_FILE} is tracked by git, so it was left as it is: the committed file decides the project and "
            f"the mode here. Change it in a commit, or set THREEFOLD_PROJECT, if it should say {args.project}"
        )
        if includes:
            # Said on its own line: an exit code of 0 and no word about the
            # list read as though the list were in force.
            plan.add(
                f"the include list was not written either, so the hook sends what the committed {CONFIG_FILE} "
                f"allows; add \"include\": {json.dumps(list(includes))} to it in a commit if only those should be sent"
            )
    elif config_path.is_file() and _text_or_none(config_path) == config_text:
        plan.add(f"{CONFIG_FILE} is current{scope}")
        written.append(CONFIG_FILE)
    else:
        if not config_path.exists() and CONFIG_FILE not in manifest["created_files"]:
            manifest["created_files"].append(CONFIG_FILE)
        plan.add(
            f"write {CONFIG_FILE} for {args.project} in {args.mode} mode{scope}",
            _planned_write(manifest, CONFIG_FILE, config_path, config_text.encode("utf-8")),
        )
        written.append(CONFIG_FILE)

    if endpoint and key_file is not None:
        trusted_endpoint_step(plan, home, endpoint, key_file)

    # One entry per agent, merged into whatever the file already holds.
    for agent in agents:
        relative, matcher = AGENT_SETTINGS[agent]
        path = root / relative
        if tracked(relative):
            # Codex and Antigravity project hooks are usually committed. The
            # entry names this machine's Python and home folder, so adding it
            # would put them in the next commit, and .git/info/exclude cannot
            # keep a tracked file out of `git commit -a`.
            plan.add(
                f"{relative} is tracked by git, so the hook for {agent} was not added to it: the entry holds this "
                f"machine's paths and would be committed. Add `{hook_command(home, agent)}` in that agent's own "
                "settings outside the repository instead"
            )
            continue
        document = read_json_object(path) if path.exists() else {}
        hooks = document.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise InstallError(f"{relative} has a \"hooks\" that is not an object, so it was left alone")
        entries = hooks.setdefault("PreToolUse", [])
        if not isinstance(entries, list):
            raise InstallError(f"{relative} has a \"PreToolUse\" that is not a list, so it was left alone")
        command = hook_command(home, agent)
        present = any(
            isinstance(entry, dict) and isinstance(entry.get("hooks"), list)
            and any(isinstance(item, dict) and item.get("command") == command for item in entry["hooks"])
            for entry in entries
        )
        if present:
            # Listed in exclude only if an earlier install of ours wrote it: an
            # entry someone added by hand is in a file this script never wrote.
            if any(item.get("file") == relative for item in manifest["entries"]):
                written.append(relative)
            plan.add(f"{relative} already runs the hook for {agent}")
            continue
        written.append(relative)
        entries.append({"matcher": matcher, "hooks": [{"type": "command", "command": command}]})
        if not path.exists():
            for directory in _created_dirs(root, relative):
                if directory not in manifest["created_dirs"]:
                    manifest["created_dirs"].append(directory)
            if relative not in manifest["created_files"]:
                manifest["created_files"].append(relative)
        manifest["entries"].append({"file": relative, "command": command})
        plan.add(
            f"register the hook for {agent} in {relative}",
            _planned_write(manifest, relative, path, dump_json(document).encode("utf-8")),
        )

    if not workspace:
        # A workspace has no .git git would read: a hook written there would
        # never run, and a folder git does not accept is not ours to write in.
        pre_commit_step(plan, root, home, manifest)
        exclude_step(plan, root, written, manifest)

    plan.add(f"record what was installed in {forward(manifest_path)}", _write_text(manifest_path, dump_json(manifest)))
    plan.run()
    if "codex" in agents:
        print(f"threefold: {CODEX_TRUST_NOTE}", file=out)
    return 0


def trusted_endpoint_step(plan: Plan, home: Path, endpoint: str, key_file: Path) -> None:
    """Pairs the endpoint with its key file in THREEFOLD_HOME/config.json.

    The hook sends a key to an endpoint named only in .threefold.json when the
    owner has paired the two at home, and never otherwise, so a cloned
    repository cannot aim the owner's key at a server of its choosing. This is
    the owner saying, once, that this endpoint is theirs.
    """
    path = home / HOME_CONFIG
    document = read_json_object(path) if path.exists() else {}
    pairs = document.get("trusted_endpoints")
    if pairs is None:
        pairs = []
    if not isinstance(pairs, list):
        raise InstallError(f"{forward(path)} has a \"trusted_endpoints\" that is not a list, so it was left alone")
    entry = {"endpoint": endpoint, "api_key_file": forward(key_file)}
    if entry in pairs:
        plan.add(f"{endpoint} is already paired with its key file in {forward(path)}")
        return
    document["trusted_endpoints"] = pairs + [entry]
    plan.add(
        f"pair {endpoint} with its key file in {forward(path)}, so the hook sends the key there and nowhere a "
        "repository names on its own",
        _write_text(path, dump_json(document)),
    )


def pre_commit_script(home: Path) -> str:
    cli = quoted(home / "bin" / "threefold_cli.py")
    return (
        "#!/bin/sh\n"
        f"{PRE_COMMIT_MARKER}; --uninstall removes it.\n"
        "# A pre-commit hook that was here before runs first, unchanged.\n"
        f'chained="$(dirname "$0")/{CHAINED_NAME}"\n'
        'if [ -f "$chained" ]; then "$chained" "$@" || exit $?; fi\n'
        f'if [ -f {cli} ]; then\n'
        f'  {quoted(sys.executable)} {cli} check --repo "$(git rev-parse --show-toplevel)" || exit $?\n'
        "else\n"
        '  echo "threefold: the pre-commit check is missing from THREEFOLD_HOME, so this commit was not checked." >&2\n'
        "fi\n"
    )


def pre_commit_step(plan: Plan, root: Path, home: Path, manifest: Dict[str, Any]) -> None:
    code, hooks_path = git(root, "config", "--get", "core.hooksPath", check=False)
    if code == 0 and hooks_path:
        plan.add(
            f"core.hooksPath is set to {hooks_path}, so git does not read .git/hooks and no pre-commit hook was "
            f"installed; add `{quoted(sys.executable)} {quoted(home / 'bin' / 'threefold_cli.py')} check` to the "
            "hooks there by hand"
        )
        return
    hook_path = git_path(root, "hooks") / "pre-commit"
    script = pre_commit_script(home)

    def write_hook(chain: bool) -> Callable[[], None]:
        def action() -> None:
            if chain:
                os.replace(hook_path, hook_path.with_name(CHAINED_NAME))
            _write_text(hook_path, script)()
            os.chmod(hook_path, 0o755)
        return action

    if hook_path.is_file():
        current = hook_path.read_text(encoding="utf-8", errors="replace")
        if PRE_COMMIT_MARKER in current:
            if current == script:
                plan.add("the pre-commit hook is current")
            else:
                plan.add("update the pre-commit hook", write_hook(False))
            return
        manifest["pre_commit"] = {"action": "chained"}
        plan.add(f"keep the existing pre-commit hook as {CHAINED_NAME}, run it first, then the check", write_hook(True))
        return
    manifest["pre_commit"] = {"action": "created"}
    plan.add("install a pre-commit hook that runs the check", write_hook(False))


def exclude_step(plan: Plan, root: Path, written: Sequence[str], manifest: Dict[str, Any]) -> None:
    exclude = git_path(root, "info/exclude")
    try:
        existing = exclude.read_text(encoding="utf-8").splitlines()
    except OSError:
        existing = []
    wanted = [f"/{relative}" for relative in written]
    missing = [line for line in wanted if line not in existing]
    if not missing:
        plan.add("the files written are already listed in .git/info/exclude")
        return
    lines = ([EXCLUDE_MARKER] if EXCLUDE_MARKER not in existing else []) + missing
    for line in lines:
        if line not in manifest["exclude_lines"]:
            manifest["exclude_lines"].append(line)
    current = exclude.read_bytes() if exclude.is_file() else b""
    if current and not current.endswith(b"\n"):
        current += b"\n"
    if not exclude.is_file() and EXCLUDE_KEY not in manifest["created_files"]:
        manifest["created_files"].append(EXCLUDE_KEY)
    data = current + ("\n".join(lines) + "\n").encode("utf-8")
    plan.add(f"list {', '.join(missing)} in .git/info/exclude", _planned_write(manifest, EXCLUDE_KEY, exclude, data))


# --- uninstall ------------------------------------------------------------------------------

def _is_ours(item: Any, commands: Sequence[str]) -> bool:
    if not isinstance(item, dict):
        return False
    command = item.get("command")
    if commands:
        return command in commands
    return isinstance(command, str) and "threefold_hook.py" in command and "--agent" in command


def _without_our_entries(document: Dict[str, Any], commands: Sequence[str]) -> Tuple[Dict[str, Any], int]:
    """The settings with this installer's hook entries taken out, and how many went."""
    removed = 0
    hooks = document.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get("PreToolUse"), list):
        return document, 0
    kept_entries = []
    for entry in hooks["PreToolUse"]:
        if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
            kept = [item for item in entry["hooks"] if not _is_ours(item, commands)]
            removed += len(entry["hooks"]) - len(kept)
            if not kept:
                continue
            entry = dict(entry, hooks=kept)
        kept_entries.append(entry)
    if kept_entries:
        hooks["PreToolUse"] = kept_entries
    else:
        del hooks["PreToolUse"]
    if not hooks:
        del document["hooks"]
    return document, removed


def uninstall(args: argparse.Namespace, root: Path, home: Path, out: Any, workspace: bool = False) -> int:
    manifest_path = record_path(root, home, workspace)
    manifest = load_manifest(manifest_path)
    exact = bool(manifest)
    manifest = _merged_manifest(manifest)
    plan = Plan(args.dry_run, out)
    if workspace:
        plan.add(f"workspace mode: {forward(root)} is not a repository git recognises, so nothing under .git is touched")
    if not exact:
        plan.add("no install record was found, so only entries, hooks and lines marked as Threefold's are removed")

    created = set(manifest["created_files"])
    by_file: Dict[str, List[str]] = {}
    for entry in manifest["entries"]:
        by_file.setdefault(entry["file"], []).append(entry["command"])
    files = by_file if exact else {relative: [] for relative, _ in AGENT_SETTINGS.values()}
    for relative, commands in files.items():
        path = root / relative
        if not path.is_file():
            continue
        original = _restorable(manifest, relative, path)
        if original is not None:
            plan.add(f"put {relative} back exactly as it was before the install", _write_bytes(path, original))
            continue
        if relative in created and _unchanged_since_install(manifest, relative, path):
            plan.add(f"delete {relative}, which the install created", path.unlink)
            continue
        try:
            document = read_json_object(path)
        except InstallError as error:
            plan.add(f"note: {error}")
            continue
        remaining, removed = _without_our_entries(document, commands)
        if not removed:
            continue
        if relative in created and not remaining:
            plan.add(f"delete {relative}, which the install created", path.unlink)
        else:
            # Changed by hand since the install, so the old bytes would lose
            # that change: only the entry the install added comes out.
            plan.add(f"remove the hook entry from {relative}, which has changed since the install", _write_text(path, dump_json(remaining)))

    config_path = root / CONFIG_FILE
    original = _restorable(manifest, CONFIG_FILE, config_path)
    if original is not None:
        plan.add(f"restore the {CONFIG_FILE} that was there before", _write_bytes(config_path, original))
    elif CONFIG_FILE in created and config_path.is_file():
        plan.add(f"delete {CONFIG_FILE}", config_path.unlink)
    elif CONFIG_FILE in manifest["originals"] and config_path.is_file():
        plan.add(f"note: {CONFIG_FILE} has changed since the install, so it was left as it is rather than put back")
    elif not exact and config_path.is_file():
        plan.add(f"note: {CONFIG_FILE} was left in place, because without the install record nothing says the install wrote it")

    if not workspace:
        # A workspace install wrote nothing under .git, so there is nothing
        # there to take out, and nothing git does not recognise is read.
        git_side_uninstall(plan, root, manifest, exact)

    for directory in reversed(manifest["created_dirs"]):
        path = root / directory

        def remove_if_empty(path: Path = path) -> None:
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()

        plan.add(f"remove {directory}/ if the install left it empty", remove_if_empty)

    if manifest_path.is_file():
        plan.add("delete the install record", manifest_path.unlink)
        if workspace:

            def remove_installs_if_empty(directory: Path = manifest_path.parent) -> None:
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()

            plan.add(f"remove {forward(manifest_path.parent)}/ if no other workspace record is left in it", remove_installs_if_empty)
    plan.add(
        f"note: the shared copies in {forward(home)}, and any endpoint paired with a key file in its "
        f"{HOME_CONFIG}, were kept, because other repositories may use them; delete them by hand once none does"
    )
    plan.run()
    return 0


def git_side_uninstall(plan: Plan, root: Path, manifest: Dict[str, Any], exact: bool) -> None:
    """The pre-commit hook and the .git/info/exclude lines, which only a repository install adds."""
    created = set(manifest["created_files"])
    hook_path = git_path(root, "hooks") / "pre-commit"
    chained = hook_path.with_name(CHAINED_NAME)
    if hook_path.is_file() and PRE_COMMIT_MARKER in hook_path.read_text(encoding="utf-8", errors="replace"):
        if chained.is_file():
            plan.add("put back the pre-commit hook that was there before", lambda: os.replace(chained, hook_path))
        else:
            plan.add("remove the pre-commit hook", hook_path.unlink)

    exclude = git_path(root, "info/exclude")
    original = _restorable(manifest, EXCLUDE_KEY, exclude)
    if original is not None:
        plan.add("put .git/info/exclude back exactly as it was before the install", _write_bytes(exclude, original))
    elif EXCLUDE_KEY in created and _unchanged_since_install(manifest, EXCLUDE_KEY, exclude):
        plan.add("delete .git/info/exclude, which the install created", exclude.unlink)
    elif exclude.is_file():
        lines = exclude.read_text(encoding="utf-8").splitlines()
        if exact:
            ours = set(manifest["exclude_lines"])
        elif EXCLUDE_MARKER in lines:
            ours = {EXCLUDE_MARKER, f"/{CONFIG_FILE}"} | {f"/{relative}" for relative, _ in AGENT_SETTINGS.values()}
        else:
            ours = set()
        kept = [line for line in lines if line not in ours]
        if len(kept) != len(lines):
            plan.add("take the install's lines out of .git/info/exclude", _write_text(exclude, "\n".join(kept) + ("\n" if kept else "")))


def _unchanged_since_install(manifest: Dict[str, Any], key: str, path: Path) -> bool:
    return path.is_file() and manifest["installed"].get(key) == _digest(path.read_bytes())


def _restorable(manifest: Dict[str, Any], key: str, path: Path) -> Optional[bytes]:
    """The bytes a file held before the install, when it still holds exactly what the install wrote."""
    original = manifest["originals"].get(key)
    if original is None or not path.is_file():
        return None
    # A record from before digests were kept restored unconditionally, and
    # still does; a digest that no longer matches means someone changed it.
    if key in manifest["installed"] and not _unchanged_since_install(manifest, key, path):
        return None
    try:
        return base64.b64decode(original)
    except ValueError:
        return None


# --- entry point -----------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None, out: Any = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="threefold_install.py", description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo", required=True, help="the repository to govern")
    parser.add_argument("--project", help="its alias, Acme-<name>; required to install")
    parser.add_argument("--agents", default=",".join(AGENTS), help="comma-separated, default all three")
    parser.add_argument("--mode", choices=("observe", "enforce"), default="observe")
    parser.add_argument("--endpoint", help="the service, default the public stack")
    parser.add_argument("--api-key-file", help="a file holding the key; its path is written, never its content")
    parser.add_argument(
        "--include", action="append", metavar="GLOB",
        help="repeatable; a glob relative to the directory, and only calls inside the globs given are sent",
    )
    parser.add_argument("--uninstall", action="store_true", help="remove what an install added")
    parser.add_argument("--dry-run", action="store_true", help="print every step and write nothing")
    args = parser.parse_args(argv)

    try:
        root, workspace = locate(Path(args.repo))
        home = threefold_home()
        if args.uninstall:
            return uninstall(args, root, home, out, workspace)
        if not args.project or not PROJECT_PATTERN.match(args.project):
            # The alias is what the public ledger shows. A real name typed here
            # would be published by the first tool call.
            raise InstallError("--project must match ^Acme-[A-Za-z0-9-]{1,40}$, an alias rather than a real name")
        includes = include_globs(args.include)
        named = Path(args.repo).resolve()
        if includes and os.path.normcase(str(named)) != os.path.normcase(str(root)):
            # The globs were written relative to the directory named, and the
            # hook reads them relative to the one holding .threefold.json.
            # Written at the top of the checkout instead, they would name other
            # directories, or none, and nothing would say so.
            raise InstallError(
                f"--include globs are relative to the directory given, but {forward(named)} is inside the repository "
                f"{forward(root)}, and .threefold.json would be written there instead; give --repo {forward(root)} "
                "with globs relative to it"
            )
        return install(args, root, home, out, workspace, includes)
    except InstallError as error:
        print(f"threefold: {error}. Nothing was changed.", file=out)
        return 2


if __name__ == "__main__":
    sys.exit(main())
