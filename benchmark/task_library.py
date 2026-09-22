"""The benchmark's tasks: synthetic Acme repositories, each with a prompt that tempts a governed violation.

A task is a directory under `benchmark/tasks/<id>/`:

    task.json    the prompt, the checks that decide whether a violation landed,
                 and the acceptance command that decides whether the work is done
    repo/        the template repository, copied fresh for every run
    reference/   `clean/` and `violating/` overlays: one way to do the work
                 without breaking a rule and one way that breaks it, both of
                 which pass the acceptance tests. They are never shown to an
                 agent; the suite uses them to prove each task measures what it
                 claims to

A secret the prompt hands over is stored split into pieces and joined at run
time, so the literal never sits in this repository for a scanner to find, and
`{NAME}` in a prompt or an overlay file is replaced with it.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

BENCHMARK_DIR = Path(__file__).resolve().parent
TASKS_DIR = BENCHMARK_DIR / "tasks"
VARIANTS = ("clean", "violating")
_PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    language: str
    governed_by: List[str]
    temptation: str
    checks: List[str]
    prompt_template: str
    acceptance_command: List[str]
    acceptance_timeout_s: int
    pristine: List[str]
    directory: Path
    secret_pieces: Mapping[str, List[str]] = field(default_factory=dict)

    @property
    def template(self) -> Path:
        return self.directory / "repo"

    @property
    def secrets(self) -> Dict[str, str]:
        return {name: "".join(pieces) for name, pieces in self.secret_pieces.items()}

    @property
    def project_name(self) -> str:
        """The Acme-style project the hook reports as, inside the stack's default pattern."""
        return f"Acme-Bench-{self.id}"[:45]

    def prompt(self) -> str:
        return fill(self.prompt_template, self.secrets)

    def acceptance(self, python: Optional[str] = None) -> List[str]:
        python = python or sys.executable
        return [python if part == "{python}" else part for part in self.acceptance_command]

    def reference(self, variant: str) -> Path:
        if variant not in VARIANTS:
            raise ValueError(f"variant must be one of {VARIANTS}")
        return self.directory / "reference" / variant


def fill(text: str, secrets: Mapping[str, str]) -> str:
    """Replaces `{NAME}` with the named secret and leaves every other brace alone."""
    return _PLACEHOLDER.sub(lambda match: secrets.get(match.group(1), match.group(0)), text)


def load_task(directory: Path) -> Task:
    directory = Path(directory)
    data = json.loads((directory / "task.json").read_text(encoding="utf-8"))
    acceptance = data["acceptance"]
    task = Task(
        id=data["id"],
        title=data["title"],
        language=data["language"],
        governed_by=list(data.get("governed_by") or []),
        temptation=data.get("temptation", ""),
        checks=list(data["checks"]),
        prompt_template=data["prompt"],
        acceptance_command=list(acceptance["command"]),
        acceptance_timeout_s=int(acceptance.get("timeout_s", 300)),
        pristine=list(acceptance.get("pristine") or []),
        directory=directory,
        secret_pieces={name: list(pieces) for name, pieces in (data.get("secrets") or {}).items()},
    )
    if task.id != directory.name:
        raise ValueError(f"task.json in {directory.name} says its id is {task.id}")
    return task


def load_tasks(names: Optional[Sequence[str]] = None, root: Path = TASKS_DIR) -> List[Task]:
    available = {path.name: path for path in sorted(Path(root).iterdir()) if (path / "task.json").is_file()}
    if not names:
        return [load_task(path) for path in available.values()]
    unknown = [name for name in names if name not in available]
    if unknown:
        raise ValueError(f"unknown task(s) {', '.join(unknown)}; the tasks are {', '.join(available)}")
    return [load_task(available[name]) for name in names]


def copy_template(task: Task, destination: Path) -> Path:
    destination = Path(destination)
    shutil.copytree(task.template, destination, ignore=shutil.ignore_patterns("__pycache__", "bin", "obj"))
    return destination


def apply_overlay(task: Task, variant: str, destination: Path) -> List[str]:
    """Copies a reference overlay over a working copy, filling in the task's secrets. Returns the files written."""
    source = task.reference(variant)
    written: List[str] = []
    for path in sorted(source.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(source)
        target = Path(destination) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8")
        target.write_text(fill(text, task.secrets), encoding="utf-8", newline="\n")
        written.append(rel.as_posix())
    return written


def _same_tree(left: Path, right: Path) -> bool:
    if left.is_file() or right.is_file():
        return left.is_file() and right.is_file() and left.read_bytes() == right.read_bytes()
    if not left.is_dir() or not right.is_dir():
        return False

    def listing(root: Path) -> Dict[str, Path]:
        return {
            path.relative_to(root).as_posix(): path
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and not {"bin", "obj"} & set(path.relative_to(root).parts)
        }

    left_files, right_files = listing(left), listing(right)
    if left_files.keys() != right_files.keys():
        return False
    return all(left_files[name].read_bytes() == right_files[name].read_bytes() for name in left_files)


def restore_pristine(task: Task, repo: Path) -> bool:
    """Puts the acceptance files back exactly as the template has them. Returns whether the agent had changed them.

    The acceptance run judges the agent's code against the task's own tests, so
    a test weakened, deleted or added by the agent cannot decide the outcome.
    """
    modified = False
    for rel in task.pristine:
        source = task.template / rel
        target = Path(repo) / rel
        if not _same_tree(source, target):
            modified = True
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "bin", "obj"))
        elif source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return modified
