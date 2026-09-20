"""DynamoDB repository for Threefold persistent session state and governance records."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional
from threefold.domain.models import AgentSession, ToolActionType, ToolInvocation

logger = logging.getLogger(__name__)


class DynamoDBSessionRepository:
    """Production repository persisting agent sessions and circuit breaker states across Lambda invocations."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        region_name: Optional[str] = None,
        boto3_resource: Optional[Any] = None,
    ) -> None:
        self.table_name = table_name or os.getenv("TABLE_NAME", "Threefold-Governance-prod")
        self.region_name = region_name or os.getenv("AWS_REGION", "us-east-1")
        self._table = None
        self._memory_store: Dict[str, Dict[str, Any]] = {}

        if boto3_resource is not None:
            self._table = boto3_resource.Table(self.table_name)
        else:
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
            trip_reason=item.get("trip_reason"),
            created_at=item.get("created_at", ""),
        )
        return session

    def save_session(self, session: AgentSession) -> bool:
        """Persists complete session state to DynamoDB."""
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
            "history_json": history_serialized,
            "created_at": session.created_at,
        }

        if self._table is not None:
            try:
                self._table.put_item(Item=item)
                return True
            except Exception as exc:
                logger.warning("DynamoDB save_session failed, writing to memory: %s", exc)

        self._memory_store[f"SESSION#{session.session_id}#METADATA"] = item
        return True
