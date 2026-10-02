"""What the second judge review found the gates letting through, or refusing wrongly.

Each case was reproduced against the evaluator before the fix: package
imports named after `import`, nested TypeScript infrastructure paths, other
spellings of a recursive removal of the root, pushes that are not forced, and
five credential shapes the scanner did not know.
"""
from __future__ import annotations

import time

import pytest

from threefold.domain.boundary_guard import ArchitecturalBoundaryGuard, SecretScanner
from threefold.domain.layering_rules import DEFAULT_RULES, violations

PY = "src/acme/domain/order.py"
TS = "src/web/domain/order.ts"


@pytest.mark.parametrize(
    "path, content",
    [
        (PY, "from acme import infrastructure\n"),
        (PY, "from .. import adapters\n"),
        (PY, "from acme import (\n    money,\n    infrastructure,\n)\n"),
        (PY, "from acme import money as m, adapters as a\n"),
        (TS, "import { pool } from '../infrastructure/db/pool'\n"),
        (TS, "import { store } from '../infrastructure'\n"),
        (TS, "import { client } from '../adapters/http/client'\n"),
    ],
)
def test_a_layer_reached_by_another_spelling_is_refused(path, content) -> None:
    assert violations(path, content, DEFAULT_RULES)[0], content


@pytest.mark.parametrize(
    "path, content",
    [
        (PY, "from acme.domain import Order\nfrom typing import List\n"),
        (TS, "import { money } from './money'\n"),
        (TS, "import { x } from './infrastructure-free'\n"),
    ],
)
def test_ordinary_imports_stay_allowed(path, content) -> None:
    assert not violations(path, content, DEFAULT_RULES)[0], content


def _destructive(command: str) -> bool:
    return any(pattern.search(command) for pattern in ArchitecturalBoundaryGuard.DESTRUCTIVE_COMMANDS)


@pytest.mark.parametrize(
    "command",
    ["rm -fr /", "rm -r -f ~", "rm --recursive --force /", 'rm -rf "$HOME"', "git push origin main --force"],
)
def test_every_spelling_of_a_destructive_command_is_caught(command) -> None:
    assert _destructive(command)


@pytest.mark.parametrize(
    "command",
    [
        "rm -f ~/notes.txt",
        "git push --force-with-lease origin feature",
        "git push origin release-f",
        "git push origin main && rm -f build.log",
    ],
)
def test_commands_that_are_not_destructive_pass(command) -> None:
    assert not _destructive(command)


def test_long_runs_of_flags_and_pushes_stay_linear() -> None:
    for command in ("rm " + "--rm " * 150_000, "rm " + "-rm " * 200_000, "git push " * 100_000):
        started = time.monotonic()
        _destructive(command)
        assert time.monotonic() - started < 2, command[:20]


@pytest.mark.parametrize(
    "secret, label",
    [
        ("github_pat_" + "A1b2" * 8, "GITHUB_FINE_GRAINED_TOKEN"),
        ("glpat-" + "x" * 20, "GITLAB_TOKEN"),
        ("sk_" + "live_" + "a1" * 12, "STRIPE_LIVE_KEY"),
        ("npm_" + "a1B2" * 9, "NPM_TOKEN"),
        ("pypi-AgE" + "a" * 60, "PYPI_TOKEN"),
    ],
)
def test_the_new_credential_shapes_are_caught(secret, label) -> None:
    clean, description = SecretScanner.scan_payload(f"token = '{secret}'")
    assert not clean and label in description


def test_a_shape_inside_a_longer_word_is_not_a_credential() -> None:
    assert SecretScanner.scan_payload("xgithub_pat_" + "A" * 30)[0]
    assert SecretScanner.scan_payload("ask_" + "live_" + "a" * 24)[0]
