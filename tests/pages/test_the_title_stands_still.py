"""Moving between the dashboard's screens and the other pages, the header, the navigation and the title stand still.

A review found the title jumping from page to page: a 24 px heading on the
dashboard's screens, a 40 px one under an eyebrow on the others, and the
navigation a few pixels apart, because the dashboard drew a header of its own.
Every page now wears the design system's shell and page head, so the brand, the
navigation's mount and the title are measured here in a real browser, at a
desk's width and a phone's, and must be where they are on every page. The
walkthrough (#/try) kept a small title of its own in its column of steps, and
sign-in (#/signin, the header's Sign in) a centred one in its card, until a
second review found the title still jumping there; both wear the same head.

These open the pages as the stack serves them in headless Chrome, Chromium or
Edge through _headless.py, with no network, and skip where none is installed.
With no network the Tailwind CDN never arrives, which is why the shell and the
page head depend on no utility class: what is measured is the design system's.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from _headless import measure

WHOAMI = {"authenticated": False, "via": None, "expires_at": None, "reads_public": True, "sandbox_writes": True}

# The dashboard's screens, by the hash they are opened at, and the shell pages.
# swagger.html wears the same head (test_the_design_system.py pins its markup),
# but its script is Swagger UI's, from a CDN no test here reaches.
PAGES = [
    ("dashboard.html", "#/overview"),
    ("dashboard.html", "#/review"),
    ("dashboard.html", "#/projects/Acme-Billing"),
    ("dashboard.html", "#/proof"),
    ("dashboard.html", "#/try"),
    ("dashboard.html", "#/signin"),
    ("rules.html", ""),
    ("sessions.html", ""),
    ("connect.html", ""),
    ("settings.html", ""),
]

PROBE = r"""() => {
  // The content box: a breadcrumb's padding, which its margins take back, is room for a focus ring, not a line.
  const box = e => {
    if (!e) return null;
    const b = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    const pad = side => parseFloat(s['padding' + side]) || 0;
    return { top: Math.round(b.top + pad('Top')), left: Math.round(b.left + pad('Left')), height: Math.round(b.height - pad('Top') - pad('Bottom')) };
  };
  const title = document.querySelector('main h1');
  const head = title && title.closest('.tf-page-head');
  const lead = head && head.querySelector('.tf-eyebrow, .tf-crumbs');
  return {
    title: Object.assign(box(title) || {}, { size: title ? getComputedStyle(title).fontSize : null }),
    lead: box(lead), primitive: !!head && !!lead,
    brand: box(document.querySelector('.tf-header .tf-brand')),
    mount: box(document.querySelector('.tf-header .tf-nav-mount')),
    header: box(document.querySelector('.tf-header')),
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth
  };
}"""


def _where(page: str, hash_: str, width: int, tmp_path: Path) -> dict:
    result = measure(
        page,
        tmp_path / (page.replace(".", "-") + hash_.replace("#/", "-").replace("/", "-")),
        width=width,
        height=900,
        replies={"/api/auth/whoami": {"status": 200, "body": WHOAMI}},
        moments={"drawn": 1200},
        probe=PROBE,
        # No network reaches the Tailwind CDN here, so each page's one line of
        # Tailwind configuration is given something to configure.
        before="window.tailwind = {};" + (f"history.replaceState(null, '', {hash_!r});" if hash_ else ""),
    )
    return result["taken"]["drawn"]


@pytest.mark.parametrize("width", [1440, 375])
def test_the_header_the_navigation_and_the_title_stand_where_they_stood(tmp_path: Path, width: int) -> None:
    seen = {page + hash_: _where(page, hash_, width, tmp_path) for page, hash_ in PAGES}
    first = seen["dashboard.html#/overview"]
    for name, where in seen.items():
        assert where["primitive"], f"{name}: the title is the design system's page head, with its eyebrow or breadcrumb"
        assert where["title"] == first["title"], f"{name}: the title stands where the overview's does ({where['title']} against {first['title']})"
        assert where["lead"] == first["lead"], f"{name}: the eyebrow or breadcrumb is one line, where the overview's is"
        for part in ("header", "brand", "mount"):
            assert where[part] == first[part], f"{name}: the {part} stands where the overview's does"
        assert where["scrollWidth"] <= where["clientWidth"], f"{name} scrolls sideways at {width} px"
    assert first["title"]["size"] == ("40px" if width == 1440 else "28px"), "The design system's h1 at both widths"
