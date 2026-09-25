"""The local server binds to loopback unless told otherwise.

Binding to every interface is only needed inside the container, where the
Dockerfile passes --host explicitly. A developer running the server on their
own machine gets 127.0.0.1 without asking for it.
"""
from __future__ import annotations

import inspect

from threefold.interfaces.server import parse_args, run_server


def test_the_default_host_is_loopback() -> None:
    assert parse_args([]).host == "127.0.0.1"
    assert parse_args([]).port == 8001
    assert inspect.signature(run_server).parameters["host"].default == "127.0.0.1"


def test_an_explicit_host_is_honoured() -> None:
    assert parse_args(["--host", "0.0.0.0"]).host == "0.0.0.0"
