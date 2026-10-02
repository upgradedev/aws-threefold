#!/usr/bin/env python3
"""Judges a pull request's diff through the deployed service, file by file.

    judge_pr.py --endpoint https://<api>/prod/ --project Acme-Widget
    judge_pr.py --endpoint https://<api>/prod/ --project Acme-Widget --base origin/main
    judge_pr.py --endpoint https://<api>/prod/ --project Acme-Widget --all-files --dry-run

The hook governs the machine it is installed on. This script governs the merge:
every added or changed text file in the diff is sent to /evaluate-tool-call as
the Write it is, with agent "ci" and origin "ci", and the check fails when the
service refuses one or when a rule fires on one. A hook nobody installed cannot
be bypassed here, because there is no hook: a cloud agent's pull request is
judged exactly like a local one's.

A rule firing is red even where the verdict is APPROVED: on a project the
server holds in Observe the call is recorded, never refused, and the response
says which rules would have refused it. The gate reads that list. The service
still does the judging; this script only maps "a rule fired" to an exit code.

Exit codes: 0 judged clean, 1 a rule fired, 2 the judge could not judge (the
service unreachable, an answer that is not JSON, 429s past the retries). A gate
that passes while blind is theater, so infrastructure failure fails the check.

Standard library only, called through subprocess for git.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Keys AWS publishes in its own documentation. They authenticate nothing, and
# a gate that flags them cries wolf on every tutorial that quotes them. The
# service still flags them by shape (its tests rely on that); the scrub here
# is this script's documented policy for inert strings, the same set the
# pre-commit gate scrubs. Anything else shaped like a credential is judged.
PUBLISHED_EXAMPLE_KEYS = (
    # Split in two so no scanner reads a whole credential from this file.
    "AKIA" + "IOSFODNN7EXAMPLE",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + "EXAMPLEKEY",
)

# Paths whose whole purpose is to carry inert attack strings: the security
# suite's adversarial fixtures. Judging them would flag the bait. Visible here
# and in the workflow, so the blind spot is auditable, not silent.
DEFAULT_EXCLUDES = ("tests/security/**",)

MAX_FILES_DEFAULT = 50
# Under the service's 1 MB body refusal, so the largest page is judged whole:
# a layering verdict needs the file's full imports, which chunks would break.
MAX_BYTES_DEFAULT = 921600


@dataclass
class Finding:
    path: str
    status: str
    rules: list = field(default_factory=list)
    message: str = ""


def _run_git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def _changed_files(repo: Path, base: str) -> list:
    # quotePath off: a name with a non-ASCII letter is listed as it is, not
    # as a quoted escape the file system has never heard of and skips.
    out = _run_git(repo, "-c", "core.quotePath=false", "diff", "--name-only", "--diff-filter=ACMRT", f"{base}...HEAD")
    return [line for line in out.splitlines() if line.strip()]


def _all_files(repo: Path) -> list:
    out = _run_git(repo, "ls-files")
    return [line for line in out.splitlines() if line.strip()]


def _excluded(path: str, patterns: tuple) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def scrub_examples(text: str) -> str:
    """Removes inert published-example keys, keeping positions with spaces."""
    for inert in PUBLISHED_EXAMPLE_KEYS:
        if inert in text:
            text = text.replace(inert, " " * len(inert))
    return text


def read_judgeable(repo: Path, path: str, max_bytes: int) -> tuple:
    """Returns (content or None, skip reason or None) for one path."""
    full = repo / path
    # Under pull_request_target the request's files sit beside the runner's
    # own; a link would read one of those and send it to the service.
    if full.is_symlink():
        return None, "a symlink, not followed"
    try:
        raw = full.read_bytes()
    except OSError as exc:
        return None, f"unreadable ({exc.strerror or exc})"
    if not raw:
        return None, "empty"
    if len(raw) > max_bytes:
        return None, f"over the {max_bytes} byte cap"
    if b"\x00" in raw[:8192]:
        return None, "not utf-8 text"
    # One stray byte used to skip a whole source file, and a skip passes: text
    # that is not quite UTF-8 is judged with the stray bytes replaced.
    text = raw.decode("utf-8", errors="replace")
    # Judged bytes are identical on every OS: a Windows checkout must judge
    # what a Linux runner judges.
    return text.replace("\r\n", "\n").replace("\r", "\n"), None


def post_verdict(endpoint: str, body: dict, timeout: float, retries: int) -> dict:
    """POSTs one evaluation, retrying 429s. Raises on anything unjudged."""
    url = endpoint.rstrip("/") + "/evaluate-tool-call"
    data = json.dumps(body).encode("utf-8")
    wait = 1.0
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                time.sleep(wait)
                wait *= 2.0
                continue
            raise RuntimeError(f"the service answered HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"the service could not judge: {exc}")
    raise RuntimeError("the service kept answering 429")


def judge_file(endpoint: str, session: str, project: str, path: str, content: str,
               timeout: float, retries: int) -> Finding | None:
    """Judges one file. Returns a Finding when a rule fired, else None."""
    body = {
        "session_id": session,
        "project_name": project,
        "tool_name": "Write",
        "action_type": "write",
        "arguments": {"path": path, "content": scrub_examples(content)},
        "agent": "ci",
        "origin": "ci",
        "explain": False,
        "dry_run": False,
    }
    answer = post_verdict(endpoint, body, timeout, retries)
    status = str(answer.get("status", ""))
    rules = [str(rule) for rule in answer.get("observed_rules", []) or []]
    if status == "APPROVED" and not rules:
        return None
    message = str(answer.get("reason") or answer.get("rule_key") or status or "a rule fired")
    if rules and not answer.get("reason"):
        message = f"rules fired: {', '.join(rules)}"
    return Finding(path=path, status=status or "UNKNOWN", rules=rules, message=message)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Judge a PR diff through Threefold.")
    parser.add_argument("--endpoint", required=True, help="the stack's base URL, e.g. https://<api>/prod/")
    parser.add_argument("--project", required=True, help="the Acme-style project to record the run under")
    parser.add_argument("--repo", default=".", help="the repository to judge (default: cwd)")
    parser.add_argument("--base", default="origin/main", help="the diff base (default: origin/main)")
    parser.add_argument("--all-files", action="store_true", help="judge the whole tree, for one-off audits")
    parser.add_argument("--exclude", action="append", default=[], help="a glob to skip (repeatable)")
    parser.add_argument("--no-default-excludes", action="store_true", help="judge the adversarial fixtures too")
    parser.add_argument("--max-files", type=int, default=MAX_FILES_DEFAULT)
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES_DEFAULT)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--pace", type=float, default=0.2, help="seconds between requests")
    parser.add_argument("--session-id", default="", help="the session to record under (default: judge-<8 hex>)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be judged and judge nothing")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    excludes = tuple(args.exclude) if args.no_default_excludes else DEFAULT_EXCLUDES + tuple(args.exclude)
    try:
        paths = _all_files(repo) if args.all_files else _changed_files(repo, args.base)
    except RuntimeError as exc:
        print(f"judge: {exc}", file=sys.stderr)
        return 2

    todo: list = []
    for path in paths:
        if _excluded(path, excludes):
            print(f"judge: skip {path} (excluded)")
            continue
        content, skipped = read_judgeable(repo, path, args.max_bytes)
        if skipped is not None:
            print(f"judge: skip {path} ({skipped})")
            continue
        todo.append((path, content))
    if len(todo) > args.max_files:
        print(f"judge: {len(todo)} files changed, over the {args.max_files} cap", file=sys.stderr)
        return 2
    if args.dry_run:
        for path, _content in todo:
            print(f"judge: would judge {path}")
        print(f"judge: {len(todo)} files, nothing judged")
        return 0

    session = args.session_id or f"judge-{uuid.uuid4().hex[:8]}"
    findings: list = []
    try:
        for index, (path, content) in enumerate(todo):
            if index:
                time.sleep(args.pace)
            found = judge_file(args.endpoint, session, args.project, path, content,
                               args.timeout, args.retries)
            if found is not None:
                findings.append(found)
                print(f"::error file={path},line=1::{found.message}")
    except RuntimeError as exc:
        print(f"judge: {exc}", file=sys.stderr)
        return 2

    if findings:
        print(f"judge: {len(findings)} of {len(todo)} files refused")
        return 1
    print(f"judge: {len(todo)} files judged, nothing refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
