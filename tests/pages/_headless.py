"""Opens a page in a real headless browser, at a phone's or a desk's size, and measures it.

The stub in _browser.py runs a page's script but lays nothing out, so a claim
about where things sit on the screen (nothing moves while the verdict lands,
nothing scrolls sideways at 375 px, a bar keeps to one line) needs a real
engine. This opens the page as the stack serves it, in Chrome, Chromium or
Edge, whichever is installed, headless and with no network at all: every host
name resolves nowhere, and the page's fetch is replaced before its first script
runs by one that answers from the replies a test gives, after the delays it
gives, in the browser's virtual time. The page sits in a frame of the size under
test, because a headless window will not go narrower than about 500 px, and
the measurements a test asks for are posted out of the frame and read from the
dumped document.

Where no such browser is installed, or one is found and will not start, the
test is skipped; a page that starts and measures wrong fails like any test.
Set THREEFOLD_TEST_BROWSER to the executable to choose one.
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _browser import page_source

_CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge", "microsoft-edge",
)
_KNOWN_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

# Replaces fetch before any script of the page runs. REPLIES maps the end of a
# path ('/api/overview', 'POST /evaluate-tool-call') to {status, body, delay},
# or to {network: true, delay} for a stack that cannot be reached, or to
# {never: true} for one that never answers; any of them may carry
# measure: '<name>' to measure the page the moment it asks. The fetch honours
# an abort, as a browser's does.
_FETCH = r"""
window.__replies = REPLIES;
window.__asked = [];
window.fetch = function (url, init) {
  init = init || {};
  const method = (init.method || 'GET').toUpperCase();
  const path = String(url).split('?')[0];
  window.__asked.push(method + ' ' + path);
  const key = Object.keys(window.__replies).find(k => {
    const parts = k.split(' ');
    const want = parts.length > 1 ? parts[1] : parts[0];
    return (parts.length === 1 || parts[0] === method) && path.slice(-want.length) === want;
  });
  const reply = key ? window.__replies[key] : { status: 404, body: { detail: 'no reply for ' + method + ' ' + path } };
  // A reply may ask for the page to be measured the moment it is asked for:
  // what the page looks like while it waits for exactly this answer.
  if (reply.measure && window.__take) window.__take(reply.measure);
  return new Promise((resolve, reject) => {
    if (init.signal) init.signal.addEventListener('abort', () => reject(new DOMException('The call was cancelled', 'AbortError')));
    if (reply.never) return;
    setTimeout(() => {
      if (reply.network) return reject(new TypeError('Failed to fetch'));
      resolve(new Response(JSON.stringify(reply.body), { status: reply.status, headers: { 'Content-Type': 'application/json' } }));
    }, reply.delay || 0);
  });
};
"""

# The measuring side: MOMENTS maps a name to a time in milliseconds after the
# document was parsed and the page's own scripts had run, and at each the
# PROBE function is called and what it returns is kept. After the last one
# everything is posted to the frame's parent, with any error the page raised.
_PROBE = r"""
window.__errors = [];
window.addEventListener('error', e => window.__errors.push(String(e.message || e)));
window.addEventListener('unhandledrejection', e => window.__errors.push('unhandled: ' + String(e.reason && e.reason.message || e.reason)));
const taken = {};
window.__take = function (name) {
  try { taken[name] = (PROBE)(); } catch (err) { taken[name] = { error: String(err && err.stack || err) }; }
};
document.addEventListener('DOMContentLoaded', function () {
  const moments = MOMENTS;
  const names = Object.keys(moments);
  const last = Math.max.apply(null, names.map(n => moments[n]));
  names.forEach(name => setTimeout(() => {
    window.__take(name);
    if (moments[name] === last) parent.postMessage(JSON.stringify({ taken: taken, errors: window.__errors, asked: window.__asked }), '*');
  }, moments[name]));
});
"""

_FRAME = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>html, body {{ margin: 0; background: #000; }} iframe {{ display: block; border: 0; }}</style></head>
<body><iframe src="{page}" width="{width}" height="{height}" style="width:{width}px;height:{height}px"></iframe>
<pre id="measured"></pre>
<script>addEventListener('message', e => {{ document.getElementById('measured').textContent = String(e.data); }});</script>
</body></html>
"""


def browser() -> str | None:
    chosen = os.environ.get("THREEFOLD_TEST_BROWSER")
    if chosen:
        return chosen if Path(chosen).exists() or shutil.which(chosen) else None
    for name in _CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for path in _KNOWN_PATHS:
        if Path(path).exists():
            return path
    return None


def measure(
    page: str,
    tmp_path: Path,
    *,
    width: int,
    height: int,
    replies: dict,
    moments: dict,
    probe: str,
    before: str = "",
    reduced_motion: bool = True,
    budget_ms: int | None = None,
) -> dict:
    """The page at width x height, answered from `replies`, measured by `probe` at each of `moments`.

    `probe` is the source of a JavaScript function of no arguments that returns
    something JSON can carry. `before` runs ahead of the page's own scripts,
    after the fetch is replaced: a test uses it to seed what the browser has
    stored. Motion is reduced unless a test asks for it, so what is measured is
    where things end up rather than a frame of an animation.
    """
    exe = browser()
    if not exe:
        pytest.skip("No Chrome, Chromium or Edge is installed here, so the layout cannot be measured")
    site = tmp_path / "site"
    (site / "assets").mkdir(parents=True, exist_ok=True)
    injected = (
        "<script>\n"
        + _FETCH.replace("REPLIES", json.dumps(replies))
        + _PROBE.replace("MOMENTS", json.dumps(moments)).replace("PROBE", probe)
        + before
        + "\n</script>\n"
    )
    markup = page_source(page)
    head = markup.index("<head>") + len("<head>")
    (site / page).write_text(markup[:head] + "\n" + injected + markup[head:], encoding="utf-8")
    for asset in ("assets/threefold.css", "assets/threefold.js"):
        (site / asset).write_text(page_source(asset), encoding="utf-8")
    frame = site / "__frame.html"
    frame.write_text(_FRAME.format(page=page, width=width, height=height), encoding="utf-8")
    profile = tmp_path / "profile"
    budget = budget_ms if budget_ms is not None else max(moments.values()) + 1500
    command = [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--mute-audio",
        "--host-resolver-rules=MAP * ~NOTFOUND",
        f"--user-data-dir={profile}",
        f"--window-size={max(width, 600) + 40},{height + 200}",
        f"--virtual-time-budget={budget}",
        "--dump-dom",
        frame.resolve().as_uri(),
    ]
    if reduced_motion:
        command.insert(1, "--force-prefers-reduced-motion")
    if sys.platform.startswith("linux"):
        # A CI runner's kernel profile refuses the sandbox's namespaces; the
        # page is a local file with no network, so there is nothing to contain.
        command.insert(1, "--no-sandbox")
        command.insert(1, "--disable-dev-shm-usage")
    try:
        finished = subprocess.run(command, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as err:
        pytest.skip(f"{exe} would not run here: {err}")
    found = re.search(r'<pre id="measured">(.*?)</pre>', finished.stdout, re.S)
    if not found or not found.group(1).strip():
        pytest.skip(f"{exe} started but reported nothing (exit {finished.returncode}): {finished.stderr[-400:]}")
    result = json.loads(html.unescape(found.group(1)))
    assert not result["errors"], f"The page raised: {result['errors']}"
    for name, taken in result["taken"].items():
        assert not (isinstance(taken, dict) and "error" in taken), f"Measuring at {name} failed: {taken['error']}"
    return result
