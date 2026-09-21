"""Regenerates docs/openapi.yaml from src/threefold/web/openapi.json.

The JSON is the document the deployment serves. The YAML twin used to be kept by
hand and the two disagreed within one change, so it is generated instead:

    python scripts/generate_openapi_yaml.py

Needs PyYAML, which is not a runtime dependency of the service.
"""
from __future__ import annotations

import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "threefold" / "web" / "openapi.json"
TARGET = ROOT / "docs" / "openapi.yaml"
HEADER = (
    "# Generated from src/threefold/web/openapi.json, which is the document the deployment serves.\n"
    "# Edit that file and run scripts/generate_openapi_yaml.py; do not edit this one by hand.\n"
)


class _Dumper(yaml.SafeDumper):
    pass


def _string(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=">" if len(value) > 100 else None)


_Dumper.add_representer(str, _string)


def main() -> None:
    spec = json.loads(SOURCE.read_text(encoding="utf-8"))
    TARGET.write_text(
        HEADER + yaml.dump(spec, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
