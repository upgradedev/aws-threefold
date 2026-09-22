"""Runs a page's own script under Node, with the shared layer it loads.

The pages are served with their base path substituted, and so is the shared
layer, so both are read through the handler's own loader rather than copied
here. The stub below is the handful of browser objects the pages touch: an
element store that makes an element on first use, a location whose hash change
fires `hashchange` as a browser does, a history that rewrites the hash without
firing, a localStorage that can be made to throw, a recording fetch that answers
from a map the scenario sets, and intervals that run only when a scenario says so.
Those skip where Node is absent; the rest of the suite needs nothing but Python.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from threefold.interfaces.api_handlers import _read_web_asset

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "src" / "threefold" / "web"

STUB = r"""
const elements = {};
function makeEl(id) {
  const attrs = {};
  const classes = new Set();
  const node = {
    id, value: '', checked: false, disabled: false, textContent: '', clientWidth: 640, parentNode: null, _html: '',
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    get className() { return Array.from(classes).join(' '); },
    set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach(c => classes.add(c)); },
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle: (c, on) => { const want = on === undefined ? !classes.has(c) : !!on; if (want) classes.add(c); else classes.delete(c); return want; }
    },
    setAttribute(n, v) { attrs[n] = String(v); },
    getAttribute(n) { return Object.prototype.hasOwnProperty.call(attrs, n) ? attrs[n] : null; },
    hasAttribute(n) { return Object.prototype.hasOwnProperty.call(attrs, n); },
    focus() { document.activeElement = this; },
    addEventListener(type, fn) { (this.listeners = this.listeners || {})[type] = fn; },
    removeChild() {}
  };
  return node;
}
function el(id) { return elements[id] || (elements[id] = makeEl(id)); }
const docListeners = {};
globalThis.window = globalThis;
globalThis.document = {
  getElementById: el,
  addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
  removeEventListener: (t, fn) => { docListeners[t] = (docListeners[t] || []).filter(f => f !== fn); },
  querySelectorAll: () => [],
  visibilityState: 'visible',
  activeElement: null,
  title: ''
};
const winListeners = {};
globalThis.addEventListener = (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); };
globalThis.removeEventListener = (t, fn) => { winListeners[t] = (winListeners[t] || []).filter(f => f !== fn); };
globalThis.scrollTo = () => {};
let currentHash = '';
const replaced = [];
globalThis.location = {
  origin: 'https://example.test', pathname: '/prod/dashboard.html', search: '',
  get href() { return this.origin + this.pathname + this.search + currentHash; },
  set href(v) { const i = String(v).indexOf('#'); currentHash = i >= 0 ? String(v).slice(i) : ''; },
  get hash() { return currentHash; },
  set hash(v) {
    v = String(v);
    if (v && v[0] !== '#') v = '#' + v;
    if (v === currentHash) return;
    currentHash = v;
    setTimeout(() => (winListeners.hashchange || []).forEach(f => f({})), 0);
  }
};
globalThis.history = {
  replaceState: (state, title, url) => {
    replaced.push(String(url));
    const i = String(url).indexOf('#');
    currentHash = i >= 0 ? String(url).slice(i) : '';
  }
};
const store = {};
let storageBlocked = false;
globalThis.localStorage = {
  getItem: k => { if (storageBlocked) throw new Error('blocked'); return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem: (k, v) => { if (storageBlocked) throw new Error('blocked'); store[k] = String(v); },
  removeItem: k => { if (storageBlocked) throw new Error('blocked'); delete store[k]; }
};
const copied = [];
Object.defineProperty(globalThis, 'navigator', {
  configurable: true, writable: true,
  value: { userAgent: 'Node', platform: 'Linux', clipboard: { writeText: async text => { copied.push(text); } } }
});
globalThis.tailwind = {};
const intervals = new Map();
let intervalSeq = 0;
globalThis.setInterval = (fn, ms) => { intervalSeq += 1; intervals.set(intervalSeq, { fn, ms }); return intervalSeq; };
globalThis.clearInterval = id => { intervals.delete(id); };
const calls = [];
// answer(url, init) returns {status, body}, 'network' for an unreachable
// service, or a promise of either.
let answer = () => ({ status: 404, body: { detail: 'no stub' } });
globalThis.fetch = async (url, init) => {
  init = init || {};
  calls.push({ url, method: init.method || 'GET', headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : null });
  const reply = await answer(url, init);
  if (reply === 'network') throw new TypeError('Failed to fetch');
  return { ok: reply.status < 400, status: reply.status, json: async () => reply.body };
};
// Answers from a map of 'METHOD /path' or '/path' to a reply or a function of
// (url, init, body). The stage prefix is taken off first.
function api(map) {
  return (url, init) => {
    const u = new URL(url);
    const path = u.pathname.replace(/^\/prod/, '');
    const method = (init && init.method) || 'GET';
    const hit = map[method + ' ' + path] !== undefined ? map[method + ' ' + path] : map[path];
    if (hit === undefined) return { status: 404, body: { detail: 'no stub for ' + method + ' ' + path } };
    return typeof hit === 'function' ? hit(u, init, init && init.body ? JSON.parse(init.body) : null) : hit;
  };
}
function held() { let release; const promise = new Promise(r => { release = r; }); return { promise, release }; }
const tick = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setTimeout(r, 0)); };
async function runIntervals() { for (const entry of Array.from(intervals.values())) entry.fn(); await tick(); }
async function visit(hash) { location.hash = hash; await tick(); }
// The address a page is opened at: set before it loads, and, as in a browser,
// without a hashchange.
function openAt(hash) { currentHash = hash; }
function view() { return el('view').innerHTML; }
function text(markup) { return String(markup).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim(); }
function metrics(markup) {
  const found = {};
  const re = /data-metric="([^"]+)"[^>]*>([^<]*)</g;
  let m;
  while ((m = re.exec(markup))) found[m[1]] = m[2].trim();
  return found;
}
function click(action, attrs) { return Dash.act(action, Object.assign({}, attrs || {})); }
"""


def page_source(filename: str) -> str:
    """The page or asset as the stack serves it at the /prod stage."""
    served = _read_web_asset(filename, "prod")
    assert served is not None, f"{filename} is missing from src/threefold/web"
    return served


def inline_scripts(markup: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", markup, re.S)


def run(page: str, scenario: str, tmp_path: Path, before: str = "", shared: bool = True, pathname: str | None = None) -> dict:
    """Runs `page`'s inline scripts, with the shared layer first when it loads it.

    `before` runs ahead of every script, for what a page reads as it loads, such
    as the address it was opened at or what the browser has stored.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not on PATH, so the page's script cannot be run here")
    markup = page_source(page)
    scripts = inline_scripts(markup)
    assert scripts, f"{page} carries no inline script"
    layer = page_source("assets/threefold.js") if shared else ""
    where = f"location.pathname = {json.dumps(pathname or '/prod/' + page)};\n"
    program = tmp_path / "page.js"
    program.write_text(
        STUB
        + where
        + before
        + "\n;\n"
        + layer
        + "\n;\n"
        + "\n;\n".join(scripts)
        + "\n;\n(async () => {\n  const out = {};\n  await tick();\n"
        + scenario
        + "\n  process.stdout.write(JSON.stringify(out), () => process.exit(0));\n})()"
        + ".catch(err => { process.stderr.write(String((err && err.stack) || err), () => process.exit(1)); });\n",
        encoding="utf-8",
    )
    finished = subprocess.run([node, str(program)], capture_output=True, text=True, timeout=90, encoding="utf-8")
    assert finished.returncode == 0, finished.stderr
    return json.loads(finished.stdout)
