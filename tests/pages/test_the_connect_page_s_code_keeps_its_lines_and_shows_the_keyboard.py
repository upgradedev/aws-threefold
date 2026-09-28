"""On connect.html a command keeps its lines whole, and a block that scrolls is a named stop that shows the keyboard.

A review found the page's code blocks scrolling sideways and taking the
keyboard's focus with no ring at all (WCAG 2.4.7): a browser makes a block
that scrolls focusable, and the design system's focus rule did not reach a
pre. It also found the one-command PowerShell line wrapping inside its flags
at 375 px, so a reader retyping it saw broken flags. Each block now keeps its
lines whole and scrolls sideways where it is wider than its box, and while it
is, the design system marks it a keyboard stop, a region named for what it
holds and announced as scrolling; one that fits is none of these, so a desk's
Tab does not stop where nothing scrolls.

The real-browser checks open the page as the stack serves it in headless
Chrome, Chromium or Edge through _headless.py, with no network, and skip where
none is installed. Whether a given block is wider than its box is the fonts'
to decide, so they assert the rule (wide if and only if a stop, with its role
and name), not which blocks are wide; the one layout they pin is that the one
command is wider than a phone.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from _browser import page_source
from _headless import measure

WHOAMI = {"authenticated": False, "via": None, "expires_at": None, "reads_public": True, "sandbox_writes": True}

# Called at "drawn" and again at "ring". The first call measures every block
# and gives the keyboard's focus to the first that scrolls; the second reads
# its ring where it comes to rest. Under reduced motion the design system
# gives every property a 1 ms transition, which a headless browser's virtual
# clock does not run, so the probe finishes it first.
PROBE = r"""() => {
  if (window.__focused) {
    const a = document.activeElement;
    (a.getAnimations ? a.getAnimations() : []).forEach(t => t.finish());
    const s = getComputedStyle(a);
    return { id: a.getAttribute('data-tf-scroll'), style: s.outlineStyle, width: s.outlineWidth, color: s.outlineColor };
  }
  window.__focused = true;
  const shown = e => e.getClientRects().length > 0;
  const wide = e => e.scrollWidth > e.clientWidth + 1;
  const blocks = Array.from(document.querySelectorAll('pre[data-tf-scroll]')).filter(shown);
  const command = document.querySelector('#connect-in-one-command .tf-tabpanel:not([hidden]) pre');
  const first = blocks.find(wide);
  if (first) first.focus();
  return {
    blocks: blocks.map(b => ({ id: b.id, name: b.getAttribute('data-tf-scroll'), wide: wide(b), tabindex: b.getAttribute('tabindex'), role: b.getAttribute('role'), label: b.getAttribute('aria-label') })),
    unmarked: Array.from(document.querySelectorAll('pre:not([data-tf-scroll])')).filter(shown).length,
    command: command && { id: command.id, wide: wide(command), whiteSpace: getComputedStyle(command).whiteSpace },
    focused: !!first && document.activeElement === first && first.matches(':focus-visible'),
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth
  };
}"""


def _measured(tmp_path: Path, width: int) -> dict:
    result = measure(
        "connect.html",
        tmp_path,
        width=width,
        height=900,
        replies={"/api/auth/whoami": {"status": 200, "body": WHOAMI}},
        moments={"drawn": 1200, "ring": 1500},
        probe=PROBE,
        # No network reaches the Tailwind CDN here, so the page's one line of
        # Tailwind configuration is given something to configure.
        before="window.tailwind = {};",
    )
    return result["taken"]


@pytest.mark.parametrize("width", [1440, 375])
def test_a_block_that_scrolls_is_a_named_stop_and_one_that_fits_is_not(tmp_path: Path, width: int) -> None:
    taken = _measured(tmp_path, width)
    seen, ring = taken["drawn"], taken["ring"]
    assert seen["blocks"] and seen["unmarked"] == 0, "Every code block on the page is one the design system measures"
    for block in seen["blocks"]:
        said = (block["tabindex"], block["role"], block["label"])
        if block["wide"]:
            assert said == ("0", "region", block["name"] + " (scrolls sideways)"), f"{block['name']} scrolls, and says so"
        else:
            assert said == (None, None, None), f"{block['name']} fits, so it is not a stop"
    assert seen["scrollWidth"] <= seen["clientWidth"], f"The page scrolls sideways at {width} px"
    if width == 375:
        assert seen["command"] == {"id": seen["command"]["id"], "wide": True, "whiteSpace": "pre"}, \
            "The one command keeps its flags on one line and scrolls, rather than wrapping inside them"
        assert seen["focused"], "A block that scrolls takes the keyboard"
        assert (ring["style"], ring["width"], ring["color"]) == ("solid", "2px", "rgb(167, 139, 250)"), \
            f"It shows where the keyboard is, in the design system's ring ({ring})"


def test_every_block_is_measured_again_when_the_page_writes_into_it() -> None:
    """New text does not change a block's size, so no observer sees it: the page asks for the measure itself."""
    page = page_source("connect.html")
    script = page.split("<script>\n    const NAV", 1)[1]
    assert "Threefold.fitScrollBoxes(document.getElementById('main'))" in script
    for writer in ("function renderSnippets()", "function renderDecision()", "async function probe(caseName)", "(function chooseShell()"):
        body = script.split(writer, 1)[1].split("\n    }", 1)[0]
        assert "fitCode();" in body, f"{writer} writes into a block and does not measure it again"
    assert "pre:focus-visible { outline: var(--tf-focus-width) solid var(--tf-focus); outline-offset: var(--tf-focus-offset); }" in page, \
        "A block a browser makes focusable without the design system's mark still shows the ring"
    commands = re.findall(r'<pre class="([^"]*)" id="oneline-(?:posix|powershell)"', page)
    assert commands == ["tf-pre code", "tf-pre code"], "The one command is a block that keeps its lines whole"
