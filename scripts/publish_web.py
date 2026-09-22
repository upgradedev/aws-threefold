#!/usr/bin/env python3
"""Publishes the pages to the edge: the private bucket, then one invalidation.

    publish_web.py --stack-name threefold-edge [--region us-east-1] [--dry-run]
    publish_web.py --bucket NAME --distribution-id ID [--region us-east-1] [--dry-run]

Reads src/threefold/web/*.html and every file under src/threefold/web/assets/,
replaces __THREEFOLD_BASE_PATH__ with the empty string, because behind
CloudFront the pages and the API share one origin and every API path sits at
its root, and uploads each object with its Content-Type and Cache-Control.
dashboard.html is uploaded a second time under the key "app", as text/html, so
/app opens the dashboard at the edge as it does on the API's own URL. Assets go
first and pages after them, so a page never names a script that is not there
yet. Last, one invalidation of /* clears the edge; a wildcard counts as a single
path, well inside the thousand free paths a month.

openapi.json is not uploaded: the function serves the document deployed with
its code, and deploy/edge.yml sends /openapi.json to the function.

--stack-name reads WebBucketName and DistributionId from the edge stack's
outputs with "aws cloudformation describe-stacks". --dry-run prints every step,
each AWS CLI command included, and runs none of them, not even that read.

Standard library and the AWS CLI v2, called through subprocess.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
DEFAULT_WEB_ROOT = HERE.parent / "src" / "threefold" / "web"
DEFAULT_REGION = "us-east-1"

BASE_PATH_TOKEN = "__THREEFOLD_BASE_PATH__"
# Same origin: the distribution routes every API path at the root to the
# function, so the pages call /api/overview rather than /prod/api/overview.
EDGE_BASE_PATH = ""

# The key /app is served from, and the page it holds.
APP_KEY = "app"
APP_SOURCE = "dashboard.html"

# A browser revalidates a page on every visit (a cheap 304 while it is
# unchanged), and the edge keeps it a minute; every publish invalidates it.
PAGE_CACHE_CONTROL = "public, max-age=0, s-maxage=60, must-revalidate"
# Assets carry no content hash in their names, so a browser keeps one only five
# minutes, while the edge keeps it up to a year until the next invalidation.
ASSET_CACHE_CONTROL = "public, max-age=300, s-maxage=31536000"

# Every response carries X-Content-Type-Options: nosniff, so a wrong type is a
# broken page rather than a guess. An extension missing here is refused, not
# uploaded as application/octet-stream.
CONTENT_TYPES: Dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
# Files the base path is substituted in. Images and fonts are copied byte for byte.
TEXT_SUFFIXES = {".html", ".js", ".mjs", ".css", ".svg", ".json", ".map", ".txt"}

REQUIRED_OUTPUTS = ("WebBucketName", "DistributionId")


class PublishError(RuntimeError):
    """A step failed; the message says which, and nothing after it ran."""


@dataclass(frozen=True)
class WebObject:
    """One object as it will be stored in the bucket."""

    key: str
    body: bytes
    content_type: str
    cache_control: str
    source: Path


def content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in CONTENT_TYPES:
        raise PublishError(
            f"no Content-Type is known for {path.name}; add its extension to CONTENT_TYPES in publish_web.py"
        )
    return CONTENT_TYPES[suffix]


def render(path: Path) -> bytes:
    """The file as the edge serves it: the base path substituted in a text file."""
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return path.read_bytes()
    text = path.read_text(encoding="utf-8").replace(BASE_PATH_TOKEN, EDGE_BASE_PATH)
    return text.encode("utf-8")


def collect(web_root: Path) -> List[WebObject]:
    """Everything to upload, in upload order: assets, then pages, then /app."""
    web_root = Path(web_root)
    pages = sorted(path for path in web_root.glob("*.html") if path.is_file())
    if not pages:
        raise PublishError(f"no pages were found in {web_root}")
    objects: List[WebObject] = []
    assets_root = web_root / "assets"
    if assets_root.is_dir():
        for path in sorted(assets_root.rglob("*")):
            relative = path.relative_to(assets_root)
            # Editor and OS droppings (.DS_Store, .swp) are never published.
            if not path.is_file() or any(part.startswith(".") for part in relative.parts):
                continue
            objects.append(
                WebObject(
                    key="assets/" + relative.as_posix(),
                    body=render(path),
                    content_type=content_type_for(path),
                    cache_control=ASSET_CACHE_CONTROL,
                    source=path,
                )
            )
    for page in pages:
        objects.append(
            WebObject(page.name, render(page), CONTENT_TYPES[".html"], PAGE_CACHE_CONTROL, page)
        )
    dashboard = web_root / APP_SOURCE
    if dashboard.is_file():
        objects.append(
            WebObject(APP_KEY, render(dashboard), CONTENT_TYPES[".html"], PAGE_CACHE_CONTROL, dashboard)
        )
    return objects


def _aws(profile: Optional[str]) -> List[str]:
    return ["aws", "--profile", profile] if profile else ["aws"]


def _show(command: Sequence[str]) -> str:
    # POSIX quoting reads the same in bash and PowerShell, and keeps /* from
    # being expanded by a shell the printed line is pasted into.
    return shlex.join(list(command))


def run_aws(command: Sequence[str]) -> str:
    """Runs one AWS CLI command and returns its standard output."""
    try:
        result = subprocess.run(list(command), capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise PublishError("the AWS CLI v2 is not on PATH; install it or run from a shell that has it") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise PublishError(f"{_show(command)} failed with exit code {result.returncode}: {detail}")
    return result.stdout or ""


def describe_stack_command(stack: str, region: str, profile: Optional[str]) -> List[str]:
    return _aws(profile) + [
        "cloudformation", "describe-stacks", "--stack-name", stack, "--region", region, "--output", "json",
    ]


def stack_outputs(stack: str, region: str, profile: Optional[str]) -> Dict[str, str]:
    """The edge stack's outputs, by key."""
    raw = run_aws(describe_stack_command(stack, region, profile))
    try:
        stacks = json.loads(raw)["Stacks"]
        outputs = stacks[0].get("Outputs") or []
        found = {item["OutputKey"]: item["OutputValue"] for item in outputs}
    except (ValueError, KeyError, IndexError, TypeError):
        raise PublishError(f"describe-stacks for {stack} answered something that is not a stack description") from None
    missing = [name for name in REQUIRED_OUTPUTS if not found.get(name)]
    if missing:
        raise PublishError(
            f"stack {stack} in {region} has no output {', '.join(missing)}; is it the stack deploy/edge.yml created?"
        )
    return found


def put_object_command(bucket: str, item: WebObject, body_path: str, region: str, profile: Optional[str]) -> List[str]:
    return _aws(profile) + [
        "s3api", "put-object",
        "--bucket", bucket,
        "--key", item.key,
        "--body", body_path,
        "--content-type", item.content_type,
        "--cache-control", item.cache_control,
        "--region", region,
        "--output", "json",
    ]


def invalidation_command(distribution_id: str, profile: Optional[str]) -> List[str]:
    return _aws(profile) + [
        "cloudfront", "create-invalidation", "--distribution-id", distribution_id, "--paths", "/*", "--output", "json",
    ]


def _parse(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="publish_web.py",
        description="Upload the pages to the edge bucket and invalidate the distribution.",
    )
    parser.add_argument("--stack-name", help="the edge stack; its outputs name the bucket and the distribution")
    parser.add_argument("--bucket", help="the pages bucket, instead of --stack-name")
    parser.add_argument("--distribution-id", help="the distribution, instead of --stack-name")
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"the edge stack's region (default {DEFAULT_REGION})")
    parser.add_argument("--profile", help="an AWS CLI profile; otherwise the CLI's own default")
    parser.add_argument("--web-root", type=Path, default=DEFAULT_WEB_ROOT, help="the directory the pages are read from")
    parser.add_argument("--dry-run", action="store_true", help="print every step and run none of them")
    args = parser.parse_args(argv)
    if args.stack_name and (args.bucket or args.distribution_id):
        parser.error("give --stack-name, or --bucket and --distribution-id, not both")
    if not args.stack_name and not (args.bucket and args.distribution_id):
        parser.error("give --stack-name, or both --bucket and --distribution-id")
    return args


def publish(args: argparse.Namespace, out=None) -> int:
    out = out or sys.stdout
    objects = collect(args.web_root)
    dry = args.dry_run
    prefix = "would run: " if dry else "run: "

    domain = None
    if args.stack_name:
        command = describe_stack_command(args.stack_name, args.region, args.profile)
        print(f"{prefix}{_show(command)}", file=out)
        if dry:
            bucket = f"<WebBucketName of {args.stack_name}>"
            distribution_id = f"<DistributionId of {args.stack_name}>"
        else:
            outputs = stack_outputs(args.stack_name, args.region, args.profile)
            bucket, distribution_id = outputs["WebBucketName"], outputs["DistributionId"]
            domain = outputs.get("DistributionDomainName")
            print(f"  bucket {bucket}, distribution {distribution_id}", file=out)
    else:
        bucket, distribution_id = args.bucket, args.distribution_id

    if not any(item.key == APP_KEY for item in objects):
        print(f"note: {APP_SOURCE} is not in {args.web_root}, so /app is not published", file=out)

    with tempfile.TemporaryDirectory(prefix="threefold-publish-") as staging:
        for index, item in enumerate(objects):
            body_path = os.path.join(staging, f"{index:03d}-{item.key.replace('/', '_')}")
            shown_body = body_path
            if dry:
                shown_body = f"<{item.source.name} rendered, {len(item.body)} bytes>"
            else:
                with open(body_path, "wb") as handle:
                    handle.write(item.body)
            print(
                f"put s3://{bucket}/{item.key}  {item.content_type}  |  {item.cache_control}  |  {len(item.body)} bytes",
                file=out,
            )
            command = put_object_command(bucket, item, shown_body if dry else body_path, args.region, args.profile)
            print(f"  {prefix}{_show(command)}", file=out)
            if not dry:
                run_aws(command)

    command = invalidation_command(distribution_id, args.profile)
    print(f"invalidate /* on {distribution_id}", file=out)
    print(f"  {prefix}{_show(command)}", file=out)
    if dry:
        print(f"dry run: {len(objects)} objects planned, nothing was sent", file=out)
        return 0
    answer = run_aws(command)
    try:
        invalidation_id = json.loads(answer)["Invalidation"]["Id"]
    except (ValueError, KeyError, TypeError):
        invalidation_id = "(id not reported)"
    print(f"published {len(objects)} objects to s3://{bucket}; invalidation {invalidation_id}", file=out)
    if domain:
        print(f"site: https://{domain}/", file=out)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse(argv)
    try:
        return publish(args)
    except PublishError as error:
        print(f"publish_web: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
