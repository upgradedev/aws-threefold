"""Reading the decision ledger a page at a time, and labelling what it holds.

The ledger is partitioned by day, newest first within a day, so a window is
read day by day from today backwards. A filtered listing may have to read many
rows to return a few, so each request reads a bounded number of pages and the
cursor says exactly where it stopped: the next request resumes after the last
row returned, never after the last row read, and a page is never skipped.

Every row that leaves here has been through `public_row`, as /api/insights
rows are, and carries its rule key whether or not it was written with one.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from threefold.application.dtos import InvalidRequestError
from threefold.application.insights import CATEGORY_LABELS
from threefold.application.labels import public_row
from threefold.application.rule_keys import NONE, category_for, kind_of, stored_rule_key

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_DAYS = 30
# How many ledger pages one request may read looking for rows that pass the
# filters. Bounded so a filter that matches nothing costs a known amount; the
# cursor carries the rest.
PAGE_SIZE = 200
MAX_PAGES_PER_REQUEST = 25

KINDS = ("all", "refused", "observed", "approved")
REVIEW_FILTERS = ("any", "unreviewed", "correct", "false_alarm")
LABELS = ("correct", "false_alarm", "clear")
MAX_REVIEW_ITEMS = 100
MAX_NOTE_LENGTH = 200

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CURSOR_VERSION = 1

Reader = Callable[[str, Optional[str], int], Tuple[List[Dict[str, Any]], Optional[str]]]


def bounded_int(query: Mapping[str, Any], name: str, default: int, low: int, high: int) -> int:
    """A numeric query value, defaulted when unreadable and clamped to its range.

    Lenient, as /api/insights reads `days`: a page that sends a bad number gets
    the default rather than an error it cannot show.
    """
    try:
        value = int((query or {}).get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


def _text(query: Mapping[str, Any], name: str, limit: int) -> Optional[str]:
    value = (query or {}).get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise InvalidRequestError(f"{name} must be text of at most {limit} characters.", name)
    return value


def _choice(query: Mapping[str, Any], name: str, allowed: tuple, default: str) -> str:
    value = (query or {}).get(name) or default
    if value not in allowed:
        raise InvalidRequestError(f"{name} must be one of {', '.join(allowed)}.", name)
    return value


@dataclass(frozen=True)
class DecisionFilters:
    project: Optional[str] = None
    rule: Optional[str] = None
    kind: str = "all"
    review: str = "any"
    agent: Optional[str] = None
    session: Optional[str] = None

    @classmethod
    def from_query(cls, query: Mapping[str, Any]) -> "DecisionFilters":
        return cls(
            project=_text(query, "project", 120),
            rule=_text(query, "rule", 80),
            kind=_choice(query, "kind", KINDS, "all"),
            review=_choice(query, "review", REVIEW_FILTERS, "any"),
            agent=_text(query, "agent", 40),
            session=_text(query, "session", 160),
        )

    def admits(self, row: Mapping[str, Any]) -> bool:
        """Whether a row, already reduced for a public page, passes every filter."""
        if self.project is not None and row.get("project_name") != self.project:
            return False
        if self.rule is not None and row.get("rule_key") != self.rule:
            return False
        if self.kind != "all" and kind_of(row) != self.kind:
            return False
        if self.review == "unreviewed" and row.get("review"):
            return False
        if self.review in ("correct", "false_alarm") and row.get("review") != self.review:
            return False
        if self.agent is not None and row.get("agent") != self.agent:
            return False
        if self.session is not None and row.get("session_id") != self.session:
            return False
        return True


def shown_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """A ledger row as the application's pages show it."""
    shown = public_row({key: value for key, value in row.items() if not key.startswith("_")})
    key = stored_rule_key(shown)
    category = category_for(key)
    shown.update(
        rule_key=key,
        stage=shown.get("stage") or ("observe" if shown.get("dry_run") else "enforce"),
        hook_mode=shown.get("hook_mode") or "unknown",
        review=shown.get("review") or None,
        reviewed_at=shown.get("reviewed_at") or None,
        review_note=shown.get("review_note") or None,
        category=category,
        category_label=CATEGORY_LABELS.get(category, "Other"),
    )
    return shown


# ---------------------------------------------------------------- the cursor


def encode_cursor(day: str, after: Optional[str], oldest: str) -> str:
    raw = json.dumps({"v": CURSOR_VERSION, "d": day, "a": after, "o": oldest}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> Tuple[str, Optional[str], str]:
    """(day to read, sort key to resume after, oldest day of the window).

    Opaque to the caller but not trusted from it: anything that does not
    decode to the shape this module writes is a 400, not a guess.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error):
        raise InvalidRequestError("cursor is not one this service issued.", "cursor") from None
    if not isinstance(data, dict) or data.get("v") != CURSOR_VERSION:
        raise InvalidRequestError("cursor is not one this service issued.", "cursor")
    day, after, oldest = data.get("d"), data.get("a"), data.get("o")
    if not (isinstance(day, str) and _DAY.match(day) and isinstance(oldest, str) and _DAY.match(oldest)):
        raise InvalidRequestError("cursor is not one this service issued.", "cursor")
    if after is not None and (not isinstance(after, str) or len(after) > 400):
        raise InvalidRequestError("cursor is not one this service issued.", "cursor")
    if oldest > day:
        raise InvalidRequestError("cursor is not one this service issued.", "cursor")
    return day, after, oldest


def _previous_day(day: str) -> str:
    return str(datetime.date.fromisoformat(day) - datetime.timedelta(days=1))


def page_decisions(
    reader: Reader,
    days: int,
    filters: DecisionFilters,
    limit: int,
    cursor: Optional[str] = None,
    today: Optional[datetime.date] = None,
) -> Dict[str, Any]:
    """GET /api/decisions: {items, next_cursor}, newest first across the window."""
    if cursor:
        day, after, oldest = decode_cursor(cursor)
    else:
        today = today or datetime.datetime.now(datetime.timezone.utc).date()
        day, after, oldest = str(today), None, str(today - datetime.timedelta(days=max(1, days) - 1))
    items: List[Dict[str, Any]] = []
    for _ in range(MAX_PAGES_PER_REQUEST):
        rows, next_after = reader(day, after, PAGE_SIZE)
        for row in rows:
            after = row.get("_sk") or after
            shown = shown_row(row)
            if filters.admits(shown):
                items.append(shown)
                if len(items) >= limit:
                    # Resume after this row. If it was the page's last and the
                    # day has nothing more, the next request starts a day back.
                    if row is rows[-1] and next_after is None:
                        return _finish(items, day, None, oldest)
                    return {"items": items, "next_cursor": encode_cursor(day, after, oldest)}
        if next_after is not None:
            after = next_after
            continue
        if day <= oldest:
            return {"items": items, "next_cursor": None}
        day, after = _previous_day(day), None
    # The page budget ran out before the window did: say where to go on.
    return {"items": items, "next_cursor": encode_cursor(day, after, oldest)}


def _finish(items: List[Dict[str, Any]], day: str, after: Optional[str], oldest: str) -> Dict[str, Any]:
    if day <= oldest:
        return {"items": items, "next_cursor": None}
    return {"items": items, "next_cursor": encode_cursor(_previous_day(day), after, oldest)}


# ---------------------------------------------------------------- reviews


def credential_hash(headers: Mapping[str, Any]) -> str:
    """Who labelled a call: 8 hex of a hash of the credential presented, never the credential.

    Read from the same two headers the middleware reads a key from. A sign-in
    session token arrives as a bearer token and is hashed the same way. No
    credential at all is "anonymous", which is what a sandbox on the public
    stack is labelled by.
    """
    normalized = {str(key).lower(): value for key, value in (headers or {}).items()}
    presented = normalized.get("x-api-key")
    if not presented:
        auth = str(normalized.get("authorization") or "")
        if auth.startswith("Bearer "):
            presented = auth[7:].strip()
    if not presented:
        return "anonymous"
    return hashlib.sha256(str(presented).encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class ReviewItem:
    timestamp: str
    verdict_id: str
    label: str
    note: str


def read_review_items(body: Mapping[str, Any]) -> Tuple[List[ReviewItem], List[Dict[str, str]]]:
    """The items of a review request that can be applied, and the ones skipped and why.

    The request as a whole must be a list of at most MAX_REVIEW_ITEMS objects;
    one bad item is skipped with its reason rather than failing the rest, so a
    bulk label on the review queue lands everything it can.
    """
    items = body.get("items") if isinstance(body, Mapping) else None
    if not isinstance(items, list) or not items:
        raise InvalidRequestError("items must be a non-empty list of reviews.", "items")
    if len(items) > MAX_REVIEW_ITEMS:
        raise InvalidRequestError(f"items holds {len(items)} reviews; the limit is {MAX_REVIEW_ITEMS}.", "items")
    usable: List[ReviewItem] = []
    skipped: List[Dict[str, str]] = []
    for entry in items:
        verdict_id = entry.get("verdict_id") if isinstance(entry, dict) else None
        name = verdict_id if isinstance(verdict_id, str) else ""
        if not isinstance(entry, dict):
            skipped.append({"verdict_id": name, "reason": "not-an-object"})
            continue
        timestamp = entry.get("timestamp")
        if not isinstance(timestamp, str) or not _DAY.match(timestamp[:10]) or len(timestamp) > 64:
            skipped.append({"verdict_id": name, "reason": "timestamp-missing"})
            continue
        if not isinstance(verdict_id, str) or not verdict_id or len(verdict_id) > 120:
            skipped.append({"verdict_id": name, "reason": "verdict-id-missing"})
            continue
        label = entry.get("label")
        if label not in LABELS:
            skipped.append({"verdict_id": name, "reason": "label-not-correct-false-alarm-or-clear"})
            continue
        note = entry.get("note") or ""
        if not isinstance(note, str) or len(note) > MAX_NOTE_LENGTH:
            # Refused rather than cut: a note is a record, and one shortened on
            # the way in says something its author did not.
            skipped.append({"verdict_id": name, "reason": f"note-longer-than-{MAX_NOTE_LENGTH}"})
            continue
        usable.append(ReviewItem(timestamp=timestamp, verdict_id=verdict_id, label=label, note=note.strip()))
    return usable, skipped


def is_flagged(row: Mapping[str, Any]) -> bool:
    """Only a call something refused, or would have, has anything to review."""
    return stored_rule_key(row) != NONE
