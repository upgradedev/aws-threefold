"""`status --names-json`: the one command that prints the owner's own folder names.

The dashboard may only ever show aliases, so the owner cannot tell their own
projects apart on it. The browser can hold a name for each alias, and this is
where that mapping comes from: the folders on this machine, printed once, for
a person to paste. Because the values are real names rather than aliases, what
this command must not do is the point of these tests. It prints and nothing
else: no file is written, nothing lands in a repository, and no stack is
called. The object goes to stdout alone, so it can be pasted or redirected
whole, and what it is goes to stderr first.

Nothing here reads ~/.threefold or any other real file: HOME, USERPROFILE and
THREEFOLD_HOME point into a temporary directory, as the other installer tests
do, and the installs are written there by hand.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = _load(ROOT / "scripts" / "threefold_install.py", "threefold_install_for_local_names")


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A machine with a THREEFOLD_HOME of its own and no way to the network."""
    for name in list(os.environ):
        if name.startswith("THREEFOLD_") and name != "THREEFOLD_OFFLINE":
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    (home / ".threefold" / "installs").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("THREEFOLD_HOME", str(home / ".threefold"))

    def refuse(*args: Any, **kwargs: Any):
        raise AssertionError("this command asked the network for something")

    monkeypatch.setattr(installer, "http_json", refuse)
    monkeypatch.setattr(installer, "open_url", refuse)
    return SimpleNamespace(home=home, threefold_home=home / ".threefold", tmp=tmp_path)


def connect(machine, folder: str, project: str | None, *, write_config: bool = True) -> Path:
    """A checkout on this machine, noted in the index exactly as an install notes it."""
    path = machine.tmp / "checkouts" / folder
    path.mkdir(parents=True)
    if write_config and project:
        (path / ".threefold.json").write_text(
            json.dumps({"project": project, "endpoint": "http://127.0.0.1:1/prod/", "mode": "managed"}),
            encoding="utf-8",
        )
    index = machine.threefold_home / "installs" / "index.json"
    document = json.loads(index.read_text(encoding="utf-8")) if index.is_file() else {"version": 1, "installs": []}
    entry: Dict[str, Any] = {"path": str(path).replace("\\", "/"), "mode": "managed", "agents": ["claude-code"]}
    if project:
        entry["project"] = project
    document["installs"].append(entry)
    index.write_text(json.dumps(document), encoding="utf-8")
    return path


def run(*argv: str) -> SimpleNamespace:
    out, err = io.StringIO(), io.StringIO()
    code = installer.main(list(argv), out, err)
    return SimpleNamespace(code=code, out=out.getvalue(), err=err.getvalue())


def snapshot(*roots: Path) -> Dict[str, bytes]:
    files = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            files[str(path)] = path.read_bytes() if path.is_file() else b"<dir>"
    return files


def test_the_flag_prints_the_mapping_and_nothing_else(machine) -> None:
    connect(machine, "frontbox-portal", "Acme-Proj-One")
    connect(machine, "ledger-service", "Acme-Proj-Two")

    result = run("status", "--names-json")

    assert result.code == 0
    assert json.loads(result.out) == {
        "Acme-Proj-One": "frontbox-portal",
        "Acme-Proj-Two": "ledger-service",
    }
    # Pasteable and redirectable whole: the object is all that reached stdout.
    assert result.out.strip().startswith("{") and result.out.strip().endswith("}")
    assert "threefold:" not in result.out and "connected" not in result.out


def test_the_flag_says_they_are_real_names_before_it_prints_them(machine) -> None:
    connect(machine, "frontbox-portal", "Acme-Proj-One")

    result = run("status", "--names-json")

    assert "real folder names on this machine, not aliases" in result.err
    assert "nothing writes them to a file" in result.err
    assert "sends them to a stack" in result.err
    assert "Your own names for these projects" in result.err, "and where they are meant to go"
    # It is said on the other stream, so a redirected stdout still holds only
    # the object and the person still sees what they are about to paste.
    assert "frontbox-portal" not in result.err


def test_the_flag_writes_nothing_and_asks_nothing_of_any_stack(machine) -> None:
    connect(machine, "frontbox-portal", "Acme-Proj-One")
    before = snapshot(machine.home, machine.tmp / "checkouts")

    result = run("status", "--names-json")

    assert "frontbox-portal" in result.out
    # http_json and open_url are replaced with a failure in the fixture, so a
    # call to either would have raised rather than reached this line.
    assert snapshot(machine.home, machine.tmp / "checkouts") == before, "the command wrote something"


def test_an_install_whose_config_is_gone_still_gives_its_alias(machine) -> None:
    connect(machine, "frontbox-portal", "Acme-Proj-One", write_config=False)

    assert json.loads(run("status", "--names-json").out) == {"Acme-Proj-One": "frontbox-portal"}


def test_an_install_with_no_alias_at_all_is_left_out(machine) -> None:
    connect(machine, "frontbox-portal", "Acme-Proj-One")
    connect(machine, "unnamed-checkout", None)

    assert json.loads(run("status", "--names-json").out) == {"Acme-Proj-One": "frontbox-portal"}


def test_two_checkouts_under_one_alias_give_one_line(machine) -> None:
    connect(machine, "frontbox-portal", "Acme-Proj-One")
    connect(machine, "frontbox-portal-worktree", "Acme-Proj-One")

    names = json.loads(run("status", "--names-json").out)
    assert names == {"Acme-Proj-One": "frontbox-portal"}, "one line an alias, the first folder"


def test_a_folder_name_that_is_not_ascii_survives_being_pasted(machine) -> None:
    connect(machine, "Crème brûlée", "Acme-Proj-One")

    assert json.loads(run("status", "--names-json").out) == {"Acme-Proj-One": "Crème brûlée"}


def test_a_machine_with_nothing_connected_prints_an_empty_object(machine) -> None:
    result = run("status", "--names-json")

    assert json.loads(result.out) == {}
    assert result.code == 0
    assert "0 of them" in result.err


def test_status_without_the_flag_is_unchanged_and_prints_no_json(machine, monkeypatch) -> None:
    connect(machine, "frontbox-portal", "Acme-Proj-One")

    # The plain listing asks each project's stack for its stage, which is the
    # difference the tests above rest on: with the flag, nothing is asked at
    # all. Here the ask is allowed, and answered the way an unreachable stack
    # answers, so the listing runs without a network.
    def unreachable(*args: Any, **kwargs: Any):
        raise installer.Unreachable("no stack in a test")

    monkeypatch.setattr(installer, "http_json", unreachable)

    result = run("status")

    assert "1 connected" in result.out
    assert "project Acme-Proj-One" in result.out
    assert "stage unknown" in result.out
    assert not result.out.lstrip().startswith("{")


def test_the_flag_is_documented_where_a_reader_would_look() -> None:
    source = (ROOT / "src" / "threefold" / "tools" / "threefold_install.py").read_text(encoding="utf-8")
    assert "threefold_install.py status [--names-json]" in source, "in the usage at the top of the file"
    assert "--names-json" in installer.USAGE, "and in what --help prints"
    help_text = installer.command_parser().parse_args(["status", "--names-json"])
    assert help_text.names_json is True
