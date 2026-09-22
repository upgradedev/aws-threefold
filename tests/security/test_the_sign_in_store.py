"""The sign-in store keeps hashes, spends a code once, and lets nothing outlive its time.

Two paths are tested because two exist. The suite runs offline, so the memory
path is the one every request test exercises, and it has to enforce single use
and expiry on its own. The table path is driven through a fake table that
behaves like DynamoDB where it matters: a conditional delete that fails raises
ConditionalCheckFailedException, and an item is still there after its ttl.
"""
from __future__ import annotations

import json

import pytest

from threefold.infrastructure import auth_store
from threefold.infrastructure.auth_store import (
    AUTH_PREFIX,
    CODE_TTL_SECONDS,
    SESSION_CACHE_SECONDS,
    SESSION_TOKEN_PREFIX,
    SESSION_TTL_SECONDS,
    AuthStore,
    token_hash,
)

KEY_REF = "0123456789abcdef"


class _Clock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(auth_store, "_now", fake)
    return fake


@pytest.fixture
def memory_store(monkeypatch) -> AuthStore:
    monkeypatch.setenv("THREEFOLD_OFFLINE", "1")
    store = AuthStore()
    assert not store.is_live
    return store


class ConditionalCheckFailedException(Exception):
    """Named as boto3 names it; the store recognises the failure by name."""


class _FakeTable:
    """Enough of a DynamoDB table to tell a conditional delete from a plain one."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail_with: Exception | None = None

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def put_item(self, Item: dict) -> dict:
        self.calls.append(("put_item", {"Item": Item}))
        self._maybe_fail()
        self.items[(Item["PK"], Item["SK"])] = dict(Item)
        return {}

    def get_item(self, Key: dict) -> dict:
        self.calls.append(("get_item", {"Key": Key}))
        self._maybe_fail()
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key: dict, **kwargs) -> dict:
        self.calls.append(("delete_item", {"Key": Key, **kwargs}))
        self._maybe_fail()
        item = self.items.get((Key["PK"], Key["SK"]))
        if "ConditionExpression" in kwargs:
            now = kwargs["ExpressionAttributeValues"][":now"]
            if item is None or int(item["ttl"]) <= now:
                raise ConditionalCheckFailedException("The conditional request failed")
        self.items.pop((Key["PK"], Key["SK"]), None)
        return {"Attributes": dict(item)} if (item and kwargs.get("ReturnValues") == "ALL_OLD") else {}


class _FakeResource:
    def __init__(self, table: _FakeTable) -> None:
        self.table = table

    def Table(self, name: str) -> _FakeTable:  # noqa: N802 - boto3's spelling
        return self.table


@pytest.fixture
def table() -> _FakeTable:
    return _FakeTable()


@pytest.fixture
def table_store(table) -> AuthStore:
    store = AuthStore(table_name="acme-test-table", boto3_resource=_FakeResource(table))
    assert store.is_live
    return store


# ------------------------------------------------------------------ memory path


def test_a_code_is_spent_once(memory_store, clock) -> None:
    code, expires = memory_store.create_code(KEY_REF)
    assert expires == int(clock.now) + CODE_TTL_SECONDS == int(clock.now) + 120
    first = memory_store.consume_code(code)
    assert first and first["key_ref"] == KEY_REF
    assert memory_store.consume_code(code) is None, "A second exchange of the same code must fail"


def test_a_code_lapses_after_two_minutes(memory_store, clock) -> None:
    code, _ = memory_store.create_code(KEY_REF)
    clock.now += CODE_TTL_SECONDS
    assert memory_store.consume_code(code) is None


def test_a_code_is_good_until_just_before_it_lapses(memory_store, clock) -> None:
    code, _ = memory_store.create_code(KEY_REF)
    clock.now += CODE_TTL_SECONDS - 1
    assert memory_store.consume_code(code) is not None


@pytest.mark.parametrize("presented", ["", "nope", "x" * 300, None, 17, ["a"]])
def test_anything_but_a_live_code_is_unknown(memory_store, presented) -> None:
    assert memory_store.consume_code(presented) is None


def test_only_hashes_are_kept_in_memory(memory_store) -> None:
    code, _ = memory_store.create_code(KEY_REF)
    token, _ = memory_store.create_session(KEY_REF)
    held = json.dumps(memory_store._memory)
    assert code not in held and token not in held
    assert f"{AUTH_PREFIX}{token_hash(code)}#CODE" in memory_store._memory
    assert f"{AUTH_PREFIX}{token_hash(token)}#SESSION" in memory_store._memory


def test_a_session_lives_twelve_hours(memory_store, clock) -> None:
    token, expires = memory_store.create_session(KEY_REF)
    assert token.startswith(SESSION_TOKEN_PREFIX)
    assert expires == int(clock.now) + SESSION_TTL_SECONDS == int(clock.now) + 43200
    assert memory_store.session(token)["key_ref"] == KEY_REF
    clock.now += SESSION_TTL_SECONDS - 1
    assert memory_store.session(token) is not None
    clock.now += 1
    assert memory_store.session(token) is None


def test_a_cached_session_is_never_trusted_past_its_expiry(memory_store, clock) -> None:
    token, _ = memory_store.create_session(KEY_REF)
    clock.now += SESSION_TTL_SECONDS - 10
    assert memory_store.session(token) is not None, "Cached now, with ten seconds left"
    clock.now += 10
    assert memory_store.session(token) is None


def test_a_revoked_session_is_refused_at_once(memory_store) -> None:
    token, _ = memory_store.create_session(KEY_REF)
    assert memory_store.session(token) is not None
    assert memory_store.revoke(token) is True
    assert memory_store.session(token) is None


def test_a_value_without_the_session_prefix_is_never_looked_up(memory_store) -> None:
    token, _ = memory_store.create_session(KEY_REF)
    assert memory_store.session(token[len(SESSION_TOKEN_PREFIX):]) is None
    assert memory_store.session("operator-key-shaped-value") is None


def test_every_code_and_token_is_different(memory_store) -> None:
    codes = {memory_store.create_code(KEY_REF)[0] for _ in range(50)}
    tokens = {memory_store.create_session(KEY_REF)[0] for _ in range(50)}
    assert len(codes) == 50 and len(tokens) == 50
    assert all(len(code) >= 32 for code in codes)


# ------------------------------------------------------------------- table path


def test_the_table_holds_only_hashes(table_store, table) -> None:
    code, _ = table_store.create_code(KEY_REF)
    token, _ = table_store.create_session(KEY_REF)
    written = json.dumps([item for item in table.items.values()])
    assert code not in written and token not in written
    assert (f"{AUTH_PREFIX}{token_hash(code)}", "CODE") in table.items
    assert (f"{AUTH_PREFIX}{token_hash(token)}", "SESSION") in table.items
    assert not table_store._memory, "Nothing is mirrored to memory while the table answers"


def test_a_code_is_taken_off_the_table_by_a_conditional_delete(table_store, table, clock) -> None:
    code, _ = table_store.create_code(KEY_REF)
    assert table_store.consume_code(code)["key_ref"] == KEY_REF
    name, kwargs = table.calls[-1]
    assert name == "delete_item"
    assert kwargs["ConditionExpression"] == "attribute_exists(PK) AND #ttl > :now"
    assert kwargs["ExpressionAttributeNames"] == {"#ttl": "ttl"}
    assert kwargs["ExpressionAttributeValues"] == {":now": int(clock.now)}
    assert kwargs["ReturnValues"] == "ALL_OLD"
    assert table_store.consume_code(code) is None, "The delete has already happened"


def test_an_expired_code_still_on_the_table_is_refused(table_store, table, clock) -> None:
    """DynamoDB's TTL is lazy, so the item is still there; the condition refuses it."""
    code, _ = table_store.create_code(KEY_REF)
    clock.now += CODE_TTL_SECONDS + 3600
    assert (f"{AUTH_PREFIX}{token_hash(code)}", "CODE") in table.items
    assert table_store.consume_code(code) is None


def test_an_expired_session_still_on_the_table_is_refused(table_store, table, clock) -> None:
    token, _ = table_store.create_session(KEY_REF)
    clock.now += SESSION_TTL_SECONDS + 3600
    assert (f"{AUTH_PREFIX}{token_hash(token)}", "SESSION") in table.items
    assert table_store.session(token) is None


def test_an_outage_during_the_exchange_fails_closed(table_store, table) -> None:
    """The code is on the table, which cannot be reached; memory holds no copy to spend."""
    code, _ = table_store.create_code(KEY_REF)
    table.fail_with = RuntimeError("synthetic outage")
    assert table_store.consume_code(code) is None


def test_a_write_the_table_refuses_is_kept_in_memory(table_store, table) -> None:
    table.fail_with = RuntimeError("synthetic outage")
    code, _ = table_store.create_code(KEY_REF)
    assert table_store.consume_code(code)["key_ref"] == KEY_REF
    assert table_store.consume_code(code) is None


def test_a_positive_lookup_is_cached_for_at_most_a_minute(table_store, table, clock) -> None:
    token, _ = table_store.create_session(KEY_REF)
    table.calls.clear()
    assert table_store.session(token) is not None
    assert table_store.session(token) is not None
    assert [name for name, _ in table.calls] == ["get_item"], "The second lookup came from the cache"

    # Revoked in another container: this one still trusts its cache, for a bounded time.
    table.items.clear()
    clock.now += SESSION_CACHE_SECONDS - 1
    assert table_store.session(token) is not None
    clock.now += 1
    assert table_store.session(token) is None


def test_a_negative_lookup_is_not_cached(table_store, table) -> None:
    token, _ = table_store.create_session(KEY_REF)
    held = table.items.pop((f"{AUTH_PREFIX}{token_hash(token)}", "SESSION"))
    assert table_store.session(token) is None
    table.items[(held["PK"], held["SK"])] = held
    assert table_store.session(token) is not None


def test_a_revoke_deletes_from_the_table_and_the_cache(table_store, table) -> None:
    token, _ = table_store.create_session(KEY_REF)
    assert table_store.session(token) is not None
    assert table_store.revoke(token) is True
    assert (f"{AUTH_PREFIX}{token_hash(token)}", "SESSION") not in table.items
    assert table_store.session(token) is None


def test_a_revoke_the_table_refuses_is_reported_and_still_refused_here(table_store, table) -> None:
    token, _ = table_store.create_session(KEY_REF)
    assert table_store.session(token) is not None
    table.fail_with = RuntimeError("synthetic outage")
    assert table_store.revoke(token) is False, "A sign-out that did not reach the table is not claimed"
    table.fail_with = None
    # The cache was dropped, so this container reads the table again rather than trusting it.
    table.calls.clear()
    table_store.session(token)
    assert [name for name, _ in table.calls] == ["get_item"]


def test_the_default_store_is_built_on_first_use(monkeypatch) -> None:
    monkeypatch.setenv("THREEFOLD_OFFLINE", "1")
    auth_store.reset_default_store()
    try:
        first = auth_store.default_store()
        assert first is auth_store.default_store()
        assert not first.is_live
    finally:
        auth_store.reset_default_store()
