"""The hook posts its verdict request to http(s) only.

The service URL is operator-configured, and urlopen also speaks file:// and
custom schemes. A URL naming anything else raises before any network call,
and the hook then fails open or closed exactly as for an unreachable service.
"""
from __future__ import annotations

import pytest


def test_post_evaluation_refuses_a_file_url(hook) -> None:
    with pytest.raises(hook.ServiceUnavailable, match="non-http"):
        hook.post_evaluation({"tool_name": "Write"}, base="file:///etc/")


def test_post_evaluation_refuses_a_custom_scheme(hook) -> None:
    with pytest.raises(hook.ServiceUnavailable, match="non-http"):
        hook.post_evaluation({"tool_name": "Write"}, base="gopher://acme.example/")
