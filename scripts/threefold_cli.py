#!/usr/bin/env python3
"""The backstop behind the hook: the same rules, judged at commit time and in CI.

The hook sees a write as the agent asks to make it. It cannot see a write made
by an agent it is not installed in, by a person, or by a route the service
could not read, and it fails open when the service is unreachable. What reaches
a commit is judged here instead, by the same engine and the same rules:

    threefold_cli.py check [--repo PATH] [--mode observe|enforce]
        the staged content of every staged file, as `git show :path` has it,
        so what is judged is what the commit will contain, not the working copy

    threefold_cli.py ci --base REF [--repo PATH] [--mode observe|enforce]
        every file changed between REF and HEAD, at HEAD

The rules are the project's own: fetched from `GET {endpoint}rules?project=...`
when an endpoint is configured, else `<repository>/.threefold/rules.json` when
present, else the rules Threefold ships. The project, endpoint, mode and key
are resolved exactly as the hook resolves them (environment, then
.threefold.json, then THREEFOLD_HOME/config.json), by the hook's own code.

In observe mode it prints what would be refused and exits 0. In enforce mode it
exits 1 when a rule that enforces is broken, naming the rule by the id the hook
names. Rules in observe mode are reported and never fail a commit.

Standard library only. It runs from a Threefold checkout, where the engine is
in ../src, or from THREEFOLD_HOME/bin after the installer has copied the engine
to ../lib.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
for _candidate in (HERE.parent / "src", HERE.parent / "lib"):
    if (_candidate / "threefold" / "domain").is_dir():
        sys.path.insert(0, str(_candidate))
        break

from threefold.domain.layering_rules import DEFAULT_RULES, ENFORCE, validate_rules, violations  # noqa: E402

MAX_FILE_BYTES = 2_000_000
MAX_RULES_BYTES = 1_000_000
LOCAL_RULES = Path(".threefold") / "rules.json"


def load_hook() -> Any:
    """The hook module, for its configuration code: beside this file once installed, in src otherwise."""
    for candidate in (HERE / "threefold_hook.py", HERE.parent / "src" / "threefold" / "hooks" / "threefold_hook.py"):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("threefold_hook_for_cli", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit("threefold: the hook is not beside this script or in ../src, so the configuration cannot be read.")


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GitError(message[-1] if message else f"git {args[0]} failed")
    return result.stdout


class GitError(Exception):
    """A git command failed; the message is git's own last line."""


def repository_root(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").decode("utf-8").strip())


def _names(raw: bytes) -> List[str]:
    return [name for name in raw.decode("utf-8", "replace").split("\0") if name]


def staged_files(repo: Path) -> List[Tuple[str, str]]:
    """(path, staged content) for every file the commit adds, copies, modifies or renames."""
    files = []
    for name in _names(git(repo, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")):
        files.append((name, _content(repo, f":{name}")))
    return files


def changed_files(repo: Path, base: str) -> List[Tuple[str, str]]:
    """(path, content at HEAD) for every file changed since the merge base with `base`."""
    files = []
    for name in _names(git(repo, "diff", "--name-only", "-z", "--diff-filter=ACMR", f"{base}...HEAD")):
        files.append((name, _content(repo, f"HEAD:{name}")))
    return files


def _content(repo: Path, spec: str) -> str:
    raw = git(repo, "show", spec)
    # A file this large is not source anyone wrote by hand; judging its first
    # two megabytes is enough to find the imports, which sit at the top.
    return raw[:MAX_FILE_BYTES].decode("utf-8", "replace")


def fetch_rules(endpoint: str, project: str, api_key: Optional[str], timeout: float) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """The project's rules from the service, or (None, why not)."""
    url = endpoint + "rules?" + urllib.parse.urlencode({"project": project})
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
            document = json.loads(response.read(MAX_RULES_BYTES).decode("utf-8"))
    except urllib.error.HTTPError as error:
        return None, f"the service answered HTTP {error.code}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        return None, f"the service could not be read ({type(error).__name__})"
    usable, problems = validate_rules(document)
    if not usable:
        return None, "the service returned no usable rules"
    return usable, ""


def local_rules(root: Path) -> Tuple[Optional[List[Dict[str, Any]]], List[str]]:
    path = root / LOCAL_RULES
    if not path.is_file():
        return None, []
    try:
        document = json.loads(path.read_bytes()[:MAX_RULES_BYTES].decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, [f"{LOCAL_RULES.as_posix()} is not readable JSON and was ignored."]
    usable, problems = validate_rules(document)
    notes = [f"{LOCAL_RULES.as_posix()}: rule {item['id'] or item['index']} ignored: {item['reason']}" for item in problems]
    return (usable or None), notes


def choose_rules(root: Path, settings: Any, timeout: float) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """The rules to judge by, where they came from, and any notes on the way."""
    notes: List[str] = []
    if settings.endpoint_source != "default" and settings.project:
        fetched, why = fetch_rules(settings.endpoint, settings.project, settings.api_key, timeout)
        if fetched is not None:
            return fetched, f"the service's rules for {settings.project}", notes
        notes.append(f"could not fetch the rules for {settings.project}: {why}.")
    found, problems = local_rules(root)
    notes.extend(problems)
    if found is not None:
        return found, LOCAL_RULES.as_posix(), notes
    return list(DEFAULT_RULES), "the rules Threefold ships", notes


def judge(files: Sequence[Tuple[str, str]], rules: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    findings = []
    for path, content in files:
        found, _ = violations(path, content, rules)
        findings.extend(dict(item, path=path) for item in found)
    return findings


def report(findings: List[Dict[str, str]], mode: str, out: Any) -> int:
    """Prints each finding and returns the exit code the mode calls for."""
    refused = 0
    for item in findings:
        enforcing = item["mode"] == ENFORCE
        if enforcing and mode == "enforce":
            label = "REFUSED"
            refused += 1
        else:
            label = "WOULD REFUSE"
        print(f"{label} {item['path']} [{item['rule_id']}] {item['reason']}", file=out)
    if refused:
        print(f"threefold: {refused} file(s) break a rule that enforces; the commit is refused.", file=out)
        return 1
    if findings and mode == "observe":
        print("threefold: observe mode, so nothing is refused.", file=out)
    return 0


def main(argv: Optional[Sequence[str]] = None, out: Any = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="threefold_cli.py", description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("check", "judge the staged files"), ("ci", "judge the files changed since a base")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--repo", default=".", help="the repository, default the current directory")
        sub.add_argument("--mode", choices=("observe", "enforce"), help="overrides the configured mode")
        if name == "ci":
            sub.add_argument("--base", required=True, help="the ref to compare HEAD against, such as origin/main")
    args = parser.parse_args(argv)

    try:
        root = repository_root(Path(args.repo).resolve())
        files = staged_files(root) if args.command == "check" else changed_files(root, args.base)
    except (GitError, OSError) as error:
        print(f"threefold: {error}", file=out)
        return 2

    hook = load_hook()
    settings = hook.resolve_settings({"cwd": str(root)})
    mode = args.mode or settings.mode
    rules, source, notes = choose_rules(root, settings, hook.timeout_seconds())
    for note in list(settings.notes) + notes:
        print(f"threefold: {note}", file=out)
    what = "staged file(s)" if args.command == "check" else f"file(s) changed since {args.base}"
    print(f"threefold: judging {len(files)} {what} against {source}, {mode} mode.", file=out)
    return report(judge(files, rules), mode, out)


if __name__ == "__main__":
    sys.exit(main())
