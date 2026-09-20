"""DynamoDB repository for Threefold persistent session state and governance records."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
from threefold.domain.models import AgentSession, ToolActionType, ToolInvocation

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 30 * 24 * 3600


class SessionConflictError(RuntimeError):
    """Raised when a write is refused because the stored session is already tripped.

    A tripped session is terminal. Once the circuit breaker has fired, no later
    write may advance or clear it, so a second Lambda container cannot approve
    work on a session another container has already halted.
    """


class DynamoDBSessionRepository:
    """Production repository persisting agent sessions and circuit breaker states across Lambda invocations."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        region_name: Optional[str] = None,
        boto3_resource: Optional[Any] = None,
    ) -> None:
        self.table_name = table_name or os.getenv("TABLE_NAME", "Threefold-Governance-prod")
        # AWS_REGION is set by the Lambda runtime; it is reserved and cannot be
        # declared in the function's own environment variables.
        self.region_name = region_name or os.getenv("AWS_REGION", "eu-west-1")
        self._table = None
        self._memory_store: Dict[str, Dict[str, Any]] = {}
        self._offline = os.getenv("THREEFOLD_OFFLINE", "").lower() in ("1", "true", "yes")

        if boto3_resource is not None:
            self._table = boto3_resource.Table(self.table_name)
        elif not self._offline:
            try:
                import boto3
                dynamo = boto3.resource("dynamodb", region_name=self.region_name)
                self._table = dynamo.Table(self.table_name)
            except Exception as exc:
                logger.info("DynamoDB client operating in local/in-memory fallback: %s", exc)
                self._table = None

    @property
    def is_live(self) -> bool:
        return self._table is not None

    @property
    def persistence_mode(self) -> str:
        """Reports where session state actually landed on the most recent write.

        'dynamodb' means the table accepted the write. 'memory' means the write
        survives only inside this process, so a second container will not see it.
        """
        return self._last_persistence_mode

    _last_persistence_mode: str = "memory"

    def probe(self) -> tuple[bool, str]:
        """Read probe exercising the same permission the application uses.

        Uses GetItem rather than DescribeTable because the function's IAM policy
        grants GetItem, so this reports the capability that actually matters.
        """
        if self._table is None:
            return False, "No DynamoDB table bound; running on the in-process store"
        try:
            self._table.get_item(Key={"PK": "SESSION#__probe__", "SK": "METADATA"})
            return True, f"GetItem succeeded against {self.table_name}"
        except Exception as exc:
            return False, f"GetItem failed against {self.table_name}: {exc}"

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        """Loads persistent session from DynamoDB or memory store."""
        item = None
        if self._table is not None:
            try:
                res = self._table.get_item(Key={"PK": f"SESSION#{session_id}", "SK": "METADATA"})
                item = res.get("Item")
            except Exception as exc:
                logger.warning("Failed to fetch session from DynamoDB: %s", exc)

        if item is None:
            item = self._memory_store.get(f"SESSION#{session_id}#METADATA")

        if not item:
            return None

        # Reconstruct ToolInvocation history
        history: List[ToolInvocation] = []
        raw_history = item.get("history_json")
        if raw_history:
            try:
                parsed_list = json.loads(raw_history)
                for h in parsed_list:
                    action_type = ToolActionType(h.get("action_type", "UNKNOWN"))
                    history.append(
                        ToolInvocation(
                            tool_name=h.get("tool_name", ""),
                            action_type=action_type,
                            arguments=h.get("arguments", {}),
                            timestamp=h.get("timestamp", ""),
                        )
                    )
            except Exception as exc:
                logger.warning("Could not parse history JSON: %s", exc)

        session = AgentSession(
            session_id=session_id,
            developer_id=item.get("developer_id", "dev-default"),
            project_name=item.get("project_name", "Acme-Core"),
            budget_usd=float(item.get("budget_usd", 10.0)),
            total_cost_usd=float(item.get("total_cost_usd", 0.0)),
            total_input_tokens=int(item.get("total_input_tokens", 0)),
            total_output_tokens=int(item.get("total_output_tokens", 0)),
            history=history,
            is_tripped=bool(item.get("is_tripped", False)),
            trip_reason=item.get("trip_reason") or None,
            is_terminated=bool(item.get("is_terminated", False)),
            terminated_by=item.get("terminated_by") or None,
            termination_reason=item.get("termination_reason") or None,
            created_at=item.get("created_at", ""),
        )
        return session

    def save_session(self, session: AgentSession, force: bool = False) -> bool:
        """Persists complete session state to DynamoDB.

        Unless ``force`` is set, the write is refused when the stored session is
        already tripped, and ``SessionConflictError`` is raised. Operators pass
        ``force`` to terminate a session that has already halted itself.
        """
        history_serialized = json.dumps([
            {
                "tool_name": h.tool_name,
                "action_type": h.action_type.value,
                "arguments": h.arguments,
                "timestamp": h.timestamp,
            }
            for h in session.history[-50:]  # Keep last 50 actions for sliding window
        ])

        item = {
            "PK": f"SESSION#{session.session_id}",
            "SK": "METADATA",
            "developer_id": session.developer_id,
            "project_name": session.project_name,
            "budget_usd": str(session.budget_usd),
            "total_cost_usd": str(session.total_cost_usd),
            "total_input_tokens": session.total_input_tokens,
            "total_output_tokens": session.total_output_tokens,
            "is_tripped": session.is_tripped,
            "trip_reason": session.trip_reason or "",
            "is_terminated": session.is_terminated,
            "terminated_by": session.terminated_by or "",
            "termination_reason": session.termination_reason or "",
            "history_json": history_serialized,
            "created_at": session.created_at,
            "ttl": int(time.time()) + SESSION_TTL_SECONDS,
        }
        memory_key = f"SESSION#{session.session_id}#METADATA"

        if self._table is not None:
            put_kwargs: Dict[str, Any] = {"Item": item}
            if not force:
                put_kwargs["ConditionExpression"] = (
                    "attribute_not_exists(PK) OR attribute_not_exists(is_tripped) "
                    "OR is_tripped = :not_tripped"
                )
                put_kwargs["ExpressionAttributeValues"] = {":not_tripped": False}
            try:
                self._table.put_item(**put_kwargs)
                self._last_persistence_mode = "dynamodb"
                self._memory_store[memory_key] = item
                return True
            except Exception as exc:
                error_code = ""
                response = getattr(exc, "response", None)
                if isinstance(response, dict):
                    error_code = response.get("Error", {}).get("Code", "")
                if (
                    type(exc).__name__ == "ConditionalCheckFailedException"
                    or error_code == "ConditionalCheckFailedException"
                ):
                    raise SessionConflictError(
                        f"Session {session.session_id} is already tripped in durable storage"
                    ) from exc
                logger.warning("DynamoDB save_session failed, writing to memory: %s", exc)

        self._last_persistence_mode = "memory"
        stored = self._memory_store.get(memory_key)
        if not force and stored and stored.get("is_tripped"):
            raise SessionConflictError(
                f"Session {session.session_id} is already tripped in the in-process store"
            )
        self._memory_store[memory_key] = item
        return True
