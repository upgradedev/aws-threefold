"""The installer and the check moved into src, and the old paths still work.

The stack is packaged from src/, so the installer it serves at /install.py and
the check it packs into its bundle have to live there. Everything that knew
them by their old paths, a README line, a page, a test that loads them with
importlib, a developer's shell history, keeps working through a shim in
scripts/ that runs the moved file in its own namespace. These tests pin what
that promise means: every name is there, a name replaced on the shim is
replaced for the functions that use it, and the paths the moved files derive
point at the checkout's src rather than at scripts/.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "src" / "threefold" / "tools"
SCRIPTS = ROOT / "scripts"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["threefold_install", "threefold_cli"])
def test_the_shim_exposes_every_name_the_moved_file_defines(name) -> None:
    moved = _load(TOOLS / f"{name}.py", f"{name}_moved")
    shim = _load(SCRIPTS / f"{name}.py", f"{name}_shim")
    public = {key for key in vars(moved) if not key.startswith("__")}
    assert public <= set(vars(shim)), sorted(public - set(vars(shim)))
    assert shim.__doc__ == moved.__doc__, "the moved file's own description, for --help"
    assert Path(shim.__file__) == TOOLS / f"{name}.py"


@pytest.mark.parametrize("name", ["threefold_install", "threefold_cli"])
def test_a_name_replaced_on_the_shim_is_replaced_for_its_functions(name) -> None:
    shim = _load(SCRIPTS / f"{name}.py", f"{name}_shim_globals")
    assert shim.main.__globals__ is vars(shim)


def test_the_installer_finds_the_hook_the_check_and_the_engine_from_its_new_place() -> None:
    installer = _load(SCRIPTS / "threefold_install.py", "threefold_install_paths")
    assert installer.HERE == TOOLS
    assert installer.HOOK_SOURCE == ROOT / "src" / "threefold" / "hooks" / "threefold_hook.py"
    assert installer.CLI_SOURCE == TOOLS / "threefold_cli.py"
    assert installer.HOOK_SOURCE.is_file() and installer.CLI_SOURCE.is_file()
    assert (installer.SOURCE / "threefold" / "domain" / "layering_rules.py").is_file()


def test_the_check_finds_the_hook_from_its_new_place() -> None:
    cli = _load(SCRIPTS / "threefold_cli.py", "threefold_cli_paths")
    assert Path(cli.load_hook().__file__) == ROOT / "src" / "threefold" / "hooks" / "threefold_hook.py"


@pytest.mark.parametrize(("script", "argv", "expected"), [
    ("threefold_install.py", ["--help"], "usage: threefold_install.py"),
    ("threefold_cli.py", ["check", "--help"], "usage: threefold_cli.py check"),
])
@pytest.mark.parametrize("where", ["scripts", "tools"])
def test_both_paths_still_run_as_scripts(script, argv, expected, where) -> None:
    path = (SCRIPTS if where == "scripts" else TOOLS) / script
    result = subprocess.run([sys.executable, str(path), *argv], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
