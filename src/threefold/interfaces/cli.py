"""Production CLI tool for Threefold CI/CD and terminal execution gates."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure src directory is on sys.path for direct CLI execution
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from threefold.application.dtos import ToolCallRequestDTO
from threefold.application.evaluator import GovernanceEvaluator
from threefold.domain.boundary_guard import SecretScanner


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="threefold",
        description="Threefold — Autonomous Coding Agent Governance & Cost Circuit-Breaker",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Eval command
    eval_parser = subparsers.add_parser("eval", help="Evaluate an agent tool invocation")
    eval_parser.add_argument("--tool", required=True, help="Name of the tool being called")
    eval_parser.add_argument("--action-type", default="FILE_READ", help="Action type (FILE_READ, FILE_WRITE, COMMAND_EXEC)")
    eval_parser.add_argument("--args", default="{}", help="JSON string of tool arguments")
    eval_parser.add_argument("--session-id", default="cli-session", help="Session ID")
    eval_parser.add_argument("--budget", type=float, default=10.0, help="Session budget in USD")

    # Scan command
    scan_parser = subparsers.add_parser("scan-file", help="Scan a file for sensitive credentials")
    scan_parser.add_argument("--path", required=True, help="Path to file to scan")

    args = parser.parse_args()
    evaluator = GovernanceEvaluator()

    if args.command == "eval":
        try:
            parsed_args = json.loads(args.args)
        except Exception as exc:
            print(f"Error: Invalid JSON arguments: {exc}", file=sys.stderr)
            sys.exit(2)

        req = ToolCallRequestDTO(
            session_id=args.session_id,
            developer_id="cli-user",
            project_name="CLI-Execution",
            tool_name=args.tool,
            action_type=args.action_type,
            arguments=parsed_args,
            budget_usd=args.budget,
        )
        verdict = evaluator.evaluate_tool_call(req)
        print(json.dumps(verdict.to_dict(), indent=2))
        if verdict.status != "APPROVED":
            sys.exit(1)
        sys.exit(0)

    elif args.command == "scan-file":
        target = Path(args.path)
        if not target.exists():
            print(f"Error: File '{args.path}' not found", file=sys.stderr)
            sys.exit(2)

        content = target.read_text(encoding="utf-8", errors="ignore")
        is_clean, reason = SecretScanner.scan_payload(content)
        if is_clean:
            print(f"PASS: {target.name} is clean (Zero credentials detected)")
            sys.exit(0)
        else:
            print(f"FAIL: {target.name} - {reason}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
