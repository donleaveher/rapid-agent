"""Shared backoff helpers for free-tier rate limits (used by the runners)."""

from __future__ import annotations

import re

# Transient upstream errors worth retrying (503 high demand, 429 rate limit).
RETRYABLE = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED")


def is_retryable(msg: str) -> bool:
    return any(tok in msg for tok in RETRYABLE)


def backoff_seconds(msg: str, attempt: int) -> float:
    """Use the server-suggested 'retry in Xs' when present, else escalate."""
    m = re.search(r"retry in ([0-9.]+)s", msg) or re.search(
        r"retryDelay['\":\s]+([0-9.]+)", msg
    )
    if m:
        return float(m.group(1)) + 2  # small buffer past the server hint
    return 15 * (attempt + 1)
