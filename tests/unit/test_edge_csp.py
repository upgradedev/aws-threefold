"""The Content-Security-Policy the edge sends is one the pages actually run under.

A policy written from memory either breaks a page (a stylesheet the browser
silently refuses) or is wider than it needs to be. This test reads every page
in src/threefold/web and every script and stylesheet in its assets/, finds
what each loads (external scripts and stylesheets, @import-ed stylesheets and
the fonts they bring, inline scripts and handlers, inline styles, images,
frames), and checks it against the policy the way a browser matches sources:
scheme and host exactly, and a path that ends in "/" as a prefix, otherwise
exactly. It also checks the other way: every host the policy names is used by
some page, so a removed dependency does not leave its host allowed.

Walked in a browser on 2026-09-22 with this policy served as a header: every
page rendered, the Tailwind Play CDN styled them, the Google Fonts loaded,
Swagger UI rendered all its operations, inline onclick handlers ran, and an
unlisted script host was refused.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Set, Tuple
from urllib.parse import urlsplit

import pytest

WEB_ROOT = Path(__file__).resolve().parents[2] / "src" / "threefold" / "web"
EXAMPLE_API_DOMAIN = "abc123def4.execute-api.eu-west-1.amazonaws.com"
GOOGLE_FONTS_CSS = "fonts.googleapis.com"
GOOGLE_FONTS_FILES = "https://fonts.gstatic.com/s/"


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


class _PageScan(HTMLParser):
    """What one page loads, and whether it relies on inline script or style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: List[str] = []
        self.stylesheets: List[str] = []
        self.images: List[str] = []
        self.frames: List[str] = []
        self.inline_script = False
        self.inline_style = False
        self.style_text: List[str] = []
        self._in_style = False
        self._in_inline_script = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str]]) -> None:
        attributes = {name: (value or "") for name, value in attrs}
        if any(name.startswith("on") for name in attributes):
            self.inline_script = True
        if "style" in attributes:
            self.inline_style = True
        if tag == "script":
            if attributes.get("src"):
                self.scripts.append(attributes["src"])
            else:
                self._in_inline_script = True
        elif tag == "link" and "stylesheet" in attributes.get("rel", "").split():
            self.stylesheets.append(attributes.get("href", ""))
        elif tag == "style":
            self.inline_style = True
            self._in_style = True
        elif tag == "img" and attributes.get("src"):
            self.images.append(attributes["src"])
        elif tag in ("iframe", "frame", "object", "embed"):
            self.frames.append(attributes.get("src") or attributes.get("data") or tag)
        for name, value in attributes.items():
            if name in ("href", "src") and value.strip().lower().startswith("javascript:"):
                self.inline_script = True

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


def _imports(css: str) -> List[str]:
    return re.findall(r"""@import\s+(?:url\()?\s*['"]?([^'")\s;]+)""", css)


def _css_urls(css: str) -> List[str]:
    return [u for u in re.findall(r"""url\(\s*['"]?([^'")]+)""", css) if not u.startswith("#")]


def pages() -> List[Path]:
    return sorted(WEB_ROOT.glob("*.html"))


def scan(page: Path) -> _PageScan:
    scanner = _PageScan()
    scanner.feed(page.read_text(encoding="utf-8").replace("__THREEFOLD_BASE_PATH__", ""))
    return scanner


def uses() -> Dict[str, Set[str]]:
    """Every absolute URL each directive has to allow, across all pages and assets."""
    needed: Dict[str, Set[str]] = {"script-src": set(), "style-src": set(), "font-src": set(), "img-src": set()}
    for page in pages():
        found = scan(page)
        needed["script-src"].update(found.scripts)
        needed["style-src"].update(found.stylesheets)
        needed["img-src"].update(found.images)
        css = "\n".join(found.style_text)
        needed["style-src"].update(_imports(css))
    assets = WEB_ROOT / "assets"
    if assets.is_dir():
        for path in assets.rglob("*.css"):
            css = path.read_text(encoding="utf-8")
            needed["style-src"].update(_imports(css))
            needed["img-src"].update(u for u in _css_urls(css) if u not in _imports(css) and not u.endswith((".woff", ".woff2")))
    if any(urlsplit(url).netloc == GOOGLE_FONTS_CSS for url in needed["style-src"]):
        needed["font-src"].add(GOOGLE_FONTS_FILES + "inter/v1/font.woff2")
    return needed


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_every_script_and_stylesheet_a_page_loads_is_allowed(page: Path) -> None:
    found = scan(page)
    for url in found.scripts:
        assert allows("script-src", url), f"{page.name} loads script {url}, which the policy refuses"
    for url in found.stylesheets + _imports("\n".join(found.style_text)):
        assert allows("style-src", url), f"{page.name} loads stylesheet {url}, which the policy refuses"
    for url in found.images:
        assert allows("img-src", url), f"{page.name} shows image {url}, which the policy refuses"
    assert not found.frames, f"{page.name} embeds {found.frames}; the policy allows no frames or plugins"


@pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
def test_inline_script_and_style_are_allowed_where_a_page_relies_on_them(page: Path) -> None:
    found = scan(page)
    if found.inline_script:
        assert "'unsafe-inline'" in sources_for("script-src"), f"{page.name} runs inline script"
    if found.inline_style or found.scripts and any("tailwindcss" in s for s in found.scripts):
        assert "'unsafe-inline'" in sources_for("style-src"), f"{page.name} relies on inline style"


def test_the_google_fonts_stylesheet_brings_its_font_files_with_it() -> None:
    if any(urlsplit(url).netloc == GOOGLE_FONTS_CSS for url in uses()["style-src"]):
        assert allows("font-src", GOOGLE_FONTS_FILES + "inter/v13/UcCO3FwrK3iLTeHuS_fvQtMwCp50KnMw2boKoduKmMEVuLyfAZ9hiA.woff2")


def _assets(pattern: str) -> List[Path]:
    assets = WEB_ROOT / "assets"
    return sorted(assets.rglob(pattern)) if assets.is_dir() else []


def test_the_asset_scripts_load_nothing_the_policy_refuses() -> None:
    for path in _assets("*.js"):
        source = path.read_text(encoding="utf-8")
        # The policy has no 'unsafe-eval', so code built from strings would not run.
        assert not re.search(r"\beval\s*\(|\bnew\s+Function\s*\(", source), f"{path.name} evaluates strings as code"
        for url in re.findall(r"""['"](https?://[^'"\s]+)['"]""", source):
            host = urlsplit(url).netloc
            if host in ("www.w3.org",):
                # SVG and XHTML namespace URIs are identifiers, never fetched.
                continue
            assert any(allows(d, url) for d in ("connect-src", "script-src", "style-src", "img-src", "font-src")), (
                f"{path.name} names {url}, which no directive allows"
            )


def test_the_asset_stylesheets_load_nothing_the_policy_refuses() -> None:
    for path in _assets("*.css"):
        css = path.read_text(encoding="utf-8")
        imported = _imports(css)
        for url in imported:
            assert allows("style-src", url), f"{path.name} imports {url}, which the policy refuses"
        for url in _css_urls(css):
            if url in imported:
                continue
            directive = "font-src" if url.split("?")[0].endswith((".woff", ".woff2", ".ttf", ".otf")) else "img-src"
            assert allows(directive, url), f"{path.name} loads {url}, which {directive} refuses"


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
