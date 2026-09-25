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
whenever a project is configured, from the configured endpoint or the default
one the hook itself sends to, else `.threefold/rules.json` as committed, else
the rules Threefold ships. The project, endpoint, mode and key are resolved
exactly as the hook resolves them (environment, then .threefold.json, then
THREEFOLD_HOME/config.json), by the hook's own code.

The rule file is read from a commit, never from the working tree: `check`
reads it at HEAD and `ci` at the merge base. A backstop that took its rules
from the change it was judging would pass a branch that deletes a forbidden
module from the list and adds the import in the same commit. `ci` reports a
change to the rule file as a finding of its own, so a person sees it.

In observe mode it prints what would be refused and exits 0. In enforce mode it
exits 1 when a rule that enforces is broken, naming the rule by the id the hook
names. Rules in observe mode are reported and never fail a commit. In managed
mode, which `connect` writes, the project's stage on the stack decides, read
from `GET {endpoint}api/projects/<project>`: in Observe it judges as observe
mode, in Enforce as enforce mode except for the rules the operator left
observing, which are reported. A stage it cannot read refuses nothing, as the
hook fails open when the stack cannot be reached.

Standard library only. It runs from a Threefold checkout, where it sits in
src/threefold/tools inside the engine's own package, or from THREEFOLD_HOME/bin
after the installer has copied the engine to ../lib.
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
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
# Installed, the engine is in ../lib beside bin/. In a checkout this file is in
# src/threefold/tools, so the package root is two folders up. The installed
# layout is tried first: two folders above THREEFOLD_HOME/bin is the home
# folder, where a stray threefold/ must not be taken for the engine.
for _candidate in (HERE.parent / "lib", HERE.parents[1]):
    if (_candidate / "threefold" / "domain").is_dir():
        sys.path.insert(0, str(_candidate))
        break

from threefold.domain.layering_rules import DEFAULT_RULES, ENFORCE, validate_rules, violations  # noqa: E402

MAX_FILE_BYTES = 2_000_000
MAX_RULES_BYTES = 1_000_000
MAX_PROJECT_BYTES = 1_000_000
MODES = ("observe", "managed", "enforce")
LOCAL_RULES = Path(".threefold") / "rules.json"


def load_hook() -> Any:
    """The hook module, for its configuration code: beside this file once installed, in ../hooks in a checkout."""
    for candidate in (HERE / "threefold_hook.py", HERE.parent / "hooks" / "threefold_hook.py"):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("threefold_hook_for_cli", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit("threefold: the hook is not beside this script or in ../hooks, so the configuration cannot be read.")


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


def _http_url_or_none(url: str) -> Optional[str]:
    """The URL when it names http(s), else None: urlopen must never see file:// or a custom scheme."""
    try:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
    except ValueError:
        return None
    return url if scheme in ("http", "https") else None


def fetch_rules(endpoint: str, project: str, api_key: Optional[str], timeout: float) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """The project's rules from the service, or (None, why not)."""
    url = endpoint + "rules?" + urllib.parse.urlencode({"project": project})
    if _http_url_or_none(url) is None:
        return None, "refusing a non-http(s) service URL"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        # The scheme is validated to http(s) above; bandit cannot see the guard.
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:  # nosec B310
            document = json.loads(response.read(MAX_RULES_BYTES).decode("utf-8"))
    except urllib.error.HTTPError as error:
        return None, f"the service answered HTTP {error.code}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        return None, f"the service could not be read ({type(error).__name__})"
    usable, problems = validate_rules(document)
    if not usable:
        return None, "the service returned no usable rules"
    return usable, ""


def managed_mode(settings: Any, timeout: float) -> Tuple[str, FrozenSet[str], str]:
    """What managed mode judges as: (observe or enforce, the rules left observing, a line saying why).

    The project's configuration on the stack names its stage and, once it is
    promoted, the rules the operator chose to keep observing. A commit is held
    to exactly that, so the backstop never refuses what the hook would only
    have recorded.
    """
    project = settings.project
    if not project:
        return "observe", frozenset(), "managed mode with no project names no stage, so nothing is refused."
    url = settings.endpoint + "api/projects/" + urllib.parse.quote(project, safe="")
    if _http_url_or_none(url) is None:
        return "observe", frozenset(), (
            f"managed mode: the stage of {project} could not be read "
            "(refusing a non-http(s) service URL), so nothing is refused."
        )
    headers = {"Accept": "application/json"}
    if settings.api_key:
        headers["X-API-Key"] = settings.api_key
    document: Any = None
    why = "the stack named no stage"
    try:
        # The scheme is validated to http(s) above; bandit cannot see the guard.
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:  # nosec B310
            document = json.loads(response.read(MAX_PROJECT_BYTES).decode("utf-8"))
    except urllib.error.HTTPError as error:
        why = f"the stack answered HTTP {error.code}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        why = f"the stack could not be read ({type(error).__name__})"
    document = document if isinstance(document, dict) else {}
    config = document.get("config") if isinstance(document.get("config"), dict) else {}
    readiness = document.get("readiness") if isinstance(document.get("readiness"), dict) else {}
    summary = readiness.get("summary") if isinstance(readiness.get("summary"), dict) else {}
    stage = config.get("stage") if config.get("stage") in ("observe", "enforce") else summary.get("stage")
    if stage == "enforce":
        listed = config.get("observe_rules")
        if not isinstance(listed, list):
            rules = readiness.get("rules") if isinstance(readiness.get("rules"), list) else []
            listed = [rule.get("rule_key") for rule in rules if isinstance(rule, dict) and rule.get("mode_now") == "observe"]
        observing = frozenset(item for item in listed if isinstance(item, str))
        return "enforce", observing, f"managed mode: {project} is in Enforce on the stack, so a rule it enforces refuses the commit."
    if stage == "observe":
        return "observe", frozenset(), f"managed mode: {project} is in Observe on the stack, so nothing is refused."
    return "observe", frozenset(), f"managed mode: the stage of {project} could not be read ({why}), so nothing is refused."


def local_rules(root: Path, ref: Optional[str]) -> Tuple[Optional[List[Dict[str, Any]]], List[str]]:
    """The rule file as the commit `ref` has it, or (None, notes) when that commit has none."""
    if not ref:
        return None, []
    try:
        raw = git(root, "show", f"{ref}:{LOCAL_RULES.as_posix()}")
    except GitError:
        return None, []
    try:
        document = json.loads(raw[:MAX_RULES_BYTES].decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None, [f"{LOCAL_RULES.as_posix()} is not readable JSON and was ignored."]
    usable, problems = validate_rules(document)
    notes = [f"{LOCAL_RULES.as_posix()}: rule {item['id'] or item['index']} ignored: {item['reason']}" for item in problems]
    return (usable or None), notes


def choose_rules(root: Path, settings: Any, timeout: float, ref: Optional[str] = "HEAD") -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """The rules to judge by, where they came from, and any notes on the way.

    A project on the default endpoint is asked for its rules there too. The
    hook sends to that endpoint, so reading them anywhere else would let the
    hook and this check name different rules for the same write.
    """
    notes: List[str] = []
    if settings.project:
        fetched, why = fetch_rules(settings.endpoint, settings.project, settings.api_key, timeout)
        if fetched is not None:
            return fetched, f"the service's rules for {settings.project}", notes
        notes.append(f"could not fetch the rules for {settings.project}: {why}.")
    found, problems = local_rules(root, ref)
    notes.extend(problems)
    if found is not None:
        return found, f"{LOCAL_RULES.as_posix()} at {ref}", notes
    return list(DEFAULT_RULES), "the rules Threefold ships", notes


def head_commit(root: Path) -> Optional[str]:
    """HEAD, or None in a repository with no commit yet."""
    try:
        return git(root, "rev-parse", "--verify", "-q", "HEAD").decode("utf-8").strip() or None
    except GitError:
        return None


def rule_file_notes(root: Path, staged: Sequence[Tuple[str, str]]) -> List[str]:
    """Why a rule file in the working tree or the commit being made is not the one used."""
    name = LOCAL_RULES.as_posix()
    if any(path == name for path, _ in staged):
        return [f"this commit changes {name}; it is judged by the rules committed before it, and the new ones apply from the next commit."]
    if (root / LOCAL_RULES).is_file():
        try:
            committed = git(root, "show", f"HEAD:{name}")
        except GitError:
            committed = None
        if committed != (root / LOCAL_RULES).read_bytes():
            return [f"{name} in the working tree is not the committed one, so it is not used; commit it to adopt it."]
    return []


RULE_FILE_ID = "threefold-rule-file"


def rule_file_finding(root: Path, base: str) -> List[Dict[str, str]]:
    """A change to the rule file under review, as a finding of its own."""
    name = LOCAL_RULES.as_posix()
    changed = _names(git(root, "diff", "--name-only", "-z", f"{base}...HEAD", "--", name))
    if not changed:
        return []
    return [{
        "path": name,
        "rule_id": RULE_FILE_ID,
        "mode": ENFORCE,
        "module": "",
        "pattern": "",
        "reason": (
            f"this change edits the rules it would be judged by. It was judged by the rules at the merge base with "
            f"{base} instead, and a change to the rules is reviewed by a person on its own"
        ),
    }]


def judge(files: Sequence[Tuple[str, str]], rules: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    findings = []
    for path, content in files:
        found, _ = violations(path, content, rules)
        findings.extend(dict(item, path=path) for item in found)
    return findings


def report(findings: List[Dict[str, str]], mode: str, out: Any, observing: FrozenSet[str] = frozenset()) -> int:
    """Prints each finding and returns the exit code the mode calls for.

    `observing` names the rules a promoted project keeps observing: reported,
    never refused, whatever the rule itself says.
    """
    refused = 0
    for item in findings:
        enforcing = item["mode"] == ENFORCE and item["rule_id"] not in observing
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
        sub.add_argument("--mode", choices=MODES, help="overrides the configured mode")
        if name == "ci":
            sub.add_argument("--base", required=True, help="the ref to compare HEAD against, such as origin/main")
    args = parser.parse_args(argv)

    extra: List[Dict[str, str]] = []
    try:
        root = repository_root(Path(args.repo).resolve())
        if args.command == "check":
            files = staged_files(root)
            ref = head_commit(root)
            notes = rule_file_notes(root, files)
        else:
            files = changed_files(root, args.base)
            ref = git(root, "merge-base", args.base, "HEAD").decode("utf-8").strip()
            notes = []
            extra = rule_file_finding(root, args.base)
    except (GitError, OSError) as error:
        print(f"threefold: {error}", file=out)
        return 2

    hook = load_hook()
    settings = hook.resolve_settings({"cwd": str(root)})
    mode = args.mode or settings.mode
    observing: FrozenSet[str] = frozenset()
    if mode == "managed":
        mode, observing, why = managed_mode(settings, hook.timeout_seconds())
        notes.append(why)
    rules, source, chosen = choose_rules(root, settings, hook.timeout_seconds(), ref)
    for note in list(settings.notes) + chosen + notes:
        print(f"threefold: {note}", file=out)
    what = "staged file(s)" if args.command == "check" else f"file(s) changed since {args.base}"
    print(f"threefold: judging {len(files)} {what} against {source}, {mode} mode.", file=out)
    return report(extra + judge(files, rules), mode, out, observing)


if __name__ == "__main__":
    sys.exit(main())
