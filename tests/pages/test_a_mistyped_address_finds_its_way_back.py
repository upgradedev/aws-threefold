"""A page for an address that has no page: Threefold's own, with a way back.

A review of the live edge found a mistyped address answered with the bucket's
raw XML error: no Threefold chrome, no link home, and the bucket's request ids
on show. `404.html` is that page. It is answered at any depth (`/nope.html`,
`/app/nothing`), so the shared layer is linked from the site's root, not from
beside the address asked for. The edge serves it with status 404 on the pages'
behavior alone, and only for a page address (no file type, or .html): the two
functions in `deploy/edge.yml`, held by `tests/unit/test_edge_page_not_found.py`.
A missing script keeps the bucket's 404 and an API path its problem document,
so the page claims no more than that.
"""
from __future__ import annotations

import re

from _browser import WEB


def _page() -> str:
    return (WEB / "404.html").read_text(encoding="utf-8")


def test_the_page_links_home_the_overview_and_connect() -> None:
    page = _page()
    for target in ("index.html", "dashboard.html#/overview", "connect.html"):
        assert f'href="/{target}" data-tf-page="{target}"' in page, f"No way to {target}"
    assert "<h1 " in page and page.count("<h1 ") == 1
    assert "Nothing lives at this address" in page


def test_the_shared_layer_is_linked_from_the_root_so_any_depth_finds_it() -> None:
    page = _page()
    assert '<link rel="stylesheet" href="__THREEFOLD_BASE_PATH__/assets/threefold.css" />' in page
    assert '<script src="__THREEFOLD_BASE_PATH__/assets/threefold.js"></script>' in page
    assert not re.search(r'(?:href|src)="assets/', page), "A relative asset breaks at /app/nothing"


def test_the_page_claims_only_the_addresses_the_edge_answers_it_for() -> None:
    page = _page()
    assert "<span>Served with status 404 for a page address that has no page</span>" in page
    assert "any address" not in page, "a missing script or an API path is not answered with this page"
    assert "/docs/x.md" not in page, "a file of another type keeps the bucket's own 404"


def test_the_address_asked_for_is_shown_as_text_only() -> None:
    script = _page().split("<script>", 1)[1].split("</script>", 1)[0]
    assert "textContent = path" in script
    assert "innerHTML" not in script, "The visitor's own address is never read as markup"
