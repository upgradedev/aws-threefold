"""Domain Value Objects for Threefold.

Enforces DDD & Clean Architecture by encapsulating currencies, token counts,
and session identifiers in immutable, self-validating value objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from threefold.domain.exceptions import InvalidValueObjectException


@dataclass(frozen=True)
class USD:
    """Immutable representation of US Dollar monetary expenditure."""

    amount: float

    def __post_init__(self) -> None:
        if not isinstance(self.amount, (int, float)):
            raise InvalidValueObjectException("USD", self.amount, "Must be numeric")
        if self.amount < 0.0:
            raise InvalidValueObjectException("USD", self.amount, "Monetary amount cannot be negative")

    @property
    def formatted(self) -> str:
        return f"${self.amount:.4f}"

    def __float__(self) -> float:
        return float(self.amount)

    def __add__(self, other: USD | float) -> USD:
        other_val = float(other)
        return USD(round(self.amount + other_val, 4))

    def __sub__(self, other: USD | float) -> USD:
        other_val = float(other)
        return USD(max(0.0, round(self.amount - other_val, 4)))

    def __str__(self) -> str:
        return self.formatted


@dataclass(frozen=True)
class TokenCount:
    """Immutable representation of LLM input/output token counts."""

    tokens: int

    def __post_init__(self) -> None:
        if not isinstance(self.tokens, int):
            raise InvalidValueObjectException("TokenCount", self.tokens, "Must be an integer")
        if self.tokens < 0:
            raise InvalidValueObjectException("TokenCount", self.tokens, "Tokens cannot be negative")

    def __int__(self) -> int:
        return int(self.tokens)

    def __add__(self, other: TokenCount | int) -> TokenCount:
        other_val = int(other)
        return TokenCount(self.tokens + other_val)

    def __str__(self) -> str:
        return f"{self.tokens:,} tokens"


@dataclass(frozen=True)
class SessionId:
    """Immutable validated identifier for an autonomous agent session."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise InvalidValueObjectException("SessionId", self.value, "Must be a string")
        stripped = self.value.strip()
        if not stripped:
            raise InvalidValueObjectException("SessionId", self.value, "SessionId cannot be empty")
        if len(stripped) > 128:
            raise InvalidValueObjectException("SessionId", self.value, "SessionId exceeds 128 characters")

    def __str__(self) -> str:
        return self.value
