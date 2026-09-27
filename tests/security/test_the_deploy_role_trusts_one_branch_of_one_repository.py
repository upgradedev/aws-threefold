"""Only this repository's main branch may assume the role CI deploys with.

deploy/iam/github-trust.json is the deploy role's trust policy. GitHub's OIDC
provider mints a token whose `sub` claim is `repo:<owner>/<repo>:ref:<ref>` for
a job with no environment, and AWS hands out the role's credentials only when
the trust policy's condition matches that claim exactly. A `sub` naming a
repository that does not exist can never match, so the role could never be
assumed and every run failed at the credentials step; a `sub` with a wildcard,
or matched with StringLike, would let a fork, a branch or a pull request deploy.
The repository is read from the URL README.md states, so the policy and the
README cannot drift apart silently. Nothing calls AWS.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TRUST = json.loads((ROOT / "deploy" / "iam" / "github-trust.json").read_text(encoding="utf-8"))
README = (ROOT / "README.md").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
PROVIDER = "token.actions.githubusercontent.com"


def _repository(text: str = README) -> str:
    """The one owner/repository README.md links to on GitHub."""
    # A URL that ends a sentence carries its full stop, and a clone URL ends in
    # .git; neither is part of the repository's name, which the token's sub
    # carries without them.
    found = {
        re.sub(r"\.git$", "", m.rstrip("."))
        for m in re.findall(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text)
    }
    # A second repository in the README would leave the test choosing between
    # them; it fails instead, so a person decides which one deploys.
    assert len(found) == 1, f"README.md links to {sorted(found)}; the trust policy can name only one"
    return found.pop()


@pytest.mark.parametrize(
    "line",
    [
        "[CI](https://github.com/acme-devco/acme-widgets/actions/workflows/ci.yml)",
        "git clone https://github.com/acme-devco/acme-widgets.git",
        "The source is at https://github.com/acme-devco/acme-widgets.",
        "Clone https://github.com/acme-devco/acme-widgets.git.",
    ],
)
def test_a_link_clone_url_or_sentence_end_names_the_same_repository(line: str) -> None:
    text = f"See https://github.com/acme-devco/acme-widgets for the source.\n{line}\n"
    assert _repository(text) == "acme-devco/acme-widgets"


def test_a_second_repository_in_the_readme_fails_rather_than_being_chosen_between() -> None:
    text = "https://github.com/acme-devco/acme-widgets\nhttps://github.com/acme-devco/acme-widgets-fork\n"
    with pytest.raises(AssertionError):
        _repository(text)


def _account() -> str:
    match = re.search(r"^  DEPLOY_ROLE: arn:aws:iam::([0-9]{12}):role/threefold-github-deploy$", WORKFLOW, re.M)
    assert match, "deploy.yml assumes no threefold-github-deploy role"
    return match.group(1)


def _statement() -> dict:
    statements = TRUST["Statement"]
    assert len(statements) == 1, "a second statement would be a second way in"
    return statements[0]


def test_the_trust_policy_is_one_statement_for_githubs_oidc_provider() -> None:
    assert TRUST["Version"] == "2012-10-17"
    statement = _statement()
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert statement["Principal"] == {"Federated": f"arn:aws:iam::{_account()}:oidc-provider/{PROVIDER}"}
    assert not {"NotAction", "NotPrincipal"} & set(statement)


def test_the_token_must_be_for_sts() -> None:
    assert _statement()["Condition"]["StringEquals"][f"{PROVIDER}:aud"] == "sts.amazonaws.com"


def test_the_subject_is_the_main_branch_of_the_repository_the_readme_names() -> None:
    assert _statement()["Condition"]["StringEquals"][f"{PROVIDER}:sub"] == f"repo:{_repository()}:ref:refs/heads/main"


def test_the_condition_matches_exactly_and_nothing_else() -> None:
    """StringEquals only: no StringLike, no set qualifier, no wildcard in any value."""
    condition = _statement()["Condition"]
    assert list(condition) == ["StringEquals"]
    assert sorted(condition["StringEquals"]) == [f"{PROVIDER}:aud", f"{PROVIDER}:sub"]
    for key, value in condition["StringEquals"].items():
        assert isinstance(value, str), f"{key} lists several values, so any one of them would do"
        assert not set(value) & {"*", "?"}, f"{key} holds a wildcard: {value}"


def test_deploys_stay_a_persons_decision() -> None:
    """The workflow runs when someone dispatches it, never on a push or a schedule."""
    on = re.search(r"^on:\n(.*?)^\S", WORKFLOW, re.S | re.M)
    assert on, "deploy.yml has no on: block"
    triggers = re.findall(r"^  (\w+):", on.group(1), re.M)
    assert triggers == ["workflow_dispatch"]


def test_the_deploy_job_names_no_environment() -> None:
    """A job with an environment is issued sub repo:<owner>/<repo>:environment:<name>, which this policy refuses."""
    assert not re.search(r"^\s+environment:", WORKFLOW, re.M)
