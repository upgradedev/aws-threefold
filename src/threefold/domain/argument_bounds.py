"""Bounds on a history entry's arguments, shared by storage and the loop gate.

Three large writes used to push a session row past DynamoDB's 400 KB item
ceiling, and the failed put fell back to the container's memory, so a halt
stopped being durable. Stored arguments are bounded, and the loop gate
compares calls through the same bound: the row holds the bounded form while
the call being judged still carries the whole, so comparing them raw would
never see a repeat of a large call. Bounded on both sides, the same large
call reads back identical and two different ones still read back different.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# A history entry's arguments, as stored. String leaves longer than the leaf
# cap keep their head and the sha256 of the whole, so two different arguments
# still read back different; an entry still over the entry cap keeps only its
# fingerprint. Fifty entries at the cap stay a comfortable half of the
# ceiling, verdicts and metadata included.
HISTORY_STRING_LEAF_CAP = 2000
HISTORY_ENTRY_BYTES_CAP = 4096

_TRUNCATED_MARKER = re.compile(r"\.\.\.\[truncated sha256:[0-9a-f]{12}\]$")


def _truncate_leaves(value: Any) -> Any:
    """Shortens every long string in a structure, keeping each one's identity.

    A leaf past the cap keeps its head and the sha256 of the whole, so two
    different arguments still read back different and the loop detector's
    signatures keep their meaning across containers. A leaf already carrying
    the marker passes through: a save reloads what the last save stored, and
    re-marking it would churn the marker on every save. A caller faking the
    marker to smuggle a long leaf past this is still caught by the entry cap
    in bounded_arguments.
    """
    if isinstance(value, str):
        if len(value) <= HISTORY_STRING_LEAF_CAP or _TRUNCATED_MARKER.search(value):
            return value
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"{value[:HISTORY_STRING_LEAF_CAP]}...[truncated sha256:{digest}]"
    if isinstance(value, dict):
        return {key: _truncate_leaves(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_truncate_leaves(item) for item in value]
    return value


def bounded_arguments(arguments: Any) -> Any:
    """Arguments as stored and as the loop gate compares them.

    Leaves shortened, and a fingerprint past the cap: many small leaves can
    still add up past what one entry may hold, so an entry serialized past
    the cap keeps only the sha256 and size of the whole. Deterministic, so
    the same arguments bound twice compare equal, and idempotent on what the
    store already holds, so a reload bounds to itself.
    """
    trimmed = _truncate_leaves(arguments)
    try:
        serialized = json.dumps(trimmed, sort_keys=True, default=str)
    except (TypeError, ValueError):
        serialized = ""
    if len(serialized.encode("utf-8")) <= HISTORY_ENTRY_BYTES_CAP:
        return trimmed
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return {
        "_arguments_truncated": True,
        "_arguments_sha256": digest,
        "_arguments_bytes": len(serialized.encode("utf-8")),
    }
