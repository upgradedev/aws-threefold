"""Figures and promises a document makes about files in this repository.

Two ways a document goes quietly wrong. It writes down a number taken from a
file (the runbook's template sizes, which the template outgrew), or it promises
a file that is not there (the enforcement evidence's rerunnable script). Both
are checked here against the repository itself, so the document fails rather
than a reader finding out.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# What CloudFormation accepts for a template sent inline, in bytes.
INLINE_TEMPLATE_LIMIT = 51200


def _text(name: str) -> str:
    return " ".join((ROOT / name).read_text(encoding="utf-8").split())


def _size(name: str) -> int:
    """Bytes on disk. `.gitattributes` forces LF, so this is the committed size."""
    return len((ROOT / name).read_bytes())


def test_the_runbook_states_the_inline_limit_and_no_size_that_goes_stale() -> None:
    runbook = _text("docs/RUNBOOK.md")
    assert "51,200 bytes" in runbook, "The runbook's deploy section must name the limit it works around"

    # The only byte count in the runbook is the limit itself. A size read off a
    # file is wrong the next time that file is edited, which is how this broke.
    counts = set(re.findall(r"\d{1,3},\d{3} bytes", runbook))
    assert counts == {"51,200 bytes"}, f"The runbook records sizes that go stale: {sorted(counts - {'51,200 bytes'})}"

    template = _size("deploy/template.yml")
    edge = _size("deploy/edge.yml")
    assert template < INLINE_TEMPLATE_LIMIT, "The template no longer fits even through the packaging bucket's own limit"
    assert INLINE_TEMPLATE_LIMIT - template <= 2048, (
        f"The runbook says the template is within two kilobytes of the limit; it has {INLINE_TEMPLATE_LIMIT - template}"
    )
    assert INLINE_TEMPLATE_LIMIT - edge > 2048, (
        f"The runbook calls edge.yml comfortably under the limit; it has {INLINE_TEMPLATE_LIMIT - edge} bytes left"
    )


def test_the_enforcement_evidence_promises_no_script_that_is_not_committed() -> None:
    evidence = _text("docs/evidence/ENFORCEMENT_2026-09-21.md")
    assert "the check is a script that can be rerun" not in evidence
    assert "No script in this repository runs it" in evidence

    named = set(re.findall(r"scripts/[A-Za-z0-9_]+\.py", evidence))
    missing = {name for name in named if not (ROOT / name).exists()}
    # A script the evidence names as existing would have to be committed; the
    # one it names as missing is named in the sentence that says it is missing.
    for name in missing:
        assert f"a committed `{name}` would make this table" in evidence, f"{name} is promised but not committed"
