"""The connect page names the merge gate, the enforcement that needs no install.

Live judgment found the merge gate invisible in the product: hooks are
voluntary on laptops, and nothing on any page said the merge is judged.
The connect page now carries the backstop where the install story ends.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "src" / "threefold" / "web" / "connect.html"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_the_merge_gate_has_its_own_section() -> None:
    text = _page()
    assert 'id="merge-gate"' in text, "The merge gate section is gone"
    section = text.split('id="merge-gate"')[1].split("</section>")[0]
    assert "pull request" in section.lower(), "The section must say what is judged"
    assert "scripts/judge_pr.py" in section, "The section must name the judge"
    assert "required check" in section, "The section must say what enforces it"
    assert "fails closed" in section or "fail closed" in section.replace("-", " "), (
        "The section must say unjudged fails the merge"
    )
    assert "https://github.com/upgradedev/aws-threefold/pull/6" in section, (
        "The red-to-green proof must link to PR #6"
    )


def test_the_section_sits_after_the_install_story() -> None:
    text = _page()
    assert text.index('id="govern-your-repositories"') < text.index('id="merge-gate"'), (
        "The backstop reads after the install it backs"
    )
    assert not re.search(r"#/default/[A-Za-z0-9_]+", text.split('id="merge-gate"')[1].split("</section>")[0]), (
        "The section adds no spec deep link, so the contract-link test needs no update"
    )
