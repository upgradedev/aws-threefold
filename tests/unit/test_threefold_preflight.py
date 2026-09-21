"""The pre-flight scan counts what the never-send list would hold back, and says nothing else.

Its output is read on a screen that may be shared or recorded, so the property
that matters most is negative: no term and no line of matching text is ever
printed, not in the count, not in the path list, not in a path that happens to
contain a term. The count itself has to be read the way the hook reads a call,
or the recommendation is about a different hook.

The never-send list here is synthetic and lives in a temporary THREEFOLD_HOME.
"""
from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
TERMS = ["Orion-Internal", "projekt-zeta"]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = _load("threefold_preflight")


@pytest.fixture
def machine(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("THREEFOLD_") and name != "THREEFOLD_OFFLINE":
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    (home / ".threefold").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("THREEFOLD_HOME", str(home / ".threefold"))
    (home / ".threefold" / "never_send.txt").write_text("# synthetic\n" + "\n".join(TERMS) + "\n", encoding="utf-8")
    repo = tmp_path / "acme-ledger"
    repo.mkdir()
    return SimpleNamespace(home=home, repo=repo, tmp=tmp_path)


def put(root: Path, relative: str, content, binary: bool = False) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def scan(*argv: str) -> SimpleNamespace:
    out = io.StringIO()
    code = preflight.main(list(argv), out)
    return SimpleNamespace(code=code, out=out.getvalue())


def _assert_no_term(text: str) -> None:
    for term in TERMS:
        assert term.casefold() not in text.casefold(), f"the output named a never-send term: {text!r}"


def test_matching_files_are_counted_and_the_advice_says_what_goes_unjudged(machine) -> None:
    """The hook holds back a call, not a file, so matches cost coverage, not safety.

    The first wording said every write to a matching file would be held back and
    advised excluding the repository, which is not what the hook does: an Edit
    elsewhere in that file carries only its own lines and is sent.
    """
    put(machine.repo, "src/app.py", "# built for ORION-INTERNAL billing\nx = 1\n")
    put(machine.repo, "docs/notes.md", "the projekt-zeta cutover\n")
    put(machine.repo, "src/clean.py", "y = 2\n")
    result = scan("--repo", str(machine.repo))
    assert result.code == 0
    assert "2 of 3 text files mention a never-send term" in result.out
    assert "calls carrying those terms go unjudged" in result.out
    assert "exclude" not in result.out
    _assert_no_term(result.out)


def test_a_repository_with_no_match_is_recommended_for_install(machine) -> None:
    put(machine.repo, "src/app.py", "x = 1\n")
    result = scan("--repo", str(machine.repo))
    assert "0 of 1 text files" in result.out
    assert "Recommendation: install" in result.out


def test_what_the_hook_never_reads_is_not_counted(machine) -> None:
    put(machine.repo, ".git/config", "Orion-Internal\n")
    put(machine.repo, "node_modules/acme/index.js", "Orion-Internal\n")
    put(machine.repo, "data/export.txt", "Orion-Internal\n")
    put(machine.repo, "reports/q3.csv", "Orion-Internal\n")
    put(machine.repo, "assets/logo.bin", b"\x00\x01Orion-Internal", binary=True)
    put(machine.repo, "dumps/big.log", "Orion-Internal\n" + "x" * (preflight.MAX_FILE_BYTES + 1))
    put(machine.repo, "src/app.py", "x = 1\n")
    result = scan("--repo", str(machine.repo))
    assert "0 of 1 text files" in result.out


def test_a_term_in_a_file_name_counts_because_the_hook_would_send_the_path(machine) -> None:
    put(machine.repo, "src/orion-internal_adapter.py", "x = 1\n")
    result = scan("--repo", str(machine.repo))
    assert "1 of 1 text files" in result.out


def test_show_paths_lists_the_files_and_withholds_a_segment_that_names_a_term(machine) -> None:
    put(machine.repo, "src/app.py", "Orion-Internal\n")
    put(machine.repo, "src/projekt-zeta/loader.py", "x = 1\n")
    result = scan("--repo", str(machine.repo), "--show-paths")
    assert "  src/app.py" in result.out
    assert f"  src/{preflight.WITHHELD}/loader.py" in result.out
    _assert_no_term(result.out)


def test_several_repositories_get_a_line_each_and_one_named_after_a_term_is_not_named(machine) -> None:
    second = machine.tmp / "Orion-Internal-tools"
    second.mkdir()
    put(machine.repo, "a.py", "x = 1\n")
    put(second, "b.py", "x = 1\n")
    result = scan("--repo", str(machine.repo), "--repo", str(second))
    lines = [line for line in result.out.splitlines() if "Recommendation" in line]
    assert len(lines) == 2
    assert "repository 2" in lines[1]
    _assert_no_term(result.out)


def test_without_a_never_send_list_there_is_no_recommendation(machine) -> None:
    (machine.home / ".threefold" / "never_send.txt").unlink()
    result = scan("--repo", str(machine.repo))
    assert result.code == 2
    assert "no never-send list" in result.out
    assert "Recommendation" not in result.out
