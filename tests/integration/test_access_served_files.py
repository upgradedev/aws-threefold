"""What connecting a repository downloads, and the dashboard, served by the stack itself.

The installer is handed out with this stack's own address written into it, so
a team runs one command and it installs from here. The bundle is built from the
deployed package, once per container, and the manifest carries the hashes of
both, so what was downloaded can be checked against what the stack says it
sent. The dashboard and its assets are served like the other pages.

The files these routes serve belong to other tracks and may not exist yet, so
the tests build a synthetic package in a temporary directory and point the
routes at it. The tests against the real tree only check what they can: a file
that is absent is reported as missing, not served as a crash.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from threefold.interfaces import access_routes
from threefold.interfaces.api_handlers import lambda_handler

DOMAIN = "acme0demo02.execute-api.eu-west-1.amazonaws.com"
INSTALLER_SOURCE = (
    '"""Synthetic installer for the served-file tests."""\n'
    'ENDPOINT = "__THREEFOLD_ENDPOINT__"\n'
    "print(ENDPOINT)\n"
)
DOMAIN_FILES = {
    "__init__.py": "from threefold.domain.models import Thing\n",
    "models.py": "class Thing:\n    pass\n",
    "rules.py": "RULES = ['acme-rule']\n",
}


@pytest.fixture
def package(tmp_path: Path, monkeypatch) -> Path:
    """A package tree with every file the routes serve, and a few they must not."""
    root = tmp_path / "threefold"
    (root / "domain" / "__pycache__").mkdir(parents=True)
    (root / "hooks").mkdir()
    (root / "tools").mkdir()
    (root / "web" / "assets").mkdir(parents=True)
    # Written as bytes, so the platform's line endings never reach an assertion.
    files = {
        "__init__.py": '"""Threefold package."""\n',
        **{f"domain/{name}": text for name, text in DOMAIN_FILES.items()},
        "domain/notes.txt": "not python\n",
        "hooks/threefold_hook.py": "# synthetic hook\r\nprint('hook')\r\n",
        "tools/threefold_cli.py": "# synthetic cli\n",
        "tools/threefold_install.py": INSTALLER_SOURCE,
        "web/dashboard.html": (
            '<title>Acme dashboard</title><script>const SERVED_BASE_PATH = "__THREEFOLD_BASE_PATH__";</script>'
        ),
        "web/assets/threefold.js": 'const BASE = "__THREEFOLD_BASE_PATH__";\n',
        "web/assets/threefold.css": "body { margin: 0; }\n",
        "web/assets/mark.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
        "web/assets/chart.min.js": "// minified\n",
        "web/assets/notes.txt": "not served\n",
        "web/secret.js": "// outside the assets directory\n",
    }
    for name, text in files.items():
        (root / name).write_bytes(text.encode("utf-8"))
    (root / "domain" / "__pycache__" / "models.cpython-311.pyc").write_bytes(b"\x00compiled")

    monkeypatch.setattr(access_routes, "PACKAGE_ROOT", str(root))
    monkeypatch.setattr(access_routes, "WEB_ROOT", str(root / "web"))
    monkeypatch.setattr(access_routes, "ASSETS_ROOT", str(root / "web" / "assets"))
    monkeypatch.setattr(access_routes, "HOOKS_ROOT", str(root / "hooks"))
    monkeypatch.setattr(access_routes, "TOOLS_ROOT", str(root / "tools"))
    access_routes._BUNDLE_CACHE.clear()
    yield root
    access_routes._BUNDLE_CACHE.clear()


def _get(path: str, stage: str = "prod", method: str = "GET", context: dict | None = None, headers=None) -> dict:
    request_context = {"http": {"method": method}, "stage": stage, "domainName": DOMAIN}
    if context is not None:
        request_context = {"http": {"method": method}, **context}
    return lambda_handler(
        {
            "rawPath": path if stage in ("", "$default") else f"/{stage}{path}",
            "headers": headers or {},
            "requestContext": request_context,
        }
    )


def _unzip(response: dict) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(base64.b64decode(response["body"])))


# ------------------------------------------------------------------- installer


def test_the_installer_points_back_at_this_stack(package) -> None:
    response = _get("/install.py")
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("text/plain")
    body = response["body"]
    assert "__THREEFOLD_ENDPOINT__" not in body
    assert f'ENDPOINT = "https://{DOMAIN}/prod/"' in body


def test_the_installer_carries_the_hash_of_exactly_what_was_sent(package) -> None:
    response = _get("/install.py")
    digest = hashlib.sha256(response["body"].encode("utf-8")).hexdigest()
    assert response["headers"][access_routes.SHA256_HEADER] == digest


def test_an_unstaged_deployment_bakes_in_its_root(package) -> None:
    body = _get("/install.py", stage="$default")["body"]
    assert f'ENDPOINT = "https://{DOMAIN}/"' in body


def test_the_local_server_bakes_in_its_own_scheme_and_host(package) -> None:
    body = _get("/install.py", stage="", context={}, headers={"Host": "127.0.0.1:8001"})["body"]
    assert 'ENDPOINT = "http://127.0.0.1:8001/"' in body


@pytest.mark.parametrize("host", ['acme.example"; import os #', "acme.example/evil", "", "acme example"])
def test_a_host_that_cannot_be_written_into_a_script_is_refused(package, host) -> None:
    response = _get("/install.py", stage="", context={}, headers={"Host": host})
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["type"] == "urn:threefold:error:unknown-host"


def test_api_gateways_record_of_the_domain_beats_the_host_header(package) -> None:
    body = _get("/install.py", headers={"Host": "elsewhere.example"})["body"]
    assert DOMAIN in body and "elsewhere.example" not in body


def test_a_missing_installer_is_reported_as_missing(package, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(access_routes, "TOOLS_ROOT", str(tmp_path / "no-tools"))
    response = _get("/install.py")
    assert response["statusCode"] == 500
    problem = json.loads(response["body"])
    assert problem["type"] == "urn:threefold:error:script-missing"
    assert "tools/threefold_install.py is missing from this deployment" in problem["detail"]


# ---------------------------------------------------------------------- bundle


def test_the_bundle_is_a_zip_sent_as_base64_through_the_proxy(package) -> None:
    response = _get("/dist/threefold-bundle.zip")
    assert response["statusCode"] == 200
    assert response["isBase64Encoded"] is True
    assert response["headers"]["Content-Type"] == "application/zip"
    assert 'filename="threefold-bundle.zip"' in response["headers"]["Content-Disposition"]
    raw = base64.b64decode(response["body"])
    assert raw[:2] == b"PK"
    assert response["headers"][access_routes.SHA256_HEADER] == hashlib.sha256(raw).hexdigest()


def test_the_bundle_carries_the_hook_the_cli_and_the_domain_package(package) -> None:
    archive = _unzip(_get("/dist/threefold-bundle.zip"))
    assert archive.namelist() == [
        "bin/threefold_hook.py",
        "bin/threefold_cli.py",
        "lib/threefold/__init__.py",
        *(f"lib/threefold/domain/{name}" for name in sorted(DOMAIN_FILES)),
    ]
    assert archive.read("bin/threefold_hook.py") == (package / "hooks" / "threefold_hook.py").read_bytes()
    assert archive.read("bin/threefold_cli.py") == (package / "tools" / "threefold_cli.py").read_bytes()
    for name in DOMAIN_FILES:
        assert archive.read(f"lib/threefold/domain/{name}") == (package / "domain" / name).read_bytes()


def test_the_bundle_keeps_every_byte_of_its_files(package) -> None:
    """Line endings included: the hook's hash in the manifest is of the file as packaged."""
    archive = _unzip(_get("/dist/threefold-bundle.zip"))
    assert archive.read("bin/threefold_hook.py") == b"# synthetic hook\r\nprint('hook')\r\n"


def test_the_bundle_is_the_same_bytes_every_time(package) -> None:
    first = _get("/dist/threefold-bundle.zip")["body"]
    assert _get("/dist/threefold-bundle.zip")["body"] == first
    access_routes._BUNDLE_CACHE.clear()
    assert _get("/dist/threefold-bundle.zip")["body"] == first, "Built again from scratch, it must not change"


def test_the_bundle_carries_no_clock_and_no_platform(package) -> None:
    for info in _unzip(_get("/dist/threefold-bundle.zip")).infolist():
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        assert info.create_system == 3
        assert info.external_attr >> 16 == 0o100644
        assert info.compress_type == zipfile.ZIP_DEFLATED


@pytest.mark.parametrize(
    "missing, name",
    [("tools/threefold_cli.py", "tools/threefold_cli.py"), ("hooks/threefold_hook.py", "hooks/threefold_hook.py")],
)
def test_a_bundle_missing_a_file_says_which(package, missing, name) -> None:
    (package / missing).unlink()
    response = _get("/dist/threefold-bundle.zip")
    assert response["statusCode"] == 500
    assert name in json.loads(response["body"])["detail"]


def test_a_head_on_the_bundle_answers_with_no_body(package) -> None:
    response = _get("/dist/threefold-bundle.zip", method="HEAD")
    assert response["statusCode"] == 200
    assert response["body"] == ""


# -------------------------------------------------------------------- manifest


def test_the_manifest_hashes_match_what_is_served(package) -> None:
    manifest = json.loads(_get("/dist/manifest.json")["body"])
    assert set(manifest) == {"files", "bundle_sha256", "installer_sha256"}

    raw = base64.b64decode(_get("/dist/threefold-bundle.zip")["body"])
    assert manifest["bundle_sha256"] == hashlib.sha256(raw).hexdigest()

    installer = _get("/install.py")["body"]
    assert manifest["installer_sha256"] == hashlib.sha256(installer.encode("utf-8")).hexdigest()

    archive = zipfile.ZipFile(io.BytesIO(raw))
    assert [entry["path"] for entry in manifest["files"]] == archive.namelist()
    for entry in manifest["files"]:
        assert set(entry) == {"path", "sha256", "bytes"}
        data = archive.read(entry["path"])
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["bytes"] == len(data)


def test_the_manifest_names_the_installer_this_host_serves(package) -> None:
    local = {"Host": "127.0.0.1:8001"}
    manifest = json.loads(_get("/dist/manifest.json", stage="", context={}, headers=local)["body"])
    installer = _get("/install.py", stage="", context={}, headers=local)["body"]
    assert manifest["installer_sha256"] == hashlib.sha256(installer.encode("utf-8")).hexdigest()


def test_a_manifest_with_nothing_to_describe_is_reported(package, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(access_routes, "TOOLS_ROOT", str(tmp_path / "no-tools"))
    response = _get("/dist/manifest.json")
    assert response["statusCode"] == 500
    assert "missing from this deployment" in json.loads(response["body"])["detail"]


# ----------------------------------------------------------- dashboard, assets


@pytest.mark.parametrize("path", ["/dashboard.html", "/app"])
def test_the_dashboard_is_served_and_told_its_base(package, path) -> None:
    response = _get(path)
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith("text/html")
    assert 'const SERVED_BASE_PATH = "/prod";' in response["body"]


def test_a_missing_dashboard_is_reported_as_missing(package) -> None:
    (package / "web" / "dashboard.html").unlink()
    response = _get("/dashboard.html")
    assert response["statusCode"] == 500
    problem = json.loads(response["body"])
    assert problem["type"] == "urn:threefold:error:page-missing"
    assert "web/dashboard.html" in problem["detail"]


@pytest.mark.parametrize(
    "name, content_type",
    [
        ("threefold.js", "text/javascript"),
        ("chart.min.js", "text/javascript"),
        ("threefold.css", "text/css"),
        ("mark.svg", "image/svg+xml"),
    ],
)
def test_an_asset_is_served_with_its_type(package, name, content_type) -> None:
    response = _get(f"/assets/{name}")
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith(content_type)
    assert response["headers"]["X-Content-Type-Options"] == "nosniff"


def test_an_asset_is_told_the_api_base(package) -> None:
    assert _get("/assets/threefold.js")["body"] == 'const BASE = "/prod";\n'


@pytest.mark.parametrize(
    "name",
    [
        "../secret.js",
        "..%2Fsecret.js",
        "%2e%2e/secret.js",
        "..\\secret.js",
        "sub/threefold.js",
        "notes.txt",
        "threefold.JS",
        ".threefold.js",
        "threefold..js",
        "missing.js",
        "/etc/passwd.js",
        "C:\\Windows\\win.js",
    ],
)
def test_nothing_outside_the_assets_directory_is_served(package, name) -> None:
    response = _get(f"/assets/{name}")
    assert response["statusCode"] == 404, f"/assets/{name} answered {response['statusCode']}"
    assert json.loads(response["body"])["type"] == "urn:threefold:error:asset-not-found"


@pytest.mark.parametrize("path", ["/assets", "/assets/"])
def test_the_assets_directory_itself_is_not_listed(package, path) -> None:
    assert _get(path)["statusCode"] == 404


def test_a_link_out_of_the_assets_directory_is_not_followed(package) -> None:
    link = package / "web" / "assets" / "escape.js"
    try:
        os.symlink(package / "web" / "secret.js", link)
    except (OSError, NotImplementedError):
        pytest.skip("This machine does not let the test create a symbolic link")
    assert _get("/assets/escape.js")["statusCode"] == 404


# ----------------------------------------------------------- the real tree


REAL_TOOLS = Path(access_routes.TOOLS_ROOT)
REAL_WEB = Path(access_routes.WEB_ROOT)


@pytest.mark.skipif((REAL_TOOLS / "threefold_install.py").is_file(), reason="the installer is in the tree")
def test_the_real_tree_without_the_installer_reports_it_missing() -> None:
    response = _get("/install.py")
    assert response["statusCode"] == 500
    assert json.loads(response["body"])["type"] == "urn:threefold:error:script-missing"


@pytest.mark.skipif((REAL_TOOLS / "threefold_cli.py").is_file(), reason="the CLI is in the tree")
def test_the_real_tree_without_the_cli_reports_the_bundle_missing() -> None:
    response = _get("/dist/threefold-bundle.zip")
    assert response["statusCode"] == 500
    assert "tools/threefold_cli.py" in json.loads(response["body"])["detail"]


@pytest.mark.skipif((REAL_WEB / "dashboard.html").is_file(), reason="the dashboard is in the tree")
def test_the_real_tree_without_the_dashboard_reports_it_missing() -> None:
    for path in ("/dashboard.html", "/app"):
        response = _get(path)
        assert response["statusCode"] == 500
        assert json.loads(response["body"])["type"] == "urn:threefold:error:page-missing"


@pytest.mark.skipif(not (REAL_WEB / "dashboard.html").is_file(), reason="the dashboard is not in the tree yet")
def test_the_real_dashboard_is_served() -> None:
    response = _get("/dashboard.html")
    assert response["statusCode"] == 200
    assert "__THREEFOLD_BASE_PATH__" not in response["body"]


def test_the_real_packages_domain_files_would_all_be_bundled() -> None:
    """The real domain package, listed as the bundle would list it, has no stray files."""
    names = [source for source, _ in access_routes._bundle_sources() if source.startswith("lib/threefold/domain/")]
    expected = sorted(p.name for p in (Path(access_routes.PACKAGE_ROOT) / "domain").glob("*.py"))
    assert names == [f"lib/threefold/domain/{name}" for name in expected]
    assert "lib/threefold/domain/__init__.py" in names
