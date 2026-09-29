"""The export button waits for a certificate, as the freeze button waits for a session.

UAT found `btnExportCert` enabled before any run while `downloadCert` silently
returned without certificate data. The button now starts disabled with a title
that says why, and the only writer of `currentCertData` enables it.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "src" / "threefold" / "web" / "index.html"

TITLE = "Run a scenario first, so there is a certificate of your own to export."


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_the_export_button_starts_disabled_with_its_reason() -> None:
    match = re.search(r'<button[^>]*id="btnExportCert"[^>]*>', _page())
    assert match, "btnExportCert is gone from the page"
    tag = match.group(0)
    assert "disabled" in tag, "The export button must wait for a run"
    assert f'title="{TITLE}"' in tag, "The disabled button must say what it waits for"


def test_one_writer_sets_the_certificate_and_enables_the_button() -> None:
    text = _page()
    assert "function setCertData(data)" in text, "The single writer is gone"
    assert "getElementById('btnExportCert').disabled = false" in text, (
        "Setting the certificate must enable the export button"
    )
    assert text.count("setCertData(") == 3, (
        "One definition and two calls (the live run and the offline replay); "
        "a third writer must enable the button too"
    )
    assert len(re.findall(r"^ *currentCertData = ", text, re.M)) == 1, (
        "Only setCertData may assign currentCertData"
    )
