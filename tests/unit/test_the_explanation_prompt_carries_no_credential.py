"""The refused credential must not leave for the model, whatever is in front of it.

`review_agent_action` promises that nothing leaves for Bedrock that the ledger
would not keep, credentials included. It redacted the arguments after
`json.dumps` had already turned them into text, and `json.dumps` writes a
newline as the two characters backslash and n. That puts 'n', a word character,
directly in front of a token that began a line, so the ``(?<![A-Za-z0-9_])``
lookbehind in most of the credential patterns no longer matches and the token
went to the model exactly as the caller wrote it — on the very call the gate
had just refused for carrying it. A tab, a carriage return and any non-ASCII
character, which is escaped as ``\\uXXXX``, do the same thing.

`SecretScanner.scan_arguments` was fixed for this once, for scanning, and says
so in its docstring. These tests are the same lesson for what is sent: the
strings are redacted where they still are strings, before anything escapes
them.

Every credential below is synthetic and assembled at runtime, so this file
carries no credential-shaped literal of its own.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from threefold.application.dtos import EvaluationResultDTO, ToolCallRequestDTO
from threefold.infrastructure.bedrock_client import BedrockGovernanceClient

# Assembled from halves, as the rest of the suite assembles them.
TOKENS = {
    "GITHUB_TOKEN": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "OPENAI_KEY": "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
    "ANTHROPIC_KEY": "sk-ant-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
    "SLACK_TOKEN": "xoxb-" + "1234567890-ABCDEFGHIJKLMNOP",
    "GOOGLE_API_KEY": "AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r",
    "AWS_ACCESS_KEY": "AKIA" + "3XQ7MZKD2TVLPR9W",
}

# What sat in front of the token in the file the agent tried to write. Each is
# a character json.dumps escapes, which is what defeated the redaction.
SEPARATORS = {
    "newline": "\n",
    "tab": "\t",
    "carriage return": "\r",
    "non-ascii": "é",
    "backslash": "\\",
}


class _Runtime:
    """A Bedrock runtime that answers, and keeps what it was asked."""

    def __init__(self) -> None:
        self.prompts: List[str] = []

    def converse(self, **kwargs: Any) -> Dict[str, Any]:
        self.prompts.append(kwargs["messages"][0]["content"][0]["text"])
        return {"output": {"message": {"content": [{"text": "The gate refused it."}]}}}


class _Session:
    def __init__(self, runtime: _Runtime) -> None:
        self._runtime = runtime

    def client(self, *args: Any, **kwargs: Any) -> _Runtime:
        return self._runtime


def _client() -> tuple[BedrockGovernanceClient, _Runtime]:
    runtime = _Runtime()
    return BedrockGovernanceClient(boto3_session=_Session(runtime)), runtime


def _request(arguments: Any) -> ToolCallRequestDTO:
    return ToolCallRequestDTO(
        session_id="prompt-redaction",
        developer_id="anonymous",
        project_name="Acme-Redaction",
        tool_name="Write",
        action_type="FILE_WRITE",
        arguments=arguments,
    )


def _refusal(label: str) -> EvaluationResultDTO:
    return EvaluationResultDTO(
        verdict_id="V-redaction",
        session_id="prompt-redaction",
        status="BLOCKED_SECRET_DETECTED",
        risk_level="CRITICAL",
        reason=f"Sensitive credential detected: {label}",
        rule_evaluations={"SECRET_LEAKAGE_FREE": False},
        current_session_cost_usd=0.0,
        session_tripped=False,
        proof_hash="hash",
    )


def _prompt_for(arguments: Any, label: str = "GITHUB_TOKEN") -> str:
    client, runtime = _client()
    text, source = client.review_agent_action(_request(arguments), _refusal(label))
    assert source == "bedrock" and text
    assert len(runtime.prompts) == 1
    return runtime.prompts[0]


@pytest.mark.parametrize("label", sorted(TOKENS))
@pytest.mark.parametrize("separator", sorted(SEPARATORS))
def test_a_credential_behind_an_escaped_character_never_reaches_the_model(label, separator) -> None:
    token = TOKENS[label]
    content = "DEBUG = True" + SEPARATORS[separator] + token + "\n"
    prompt = _prompt_for({"file_path": "src/acme/app/settings.py", "content": content}, label)

    assert token not in prompt, f"{label} after a {separator} reached the model"
    # Which label replaced it is the scanner's business and the patterns
    # overlap by design: an Anthropic key is matched by the OpenAI pattern
    # first, because both begin "sk-". What matters here is that a label
    # stands where the credential stood.
    assert "REDACTED]" in prompt


def test_a_credential_nested_in_the_arguments_is_redacted_too() -> None:
    """An edit list is a list of objects, and the secret is a leaf inside it."""
    token = TOKENS["GITHUB_TOKEN"]
    prompt = _prompt_for(
        {"edits": [{"new_source": "import os\n" + token}, {"new_source": "clean"}]}
    )
    assert token not in prompt
    assert "[GITHUB_TOKEN REDACTED]" in prompt


def test_a_credential_used_as_a_key_is_redacted() -> None:
    """Keys are strings a caller chooses as much as values are."""
    token = TOKENS["GITHUB_TOKEN"]
    prompt = _prompt_for({"env": {"line\n" + token: "value"}})
    assert token not in prompt


def test_a_credential_in_a_value_that_is_not_json_is_redacted() -> None:
    """Anything the serialiser has to render itself is rendered redacted."""

    class _Opaque:
        def __init__(self, text: str) -> None:
            self._text = text

        def __str__(self) -> str:
            return self._text

    token = TOKENS["GITHUB_TOKEN"]
    prompt = _prompt_for({"payload": _Opaque("line\n" + token)})
    assert token not in prompt


def test_the_prompt_still_says_what_it_always_said() -> None:
    """Redacting the arguments must not cost the model the rest of the verdict."""
    prompt = _prompt_for({"file_path": "src/acme/domain/order.py", "content": "import boto3"})
    assert "Project: Acme-Redaction" in prompt
    assert "Tool requested: Write (FILE_WRITE)" in prompt
    assert "Arguments: {" in prompt
    assert "src/acme/domain/order.py" in prompt
    assert "Deterministic verdict: BLOCKED_SECRET_DETECTED" in prompt


def test_the_arguments_are_still_cut_to_about_two_kilobytes() -> None:
    """The bound on what one request sends a third party is unchanged."""
    prompt = _prompt_for({"content": "a" * 9000})
    assert "[truncated," in prompt
    assert len(prompt) < 4000


def test_a_credential_at_the_very_edge_of_the_cut_does_not_leave_in_halves() -> None:
    """prompt_safe redacts before truncating; the leaves are redacted before that."""
    token = TOKENS["GITHUB_TOKEN"]
    prompt = _prompt_for({"content": "b" * 2040 + "\n" + token})
    assert token not in prompt
    assert token[:20] not in prompt
