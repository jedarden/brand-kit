"""Shared bounded retry policy for external HTTP calls.

Only idempotent reads may be retried.  A request that can mutate an external
service is deliberately single-attempt so an ambiguous timeout cannot replay
the mutation.
"""

from __future__ import annotations

import math
import time
import urllib.error
from typing import Any, Callable


API_TIMEOUT_SECONDS = 20.0
PUBLIC_TIMEOUT_SECONDS = 15.0
GARAGE_TIMEOUT_SECONDS = 30.0
READ_METHODS = frozenset({"GET", "HEAD"})
MAX_READ_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0
MAX_RETRY_DELAY_SECONDS = 5.0


def validate_timeout(value: Any) -> float:
    """Return a positive finite timeout, rejecting unbounded values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("request timeout must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("request timeout must be a positive finite number")
    return float(value)


def max_attempts(method: str) -> int:
    """Return the total attempts allowed for one HTTP operation."""
    return MAX_READ_ATTEMPTS if method.upper() in READ_METHODS else 1


def retryable_status(status: int) -> bool:
    """429 and server errors are transient candidates for read retries."""
    return status == 429 or 500 <= status <= 599


def retryable_error(method: str, error: BaseException) -> bool:
    """Whether ``error`` is retryable for this method."""
    if method.upper() not in READ_METHODS:
        return False
    if isinstance(error, urllib.error.HTTPError):
        return retryable_status(error.code)
    return isinstance(error, (urllib.error.URLError, OSError, TimeoutError))


def retry_delay(error: BaseException, retry_number: int) -> float:
    """Return a capped exponential delay, honoring a bounded Retry-After."""
    retry_after: Any = None
    if isinstance(error, urllib.error.HTTPError) and error.headers is not None:
        retry_after = error.headers.get("Retry-After")
    try:
        if retry_after is not None:
            delay = float(str(retry_after).strip())
            if math.isfinite(delay) and delay >= 0:
                return min(delay, MAX_RETRY_DELAY_SECONDS)
    except (TypeError, ValueError):
        pass
    return min(
        RETRY_BACKOFF_SECONDS * (2 ** max(0, retry_number - 1)),
        MAX_RETRY_DELAY_SECONDS,
    )


def sleep_before_retry(
    error: BaseException,
    retry_number: int,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Sleep for one bounded retry delay; injectable for deterministic tests."""
    (sleep or time.sleep)(retry_delay(error, retry_number))
