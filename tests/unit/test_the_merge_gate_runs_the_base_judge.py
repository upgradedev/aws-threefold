"""The merge gate runs the base branch's judge over the pull request as data.

The workflow used to check out the pull request and run its copy of
scripts/judge_pr.py, so a request that rewrote the judge to `return 0`
passed the required check. Now the workflow runs on pull_request_target from
the base, checks the base out for its judge and the request's head beside it
as data only, and runs the base's copy with the request as --repo. Nothing
in the request is executed, and no request text reaches a run step.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-judge.yml"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_the_workflow_runs_from_the_base_not_the_request() -> None:
    lines = [line.strip() for line in _workflow().splitlines()]
    assert "pull_request_target:" in lines, "The workflow must run the base's copy"
    assert "pull_request:" not in lines, "A pull_request trigger runs the request's copy"


def test_the_base_is_checked_out_for_its_judge_and_the_request_as_data() -> None:
    text = _workflow()
    assert "github.event.pull_request.base.sha" in text, "The judge comes from the base sha"
    assert "path: base" in text
    assert "github.event.pull_request.head.sha" in text, "The request is checked out by sha"
    assert "path: pr" in text
    assert "fetch-depth: 0" in text, "The diff needs the base ref present"


def test_only_the_base_copy_judges_and_nothing_in_the_request_runs() -> None:
    text = _workflow()
    assert "python base/scripts/judge_pr.py" in text
    assert "--repo pr" in text, "The request is the data under judgment"
    assert "pr/scripts" not in text and "python pr/" not in text, "Nothing in the request runs"


def test_no_request_text_reaches_a_run_step() -> None:
    text = _workflow()
    for expression in ("head.ref", "pull_request.title", "pull_request.body", "github.head_ref"):
        assert expression not in text, f"{expression} is attacker-controlled text"


def test_the_check_stays_required_and_read_only() -> None:
    text = _workflow()
    assert "name: Judge the diff" in text, "Branch protection names this check"
    assert "contents: read" in text


def test_codeowners_names_the_gate_s_own_files() -> None:
    assert CODEOWNERS.is_file(), "The gate's wiring needs an owner on review"
    owned = CODEOWNERS.read_text(encoding="utf-8")
    assert ".github/" in owned and "scripts/judge_pr.py" in owned
