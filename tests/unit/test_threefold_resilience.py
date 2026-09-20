"""Unit tests for exponential backoff & full jitter resilience decorator in Threefold."""
import pytest
from threefold.infrastructure.resilience import retry_with_exponential_backoff


def test_retry_eventually_succeeds():
    attempts = 0

    @retry_with_exponential_backoff(max_retries=3, base_delay_seconds=0.01, max_delay_seconds=0.05)
    def flaky_llm_call():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("Bedrock ThrottlingException")
        return {"status": "SUCCESS"}

    result = flaky_llm_call()
    assert result == {"status": "SUCCESS"}
    assert attempts == 3


def test_retry_raises_when_retries_exhausted():
    attempts = 0

    @retry_with_exponential_backoff(max_retries=2, base_delay_seconds=0.01, max_delay_seconds=0.05)
    def dead_endpoint_call():
        nonlocal attempts
        attempts += 1
        raise TimeoutError("DynamoDB ServiceUnavailable")

    with pytest.raises(TimeoutError):
        dead_endpoint_call()

    assert attempts == 3  # 1 initial call + 2 retries
