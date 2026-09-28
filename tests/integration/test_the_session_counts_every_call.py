"""The sessions listing counts every call, not the retained fifty.

The store keeps the last fifty calls of a session for the loop detector's
sliding window. The count beside them used to be that window's length, so a
longer session counted 50. The session now keeps a cumulative counter beside
the history it counts, and both the listing and the detail read it. Rows
written before the counter read back the retained history's length, the best
count they hold.
"""
from __future__ import annotations

from threefold.domain.models import AgentSession, ToolActionType, ToolInvocation
from threefold.infrastructure.dynamo_repo import DynamoDBSessionRepository


def _call(n: int) -> ToolInvocation:
    return ToolInvocation(
        tool_name="run_tests",
        action_type=ToolActionType.COMMAND_EXEC,
        arguments={"cmd": f"pytest -q -k case{n}"},
        timestamp=f"2026-09-28T00:{n:02d}:00+00:00",
    )


def _session(session_id: str) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        developer_id="dev-counts",
        project_name="Acme-Counts",
        budget_usd=10.0,
    )


def test_sixty_calls_count_sixty_while_the_window_keeps_fifty() -> None:
    repo = DynamoDBSessionRepository()
    session = _session("session-counts-60")
    for n in range(60):
        session.record_tool_call(_call(n))
    assert session.total_calls == 60
    assert repo.save_session(session) is not False

    reloaded = repo.get_session("session-counts-60")
    assert reloaded is not None
    assert reloaded.total_calls == 60
    assert len(reloaded.history) == 50

    listing = {row["session_id"]: row for row in repo.list_sessions(limit=50)}
    assert listing["session-counts-60"]["calls"] == 60


def test_a_row_from_before_the_counter_counts_its_retained_history() -> None:
    repo = DynamoDBSessionRepository()
    session = _session("session-counts-old")
    for n in range(50):
        session.record_tool_call(_call(n))
    assert repo.save_session(session) is not False
    del repo._memory_store["SESSION#session-counts-old#METADATA"]["total_calls"]

    reloaded = repo.get_session("session-counts-old")
    assert reloaded is not None
    assert reloaded.total_calls == 50

    listing = {row["session_id"]: row for row in repo.list_sessions(limit=50)}
    assert listing["session-counts-old"]["calls"] == 50
