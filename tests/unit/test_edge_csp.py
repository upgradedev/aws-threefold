"""The Content-Security-Policy the edge sends is one the pages actually run under.

A policy written from memory either breaks a page (a stylesheet the browser
silently refuses) or is wider than it needs to be. This test reads every page
in src/threefold/web and every script and stylesheet in its assets/, after the
base path is substituted as publish_web.py substitutes it, and checks what
each would load against the policy the way a browser matches sources: scheme
and host exactly, and a path that ends in "/" as a prefix, otherwise exactly.

What the scan reads, in a page:
- elements: <script src>, <link rel=stylesheet|icon|preload|manifest>, <img
  src> and srcset, <source>, <video>, <audio>, <track>, and any frame, object
  or embed, which the policy refuses outright;
- inline script (script bodies, on* handlers, javascript: URLs) and inline
  style (<style> blocks and style attributes), which need 'unsafe-inline';
- inside inline script and asset scripts: code built from a string (eval,
  new Function, Function(), setTimeout or setInterval with a string), which
  needs the 'unsafe-eval' the policy withholds, and every absolute URL written
  as a string literal, which some directive has to allow: script-src for .js,
  style-src for .css, font-src for a font, img-src for an image, and any fetch
  directive otherwise (fetch(), a beacon, an injected <script src>);
- inside <style>, style attributes and asset stylesheets: @import (style-src)
  and every url() (font-src for a font, img-src otherwise).

Skipped on purpose, each for a stated reason: a URL a string assigns to href,
location or window.open, since the policy does not govern navigation; XML
namespace URIs (www.w3.org), which name a vocabulary and are never fetched;
and the pages' loopback fallback (http://localhost:8001), which a page takes
only when it is opened from disk with the base path token still in place, so
it is excused only in a script that tests for that token.

What a static scan cannot see: a URL assembled at run time from pieces
("https://" + host), or one a library the page loads fetches by itself. A
literal whose host is an expression (`https://${host}/x`) is reported rather
than guessed at. The scan's own tests feed it a hostile page and a benign one,
so a scan gone blind fails here rather than passing over nothing.

It also checks the other way: every host the policy names is used by some
page, so a removed dependency does not leave its host allowed.

When this policy was first written (2026-09-22) it was walked in a browser,
served as a header: every page rendered, the Tailwind Play CDN styled them,
the Google Fonts loaded, Swagger UI rendered all its operations, inline
onclick handlers ran, and an unlisted script host was refused. The policy has
not changed since; a change to it, or a new page, needs that walk again.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

import pytest

WEB_ROOT = Path(__file__).resolve().parents[2] / "src" / "threefold" / "web"
EXAMPLE_API_DOMAIN = "abc123def4.execute-api.eu-west-1.amazonaws.com"
GOOGLE_FONTS_CSS = "fonts.googleapis.com"
GOOGLE_FONTS_FILES = "https://fonts.gstatic.com/s/"
BASE_PATH_TOKEN = "__THREEFOLD_BASE_PATH__"

FETCH_DIRECTIVES = ("connect-src", "script-src", "style-src", "img-src", "font-src")
FONT_SUFFIXES = (".woff", ".woff2", ".ttf", ".otf")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico")
# Namespace URIs name an XML vocabulary (createElementNS for inline SVG); nothing fetches them.
NAMESPACE_HOSTS = {"www.w3.org"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
# What the pages test for to know they were opened from disk. The token itself is
# substituted away, but this shorter literal survives substitution, so its presence
# in a rendered script shows the loopback URL sits behind that test.
FROM_DISK_GUARD = "THREEFOLD_BASE_PATH"

# Code built from a string, which runs only under 'unsafe-eval'.
STRING_EVALUATION = re.compile(
    r"""\beval\s*\(|\bnew\s+Function\s*\(|(?<![\w.$])Function\s*\(|\bset(?:Timeout|Interval)\s*\(\s*['"`]"""
)
# An absolute URL written as a string literal, in any of JavaScript's three quotes.
URL_LITERAL = re.compile(r"""(["'`])(https?://[^"'`\s<>\\]+)""")
# Text just before a literal that makes it a navigation target rather than a fetch.
NAVIGATION_BEFORE = re.compile(
    r"""(?:\bhref\s*=\s*\\?|\.href\s*=\s*|\blocation\s*=\s*|window\.open\(\s*|location\.(?:assign|replace)\(\s*)$"""
)


def _reader():
    name = "edge_cfn_yaml_reader"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_edge_cfn_yaml.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def policy() -> Dict[str, List[str]]:
    template = _reader().load_edge_template()
    headers = template["Resources"]["SecurityHeadersPolicy"]["Properties"]["ResponseHeadersPolicyConfig"]
    text = headers["SecurityHeadersConfig"]["ContentSecurityPolicy"]["ContentSecurityPolicy"]["Fn::Sub"]
    text = text.replace("${ApiDomainName}", EXAMPLE_API_DOMAIN)
    return {part.split()[0]: part.split()[1:] for part in text.split(";") if part.split()}


POLICY = policy()


def sources_for(directive: str) -> List[str]:
    """The sources a fetch directive allows, falling back to default-src as browsers do."""
    return POLICY.get(directive, POLICY.get("default-src", []))


def allows(directive: str, url: str) -> bool:
    """Whether a URL (absolute, or relative to the page's own origin) is allowed by a directive."""
    sources = sources_for(directive)
    parts = urlsplit(url)
    if not parts.scheme and not parts.netloc:
        return "'self'" in sources
    if parts.scheme == "data":
        return "data:" in sources
    for source in sources:
        if source.startswith("'") or source.endswith(":"):
            continue
        allowed = urlsplit(source)
        if allowed.scheme != parts.scheme or allowed.netloc != parts.netloc:
            continue
        if not allowed.path or allowed.path == "/":
            return True
        if allowed.path.endswith("/") and parts.path.startswith(allowed.path):
            return True
        if parts.path == allowed.path:
            return True
    return False


def directive_for(url: str) -> Optional[str]:
    """The one directive a URL's file type answers to, or None when it could be any fetch."""
    path = urlsplit(url).path.lower()
    if path.endswith((".js", ".mjs")):
        return "script-src"
    if path.endswith(".css"):
        return "style-src"
    if path.endswith(FONT_SUFFIXES):
        return "font-src"
    if path.endswith(IMAGE_SUFFIXES):
        return "img-src"
    return None


def _srcset(value: str) -> List[str]:
    return [candidate.split()[0] for candidate in value.split(",") if candidate.split()]


class _PageScan(HTMLParser):
    """What one page loads, and the inline script and style it runs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: List[str] = []
        self.stylesheets: List[str] = []
        self.images: List[str] = []
        self.fonts: List[str] = []
        self.media: List[str] = []
        self.manifests: List[str] = []
        self.frames: List[str] = []
        self.inline_script = False
        self.inline_style = False
        self.script_text: List[str] = []
        self.style_text: List[str] = []
        self._in_style = False
        self._in_inline_script = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attributes = {name: (value or "") for name, value in attrs}
        for name, value in attributes.items():
            if name.startswith("on"):
                self.inline_script = True
                self.script_text.append(value)
            elif name in ("href", "src", "action", "formaction") and value.strip().lower().startswith("javascript:"):
                self.inline_script = True
                self.script_text.append(value.strip()[len("javascript:"):])
        if "style" in attributes:
            self.inline_style = True
            self.style_text.append(attributes["style"])
        src = attributes.get("src", "")
        if tag == "script":
            if src:
                self.scripts.append(src)
            else:
                self._in_inline_script = True
        elif tag == "link":
            self._link(attributes)
        elif tag == "style":
            self.inline_style = True
            self._in_style = True
        elif tag == "img" or (tag == "input" and attributes.get("type", "").lower() == "image"):
            self.images += [src] if src else []
            self.images += _srcset(attributes.get("srcset", ""))
        elif tag == "source":
            # Inside <picture> a source offers images; inside <video> or <audio>, media.
            self.images += _srcset(attributes.get("srcset", ""))
            self.media += [src] if src else []
        elif tag in ("video", "audio", "track"):
            self.media += [src] if src else []
            self.images += [attributes["poster"]] if attributes.get("poster") else []
        elif tag in ("iframe", "frame", "object", "embed"):
            self.frames.append(src or attributes.get("data") or tag)

    def _link(self, attributes: Dict[str, str]) -> None:
        rel = attributes.get("rel", "").lower().split()
        href = attributes.get("href", "")
        if not href:
            return
        if "stylesheet" in rel:
            self.stylesheets.append(href)
        elif "icon" in rel or "apple-touch-icon" in rel:
            self.images.append(href)
        elif "manifest" in rel:
            self.manifests.append(href)
        elif {"preload", "prefetch", "modulepreload"} & set(rel):
            kind = attributes.get("as", "script" if "modulepreload" in rel else "").lower()
            target = {"script": self.scripts, "style": self.stylesheets, "font": self.fonts, "image": self.images}
            target.get(kind, self.media if kind in ("audio", "video", "track") else self.scripts).append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False
        if tag == "script":
            self._in_inline_script = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.style_text.append(data)
        if self._in_inline_script and data.strip():
            self.inline_script = True
            self.script_text.append(data)


def _imports(css: str) -> List[str]:
    """The stylesheets an @import names, whole: a Google Fonts URL carries ";" in its query."""
    urls = []
    for match in re.finditer(r"""@import\s+(url\([^)]*\)|'[^']*'|"[^"]*")""", css):
        token = match.group(1)
        if token.startswith("url("):
            token = token[len("url("):-1].strip()
        urls.append(token.strip("'\""))
    return urls


def _css_urls(css: str) -> List[str]:
    return [u.strip() for u in re.findall(r"""url\(\s*['"]?([^'")]+)""", css) if not u.strip().startswith("#")]


def render(text: str) -> str:
    """The file as the edge serves it: publish_web.py substitutes an empty base path."""
    return text.replace(BASE_PATH_TOKEN, "")


def scan_text(text: str) -> _PageScan:
    scanner = _PageScan()
    scanner.feed(text)
    scanner.close()
    return scanner


def script_problems(source: str, where: str) -> List[str]:
    """What a script would load or run that the policy refuses."""
    problems = [
        f"{where} builds code from a string ({match.group(0).strip()}), which needs 'unsafe-eval'"
        for match in STRING_EVALUATION.finditer(source)
    ]
    from_disk_fallback = FROM_DISK_GUARD in source
    for match in URL_LITERAL.finditer(source):
        url = match.group(2)
        host = (urlsplit(url).hostname or "").lower()
        if host in NAMESPACE_HOSTS:
            continue
        if NAVIGATION_BEFORE.search(source[max(0, match.start() - 40):match.start()]):
            continue
        if host in LOOPBACK_HOSTS and from_disk_fallback:
            continue
        directive = directive_for(url)
        candidates = (directive,) if directive else FETCH_DIRECTIVES
        if not any(allows(name, url) for name in candidates):
            problems.append(f"{where} names {url}, which {' and '.join(candidates)} refuse")
    return problems


def css_problems(css: str, where: str) -> List[str]:
    """What a stylesheet would load that the policy refuses."""
    problems = []
    imported = _imports(css)
    for url in imported:
        if not allows("style-src", url):
            problems.append(f"{where} imports {url}, which style-src refuses")
    for url in _css_urls(css):
        if url in imported:
            continue
        directive = "font-src" if urlsplit(url).path.lower().endswith(FONT_SUFFIXES) else "img-src"
        if not allows(directive, url):
            problems.append(f"{where} loads {url}, which {directive} refuses")
    return problems


def page_problems(text: str, name: str) -> List[str]:
    """Everything one rendered page would load or run that the policy refuses."""
    found = scan_text(text)
    problems: List[str] = []
    for directive, urls in (
        ("script-src", found.scripts),
        ("style-src", found.stylesheets),
        ("img-src", found.images),
        ("font-src", found.fonts),
        ("media-src", found.media),
        ("manifest-src", found.manifests),
    ):
        problems += [f"{name} loads {url}, which {directive} refuses" for url in urls if not allows(directive, url)]
    problems += [f"{name} embeds {frame}; the policy allows no frames or plugins" for frame in found.frames]
    if found.inline_script and "'unsafe-inline'" not in sources_for("script-src"):
        problems.append(f"{name} runs inline script, which script-src refuses")
    # The Tailwind Play CDN writes its styles into <style> elements at run time.
    writes_styles = any("tailwindcss" in url for url in found.scripts)
    if (found.inline_style or writes_styles) and "'unsafe-inline'" not in sources_for("style-src"):
        problems.append(f"{name} relies on inline style, which style-src refuses")
    problems += script_problems("\n".join(found.script_text), f"{name} (inline script)")
    problems += css_problems("\n".join(found.style_text), f"{name} (inline style)")
    return problems


def pages() -> List[Path]:
    return sorted(WEB_ROOT.glob("*.html"))


def scan(page: Path) -> _PageScan:
    return scan_text(render(page.read_text(encoding="utf-8")))


def _assets(pattern: str) -> List[Path]:
    assets = WEB_ROOT / "assets"
    return sorted(assets.rglob(pattern)) if assets.is_dir() else []


def uses() -> Dict[str, Set[str]]:
    """Every absolute URL each directive has to allow, across all pages and assets."""
    needed: Dict[str, Set[str]] = {"script-src": set(), "style-src": set(), "font-src": set(), "img-src": set()}

    def literals(source: str) -> None:
        for match in URL_LITERAL.finditer(source):
            directive = directive_for(match.group(2))
            if directive in needed:
                needed[directive].add(match.group(2))

    def stylesheet(css: str) -> None:
        imported = _imports(css)
        needed["style-src"].update(imported)
        for url in _css_urls(css):
            if url not in imported:
                directive = "font-src" if urlsplit(url).path.lower().endswith(FONT_SUFFIXES) else "img-src"
                needed[directive].add(url)

    for page in pages():
        found = scan(page)
        needed["script-src"].update(found.scripts)
        needed["style-src"].update(found.stylesheets)
        needed["img-src"].update(found.images)
        needed["font-src"].update(found.fonts)
        stylesheet("\n".join(found.style_text))
        literals("\n".join(found.script_text))
    for path in _assets("*.css"):
        stylesheet(render(path.read_text(encoding="utf-8")))
    for path in _assets("*.js"):
        literals(render(path.read_text(encoding="utf-8")))
    if any(urlsplit(url).netloc == GOOGLE_FONTS_CSS for url in needed["style-src"]):
        needed["font-src"].add(GOOGLE_FONTS_FILES + "inter/v1/font.woff2")
    return needed


# --- the pages, the assets and the policy ----------------------------------------------------


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_every_page_loads_and_runs_only_what_the_policy_allows(page: Path) -> None:
    assert page_problems(render(page.read_text(encoding="utf-8")), page.name) == []


def test_the_google_fonts_stylesheet_brings_its_font_files_with_it() -> None:
    if any(urlsplit(url).netloc == GOOGLE_FONTS_CSS for url in uses()["style-src"]):
        assert allows("font-src", GOOGLE_FONTS_FILES + "inter/v13/UcCO3FwrK3iLTeHuS_fvQtMwCp50KnMw2boKoduKmMEVuLyfAZ9hiA.woff2")


def test_the_asset_scripts_load_and_run_nothing_the_policy_refuses() -> None:
    for path in _assets("*.js"):
        assert script_problems(render(path.read_text(encoding="utf-8")), path.name) == []


def test_the_asset_stylesheets_load_nothing_the_policy_refuses() -> None:
    for path in _assets("*.css"):
        assert css_problems(render(path.read_text(encoding="utf-8")), path.name) == []


def test_every_host_the_policy_names_is_used_by_some_page() -> None:
    """A host left allowed after the page stopped using it is a script source nobody needs."""
    needed = uses()
    for directive in ("script-src", "style-src", "font-src", "img-src"):
        for source in sources_for(directive):
            if source.startswith("'") or source.endswith(":"):
                continue
            assert any(
                urlsplit(url).netloc == urlsplit(source).netloc and allows(directive, url) for url in needed[directive]
            ), f"{directive} allows {source}, which no page loads"


def test_pages_talk_only_to_their_own_origin_or_the_api() -> None:
    """Every fetch goes to the page's origin once the base path is empty; Swagger UI also tries the API's URL."""
    assert sources_for("connect-src") == ["'self'", f"https://{EXAMPLE_API_DOMAIN}"]
    assert allows("connect-src", "/api/overview")
    assert allows("connect-src", f"https://{EXAMPLE_API_DOMAIN}/prod/status")
    assert not allows("connect-src", "http://localhost:8001/status")


# --- the scan sees what it claims to see -------------------------------------------------------


def test_the_scan_sees_what_the_pages_are_known_to_load() -> None:
    """If this fails the scan has gone blind, and every page would pass over nothing."""
    scans = {page.name: scan(page) for page in pages()}
    assert scans, f"no pages were found in {WEB_ROOT}"
    for name, found in scans.items():
        assert "".join(found.script_text).strip(), f"{name}: every page runs inline script, and none was read"
    assert any("https://cdn.tailwindcss.com/3.4.17" in found.scripts for found in scans.values())
    assert any(
        url.startswith(f"https://{GOOGLE_FONTS_CSS}/") for found in scans.values() for url in _imports("\n".join(found.style_text))
    )


# Everything the review of this test found it could not see, in one page.
HOSTILE_PAGE = """<!DOCTYPE html>
<html><head>
<script>
  new Function("return 1");
  eval("2");
  setTimeout("tick()", 10);
  fetch("https://metrics.acme-thirdparty.example/beacon");
  const s = document.createElement("script");
  s.src = "https://cdn.acme-charts.example/chart.js";
  fetch(`https://${window.ACME_HOST}/data`);
</script>
<style>.hero { background: url("https://img.acme.example/hero.png"); }</style>
<link rel="preload" as="font" href="https://fonts.acme.example/f.woff2">
</head><body>
<img srcset="https://img.acme.example/a.png 1x, https://img.acme.example/a2.png 2x">
<div style="background-image: url('https://img.acme.example/b.png')"></div>
<video src="https://media.acme.example/v.mp4" poster="https://img.acme.example/poster.png"></video>
<button onclick="Function('return 3')()">x</button>
<iframe src="https://frame.acme.example/"></iframe>
</body></html>
"""


@pytest.mark.parametrize(
    "fragment",
    [
        "(new Function(",
        "(eval(",
        '(setTimeout("',
        "(Function(",
        "https://metrics.acme-thirdparty.example/beacon",
        "https://cdn.acme-charts.example/chart.js, which script-src refuse",
        "https://${window.ACME_HOST}/data",
        "https://img.acme.example/hero.png, which img-src refuses",
        "https://fonts.acme.example/f.woff2, which font-src refuses",
        "https://img.acme.example/a.png, which img-src refuses",
        "https://img.acme.example/a2.png, which img-src refuses",
        "https://img.acme.example/b.png, which img-src refuses",
        "https://media.acme.example/v.mp4, which media-src refuses",
        "https://img.acme.example/poster.png, which img-src refuses",
        "embeds https://frame.acme.example/",
    ],
)
def test_the_scan_reports_what_a_page_could_load_or_run_past_the_policy(fragment: str) -> None:
    problems = page_problems(HOSTILE_PAGE, "hostile.html")
    assert any(fragment in problem for problem in problems), f"{fragment!r} was not reported; got {problems}"


def test_the_scan_passes_what_the_policy_allows_or_never_fetches() -> None:
    page = """<!DOCTYPE html>
<html><head>
<script src="https://cdn.tailwindcss.com/3.4.17"></script>
<script>
  const SERVED_BASE_PATH = "";
  const API = SERVED_BASE_PATH.indexOf("THREEFOLD_BASE_PATH") !== -1 ? "http://localhost:8001" : location.origin;
  fetch(`${API}/status`);
  const help = '<a href="https://docs.acme.example/guide">guide</a>';
  document.createElementNS("http://www.w3.org/2000/svg", "svg");
  window.open("https://code.acme.example/threefold");
  fetch("https://""" + EXAMPLE_API_DOMAIN + """/prod/status");
</script>
<style>@import url('https://fonts.googleapis.com/css2?family=Inter&display=swap');</style>
</head><body onload="start()"><img src="data:image/png;base64,AAAA"><img src="/assets/acme.svg"></body></html>
"""
    assert page_problems(page, "benign.html") == []


def test_a_loopback_url_is_excused_only_behind_the_from_disk_test() -> None:
    problems = page_problems('<script>fetch("http://localhost:8001/status");</script>', "unguarded.html")
    assert any("http://localhost:8001/status" in problem for problem in problems)


def test_an_asset_script_and_stylesheet_are_read_the_same_way() -> None:
    script = 'const s = document.createElement("script"); s.src = "https://cdn.acme.example/x.js"; eval(code);'
    assert len(script_problems(script, "acme.js")) == 2
    css = (
        '@import url("https://css.acme.example/a.css");\n'
        ".x { background: url(https://img.acme.example/c.png); }\n"
        "@font-face { src: url('https://fonts.acme.example/f.woff2'); }\n"
        ".y { background: url(data:image/svg+xml;base64,AAAA); }\n"
    )
    assert [p.split(", which ")[1] for p in css_problems(css, "acme.css")] == [
        "style-src refuses", "img-src refuses", "font-src refuses",
    ]


@pytest.mark.parametrize(
    "directive, url, expected",
    [
        ("script-src", "https://cdn.tailwindcss.com/3.4.17", True),
        ("script-src", "https://cdn.tailwindcss.com/3.4.18", False),
        ("script-src", "https://unpkg.com/swagger-ui-dist@5.33.0/swagger-ui-bundle.js", True),
        ("script-src", "https://unpkg.com/other-package@1.0.0/index.js", False),
        ("style-src", "https://fonts.googleapis.com/css2?family=Inter", True),
        ("font-src", "https://fonts.gstatic.com/s/inter/v13/x.woff2", True),
        ("script-src", "/assets/threefold.js", True),
        ("img-src", "data:image/svg+xml;base64,AAAA", True),
        ("script-src", "data:text/javascript,alert(1)", False),
    ],
)
def test_the_matcher_reads_sources_as_a_browser_does(directive: str, url: str, expected: bool) -> None:
    assert allows(directive, url) is expected
