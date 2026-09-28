"""The merge gate judges a diff through the service and fails closed.

`scripts/judge_pr.py` sends every added or changed text file to
/evaluate-tool-call as the Write it is. These tests drive it against canned
answers, so every exit code is reached here without a network: a refusal fails
the run with a GitHub annotation, observed rules fail it even where the
verdict is APPROVED (a project the server holds in Observe), and anything
unjudged - the service down, a 429 past the retries, an answer that is not
JSON - fails it too, because a gate that passes while blind is theater.

Names are synthetic, as the clean-room rule requires. The AWS example key is
split in two so no scanner reads a whole credential from this file.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any, Dict, List

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


judge_pr = _load("judge_pr")

BASE = "https://acme-judge.invalid/prod/"
PROJECT = "Acme-Widget"
EXAMPLE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
LIVE_SHAPED_KEY = "AKIA" + "ZZZZZZZZZZZZZZZZ"


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _approve(*_args, **_kwargs) -> Dict[str, Any]:
    return {"status": "APPROVED", "observed_rules": []}


def _refuse(*_args, **_kwargs) -> Dict[str, Any]:
    return {"status": "BLOCKED_CREDENTIAL", "rule_key": "CREDENTIAL",
            "reason": "a credential in the arguments"}


def _observe_fires(*_args, **_kwargs) -> Dict[str, Any]:
    return {"status": "APPROVED", "observed_rules": ["python-domain-stays-pure"]}


def _stub_post(monkeypatch, func, seen: List[Dict[str, Any]] | None = None):
    def post(endpoint: str, body: Dict[str, Any], timeout: float, retries: int):
        if seen is not None:
            seen.append({"endpoint": endpoint, "body": body})
        return func(body)

    monkeypatch.setattr(judge_pr, "post_verdict", post)


def _repo(tmp_path: Path, files: Dict[str, str], changed: List[str], monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(judge_pr, "_changed_files", lambda _repo, _base: list(changed))
    return repo


def test_a_refused_file_fails_the_run_with_an_annotation(tmp_path, monkeypatch, capsys):
    _stub_post(monkeypatch, _refuse)
    repo = _repo(tmp_path, {"src/app.py": "x = 1\n"}, ["src/app.py"], monkeypatch)
    code = judge_pr.main(["--endpoint", BASE, "--project", PROJECT, "--repo", str(repo),
                          "--pace", "0", "--session-id", "judge-test"])
    assert code == 1
    out = capsys.readouterr().out
    assert "::error file=src/app.py,line=1::a credential in the arguments" in out
    assert "1 of 1 files refused" in out


def test_observed_rules_fail_even_where_the_verdict_is_approved(tmp_path, monkeypatch, capsys):
    _stub_post(monkeypatch, _observe_fires)
    repo = _repo(tmp_path, {"src/domain/orders.py": "import boto3\n"}, ["src/domain/orders.py"], monkeypatch)
    code = judge_pr.main(["--endpoint", BASE, "--project", PROJECT, "--repo", str(repo),
                          "--pace", "0", "--session-id", "judge-test"])
    assert code == 1
    out = capsys.readouterr().out
    assert "python-domain-stays-pure" in out


def test_a_clean_diff_passes(tmp_path, monkeypatch, capsys):
    _stub_post(monkeypatch, _approve)
    repo = _repo(tmp_path, {"src/app.py": "x = 1\n"}, ["src/app.py"], monkeypatch)
    code = judge_pr.main(["--endpoint", BASE, "--project", PROJECT, "--repo", str(repo),
                          "--pace", "0", "--session-id", "judge-test"])
    assert code == 0
    assert "1 files judged, nothing refused" in capsys.readouterr().out


def test_files_are_judged_as_ci_writes_without_explanation(tmp_path, monkeypatch):
    seen: List[Dict[str, Any]] = []
    _stub_post(monkeypatch, _approve, seen)
    repo = _repo(tmp_path, {"src/app.py": "x = 1\n"}, ["src/app.py"], monkeypatch)
    code = judge_pr.main(["--endpoint", BASE, "--project", PROJECT, "--repo", str(repo),
                          "--pace", "0", "--session-id", "judge-pr-7"])
    assert code == 0
    assert len(seen) == 1
    assert seen[0]["endpoint"] == BASE
    body = seen[0]["body"]
    assert body["session_id"] == "judge-pr-7"
    assert body["project_name"] == PROJECT
    assert body["tool_name"] == "Write" and body["action_type"] == "write"
    assert body["arguments"] == {"path": "src/app.py", "content": "x = 1\n"}
    assert body["agent"] == "ci" and body["origin"] == "ci"
    assert body["explain"] is False and body["dry_run"] is False


def test_example_keys_are_scrubbed_before_sending(tmp_path, monkeypatch):
    seen: List[Dict[str, Any]] = []
    _stub_post(monkeypatch, _approve, seen)
    content = f"# see {EXAMPLE_KEY} in the tutorial\nx = 1\n"
    repo = _repo(tmp_path, {"docs/notes.md": content}, ["docs/notes.md"], monkeypatch)
    code = judge_pr.main(["--endpoint", BASE, "--project", PROJECT, "--repo", str(repo),
                          "--pace", "0", "--session-id", "judge-test"])
    assert code == 0
    sent = seen[0]["body"]["arguments"]["content"]
    assert EXAMPLE_KEY not in sent
    assert len(sent) == len(content)


def test_excluded_binary_empty_and_oversize_files_are_skipped_not_judged(tmp_path, monkeypatch, capsys):
    calls: List[str] = []

    def post(endpoint: str, body: Dict[str, Any], timeout: float, retries: int):
        calls.append(body["arguments"]["path"])
        return {"status": "APPROVED", "observed_rules": []}

    monkeypatch.setattr(judge_pr, "post_verdict", post)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src" / "empty.py").write_text("", encoding="utf-8")
    (repo / "src" / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
    (repo / "src" / "big.py").write_text("x = 1\n" * 1000, encoding="utf-8")
    (repo / "tests" / "security").mkdir(parents=True)
    (repo / "tests" / "security" / "bait.py").write_text(LIVE_SHAPED_KEY + "\n", encoding="utf-8")
    changed = ["src/ok.py", "src/empty.py", "src/blob.bin", "src/big.py", "tests/security/bait.py"]
    monkeypatch.setattr(judge_pr, "_changed_files", lambda _repo, _base: list(changed))
    code = judge_pr.main(["--endpoint", BASE, "--project", PROJECT, "--repo", str(repo),
                          "--pace", "0", "--max-bytes", "64", "--session-id", "judge-test"])
    assert code == 0
    assert calls == ["src/ok.py"]
    out = capsys.readouterr().out
    assert "skip tests/security/bait.py (excluded)" in out
    assert "skip src/empty.py (empty)" in out
    assert "skip src/blob.bin (not utf-8 text)" in out
    assert "skip src/big.py (over the 64 byte cap)" in out


def test_an_unreachable_service_fails_closed(tmp_path, monkeypatch, capsys):
    def down(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(judge_pr.urllib.request, "urlopen", down)
    repo = _repo(tmp_path, {"src/app.py": "x = 1\n"}, ["src/app.py"], monkeypatch)
    code = judge_pr.main(["--endpoint", BASE, "--project", PROJECT, "--repo", str(repo),
                          "--pace", "0", "--session-id", "judge-test"])
    assert code == 2
    err = capsys.readouterr().err
    assert "could not judge" in err


def test_429s_are_retried_then_fail_closed(monkeypatch):
    calls: List[str] = []

    def throttle_then_answer(request, timeout=None):
        calls.append(request.full_url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(request.full_url, 429, "slow down", {}, io.BytesIO(b""))
        return _FakeResponse(json.dumps({"status": "APPROVED", "observed_rules": []}).encode("utf-8"))

    monkeypatch.setattr(judge_pr.urllib.request, "urlopen", throttle_then_answer)
    monkeypatch.setattr(judge_pr.time, "sleep", lambda _seconds: None)
    answer = judge_pr.post_verdict(BASE, {"ping": 1}, timeout=5, retries=3)
    assert answer["status"] == "APPROVED"
    assert len(calls) == 3 and all(url.endswith("/evaluate-tool-call") for url in calls)

    def always_throttled(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "slow down", {}, io.BytesIO(b""))

    monkeypatch.setattr(judge_pr.urllib.request, "urlopen", always_throttled)
    with pytest.raises(RuntimeError, match="429"):
        judge_pr.post_verdict(BASE, {"ping": 1}, timeout=5, retries=1)


def test_an_answer_that_is_not_json_fails_closed(monkeypatch):
    def garbage(request, timeout=None):
        return _FakeResponse(b"not json{")

    monkeypatch.setattr(judge_pr.urllib.request, "urlopen", garbage)
    with pytest.raises(RuntimeError, match="could not judge"):
        judge_pr.post_verdict(BASE, {"ping": 1}, timeout=5, retries=0)


def test_dry_run_lists_and_judges_nothing(tmp_path, monkeypatch, capsys):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dry run judged")

    monkeypatch.setattr(judge_pr, "post_verdict", fail_if_called)
    repo = _repo(tmp_path, {"src/app.py": "x = 1\n"}, ["src/app.py"], monkeypatch)
    code = judge_pr.main(["--endpoint", BASE, "--project", PROJECT, "--repo", str(repo),
                          "--dry-run", "--session-id", "judge-test"])
    assert code == 0
    out = capsys.readouterr().out
    assert "would judge src/app.py" in out
    assert "nothing judged" in out


def test_line_endings_are_normalised_before_judging(tmp_path, monkeypatch):
    seen: List[Dict[str, Any]] = []
    _stub_post(monkeypatch, _approve, seen)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "crlf.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    monkeypatch.setattr(judge_pr, "_changed_files", lambda _repo, _base: ["src/crlf.py"])
    code = judge_pr.main(["--endpoint", BASE, "--project", PROJECT, "--repo", str(repo),
                          "--pace", "0", "--session-id", "judge-test"])
    assert code == 0
    assert seen[0]["body"]["arguments"]["content"] == "x = 1\ny = 2\n"


def test_over_the_file_cap_fails_closed(tmp_path, monkeypatch, capsys):
    _stub_post(monkeypatch, _approve)
    repo = _repo(tmp_path, {"a.py": "x\n", "b.py": "y\n"}, ["a.py", "b.py"], monkeypatch)
    code = judge_pr.main(["--endpoint", BASE, "--project", PROJECT, "--repo", str(repo),
                          "--max-files", "1", "--session-id", "judge-test"])
    assert code == 2
    assert "over the 1 cap" in capsys.readouterr().err
