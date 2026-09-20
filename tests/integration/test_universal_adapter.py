"""Integration tests for Universal Agent Adapter and Session Inspection."""

import json
from pathlib import Path
from threefold.interfaces.api_handlers import lambda_handler


def test_openai_function_call_adapter():
    """Verify that OpenAI function call payload format is evaluated correctly."""
    event = {
        "httpMethod": "POST",
        "path": "/adapter/universal-tool-call",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "session_id": "sess-openai-1",
            "name": "read_file",
            "arguments": json.dumps({"path": "src/domain/models.py"}),
        }),
    }
    res = lambda_handler(event)
    assert res["statusCode"] == 200

    data = json.loads(res["body"])
    assert data["adapter_status"] == "SUCCESS"
    assert data["detected_tool_name"] == "read_file"
    assert data["evaluation"]["status"] == "APPROVED"


def test_anthropic_tool_use_adapter():
    """Verify that Anthropic tool_use payload format is evaluated correctly."""
    event = {
        "httpMethod": "POST",
        "path": "/adapter/universal-tool-call",
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "session_id": "sess-anthropic-1",
            "type": "tool_use",
            "name": "edit_file",
            "input": {"target_file": "src/utils.py", "content": "print('hello')"},
        }),
    }
    res = lambda_handler(event)
    assert res["statusCode"] == 200

    data = json.loads(res["body"])
    assert data["adapter_status"] == "SUCCESS"
    assert data["detected_tool_name"] == "edit_file"
    assert data["detected_action_type"] == "FILE_WRITE"


def test_session_inspection_endpoint():
    """Verify GET /sessions/{session_id} returns accurate operational counters."""
    session_id = "sess-inspect-42"
    # First make a tool call to populate session
    lambda_handler({
        "httpMethod": "POST",
        "path": "/evaluate-tool-call",
        "headers": {},
        "body": json.dumps({"session_id": session_id, "tool_name": "list_dir"}),
    })

    # Now inspect session
    res = lambda_handler({
        "httpMethod": "GET",
        "path": f"/sessions/{session_id}",
        "headers": {},
    })
    assert res["statusCode"] == 200
    data = json.loads(res["body"])
    assert data["session_id"] == session_id
    assert data["cumulative_cost_usd"] > 0.0
    assert data["budget_remaining_usd"] > 0.0
    assert data["tool_call_history_count"] >= 1


def test_pre_commit_gate_standalone(tmp_path: Path):
    """Verify that pre-commit gate blocks secret leaks in synthetic files."""
    import subprocess
    import sys

    # Create a synthetic dirty file with AWS key
    dirty_file = tmp_path / "dirty.py"
    dirty_file.write_text('AWS_SECRET = "AKIAIOSFODNN7EXAMPLE"', encoding="utf-8")

    # Run pre-commit-gate
    candidates = [
        Path(__file__).parents[2] / "scripts" / "pre-commit-gate.py",
        Path("repos/threefold/scripts/pre-commit-gate.py").resolve(),
        Path("scripts/pre-commit-gate.py").resolve(),
    ]
    script_path = next(p for p in candidates if p.is_file())
    result = subprocess.run(
        [sys.executable, str(script_path), "--file", str(dirty_file)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "SECRET LEAK DETECTED" in result.stderr

    # Clean file
    clean_file = tmp_path / "clean.py"
    clean_file.write_text('X = 42\nprint("safe")', encoding="utf-8")
    result_clean = subprocess.run(
        [sys.executable, str(script_path), "--file", str(clean_file)],
        capture_output=True,
        text=True,
    )
    assert result_clean.returncode == 0
