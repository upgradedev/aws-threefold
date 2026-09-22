"""Each benchmark task measures what it claims to.

For every task: the untouched template carries no violation and fails its
acceptance tests (there is work to do); the clean reference passes them with no
violation; the violating reference passes them too, and the checker catches it.
The last property is the one that matters most: if only a violating solution
could pass, the task would be asking for the violation rather than tempting it.

The C# task needs a dotnet build per variant, about half a minute each, so its
acceptance runs only with BENCHMARK_DOTNET=1; its checker runs always.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmark import checks, task_library  # noqa: E402

TASKS = task_library.load_tasks()
PYTHON_TASKS = [task for task in TASKS if task.language == "python"]
DOTNET_TASKS = [task for task in TASKS if task.language == "csharp"]
RUN_DOTNET = os.environ.get("BENCHMARK_DOTNET") == "1" and shutil.which("dotnet") is not None


def _working_copy(task, tmp_path, variant=None):
    repo = task_library.copy_template(task, tmp_path / "repo")
    if variant:
        task_library.apply_overlay(task, variant, repo)
    return repo


def _judged_copy(task, tmp_path, variant=None):
    """A working copy as the runner judges it: the task's own tests put back first."""
    repo = _working_copy(task, tmp_path, variant)
    task_library.restore_pristine(task, repo)
    return repo


def _acceptance(task, repo):
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST")}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"})
    completed = subprocess.run(
        task.acceptance(sys.executable), cwd=str(repo), env=env, capture_output=True,
        timeout=task.acceptance_timeout_s,
    )
    return completed.returncode, completed.stdout.decode("utf-8", "replace")[-2000:]


def _violations(task, repo):
    return checks.check_repository(repo, task.checks, task.secrets).violations


def test_there_are_six_tasks_in_the_languages_the_rules_cover():
    assert len(TASKS) == 6
    assert {task.language for task in TASKS} <= {"python", "csharp", "java", "typescript"}
    assert len({task.id for task in TASKS}) == 6


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_a_task_is_fully_described(task):
    assert task.prompt().strip() and task.temptation.strip() and task.governed_by
    assert set(task.checks) <= set(checks.CHECKS)
    assert task.pristine, "the acceptance files must be restored before they judge the work"
    assert re.search(r"\{[A-Z][A-Z0-9_]*\}", task.prompt()) is None, "a placeholder was left unfilled"
    assert task.project_name.startswith("Acme-Bench-") and len(task.project_name) <= 45
    for variant in task_library.VARIANTS:
        assert any(task.reference(variant).rglob("*")), f"{task.id} has no {variant} reference"


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_the_template_starts_clean(task, tmp_path):
    assert _violations(task, _working_copy(task, tmp_path)) == []


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_the_violating_reference_is_caught(task, tmp_path):
    found = _violations(task, _working_copy(task, tmp_path, "violating"))
    assert found, f"the checker missed the violating reference of {task.id}"


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_the_clean_reference_is_not_flagged(task, tmp_path):
    assert _violations(task, _working_copy(task, tmp_path, "clean")) == []


@pytest.fixture(scope="module")
def python_acceptance(tmp_path_factory):
    """Every Python task's template and both references, judged at once.

    Fifteen pytest runs one after another cost the suite most of a minute on
    Windows, almost all of it interpreter start-up, so they run side by side.
    """
    jobs = {}
    for task in PYTHON_TASKS:
        for variant in (None,) + task_library.VARIANTS:
            label = variant or "template"
            jobs[(task.id, label)] = (task, _judged_copy(task, tmp_path_factory.mktemp(f"{task.id}-{label}"), variant))
    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        futures = {key: pool.submit(_acceptance, task, repo) for key, (task, repo) in jobs.items()}
        return {key: future.result() for key, future in futures.items()}


@pytest.mark.parametrize("task", PYTHON_TASKS, ids=lambda task: task.id)
def test_a_python_task_has_work_to_do_and_both_references_complete_it(task, python_acceptance):
    code, output = python_acceptance[(task.id, "template")]
    assert code != 0, f"{task.id}: the untouched template already passes its acceptance tests\n{output}"
    for variant in task_library.VARIANTS:
        code, output = python_acceptance[(task.id, variant)]
        assert code == 0, f"{task.id}: the {variant} reference fails the acceptance tests\n{output}"


@pytest.mark.skipif(not RUN_DOTNET, reason="set BENCHMARK_DOTNET=1 to build the C# task (about 30 s per variant)")
@pytest.mark.parametrize("task", DOTNET_TASKS, ids=lambda task: task.id)
def test_a_csharp_task_has_work_to_do_and_both_references_complete_it(task, tmp_path):
    code, output = _acceptance(task, _judged_copy(task, tmp_path / "template"))
    assert code != 0, f"{task.id}: the untouched template already passes\n{output}"
    for variant in task_library.VARIANTS:
        code, output = _acceptance(task, _judged_copy(task, tmp_path / variant, variant))
        assert code == 0, f"{task.id}: the {variant} reference fails\n{output}"


def test_a_secret_is_never_stored_whole_in_the_repository():
    """The staging key is joined at run time, so no file here carries it for a scanner to find."""
    for task in TASKS:
        for name, value in task.secrets.items():
            for path in task.directory.rglob("*"):
                if path.is_file():
                    assert value not in path.read_text(encoding="utf-8", errors="replace"), (
                        f"{path} carries the whole {name}")


def test_restoring_the_acceptance_files_undoes_an_agent_s_edits(tmp_path):
    task = task_library.load_tasks(["orders-s3-archive"])[0]
    repo = _working_copy(task, tmp_path)
    assert task_library.restore_pristine(task, repo) is False
    test_file = repo / "tests" / "acceptance" / "test_archive_on_confirm.py"
    test_file.write_text("def test_nothing():\n    pass\n", encoding="utf-8")
    (repo / "tests" / "acceptance" / "test_added.py").write_text("x = 1\n", encoding="utf-8")
    assert task_library.restore_pristine(task, repo) is True
    assert "archive" in test_file.read_text(encoding="utf-8")
    assert not (repo / "tests" / "acceptance" / "test_added.py").exists()
