#!/usr/bin/env python3
"""Puts Threefold in front of the coding agents of one repository, and takes it out again.

    threefold_install.py connect [PATH] [--project NAME] [--agents auto|LIST]
        [--mode managed|observe|enforce] [--endpoint URL] [--api-key-file F]
        [--include GLOB ...] [--dry-run] [--no-open]
    threefold_install.py disconnect [PATH] [--dry-run]
    threefold_install.py status
    threefold_install.py open [--next /projects/NAME] [--endpoint URL]

and the older form, which keeps working exactly as it did:

    threefold_install.py --repo PATH --project Acme-Payments
        [--agents claude-code,codex,antigravity] [--mode observe|managed|enforce]
        [--endpoint URL] [--api-key-file PATH] [--include GLOB ...]
        [--uninstall] [--dry-run]

`connect` is the one command a team runs. Every part has a default: PATH is
the current directory, the project is `Acme-<folder name>` made to fit the
alias pattern, the agents are the ones found on this machine (Claude Code by
~/.claude or `claude` on PATH, Codex by ~/.codex or `codex`, Antigravity by
~/.gemini/antigravity, ~/.antigravity or `antigravity`, all three when none is
found), and the mode is managed, so the project's stage on the dashboard
decides and every project starts in Observe. Run again on a connected folder,
it keeps the project, mode and include list it finds there unless told
otherwise. It ends by sending one harmless dry-run call, printing whether the
stack recorded it, and opening the dashboard on the project: through a
sign-in link when an operator key is configured for the endpoint, so nobody
pastes a key into a browser. `status` lists every install on this machine and
asks the stack for each project's stage; `open` signs in to the dashboard on
its own; `disconnect` is the uninstall below.

What an install does, in order:

1. Puts the hook, the pre-commit check and the engine that check runs on into
   THREEFOLD_HOME (`~/.threefold` unless set): `bin/threefold_hook.py`,
   `bin/threefold_cli.py` and `lib/threefold/`. One shared copy serves every
   repository, so updating Threefold is one install, not one per repository.
   From a checkout they are copied from the source tree. The copy a stack
   serves at /install.py has that stack's URL baked in; it downloads them
   from the stack's /dist/threefold-bundle.zip instead, writes a file only
   when its sha256 matches the stack's /dist/manifest.json, and keeps a copy
   of itself as `bin/threefold_install.py` for `status`, `open` and
   `disconnect` later.
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
6. Notes the install in `THREEFOLD_HOME/installs/index.json`, which `status`
   reads and an uninstall prunes.

A file git already tracks is never written. Codex and Antigravity project hook
files are usually committed, and the entry names this machine's Python and
home folder, so writing it would put those paths in the next `git commit -a`;
.git/info/exclude cannot keep a tracked file out. The output says which file
was left alone and what to add by hand instead. With `--endpoint` and
`--api-key-file` together, the pair is also written to THREEFOLD_HOME/config.json,
which is what lets the hook send the key to an endpoint a repository names.

The older form's mode defaults to observe, as it always has; `connect`'s to
managed, whose projects start in Observe on the stack. Either way a rule set
introduced to a team shows what it would stop before it stops anything.

`--uninstall` (or `disconnect`) removes exactly what an install added, from a
record kept in the git directory. A file that was there before is put back
byte for byte, from the copy the record keeps, as long as it still holds
exactly what the install wrote; one changed by hand since keeps the change and
loses only the install's entry. The shared copies in THREEFOLD_HOME stay,
because other repositories may be using them. `--dry-run` prints every step
and writes nothing at all, here or on the stack: no download, no call, no
browser.

`--include GLOB`, repeatable, writes an `include` list into `.threefold.json`:
globs relative to that directory, and the hook then sends only calls inside
them. A glob that is empty, only `.`, absolute or contains `..` is refused,
because it could name nothing inside the directory. So is `--include` with a
directory below the top of a repository: the file goes to the top, where the
globs would be read relative to a directory other than the one they were
written for.

A directory git does not recognise as a repository, such as a workspace root
that holds several repositories, is installed in workspace mode instead of
being refused. Only `.threefold.json` and the three agents' settings files are
written there, merged as above; there is no pre-commit hook, so nothing checks
a commit, and nothing is written under any `.git`, including a `.git` folder
git itself does not accept. The install record is kept in
`THREEFOLD_HOME/installs/<16 hex of the directory's path>.json`, so an uninstall
still removes exactly what was added. With `--include` this is how a
workspace governs only the repositories the owner chose. The home folder
itself is refused: its `.claude` and `.codex` are the agents' own
configuration for every project, not a project's.

Codex reads a project's hooks only when that project is trusted in Codex. This
script does not edit `~/.codex/config.toml` to trust it: that is a decision
about the whole machine, and it is the developer's.

Standard library only, and ASCII only, so it survives being piped into Python
by a shell that re-encodes what it pipes.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import importlib.util
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# In a checkout this file is src/threefold/tools/threefold_install.py, beside
# the CLI, one folder from the hook and two from the engine's package root.
# The stack serves it from there too, so these are the only paths it derives.
# Piped into Python (`curl ... | python3 - connect`) there is no file at all,
# and so nothing beside it to copy: only a served copy can install from there.
try:
    HERE: Optional[Path] = Path(__file__).resolve().parent
except NameError:
    HERE = None
SOURCE: Optional[Path] = HERE.parents[1] if HERE is not None and len(HERE.parents) > 1 else None
HOOK_SOURCE: Optional[Path] = SOURCE / "threefold" / "hooks" / "threefold_hook.py" if SOURCE is not None else None
CLI_SOURCE: Optional[Path] = HERE / "threefold_cli.py" if HERE is not None else None

# The stack replaces this with its own URL when it serves the file at
# /install.py. Replaced, the copy is a served one: its endpoint is the stack
# that served it, and it installs from that stack's bundle rather than from a
# source tree it does not have. The comparison value below is spelled in two
# pieces so the stack's replacement cannot reach it too.
BAKED_ENDPOINT = "__THREEFOLD_ENDPOINT__"
_UNBAKED = "__THREEFOLD" "_ENDPOINT__"

PROJECT_PATTERN = re.compile(r"^Acme-[A-Za-z0-9-]{1,40}$")
AGENTS = ("claude-code", "codex", "antigravity")
MODES = ("managed", "observe", "enforce")
COMMANDS = ("connect", "disconnect", "status", "open")

# The file each agent reads its project hooks from, and the tools that reach
# the hook. The same matchers the enforcement measurement of 2026-09-21 used.
AGENT_SETTINGS = {
    "claude-code": (".claude/settings.local.json", "Write|Edit|MultiEdit|NotebookEdit|Bash"),
    "codex": (".codex/hooks.json", "apply_patch|Edit|Write|Bash"),
    "antigravity": (".agents/hooks.json", "write_to_file|replace_file_content|multi_replace_file_content|run_command"),
}

# How each agent shows that it is installed: a folder in the home folder, or
# its command on PATH. ~/.claude and ~/.codex are the folders the hook already
# treats as those agents' own; Antigravity keeps its state under ~/.gemini,
# which the hook protects for the same reason, in an antigravity folder.
AGENT_SIGNS = {
    "claude-code": ((".claude",), "claude"),
    "codex": ((".codex",), "codex"),
    "antigravity": ((".gemini/antigravity", ".antigravity"), "antigravity"),
}

CONFIG_FILE = ".threefold.json"
HOME_CONFIG = "config.json"
EXCLUDE_KEY = "git:info/exclude"
MANIFEST_NAME = "threefold-install.json"
INSTALLS_DIR = "installs"
INDEX_NAME = "index.json"
PRE_COMMIT_MARKER = "# threefold pre-commit: installed by threefold_install.py"
CHAINED_NAME = "pre-commit.before-threefold"
EXCLUDE_MARKER = "# threefold: files written by threefold_install.py"

# What a served bundle may put in THREEFOLD_HOME, and nothing else: a path the
# manifest names that is not one of these is refused before anything is
# written, so no bundle can place a file outside bin/ and lib/threefold/.
BUNDLE_PATH = re.compile(
    r"^(?:bin/threefold_(?:hook|cli)\.py|lib/threefold/__init__\.py|lib/threefold/domain/[A-Za-z0-9_]{1,64}\.py)$"
)
REQUIRED_BUNDLE_FILES = ("bin/threefold_hook.py", "bin/threefold_cli.py", "lib/threefold/__init__.py")
INSTALLER_COPY = "bin/threefold_install.py"
MAX_BUNDLE_BYTES = 20_000_000
MAX_BUNDLE_FILE_BYTES = 5_000_000
MAX_JSON_BYTES = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 4.0
DOWNLOAD_TIMEOUT_SECONDS = 20.0
SESSION_PREFIX = "threefold-connect-"
NEXT_ROUTE = re.compile(r"^/[A-Za-z0-9/_\-]{0,200}$")

CODEX_TRUST_NOTE = (
    "Codex loads a project's .codex/hooks.json only when the project is trusted in Codex. "
    "This installer does not edit ~/.codex/config.toml; trust the project from Codex itself."
)


class InstallError(Exception):
    """Something that stops the install before anything is written."""


class Unreachable(Exception):
    """The stack could not be asked: no connection, a timeout, or an answer too large to read."""


# --- small helpers -------------------------------------------------------------------

def threefold_home() -> Path:
    configured = (os.environ.get("THREEFOLD_HOME") or "").strip()
    return Path(os.path.expanduser(configured or os.path.join("~", ".threefold"))).resolve()


def user_home() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def forward(path: Any) -> str:
    return str(path).replace("\\", "/")


def quoted(path: Any) -> str:
    """A path for a command line: forward slashes, and quotes only when a space needs them."""
    text = forward(path)
    return f'"{text}"' if " " in text else text


def with_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


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


def refuse_home_folder(root: Path) -> None:
    """The home folder and a drive's root are not projects.

    Installed there, `.claude/settings.local.json` and `.codex/hooks.json` land
    in the agents' own configuration folders, where they govern every project
    on the machine under one project's name, which nobody asked for.
    """
    if os.path.normcase(str(root)) == os.path.normcase(str(user_home())) or root.parent == root:
        raise InstallError(
            f"{forward(root)} is your home folder or a drive's root, where the agents keep their own settings for "
            "every project; connect a repository, or a folder that holds repositories, instead"
        )


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


def read_json_quietly(path: Path) -> Dict[str, Any]:
    """A file's JSON object, or empty for anything that is not one. For reading only, never before a write."""
    try:
        return read_json_object(path) if path.is_file() else {}
    except (InstallError, UnicodeDecodeError):
        return {}


def dump_json(document: Dict[str, Any]) -> str:
    return json.dumps(document, indent=2) + "\n"


def _text_or_none(path: Path) -> Optional[str]:
    """A file's text as UTF-8, or None when it is not, such as a .threefold.json Windows PowerShell wrote in UTF-16."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def python_command() -> str:
    return "python" if os.name == "nt" else "python3"


# --- the stack, over HTTP -----------------------------------------------------------------

def served_endpoint() -> Optional[str]:
    """The endpoint baked into a served copy, or None when this copy runs from a checkout."""
    value = BAKED_ENDPOINT.strip()
    if value == _UNBAKED or not value.lower().startswith(("https://", "http://")):
        return None
    return with_slash(value)


def api_timeout() -> float:
    """The hook's own THREEFOLD_TIMEOUT, so a slow stack is waited for exactly as long as the hook waits."""
    try:
        value = float((os.environ.get("THREEFOLD_TIMEOUT") or "").strip() or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def http_request(
    method: str, url: str, body: Optional[Dict[str, Any]] = None, key: Optional[str] = None,
    timeout: Optional[float] = None, limit: int = MAX_JSON_BYTES,
) -> Tuple[int, bytes]:
    """One request, returning (status, body). A refusal is a status; only no answer at all raises Unreachable.

    HTTPError is caught before URLError on purpose: it is a subclass, and the
    other order files every 401 under "could not be reached".
    """
    headers = {"Accept": "application/json", "User-Agent": "threefold-install"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["X-API-Key"] = key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout or api_timeout()) as response:
            raw = response.read(limit + 1)
            status = response.status
    except urllib.error.HTTPError as error:
        try:
            raw = error.read(limit + 1) or b""
        except (OSError, http.client.HTTPException):
            raw = b""
        finally:
            error.close()
        return error.code, raw[:limit]
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
        reason = getattr(error, "reason", error)
        raise Unreachable(type(reason).__name__ if isinstance(reason, BaseException) else str(reason)[:120]) from None
    if len(raw) > limit:
        raise Unreachable(f"the answer from {url} was larger than {limit} bytes")
    return status, raw


def http_json(method: str, url: str, body: Optional[Dict[str, Any]] = None, key: Optional[str] = None,
              timeout: Optional[float] = None) -> Tuple[int, Any]:
    """One request whose answer is read as JSON, or None when it is not JSON."""
    code, raw = http_request(method, url, body, key, timeout)
    try:
        return code, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return code, None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha(value: Any) -> Optional[str]:
    return value.lower() if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) else None


def fetch_bundle(endpoint: str) -> Tuple[List[Tuple[str, bytes]], Dict[str, Any]]:
    """Every file the stack's bundle holds, each checked against the stack's manifest, or an InstallError.

    The manifest's list decides what is read and where it goes, never the
    archive's own names: each file is read by the name the manifest gives, the
    name must be one Threefold installs, and its bytes must hash to the sha256
    the manifest gives, all before anything is written. A bundle that is short,
    swapped or cut off on the way installs nothing rather than half of itself.
    """
    timeout = max(api_timeout(), DOWNLOAD_TIMEOUT_SECONDS)
    try:
        code, manifest = http_json("GET", endpoint + "dist/manifest.json", timeout=timeout)
        if code != 200 or not isinstance(manifest, dict):
            raise InstallError(f"{endpoint}dist/manifest.json answered HTTP {code} rather than a manifest")
        code, bundle = http_request("GET", endpoint + "dist/threefold-bundle.zip", timeout=timeout, limit=MAX_BUNDLE_BYTES)
        if code != 200:
            raise InstallError(f"{endpoint}dist/threefold-bundle.zip answered HTTP {code}")
    except Unreachable as failure:
        raise InstallError(f"the hook could not be downloaded from {endpoint} ({failure})") from None

    whole = manifest.get("bundle_sha256")
    if whole is not None and _sha(whole) != _digest(bundle):
        raise InstallError("the bundle does not match the sha256 its manifest gives for it, so nothing was written")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise InstallError(f"{endpoint}dist/manifest.json lists no files")
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle))
    except (zipfile.BadZipFile, ValueError):
        raise InstallError("the bundle the stack served is not a zip file, so nothing was written") from None

    files: List[Tuple[str, bytes]] = []
    with archive:
        for entry in entries:
            path = entry.get("path") if isinstance(entry, dict) else None
            expected = _sha(entry.get("sha256")) if isinstance(entry, dict) else None
            if not isinstance(path, str) or not BUNDLE_PATH.match(path):
                raise InstallError(f"the manifest names {str(path)[:80]!r}, which is not a file Threefold installs, so nothing was written")
            if expected is None:
                raise InstallError(f"the manifest gives no sha256 for {path}, so nothing was written")
            if any(name == path for name, _ in files):
                continue
            try:
                info = archive.getinfo(path)
                if info.file_size > MAX_BUNDLE_FILE_BYTES:
                    raise InstallError(f"{path} in the bundle is larger than {MAX_BUNDLE_FILE_BYTES} bytes, so nothing was written")
                data = archive.read(info)
            except KeyError:
                raise InstallError(f"{path} is in the manifest but not in the bundle, so nothing was written") from None
            except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError, EOFError, ValueError):
                raise InstallError(f"{path} could not be read from the bundle, so nothing was written") from None
            size = entry.get("bytes")
            if _digest(data) != expected or (isinstance(size, int) and not isinstance(size, bool) and size != len(data)):
                raise InstallError(f"{path} in the bundle does not match the sha256 in the manifest, so nothing was written")
            files.append((path, data))
    missing = [path for path in REQUIRED_BUNDLE_FILES if not any(name == path for name, _ in files)]
    if missing:
        raise InstallError(f"the bundle has no {', '.join(missing)}, so nothing was written")
    return files, manifest


def own_source() -> Optional[bytes]:
    """The bytes of the file running now, when there is one to read."""
    try:
        return Path(__file__).read_bytes()
    except (NameError, OSError):
        return None


def installer_copy(endpoint: str, manifest: Dict[str, Any]) -> Optional[bytes]:
    """What to keep as bin/threefold_install.py: this file, or when piped, the stack's copy checked by its hash."""
    mine = own_source()
    if mine is not None:
        return mine
    expected = _sha(manifest.get("installer_sha256"))
    if expected is None:
        return None
    try:
        code, data = http_request("GET", endpoint + "install.py", timeout=max(api_timeout(), DOWNLOAD_TIMEOUT_SECONDS))
    except Unreachable:
        return None
    return data if code == 200 and _digest(data) == expected else None


# --- the plan ---------------------------------------------------------------------------

class Plan:
    """Steps described first and carried out second, so --dry-run is the same code minus the writes."""

    def __init__(self, dry_run: bool, out: Any, prefix: str = "threefold: ") -> None:
        self.dry_run = dry_run
        self.out = out
        self.prefix = prefix
        self.steps: List[Tuple[str, Optional[Callable[[], None]]]] = []

    def add(self, description: str, action: Optional[Callable[[], None]] = None) -> None:
        self.steps.append((description, action))

    def run(self) -> None:
        would = "would " if self.dry_run else ""
        for description, action in self.steps:
            print(f"{self.prefix}{would}{description}" if action else f"{self.prefix}{description}", file=self.out)
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


# --- the shared copies ------------------------------------------------------------------------

def shared_copies(plan: Plan, home: Path) -> None:
    """The hook, the check and the engine in THREEFOLD_HOME: from the stack when served, else from the checkout."""
    endpoint = served_endpoint()
    if endpoint:
        served_copies(plan, home, endpoint)
        return
    if HOOK_SOURCE is None or CLI_SOURCE is None or SOURCE is None or not HOOK_SOURCE.is_file() or not CLI_SOURCE.is_file():
        raise InstallError(
            "this copy of the installer has neither a stack's address baked in nor a Threefold checkout around it; "
            "take it from a stack's /install.py, or run it from src/threefold/tools in a checkout"
        )
    engine = SOURCE / "threefold"
    targets = [(HOOK_SOURCE, home / "bin" / "threefold_hook.py"), (CLI_SOURCE, home / "bin" / "threefold_cli.py")]
    targets.append((engine / "__init__.py", home / "lib" / "threefold" / "__init__.py"))
    targets.extend((module, home / "lib" / "threefold" / "domain" / module.name) for module in sorted((engine / "domain").glob("*.py")))
    changed = [(target, source.read_bytes()) for source, target in targets if not target.is_file() or target.read_bytes() != source.read_bytes()]
    _plan_copies(plan, home, changed, f"copy the hook, the pre-commit check and the engine into {forward(home)}")


def served_copies(plan: Plan, home: Path, endpoint: str) -> None:
    if plan.dry_run:
        # Nothing is fetched on a dry run either: it asks nothing of the stack.
        plan.add(
            f"download the hook, the pre-commit check and the engine from {endpoint}dist/threefold-bundle.zip, check "
            f"each file against {endpoint}dist/manifest.json, and put them in {forward(home)}",
            lambda: None,
        )
        return
    files, manifest = fetch_bundle(endpoint)
    targets = [(home / path, data) for path, data in files]
    copy = installer_copy(endpoint, manifest)
    if copy is not None:
        targets.append((home / INSTALLER_COPY, copy))
    changed = [(target, data) for target, data in targets if not target.is_file() or target.read_bytes() != data]
    _plan_copies(
        plan, home, changed,
        f"install the hook, the pre-commit check and the engine from {endpoint} into {forward(home)}, each file "
        "checked against the stack's manifest",
    )
    if copy is None:
        plan.add(f"note: no copy of the installer was kept in {forward(home / 'bin')}; take it again from {endpoint}install.py")


def _plan_copies(plan: Plan, home: Path, changed: List[Tuple[Path, bytes]], description: str) -> None:
    if not changed:
        plan.add(f"the shared copies in {forward(home)} are current")
        return

    def action() -> None:
        for target, data in changed:
            _write_bytes(target, data)()

    plan.add(f"{description} ({len(changed)} file(s))", action)


# --- the list of installs on this machine --------------------------------------------------------

def index_path(home: Path) -> Path:
    return home / INSTALLS_DIR / INDEX_NAME


def _index_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def load_index(home: Path) -> List[Dict[str, Any]]:
    """The installs this machine has, as written on install. A file that cannot be read lists none."""
    document = read_json_quietly(index_path(home))
    entries = document.get("installs")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("path"), str)]


def _index_text(entries: List[Dict[str, Any]]) -> str:
    return dump_json({"version": 1, "installs": entries})


def index_step(plan: Plan, home: Path, entry: Dict[str, Any]) -> None:
    """Adds or replaces this install's line in the index. Nothing is written when it already says this."""
    entries = load_index(home)
    others = [item for item in entries if _index_key(item["path"]) != _index_key(entry["path"])]
    updated = sorted(others + [entry], key=lambda item: _index_key(item["path"]))
    if updated == entries:
        return
    plan.add(f"note the install in {forward(index_path(home))}", _write_text(index_path(home), _index_text(updated)))


def unindex_step(plan: Plan, home: Path, root: Path) -> bool:
    """Takes this install out of the index, deleting the file when it lists nothing else. Whether it was listed."""
    entries = load_index(home)
    kept = [item for item in entries if _index_key(item["path"]) != _index_key(forward(root))]
    if len(kept) == len(entries):
        return False
    path = index_path(home)
    if kept:
        plan.add(f"take {forward(root)} out of {forward(path)}", _write_text(path, _index_text(kept)))
    else:
        plan.add(f"delete {forward(path)}, which lists no other install", path.unlink)
    return True


# --- install ------------------------------------------------------------------------------

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


def parse_agents(value: str) -> List[str]:
    agents = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in agents if name not in AGENTS]
    if unknown or not agents:
        raise InstallError(f"--agents takes a comma-separated list of {', '.join(AGENTS)}")
    return agents


def plan_install(
    args: argparse.Namespace, root: Path, home: Path, out: Any, workspace: bool = False,
    includes: Sequence[str] = (), prefix: str = "threefold: ",
) -> Tuple[Plan, List[str]]:
    """Every step of an install, described but not yet carried out, and the agents it registers."""
    agents = parse_agents(args.agents)
    if args.endpoint and not args.endpoint.lower().startswith(("https://", "http://")):
        raise InstallError("--endpoint must be an http(s) URL")

    manifest_path = record_path(root, home, workspace)
    manifest = _merged_manifest(load_manifest(manifest_path))
    plan = Plan(args.dry_run, out, prefix)
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
        endpoint = with_slash(args.endpoint)
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
    registered: List[str] = []
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
            registered.append(agent)
            plan.add(f"{relative} already runs the hook for {agent}")
            continue
        written.append(relative)
        registered.append(agent)
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
    # Every agent whose hook runs here now, from this install or an earlier
    # one: a second install for fewer agents takes none of the others out.
    recorded_files = {item.get("file") for item in manifest["entries"] if isinstance(item, dict)}
    running = [agent for agent in AGENTS if agent in registered or AGENT_SETTINGS[agent][0] in recorded_files]
    index_step(plan, home, {
        "path": forward(root), "project": args.project, "mode": args.mode, "agents": running, "workspace": workspace,
    })
    return plan, agents


def install(
    args: argparse.Namespace, root: Path, home: Path, out: Any, workspace: bool = False, includes: Sequence[str] = ()
) -> int:
    plan, agents = plan_install(args, root, home, out, workspace, includes)
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


def uninstall(
    args: argparse.Namespace, root: Path, home: Path, out: Any, workspace: bool = False, prefix: str = "threefold: "
) -> int:
    manifest_path = record_path(root, home, workspace)
    manifest = load_manifest(manifest_path)
    exact = bool(manifest)
    manifest = _merged_manifest(manifest)
    plan = Plan(args.dry_run, out, prefix)
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
    listed = unindex_step(plan, home, root)
    if workspace or listed:
        installs = home / INSTALLS_DIR

        def remove_installs_if_empty(directory: Path = installs) -> None:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()

        plan.add(f"remove {forward(installs)}/ if nothing else is left in it", remove_installs_if_empty)
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


# --- connect: the defaults ---------------------------------------------------------------------

def default_project(root: Path) -> str:
    """`Acme-<folder name>`, made to fit ^Acme-[A-Za-z0-9-]{1,40}$.

    Accents are taken off, anything else outside [A-Za-z0-9-] becomes a
    hyphen, runs of hyphens become one, and a leading `acme` word is dropped
    so acme-ledger becomes Acme-ledger rather than Acme-acme-ledger. A name
    with nothing left is named by a short hash of its path instead, which says
    nothing about it.
    """
    name = unicodedata.normalize("NFKD", root.name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^A-Za-z0-9-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    name = re.sub(r"^acme(?:-|$)", "", name, flags=re.IGNORECASE)
    name = name[:40].strip("-")
    if not name:
        name = "Repo-" + hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:8]
    return f"Acme-{name}"


def detect_agents(home: Optional[Path] = None, search_path: Optional[str] = None) -> List[Tuple[str, str]]:
    """The agents installed on this machine, each with the reason it was taken to be there."""
    home = home or user_home()
    found: List[Tuple[str, str]] = []
    for agent in AGENTS:
        folders, command = AGENT_SIGNS[agent]
        why = next((f"~/{folder} exists" for folder in folders if (home / folder).is_dir()), None)
        if why is None and shutil.which(command, path=search_path):
            why = f"{command} is on PATH"
        if why:
            found.append((agent, why))
    return found


def load_hook_module(home: Path) -> Optional[Any]:
    """The hook, for its configuration code: the installed copy, else the checkout's. None when neither loads."""
    for candidate in (home / "bin" / "threefold_hook.py", HOOK_SOURCE):
        if candidate is None or not candidate.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("threefold_hook_for_install", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception:  # noqa: BLE001 - a copy that does not load is one this command does without
            continue
    return None


def resolved_settings(hook: Optional[Any], directory: Path) -> Optional[Any]:
    """What the hook itself would send under from this directory: project, endpoint, key, and why not a key."""
    if hook is None:
        return None
    try:
        return hook.resolve_settings({"cwd": str(directory)})
    except Exception:  # noqa: BLE001 - reading configuration must not take the command down
        return None


def installer_command(home: Path) -> str:
    """How to run this installer again: the kept copy for a served one, this file for a checkout."""
    if served_endpoint() and (home / INSTALLER_COPY).is_file():
        return f"{python_command()} {quoted(home / INSTALLER_COPY)}"
    try:
        return f"{python_command()} {quoted(Path(__file__).resolve())}"
    except NameError:
        return f"{python_command()} threefold_install.py"


def open_url(url: str) -> bool:
    """Opens a page in the browser, and says whether one opened. A machine with no browser is not an error."""
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001
        return False


# --- connect: the proof and the dashboard ------------------------------------------------------

def first_call(endpoint: str, project: str, agent: str, mode: str, key: Optional[str], developer: str) -> Tuple[bool, str, Optional[str]]:
    """Sends one harmless dry-run call as a hook would, and says whether the stack recorded it.

    `git status` is read-only and a dry run is never refused and never trips a
    session, so the call proves the path from this machine to the ledger and
    changes nothing. Returns (recorded, a line to print, the project's stage).
    """
    body = {
        "session_id": SESSION_PREFIX + secrets.token_hex(4),
        "project_name": project,
        "developer": developer,
        "tool_name": "Bash",
        "action_type": "COMMAND_EXEC",
        "arguments": {"command": "git status"},
        "agent": agent,
        "origin": "hook",
        "explain": False,
        "dry_run": True,
        "hook_mode": mode,
    }
    try:
        code, document = http_json("POST", endpoint + "evaluate-tool-call", body, key)
    except Unreachable as failure:
        return False, (
            f"not recorded: {endpoint} could not be reached ({failure}). Until it can, the hook lets every call "
            "through, and your agents work as before"
        ), None
    if code == 200 and isinstance(document, dict) and document.get("verdict_id"):
        stage = document.get("project_stage")
        stage = stage if stage in ("observe", "enforce") else None
        line = f"recorded: {document.get('status') or 'judged'}, project stage {stage or 'not reported'}"
        warnings = [str(item)[:200] for item in document.get("warnings") or [] if isinstance(item, str)]
        return True, "; ".join([line] + warnings), stage
    if code in (401, 403):
        return False, (
            f"not recorded: the stack answered HTTP {code}; it needs its operator key. Connect again with "
            "--api-key-file naming the file that holds it"
        ), None
    title = document.get("title") if isinstance(document, dict) else None
    return False, f"not recorded: the stack answered HTTP {code}{' ' + str(title)[:120] if title else ''}", None


def signin_link(endpoint: str, route: str, key: str) -> Tuple[Optional[str], str]:
    """A single-use sign-in link to `route`, or (None, why not). The key goes in a header, never in the link."""
    try:
        code, document = http_json("POST", endpoint + "api/auth/links", {"next": route}, key)
    except Unreachable as failure:
        return None, f"no sign-in link: {endpoint} could not be reached ({failure})"
    url = document.get("url") if isinstance(document, dict) else None
    if code in (200, 201) and isinstance(url, str):
        if url.startswith("dashboard.html"):
            url = endpoint + url
        # Only a page of the stack that was asked. Anything else is not opened,
        # whatever the answer says, because the browser would carry the code there.
        if url.startswith(endpoint) and not any(c.isspace() for c in url):
            return url, ""
        return None, "no sign-in link: the stack answered with a link to somewhere else, which was not opened"
    if code in (401, 403):
        return None, f"no sign-in link: the stack answered HTTP {code} to the operator key"
    return None, f"no sign-in link: the stack answered HTTP {code}"


def show_dashboard(endpoint: str, route: str, key: Optional[str], out: Any, label: str = "dashboard") -> None:
    """Opens the dashboard on `route`, signed in when there is a key, and prints what happened in one or two lines."""
    page = f"{endpoint}dashboard.html#{route}"
    link, why = signin_link(endpoint, route, key) if key else (None, "")
    if link:
        if open_url(link):
            print(f"  {label:<10}  {page}  (opened in your browser, signed in)", file=out)
        else:
            # No browser here: the link is the only way in, so it is printed,
            # with what makes printing it tolerable.
            print(f"  {label:<10}  {page}", file=out)
            print(f"  {'':<10}  sign in within 2 minutes, once, at: {link}", file=out)
        return
    opened = open_url(page)
    print(f"  {label:<10}  {page}{'  (opened in your browser)' if opened else ''}", file=out)
    if why:
        print(f"  {'':<10}  {why}", file=out)


# --- connect -----------------------------------------------------------------------------------

def connect(args: argparse.Namespace, out: Any) -> int:
    home = threefold_home()
    named = Path(args.path).expanduser()
    root, workspace = locate(named)
    refuse_home_folder(root)
    existing = read_json_quietly(root / CONFIG_FILE)
    kept: List[str] = []

    project = args.project
    project_note = ""
    if not project:
        previous = existing.get("project")
        if isinstance(previous, str) and PROJECT_PATTERN.match(previous):
            project, project_note = previous, "(kept from .threefold.json)"
            kept.append("project")
        else:
            project, project_note = default_project(root), "(from the folder name; --project NAME picks another)"
    if not PROJECT_PATTERN.match(project):
        # The alias is what the dashboard shows. A real name typed here would
        # be published by the first tool call.
        raise InstallError("--project must match ^Acme-[A-Za-z0-9-]{1,40}$, an alias rather than a real name")

    mode = args.mode
    if not mode:
        previous = existing.get("mode")
        mode = previous if previous in MODES else "managed"
        if previous in MODES:
            kept.append("mode")

    endpoint = with_slash(args.endpoint) if args.endpoint else served_endpoint()
    previous_endpoint = existing.get("endpoint") if isinstance(existing.get("endpoint"), str) else None
    if not endpoint and previous_endpoint:
        endpoint = with_slash(previous_endpoint)
        kept.append("endpoint")
    api_key_file = args.api_key_file
    previous_key = existing.get("api_key_file")
    if not api_key_file and isinstance(previous_key, str) and previous_endpoint and endpoint == with_slash(previous_endpoint):
        # Only with the endpoint it was written for: a key file must never
        # follow a project to a stack nobody paired it with.
        api_key_file = previous_key
        kept.append("key file")

    includes = include_globs(args.include)
    if not args.include and isinstance(existing.get("include"), list):
        # Dropping a workspace's list on a second connect would send every
        # repository the owner left out, so it stays unless replaced.
        includes = include_globs([item for item in existing["include"] if isinstance(item, str)])
        if includes:
            kept.append("include list")
    if args.include and os.path.normcase(str(named.resolve())) != os.path.normcase(str(root)):
        # Globs given here were written for the directory named; kept ones
        # were written for the root, where they already are.
        raise InstallError(
            f"--include globs are relative to the directory given, but {forward(named.resolve())} is inside the "
            f"repository {forward(root)}, and .threefold.json would be written there instead; connect "
            f"{forward(root)} with globs relative to it"
        )

    if args.agents.strip().lower() == "auto":
        detected = detect_agents()
        agents = [agent for agent, _ in detected] or list(AGENTS)
        if detected:
            agent_line = ", ".join(f"{agent} ({why})" for agent, why in detected)
        else:
            agent_line = (
                "all three, because none was found (looked for ~/.claude or claude, ~/.codex or codex, "
                "~/.gemini/antigravity, ~/.antigravity or antigravity)"
            )
    else:
        agents = parse_agents(args.agents)
        agent_line = ", ".join(agents)

    hook = load_hook_module(home)
    before = resolved_settings(hook, root)
    shown_endpoint = endpoint or (before.endpoint if before is not None else "")

    print(f"Threefold connect: {forward(root)}", file=out)
    print(f"  {'folder':<10}  {'a workspace, not a git repository' if workspace else 'a git repository'}", file=out)
    print(f"  {'project':<10}  {project} {project_note}".rstrip(), file=out)
    print(f"  {'mode':<10}  {mode}: {mode_line(mode)}", file=out)
    print(f"  {'agents':<10}  {agent_line}", file=out)
    print(f"  {'endpoint':<10}  {shown_endpoint or 'the hook default'}", file=out)
    if kept:
        print(f"  {'kept':<10}  the {', '.join(kept)} already in .threefold.json; pass them to change them", file=out)
    print("", file=out)

    install_args = argparse.Namespace(
        project=project, mode=mode, agents=",".join(agents), endpoint=endpoint, api_key_file=api_key_file,
        dry_run=args.dry_run,
    )
    plan, agents = plan_install(install_args, root, home, out, workspace, includes, prefix="  ")
    plan.run()
    print("", file=out)

    route = f"/projects/{project}"
    if args.dry_run:
        base = shown_endpoint or "<the hook default>/"
        print(f"  {'first call':<10}  would send one dry-run call (Bash: git status) to {base}evaluate-tool-call", file=out)
        print(f"  {'dashboard':<10}  would open {base}dashboard.html#{route}", file=out)
        print("", file=out)
        print("Nothing was written, sent or opened: this was a dry run.", file=out)
        return 0

    hook = load_hook_module(home)
    settings = resolved_settings(hook, root)
    if settings is None:
        print(f"  {'first call':<10}  not sent: the installed hook could not be loaded to read its configuration", file=out)
    else:
        for note in settings.notes:
            print(f"  {'note':<10}  {note}", file=out)
        sent_as = settings.project or project
        if sent_as != project:
            print(f"  {'note':<10}  THREEFOLD_PROJECT in your environment names {sent_as}, which the hook sends instead", file=out)
        developer = hook.developer_id() if hasattr(hook, "developer_id") else "anonymous"
        # The mode the hook will send under, which the environment can override.
        sent_mode = settings.mode if settings.mode in MODES else mode
        _, line, stage = first_call(settings.endpoint, sent_as, agents[0], sent_mode, settings.api_key, developer)
        if stage and hasattr(hook, "remember_stage"):
            # The hook starts from the stage the stack just named, not from none.
            hook.remember_stage(str(home), sent_as, stage)
        print(f"  {'first call':<10}  {line}", file=out)
        route = f"/projects/{sent_as}"
        if args.no_open:
            print(f"  {'dashboard':<10}  {settings.endpoint}dashboard.html#{route}", file=out)
            if settings.api_key:
                print(f"  {'':<10}  to sign in from this machine: {installer_command(home)} open", file=out)
        else:
            show_dashboard(settings.endpoint, route, settings.api_key, out)

    print("", file=out)
    print("Next", file=out)
    for line in next_steps(agents, mode, home, root):
        print(f"  - {line}", file=out)
    return 0


def mode_line(mode: str) -> str:
    if mode == "managed":
        return "the project's stage on the dashboard decides; it starts in Observe"
    if mode == "observe":
        return "every call is recorded as a dry run and nothing is refused, whatever the stage"
    return "every call is judged and refused when it breaks a rule, whatever the stage"


def next_steps(agents: Sequence[str], mode: str, home: Path, root: Path) -> List[str]:
    lines = []
    if "claude-code" in agents:
        lines.append("Claude Code: start a new session in this folder; a running one keeps the hooks it started with.")
    if "codex" in agents:
        lines.append("Codex: trust this project in Codex, which loads .codex/hooks.json only for trusted projects.")
    if "antigravity" in agents:
        lines.append("Antigravity: if it asks whether to trust this workspace's hooks, say yes.")
    if mode == "managed":
        lines.append(
            "In Observe every call is recorded and nothing but a credential is refused. Mark what it would refuse on "
            "the dashboard, then Promote the project when that reads right."
        )
    command = installer_command(home)
    lines.append(f"status: {command} status")
    lines.append(f"undo:   {command} disconnect {quoted(root)}")
    return lines


# --- disconnect, status, open --------------------------------------------------------------------

def disconnect(args: argparse.Namespace, out: Any) -> int:
    home = threefold_home()
    root, workspace = locate(Path(args.path).expanduser())
    print(f"Threefold disconnect: {forward(root)}", file=out)
    code = uninstall(args, root, home, out, workspace, prefix="  ")
    if not args.dry_run:
        print("Disconnected. Agents started here from now on run without Threefold.", file=out)
    return code


def project_stage(endpoint: str, project: str, key: Optional[str]) -> str:
    """The project's stage as the stack reports it, or `unknown`. Best effort: nothing here is worth failing over."""
    try:
        code, document = http_json("GET", endpoint + "api/projects/" + urllib.parse.quote(project, safe=""), key=key)
    except Unreachable:
        return "unknown"
    if code in (401, 403):
        return "unknown (the stack needs its operator key)"
    if code != 200 or not isinstance(document, dict):
        return "unknown"
    config = document.get("config") if isinstance(document.get("config"), dict) else {}
    readiness = document.get("readiness") if isinstance(document.get("readiness"), dict) else {}
    summary = readiness.get("summary") if isinstance(readiness.get("summary"), dict) else {}
    for stage in (config.get("stage"), summary.get("stage"), document.get("stage")):
        if stage in ("observe", "enforce"):
            return stage
    return "unknown"


def status(args: argparse.Namespace, out: Any) -> int:
    home = threefold_home()
    entries = load_index(home)
    if not entries:
        print("Threefold is not connected to anything on this machine yet.", file=out)
        print(f"Connect a repository with: {installer_command(home)} connect PATH", file=out)
        return 0
    hook = load_hook_module(home)
    print(f"Threefold on this machine: {len(entries)} connected", file=out)
    for entry in entries:
        path = Path(entry["path"])
        present = (path / CONFIG_FILE).is_file()
        config = read_json_quietly(path / CONFIG_FILE)
        project = config.get("project") if isinstance(config.get("project"), str) else entry.get("project")
        mode = config.get("mode") if config.get("mode") in MODES else entry.get("mode") or "enforce"
        agents = [agent for agent in entry.get("agents") or [] if agent in AGENTS]
        stage = "unknown"
        settings = resolved_settings(hook, path) if present else None
        if settings is not None and isinstance(project, str) and project:
            stage = project_stage(settings.endpoint, project, settings.api_key)
        print("", file=out)
        print(f"  {forward(path)}{'' if present else '  (its .threefold.json is gone)'}", file=out)
        print(f"    project {project or 'none'}   mode {mode}   stage {stage}", file=out)
        print(f"    agents  {', '.join(agents) or 'none'}{'   (workspace)' if entry.get('workspace') else ''}", file=out)
        if settings is not None:
            print(f"    stack   {settings.endpoint}", file=out)
    return 0


def open_dashboard(args: argparse.Namespace, out: Any) -> int:
    home = threefold_home()
    hook = load_hook_module(home)
    settings = resolved_settings(hook, Path.cwd())
    if args.endpoint:
        if not args.endpoint.lower().startswith(("https://", "http://")):
            raise InstallError("--endpoint must be an http(s) URL")
        endpoint = with_slash(args.endpoint)
    elif settings is not None and settings.endpoint_source != "default":
        endpoint = settings.endpoint
    elif served_endpoint():
        endpoint = served_endpoint()
    elif settings is not None:
        endpoint = settings.endpoint
    else:
        raise InstallError("no stack is configured here; pass --endpoint URL")
    route = args.next
    if not route:
        project = settings.project if settings is not None else ""
        route = f"/projects/{project}" if PROJECT_PATTERN.match(project or "") else "/overview"
    if not NEXT_ROUTE.match(route):
        raise InstallError("--next takes a dashboard route such as /projects/Acme-Ledger")
    key = operator_key(hook, settings, endpoint, home)
    print(f"Threefold: {endpoint}", file=out)
    show_dashboard(endpoint, route, key, out)
    if not key:
        print(f"  {'':<10}  no operator key is configured for this stack here, so the dashboard opens without signing "
              "in; a private stack needs connect --api-key-file first", file=out)
    return 0


def operator_key(hook: Optional[Any], settings: Optional[Any], endpoint: str, home: Path) -> Optional[str]:
    """The key the hook would send to this endpoint, or the one the owner paired with it at home."""
    if settings is not None and settings.endpoint == endpoint and settings.api_key:
        return settings.api_key
    if hook is None or not hasattr(hook, "_read_key_file"):
        return None
    pairs = read_json_quietly(home / HOME_CONFIG).get("trusted_endpoints")
    for pair in pairs if isinstance(pairs, list) else []:
        if not isinstance(pair, dict) or not isinstance(pair.get("endpoint"), str) or with_slash(pair["endpoint"].strip()) != endpoint:
            continue
        value = pair.get("api_key_file")
        if isinstance(value, str) and value.strip():
            path = Path(os.path.expanduser(value.strip()))
            path = path if path.is_absolute() else home / path
            return hook._read_key_file(str(path), "THREEFOLD_HOME/config.json", [])
    return None


# --- entry point -----------------------------------------------------------------------------

USAGE = """usage: threefold_install.py <command> [options]

  connect [PATH]      govern the agents working in PATH (default: here)
  disconnect [PATH]   take Threefold out of PATH again, exactly
  status              every connected folder on this machine, and its stage
  open                sign in to the dashboard from this machine

  threefold_install.py <command> --help   says more about each
  threefold_install.py --repo PATH ...    the older form, unchanged
"""


def command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threefold_install.py", description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    connect_parser = commands.add_parser("connect", help="govern the agents working in a folder")
    connect_parser.add_argument("path", nargs="?", default=".", help="the repository or workspace, default here")
    connect_parser.add_argument("--project", help="its alias, Acme-<name>; default Acme-<folder name>")
    connect_parser.add_argument("--agents", default="auto", help="auto (default: the ones on this machine), or a comma-separated list")
    connect_parser.add_argument("--mode", choices=MODES, help="default managed: the project's stage decides")
    connect_parser.add_argument("--endpoint", help="the stack; default the one that served this installer")
    connect_parser.add_argument("--api-key-file", help="a file holding the operator key; its path is written, never its content")
    connect_parser.add_argument("--include", action="append", metavar="GLOB", help="repeatable; only calls inside these globs are sent")
    connect_parser.add_argument("--dry-run", action="store_true", help="print every step and write, send and open nothing")
    connect_parser.add_argument("--no-open", action="store_true", help="print the dashboard's address instead of opening it")

    disconnect_parser = commands.add_parser("disconnect", help="take Threefold out of a folder again")
    disconnect_parser.add_argument("path", nargs="?", default=".", help="the repository or workspace, default here")
    disconnect_parser.add_argument("--dry-run", action="store_true", help="print every step and write nothing")

    commands.add_parser("status", help="every connected folder on this machine, and its stage")

    open_parser = commands.add_parser("open", help="sign in to the dashboard from this machine")
    open_parser.add_argument("--next", help="the dashboard route to land on, such as /projects/Acme-Ledger")
    open_parser.add_argument("--endpoint", help="the stack; default the one configured here")
    return parser


def legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threefold_install.py", description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo", required=True, help="the repository to govern")
    parser.add_argument("--project", help="its alias, Acme-<name>; required to install")
    parser.add_argument("--agents", default=",".join(AGENTS), help="comma-separated, default all three")
    parser.add_argument("--mode", choices=("observe", "managed", "enforce"), default="observe")
    parser.add_argument("--endpoint", help="the service, default the public stack")
    parser.add_argument("--api-key-file", help="a file holding the key; its path is written, never its content")
    parser.add_argument(
        "--include", action="append", metavar="GLOB",
        help="repeatable; a glob relative to the directory, and only calls inside the globs given are sent",
    )
    parser.add_argument("--uninstall", action="store_true", help="remove what an install added")
    parser.add_argument("--dry-run", action="store_true", help="print every step and write nothing")
    return parser


def legacy(argv: Sequence[str], out: Any) -> int:
    args = legacy_parser().parse_args(argv)
    if not args.endpoint and served_endpoint():
        args.endpoint = served_endpoint()
    root, workspace = locate(Path(args.repo))
    home = threefold_home()
    if args.uninstall:
        return uninstall(args, root, home, out, workspace)
    if not args.project or not PROJECT_PATTERN.match(args.project):
        # The alias is what the public ledger shows. A real name typed here
        # would be published by the first tool call.
        raise InstallError("--project must match ^Acme-[A-Za-z0-9-]{1,40}$, an alias rather than a real name")
    refuse_home_folder(root)
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


def main(argv: Optional[Sequence[str]] = None, out: Any = None) -> int:
    out = out or sys.stdout
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE, file=out, end="")
        return 0 if argv else 2
    try:
        if argv[0] not in COMMANDS:
            return legacy(argv, out)
        args = command_parser().parse_args(argv)
        if args.command == "connect":
            return connect(args, out)
        if args.command == "disconnect":
            return disconnect(args, out)
        if args.command == "status":
            return status(args, out)
        return open_dashboard(args, out)
    except InstallError as error:
        print(f"threefold: {error}. Nothing was changed.", file=out)
        return 2


if __name__ == "__main__":
    sys.exit(main())
