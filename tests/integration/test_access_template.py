"""The template gives the application what it needs, and nothing it does not.

One new parameter reaches the function: DefaultHookStage, the stage a hook
call is judged in when its project has none. It defaults to observe, which is
how a newly connected repository starts. The function role gains one action,
DeleteItem, on the table it already had, because a sign-in code is spent by a
conditional delete and signing out deletes the session. A browser signs out
with DELETE and a bearer, so the API's CORS configuration has to allow both,
and the catch-all route has to carry DELETE to the function at all.
"""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = (Path(__file__).resolve().parents[2] / "deploy" / "template.yml").read_text(encoding="utf-8")


def _block(heading: str, indent: int) -> str:
    """The lines under `heading` at `indent` spaces, up to the next line as shallow."""
    match = re.search(rf"^{' ' * indent}{re.escape(heading)}:\n(.*?)(?=^ {{0,{indent}}}\S)", TEMPLATE, re.S | re.M)
    assert match, f"The template has no {heading}"
    return match.group(1)


def test_the_default_hook_stage_is_a_parameter_that_starts_in_observe() -> None:
    block = _block("DefaultHookStage", 2)
    assert re.search(r"^    Type: String$", block, re.M)
    assert re.search(r"^    Default: observe$", block, re.M)
    assert re.search(r"^    AllowedValues: \['observe', 'enforce'\]$", block, re.M), (
        "Anything else would reach the evaluator as a stage it does not know"
    )


def test_the_default_hook_stage_reaches_the_function() -> None:
    assert re.search(r"^        DEFAULT_HOOK_STAGE: !Ref DefaultHookStage$", _block("Variables", 6), re.M)


def test_the_role_may_delete_from_the_table_and_nowhere_else() -> None:
    policies = _block("Policies", 6)
    statements = re.split(r"^            - Effect: ", policies, flags=re.M)[1:]
    deleting = [s for s in statements if "dynamodb:DeleteItem" in s]
    assert len(deleting) == 1, "DeleteItem is granted once"
    assert re.search(r"^              Resource: !GetAtt ThreefoldTable\.Arn$", deleting[0], re.M)
    for action in ("PutItem", "GetItem", "UpdateItem", "DeleteItem", "Query"):
        assert f"dynamodb:{action}" in deleting[0], f"{action} is needed for the AUTH#, STATS# and CONFIG# items"
    assert "dynamodb:*" not in policies and "Resource: '*'" not in policies


def test_a_browser_may_sign_out_with_a_bearer() -> None:
    cors = _block("CorsConfiguration", 6)
    methods = re.search(r"AllowMethods: \[(.*)\]", cors).group(1)
    headers = re.search(r"AllowHeaders: \[(.*)\]", cors).group(1)
    assert "'DELETE'" in methods
    assert "'Authorization'" in headers


def test_the_catch_all_route_carries_every_method_to_the_function() -> None:
    proxy = _block("ProxyRoute", 8)
    assert re.search(r"^            Path: /\{proxy\+\}$", proxy, re.M)
    assert re.search(r"^            Method: ANY$", proxy, re.M), "DELETE /api/auth/sessions would never reach the function"
