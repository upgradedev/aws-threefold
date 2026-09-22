"""publish_web.py puts the pages in the edge bucket as the edge must serve them.

Behind CloudFront the pages and the API share one origin, so the base path a
page is told has to be empty, not the stage the function substitutes. Every
object needs the Content-Type a browser will accept under nosniff and the
Cache-Control the edge's TTLs were designed around; /app has to exist as a key,
because the bucket has no routing of its own. Nothing here reaches AWS: every
subprocess call is replaced by a fake that records it and answers as the AWS
CLI would, and the pages are synthetic Acme pages in a temporary directory.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REAL_WEB_ROOT = Path(__file__).resolve().parents[2] / "src" / "threefold" / "web"


def _load():
    spec = importlib.util.spec_from_file_location("publish_web_under_test", SCRIPTS / "publish_web.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: a dataclass looks its own module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish_web = _load()

PAGE = """<!DOCTYPE html>
<html><head><title>Acme {name}</title>
<script>const SERVED_BASE_PATH = "__THREEFOLD_BASE_PATH__";</script>
</head><body><a href="__THREEFOLD_BASE_PATH__/rules.html">rules</a></body></html>
"""


@pytest.fixture
def web_root(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    (root / "assets" / "icons").mkdir(parents=True)
    for name in ("index", "dashboard", "rules"):
        (root / f"{name}.html").write_text(PAGE.format(name=name), encoding="utf-8")
    (root / "openapi.json").write_text('{"openapi": "3.1.0"}', encoding="utf-8")
    (root / "assets" / "threefold.js").write_text(
        'const API = "__THREEFOLD_BASE_PATH__/api/overview";\n', encoding="utf-8"
    )
    (root / "assets" / "threefold.css").write_text("body { color: #111; }\n", encoding="utf-8")
    (root / "assets" / "icons" / "acme.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (root / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n__THREEFOLD_BASE_PATH__")
    (root / "assets" / ".DS_Store").write_bytes(b"\0\0")
    return root


def _service_call(command: List[str]) -> List[str]:
    """The command after "aws" and any --profile, so both forms read the same."""
    rest = command[1:]
    if rest[:1] == ["--profile"]:
        rest = rest[2:]
    return rest[:2]


class FakeAws:
    """Stands in for subprocess.run: records each AWS CLI call and answers it."""

    def __init__(self, fail_on_put: int = 0, outputs=None, listing=None) -> None:
        self.calls: List[List[str]] = []
        self.bodies: dict = {}
        self.fail_on_put = fail_on_put
        self.puts = 0
        # What list-objects-v2 --query Contents[].Key prints: a JSON list, or null
        # for an empty bucket.
        self.listing = listing
        self.outputs = outputs if outputs is not None else {
            "WebBucketName": "acme-edge-webbucket-1a2b3c",
            "DistributionId": "E2ACMEEDGE0001",
            "DistributionDomainName": "d1acme.cloudfront.net",
            "WebAclArn": "arn:aws:wafv2:us-east-1:000000000000:global/webacl/acme/1",
        }

    def __call__(self, command, capture_output=False, text=False, check=False):
        command = list(command)
        self.calls.append(command)
        call = _service_call(command)
        if call == ["cloudformation", "describe-stacks"]:
            document = {"Stacks": [{"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in self.outputs.items()]}]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr="")
        if call == ["s3api", "put-object"]:
            self.puts += 1
            if self.puts == self.fail_on_put:
                return SimpleNamespace(returncode=254, stdout="", stderr="An error occurred (AccessDenied)")
            key = command[command.index("--key") + 1]
            self.bodies[key] = Path(command[command.index("--body") + 1]).read_bytes()
            return SimpleNamespace(returncode=0, stdout='{"ETag": "\\"x\\""}', stderr="")
        if call == ["cloudfront", "create-invalidation"]:
            return SimpleNamespace(returncode=0, stdout='{"Invalidation": {"Id": "I2ACME", "Status": "InProgress"}}', stderr="")
        if call == ["s3api", "list-objects-v2"]:
            assert command[command.index("--query") + 1] == "Contents[].Key"
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.listing) + "\n", stderr="")
        if call == ["s3api", "delete-object"]:
            return SimpleNamespace(returncode=0, stdout='{"DeleteMarker": true, "VersionId": "v2"}', stderr="")
        raise AssertionError(f"unexpected command {command}")

    def deleted(self) -> List[str]:
        return [c[c.index("--key") + 1] for c in self.calls if _service_call(c) == ["s3api", "delete-object"]]

    def puts_in_order(self) -> List[str]:
        return [c[c.index("--key") + 1] for c in self.calls if c[1:3] == ["s3api", "put-object"]]

    def option(self, key: str, option: str) -> str:
        for call in self.calls:
            if call[1:3] == ["s3api", "put-object"] and call[call.index("--key") + 1] == key:
                return call[call.index(option) + 1]
        raise AssertionError(f"{key} was not uploaded")


@pytest.fixture
def aws(monkeypatch) -> FakeAws:
    fake = FakeAws()
    monkeypatch.setattr(publish_web.subprocess, "run", fake)
    return fake


def run(*argv: str):
    out, err = io.StringIO(), io.StringIO()
    args = publish_web._parse(list(argv))
    try:
        code = publish_web.publish(args, out=out)
    except publish_web.PublishError as error:
        err.write(str(error))
        code = 1
    return SimpleNamespace(code=code, out=out.getvalue(), err=err.getvalue())


# --- what is uploaded ---------------------------------------------------------------


def test_every_page_and_asset_is_collected_and_nothing_else(web_root: Path) -> None:
    keys = [item.key for item in publish_web.collect(web_root)]
    assert sorted(keys) == sorted([
        "assets/icons/acme.svg", "assets/logo.png", "assets/threefold.css", "assets/threefold.js",
        "dashboard.html", "index.html", "rules.html", "app",
    ])
    assert "openapi.json" not in keys, "the function serves the OpenAPI document deployed with its code"
    assert not any(".DS_Store" in key for key in keys)


def test_the_base_path_is_empty_behind_the_edge(web_root: Path) -> None:
    objects = {item.key: item for item in publish_web.collect(web_root)}
    for key in ("index.html", "dashboard.html", "app", "assets/threefold.js"):
        body = objects[key].body.decode("utf-8")
        assert "__THREEFOLD_BASE_PATH__" not in body, key
    assert 'const SERVED_BASE_PATH = "";' in objects["index.html"].body.decode("utf-8")
    assert 'href="/rules.html"' in objects["index.html"].body.decode("utf-8")
    assert 'const API = "/api/overview";' in objects["assets/threefold.js"].body.decode("utf-8")


def test_a_binary_asset_is_copied_byte_for_byte(web_root: Path) -> None:
    objects = {item.key: item for item in publish_web.collect(web_root)}
    assert objects["assets/logo.png"].body == (web_root / "assets" / "logo.png").read_bytes()


@pytest.mark.parametrize(
    "key, content_type",
    [
        ("index.html", "text/html; charset=utf-8"),
        ("app", "text/html; charset=utf-8"),
        ("assets/threefold.js", "text/javascript; charset=utf-8"),
        ("assets/threefold.css", "text/css; charset=utf-8"),
        ("assets/icons/acme.svg", "image/svg+xml"),
        ("assets/logo.png", "image/png"),
    ],
)
def test_each_object_carries_the_type_a_browser_accepts_under_nosniff(web_root: Path, key: str, content_type: str) -> None:
    objects = {item.key: item for item in publish_web.collect(web_root)}
    assert objects[key].content_type == content_type


def test_pages_revalidate_and_assets_stay_at_the_edge(web_root: Path) -> None:
    for item in publish_web.collect(web_root):
        if item.key.startswith("assets/"):
            assert item.cache_control == "public, max-age=300, s-maxage=31536000", item.key
        else:
            assert "max-age=0" in item.cache_control and "s-maxage=60" in item.cache_control, item.key


def test_app_is_the_dashboard(web_root: Path) -> None:
    objects = {item.key: item for item in publish_web.collect(web_root)}
    assert objects["app"].body == objects["dashboard.html"].body
    assert objects["app"].content_type.startswith("text/html")


def test_without_a_dashboard_app_is_skipped_and_said_so(web_root: Path, aws: FakeAws) -> None:
    (web_root / "dashboard.html").unlink()
    result = run("--bucket", "acme-bucket", "--distribution-id", "E2ACME", "--web-root", str(web_root))
    assert result.code == 0
    assert "app" not in aws.puts_in_order()
    assert "/app is not published" in result.out


def test_an_unknown_extension_is_refused_rather_than_guessed(web_root: Path) -> None:
    (web_root / "assets" / "notes.rst").write_text("x", encoding="utf-8")
    with pytest.raises(publish_web.PublishError, match="notes.rst"):
        publish_web.collect(web_root)


def test_a_directory_with_no_pages_is_refused(tmp_path: Path) -> None:
    with pytest.raises(publish_web.PublishError, match="no pages"):
        publish_web.collect(tmp_path)


def test_the_real_pages_all_render_with_the_token_replaced() -> None:
    objects = publish_web.collect(REAL_WEB_ROOT)
    pages = {path.name for path in REAL_WEB_ROOT.glob("*.html")}
    assert pages <= {item.key for item in objects}
    for item in objects:
        if item.content_type.startswith(("text/", "image/svg")):
            assert b"__THREEFOLD_BASE_PATH__" not in item.body, item.key


# --- how it is uploaded ---------------------------------------------------------------


def test_the_stack_outputs_name_the_bucket_and_the_distribution(web_root: Path, aws: FakeAws) -> None:
    result = run("--stack-name", "acme-edge", "--web-root", str(web_root))
    assert result.code == 0, result.err
    describe = aws.calls[0]
    assert describe[:3] == ["aws", "cloudformation", "describe-stacks"]
    assert describe[describe.index("--stack-name") + 1] == "acme-edge"
    assert describe[describe.index("--region") + 1] == "us-east-1"
    for call in aws.calls[1:-1]:
        assert call[call.index("--bucket") + 1] == "acme-edge-webbucket-1a2b3c"
        assert call[call.index("--region") + 1] == "us-east-1"
    invalidation = aws.calls[-1]
    assert invalidation[:3] == ["aws", "cloudfront", "create-invalidation"]
    assert invalidation[invalidation.index("--distribution-id") + 1] == "E2ACMEEDGE0001"
    assert invalidation[invalidation.index("--paths") + 1] == "/*"
    assert "https://d1acme.cloudfront.net/" in result.out
    assert "I2ACME" in result.out


def test_what_is_uploaded_is_the_rendered_file_with_its_type_and_cache(web_root: Path, aws: FakeAws) -> None:
    run("--bucket", "acme-bucket", "--distribution-id", "E2ACME", "--web-root", str(web_root))
    assert b"__THREEFOLD_BASE_PATH__" not in aws.bodies["index.html"]
    assert aws.bodies["app"] == aws.bodies["dashboard.html"]
    assert aws.option("app", "--content-type") == "text/html; charset=utf-8"
    assert aws.option("assets/threefold.js", "--content-type") == "text/javascript; charset=utf-8"
    assert aws.option("assets/threefold.js", "--cache-control") == "public, max-age=300, s-maxage=31536000"
    assert aws.option("index.html", "--cache-control") == "public, max-age=0, s-maxage=60, must-revalidate"


def test_assets_go_up_before_the_pages_that_name_them(web_root: Path, aws: FakeAws) -> None:
    run("--bucket", "acme-bucket", "--distribution-id", "E2ACME", "--web-root", str(web_root))
    order = aws.puts_in_order()
    last_asset = max(i for i, key in enumerate(order) if key.startswith("assets/"))
    first_page = min(i for i, key in enumerate(order) if not key.startswith("assets/"))
    assert last_asset < first_page
    assert aws.calls[-1][1:3] == ["cloudfront", "create-invalidation"], "the edge is cleared after the last upload"


def test_a_failed_upload_stops_everything_after_it(web_root: Path, monkeypatch) -> None:
    fake = FakeAws(fail_on_put=2)
    monkeypatch.setattr(publish_web.subprocess, "run", fake)
    result = run("--bucket", "acme-bucket", "--distribution-id", "E2ACME", "--web-root", str(web_root))
    assert result.code == 1
    assert "AccessDenied" in result.err
    assert fake.puts == 2
    assert not any(call[1:3] == ["cloudfront", "create-invalidation"] for call in fake.calls)


def test_a_stack_without_the_edge_outputs_is_named_as_the_wrong_stack(web_root: Path, monkeypatch) -> None:
    fake = FakeAws(outputs={"ApiEndpoint": "https://example.execute-api.eu-west-1.amazonaws.com/prod/"})
    monkeypatch.setattr(publish_web.subprocess, "run", fake)
    result = run("--stack-name", "acme-api", "--web-root", str(web_root))
    assert result.code == 1
    assert "WebBucketName" in result.err and "DistributionId" in result.err
    assert len(fake.calls) == 1, "nothing is uploaded once the stack is known to be the wrong one"


def test_a_missing_aws_cli_is_said_in_words(web_root: Path, monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError("aws")

    monkeypatch.setattr(publish_web.subprocess, "run", missing)
    result = run("--bucket", "acme-bucket", "--distribution-id", "E2ACME", "--web-root", str(web_root))
    assert result.code == 1
    assert "AWS CLI" in result.err


def test_a_profile_is_passed_to_every_call(web_root: Path, aws: FakeAws) -> None:
    run("--stack-name", "acme-edge", "--profile", "acme-deployer", "--web-root", str(web_root))
    assert aws.calls and all(call[:3] == ["aws", "--profile", "acme-deployer"] for call in aws.calls)


def test_a_dry_run_prints_every_step_and_runs_none(web_root: Path, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("a dry run must not run anything")

    monkeypatch.setattr(publish_web.subprocess, "run", forbidden)
    result = run("--stack-name", "acme-edge", "--web-root", str(web_root), "--dry-run")
    assert result.code == 0
    assert "would run: aws cloudformation describe-stacks --stack-name acme-edge" in result.out
    for key in ("index.html", "dashboard.html", "rules.html", "app", "assets/threefold.js", "assets/logo.png"):
        assert f"--key {key} " in result.out, key
    assert "aws cloudfront create-invalidation" in result.out
    assert "'/*'" in result.out, "the wildcard is quoted, so a pasted line is not expanded by the shell"
    assert "nothing was sent" in result.out


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--bucket", "acme-bucket"],
        ["--distribution-id", "E2ACME"],
        ["--stack-name", "acme-edge", "--bucket", "acme-bucket"],
    ],
)
def test_the_target_is_either_a_stack_or_a_bucket_and_a_distribution(argv) -> None:
    with pytest.raises(SystemExit) as raised:
        publish_web._parse(argv)
    assert raised.value.code == 2


def test_main_turns_a_failure_into_exit_code_one(web_root: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(publish_web.subprocess, "run", FakeAws(fail_on_put=1))
    code = publish_web.main(["--bucket", "acme-bucket", "--distribution-id", "E2ACME", "--web-root", str(web_root)])
    assert code == 1
    assert capsys.readouterr().err.startswith("publish_web: ")


# --- what a publish leaves behind ------------------------------------------------------------


def test_without_prune_nothing_is_listed_or_deleted(web_root: Path, aws: FakeAws) -> None:
    """A page removed from the source stays at the edge; the docstring says so, and --prune is opt-in."""
    result = run("--bucket", "acme-bucket", "--distribution-id", "E2ACME", "--web-root", str(web_root))
    assert result.code == 0
    assert not [c for c in aws.calls if _service_call(c) in (["s3api", "list-objects-v2"], ["s3api", "delete-object"])]
    assert "only adds and overwrites" in publish_web.__doc__


def test_prune_deletes_the_pages_and_assets_this_publish_did_not_write(web_root: Path, monkeypatch) -> None:
    fake = FakeAws(listing=[
        "index.html", "rules.html", "dashboard.html", "app", "assets/threefold.js",
        "retired.html", "assets/old-chart.js", "assets/icons/gone.svg",
        "backups/2026-09-01.tar", "notes/readme.html", "robots.txt",
    ])
    monkeypatch.setattr(publish_web.subprocess, "run", fake)
    result = run("--bucket", "acme-bucket", "--distribution-id", "E2ACME", "--web-root", str(web_root), "--prune")
    assert result.code == 0, result.err
    assert fake.deleted() == ["assets/icons/gone.svg", "assets/old-chart.js", "retired.html"]
    # Only keys of a shape this script writes are ever deleted; the rest are named and kept.
    assert "left alone, not a key this script writes: backups/2026-09-01.tar, notes/readme.html, robots.txt" in result.out
    for call in fake.calls:
        if _service_call(call) == ["s3api", "delete-object"]:
            assert call[call.index("--bucket") + 1] == "acme-bucket"
            assert call[call.index("--region") + 1] == "us-east-1"


def test_prune_runs_after_every_upload_and_before_the_invalidation(web_root: Path, monkeypatch) -> None:
    fake = FakeAws(listing=["retired.html"])
    monkeypatch.setattr(publish_web.subprocess, "run", fake)
    run("--bucket", "acme-bucket", "--distribution-id", "E2ACME", "--web-root", str(web_root), "--prune")
    services = [_service_call(call) for call in fake.calls]
    last_put = max(i for i, s in enumerate(services) if s == ["s3api", "put-object"])
    listing = services.index(["s3api", "list-objects-v2"])
    delete = services.index(["s3api", "delete-object"])
    invalidation = services.index(["cloudfront", "create-invalidation"])
    assert last_put < listing < delete < invalidation


def test_prune_on_an_empty_listing_deletes_nothing(web_root: Path, monkeypatch) -> None:
    fake = FakeAws(listing=None)
    monkeypatch.setattr(publish_web.subprocess, "run", fake)
    result = run("--bucket", "acme-bucket", "--distribution-id", "E2ACME", "--web-root", str(web_root), "--prune")
    assert result.code == 0
    assert fake.deleted() == []
    assert "nothing to delete" in result.out


def test_prune_refuses_a_listing_it_cannot_read(web_root: Path, monkeypatch) -> None:
    fake = FakeAws(listing={"Contents": "not a list"})
    monkeypatch.setattr(publish_web.subprocess, "run", fake)
    result = run("--bucket", "acme-bucket", "--distribution-id", "E2ACME", "--web-root", str(web_root), "--prune")
    assert result.code == 1
    assert "not a key list" in result.err
    assert fake.deleted() == []
    assert not any(_service_call(c) == ["cloudfront", "create-invalidation"] for c in fake.calls)


def test_a_dry_run_with_prune_prints_the_listing_and_runs_nothing(web_root: Path, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("a dry run must not run anything")

    monkeypatch.setattr(publish_web.subprocess, "run", forbidden)
    result = run("--stack-name", "acme-edge", "--web-root", str(web_root), "--dry-run", "--prune")
    assert result.code == 0
    assert "would run: aws s3api list-objects-v2 --bucket '<WebBucketName of acme-edge>'" in result.out
    assert "which keys would go is not known" in result.out


@pytest.mark.parametrize(
    "key, publishable",
    [
        ("index.html", True),
        ("app", True),
        ("assets/threefold.js", True),
        ("assets/icons/acme.svg", True),
        ("notes/readme.html", False),
        ("robots.txt", False),
        ("apps", False),
        ("assets", False),
    ],
)
def test_only_keys_of_a_shape_publish_writes_can_be_pruned(key: str, publishable: bool) -> None:
    assert publish_web.is_publishable_key(key) is publishable


def test_a_printed_command_quotes_hostile_values_for_a_posix_shell() -> None:
    """Printed, never run: a value that looks like shell syntax is one quoted word."""
    shown = publish_web._show(["aws", "s3api", "put-object", "--bucket", "acme; rm -rf /", "--key", "E1 $(whoami)"])
    assert shown == "aws s3api put-object --bucket 'acme; rm -rf /' --key 'E1 $(whoami)'"


def test_the_script_never_calls_a_shell() -> None:
    """Every command is an argument list: a bucket name can never become shell syntax."""
    source = (SCRIPTS / "publish_web.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system" not in source
    assert "os.popen" not in source
