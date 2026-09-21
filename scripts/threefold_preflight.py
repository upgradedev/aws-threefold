#!/usr/bin/env python3
"""Before a repository is governed: how much of it the never-send list would hold back.

    threefold_preflight.py --repo PATH [--repo PATH ...] [--show-paths]

The hook never sends a call whose own text mentions a term from
THREEFOLD_HOME/never_send.txt. That keeps the term on the machine. It holds
back the call, not the file: an Edit elsewhere in a file that mentions a term
carries only its own lines and is sent, while a Write of the whole file is not.
So a repository with matching files is still safe to govern; what it loses is
coverage of the calls that carry a term, and this count says how much of the
repository that could touch.

For each repository this prints one line: how many text files mention a term,
in their content or in their path, out of how many were read, and a
recommendation, install or exclude. With --show-paths it lists the matching
files by relative path. It never prints a term or a line of matching text, and
a path segment that itself contains a term is printed as `[name withheld]`.

Read the way the hook reads a call: case is ignored, data files are skipped by
the same extensions and directories the hook holds back, and so are `.git`,
`node_modules`, binary files and files over a megabyte.

Standard library only.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 200_000
BINARY_SNIFF_BYTES = 8_192
WITHHELD = "[name withheld]"


def load_hook() -> Any:
    """The hook, for its never-send parsing and its definition of a data file."""
    for candidate in (HERE / "threefold_hook.py", HERE.parent / "src" / "threefold" / "hooks" / "threefold_hook.py"):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("threefold_hook_for_preflight", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit("threefold: the hook is not beside this script or in ../src.")


_HOOK = None


def _mentions(text: str, terms: Sequence[str]) -> bool:
    # The hook's own matcher, so a count here is what the hook would hold back.
    return any(_HOOK.term_occurs(text, term) for term in terms)


def scan(repo: Path, terms: Sequence[str], hook: Any) -> Tuple[int, int, List[str]]:
    """(files read, files that mention a term, their relative paths)."""
    skipped_directories = {name.lower() for name in hook.DATA_DIRECTORIES} | {".git", "node_modules"}
    read = 0
    matching: List[str] = []
    for directory, subdirectories, files in os.walk(repo):
        subdirectories[:] = sorted(name for name in subdirectories if name.lower() not in skipped_directories)
        for name in sorted(files):
            if read >= MAX_FILES:
                return read, len(matching), matching
            if name.lower().endswith(hook.DATA_EXTENSIONS):
                continue
            path = Path(directory) / name
            try:
                if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:BINARY_SNIFF_BYTES]:
                continue
            read += 1
            relative = path.relative_to(repo).as_posix()
            if _mentions(relative, terms) or _mentions(raw.decode("utf-8", "replace"), terms):
                matching.append(relative)
    return read, len(matching), matching


def safe_path(relative: str, terms: Sequence[str]) -> str:
    """A relative path with every segment that names a term replaced, so listing it cannot print the term."""
    return "/".join(WITHHELD if _mentions(segment, terms) else segment for segment in relative.split("/"))


def main(argv: Optional[Sequence[str]] = None, out: Any = None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="threefold_preflight.py", description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo", action="append", required=True, help="a repository to scan; repeat for several")
    parser.add_argument("--show-paths", action="store_true", help="list the matching files by relative path")
    args = parser.parse_args(argv)

    global _HOOK
    hook = load_hook()
    _HOOK = hook
    home = hook.threefold_home()
    terms = hook.read_never_send(home)
    if not terms:
        print(
            f"threefold: there is no never-send list at {Path(home, 'never_send.txt').as_posix()}, "
            "so there is nothing to check a repository against and no recommendation.",
            file=out,
        )
        return 2

    for position, given in enumerate(args.repo, start=1):
        repo = Path(given).resolve()
        label = given if not _mentions(given, terms) else f"repository {position}"
        if not repo.is_dir():
            print(f"threefold: {label}: not a directory.", file=out)
            continue
        read, count, paths = scan(repo, terms, hook)
        if count:
            advice = (
                "install if you accept that calls carrying those terms go unjudged; "
                "to govern them too, remove the terms from those files first"
            )
        else:
            advice = "install: nothing in it would be held back"
        print(f"threefold: {label}: {count} of {read} text files mention a never-send term. Recommendation: {advice}.", file=out)
        if args.show_paths:
            for relative in paths:
                print(f"  {safe_path(relative, terms)}", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
