"""Sign-in codes and sessions, so an operator never pastes the key into a page.

The operator key stays on the command line. With it, the CLI mints a
single-use sign-in code; the browser trades that code for a session token and
from then on presents the token instead of the key. Both live on the service's
one table beside everything else:

    PK = AUTH#<sha256 of the code or token>, SK = CODE | SESSION, ttl

Only the hash is ever written. A reader of the table, a backup or a log line
learns nothing that signs anyone in, and a code or token is checked by hashing
what was presented and reading that one item.

Expiry is decided here on every read, never by DynamoDB: its TTL deletes items
lazily, hours or days after they lapse, so an item that is still present is not
an item that is still valid.

When the table cannot be reached, as in the test suite (THREEFOLD_OFFLINE) or a
local run with no table, the store falls back to memory exactly as the session
repository does. A code or session made in memory is valid only in the process
that made it, which is the honest behaviour for a store nobody else can read.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

AUTH_PREFIX = "AUTH#"
CODE_SORT_KEY = "CODE"
SESSION_SORT_KEY = "SESSION"

# Long enough to copy a link from a terminal into a browser, short enough that a
# link left in scrollback or shell history is dead by the time anyone reads it.
CODE_TTL_SECONDS = 120
# One working day. A session is the operator, so it is not open-ended.
SESSION_TTL_SECONDS = 43200
# How long a container may trust a session it has already read. A revoke made in
# this container takes effect at once; one made in another container reaches
# this one within this bound.
SESSION_CACHE_SECONDS = 60
# Bounds the cache in a container that sees many sessions. Clearing it costs one
# read per live session, which is cheaper than an unbounded dict.
SESSION_CACHE_LIMIT = 1024

# Every session token carries this prefix, so the middleware looks up only
# values shaped like one. Anything else presented as a bearer is compared with
# the configured keys and never costs a read of the table. It also makes a token
# recognisable to a secret scanner if one is ever pasted where it should not be.
SESSION_TOKEN_PREFIX = "tfs_"
# The longest code or token worth hashing. Anything longer is not one of ours.
MAX_PRESENTED_LENGTH = 256


def _now() -> float:
    """The clock the store reads, kept separate so a test can move it."""
    return time.time()


def token_hash(value: str) -> str:
    """The only form of a code or token that is ever stored.

    A plain SHA-256 is enough: both are 192 bits or more from `secrets`, so
    there is no dictionary to run against the hash.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    code = response.get("Error", {}).get("Code", "") if isinstance(response, dict) else ""
    return type(exc).__name__ == "ConditionalCheckFailedException" or code == "ConditionalCheckFailedException"


def _plain(item: Dict[str, Any]) -> Dict[str, Any]:
    """An item as the rest of the service reads it: ints, not Decimals."""
    record = {k: v for k, v in item.items() if k not in ("PK", "SK")}
    record["ttl"] = int(item.get("ttl", 0) or 0)
    return record


class AuthStore:
    """Sign-in codes and sessions on the service's table, or in memory."""

    def __init__(
        self,
        table_name: Optional[str] = None,
        region_name: Optional[str] = None,
        boto3_resource: Optional[Any] = None,
    ) -> None:
        self.table_name = table_name or os.getenv("TABLE_NAME", "Threefold-Governance-prod")
        self.region_name = region_name or os.getenv("AWS_REGION", "eu-west-1")
        self._table = None
        self._memory: Dict[str, Dict[str, Any]] = {}
        self._cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
        # One lock for memory and cache, so taking a code out of memory is as
        # atomic in one process as the conditional delete is across all of them.
        self._lock = threading.Lock()
        offline = os.getenv("THREEFOLD_OFFLINE", "").lower() in ("1", "true", "yes")
        if boto3_resource is not None:
            self._table = boto3_resource.Table(self.table_name)
        elif not offline:
            try:
                import boto3

                self._table = boto3.resource("dynamodb", region_name=self.region_name).Table(self.table_name)
            except Exception as exc:
                logger.info("Sign-in store operating in memory: %s", exc)
                self._table = None

    @property
    def is_live(self) -> bool:
        return self._table is not None

    # ------------------------------------------------------------------ codes

    def create_code(self, key_ref: str) -> Tuple[str, int]:
        """A new single-use code, bound to the key that asked for it.

        Returns the code and the epoch second it lapses at. The code itself is
        returned once, to the caller, and never stored.
        """
        code = secrets.token_urlsafe(24)
        expires = int(_now()) + CODE_TTL_SECONDS
        self._put(token_hash(code), CODE_SORT_KEY, {"key_ref": key_ref}, expires)
        return code, expires

    def consume_code(self, code: str) -> Optional[Dict[str, Any]]:
        """Takes a code out of the store if it is live, and says whom it was for.

        Single use is enforced by the store, not by the caller: on the table a
        conditional delete removes the item only if it exists and has not
        lapsed, and returns what it removed, so two exchanges of one code
        cannot both succeed however they interleave. None means unknown, used
        or expired, which the caller answers alike.
        """
        if not self._presentable(code):
            return None
        pk = AUTH_PREFIX + token_hash(code)
        now = int(_now())
        if self._table is not None:
            try:
                response = self._table.delete_item(
                    Key={"PK": pk, "SK": CODE_SORT_KEY},
                    ConditionExpression="attribute_exists(PK) AND #ttl > :now",
                    # TTL is a reserved word in DynamoDB expressions.
                    ExpressionAttributeNames={"#ttl": "ttl"},
                    ExpressionAttributeValues={":now": now},
                    ReturnValues="ALL_OLD",
                )
                attributes = response.get("Attributes")
                if attributes:
                    return _plain(attributes)
            except Exception as exc:
                if _is_conditional_failure(exc):
                    return None
                logger.warning("Sign-in code exchange could not reach DynamoDB, trying memory: %s", exc)
        with self._lock:
            item = self._memory.pop(f"{pk}#{CODE_SORT_KEY}", None)
        if item is None or int(item["ttl"]) <= now:
            return None
        return _plain(item)

    # --------------------------------------------------------------- sessions

    def create_session(self, key_ref: str) -> Tuple[str, int]:
        """A new session token, bound to the key whose code was exchanged for it."""
        token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
        expires = int(_now()) + SESSION_TTL_SECONDS
        self._put(token_hash(token), SESSION_SORT_KEY, {"key_ref": key_ref}, expires)
        return token, expires

    def session(self, token: str) -> Optional[Dict[str, Any]]:
        """The live session this token names, or None.

        A positive answer is kept for up to SESSION_CACHE_SECONDS, and never
        past the session's own expiry, so a busy page does not read the table
        on every request. A negative answer is never kept: a token that has
        just been issued elsewhere must work on its first use here.
        """
        if not self._presentable(token) or not token.startswith(SESSION_TOKEN_PREFIX):
            return None
        digest = token_hash(token)
        now = _now()
        with self._lock:
            cached = self._cache.get(digest)
            if cached is not None:
                record, until = cached
                if now < until:
                    return dict(record)
                del self._cache[digest]

        item = None
        pk = AUTH_PREFIX + digest
        if self._table is not None:
            try:
                item = self._table.get_item(Key={"PK": pk, "SK": SESSION_SORT_KEY}).get("Item")
            except Exception as exc:
                logger.warning("Session lookup could not reach DynamoDB, trying memory: %s", exc)
        if item is None:
            with self._lock:
                item = self._memory.get(f"{pk}#{SESSION_SORT_KEY}")
        if not item:
            return None
        record = _plain(item)
        if record["ttl"] <= now:
            return None
        with self._lock:
            if len(self._cache) >= SESSION_CACHE_LIMIT:
                self._cache.clear()
            self._cache[digest] = (dict(record), min(now + SESSION_CACHE_SECONDS, record["ttl"]))
        return record

    def revoke(self, token: str) -> bool:
        """Ends a session. True once no store this one writes to still holds it.

        The local cache is dropped first, so this container refuses the token
        from this moment even if the table cannot be reached. False means the
        table refused the delete, and the session may still be honoured by other
        containers until it lapses; the caller says so rather than claiming a
        sign-out that did not happen.
        """
        if not self._presentable(token):
            return True
        digest = token_hash(token)
        pk = AUTH_PREFIX + digest
        with self._lock:
            self._cache.pop(digest, None)
            self._memory.pop(f"{pk}#{SESSION_SORT_KEY}", None)
        if self._table is not None:
            try:
                self._table.delete_item(Key={"PK": pk, "SK": SESSION_SORT_KEY})
            except Exception as exc:
                logger.warning("Session revoke could not reach DynamoDB: %s", exc)
                return False
        return True

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _presentable(value: Any) -> bool:
        return isinstance(value, str) and 0 < len(value) <= MAX_PRESENTED_LENGTH

    def _put(self, digest: str, sort_key: str, fields: Dict[str, Any], expires: int) -> None:
        """Writes one hashed item, to the table when it answers and to memory when not.

        Memory is written only when the table is not: a copy in both would let
        a code consumed on the table be consumed again from memory during a
        later outage.
        """
        item = {
            "PK": AUTH_PREFIX + digest,
            "SK": sort_key,
            "ttl": expires,
            "created_at": int(_now()),
            **fields,
        }
        if self._table is not None:
            try:
                self._table.put_item(Item=item)
                return
            except Exception as exc:
                logger.warning("Sign-in store could not write to DynamoDB, keeping it in memory: %s", exc)
        with self._lock:
            self._memory[f"{item['PK']}#{sort_key}"] = item


_default_store: Optional[AuthStore] = None
_default_lock = threading.Lock()


def default_store() -> AuthStore:
    """The store this container uses, built on first use rather than at import.

    Built lazily so importing the middleware does not reach for AWS, and so a
    test that sets THREEFOLD_OFFLINE before its first request gets memory.
    """
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = AuthStore()
        return _default_store


def reset_default_store(store: Optional[AuthStore] = None) -> None:
    """Replaces the container's store; for tests, which need a clean one."""
    global _default_store
    with _default_lock:
        _default_store = store
