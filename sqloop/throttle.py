"""Shared backoff helpers for free-tier rate limits (used by the runners)."""

from __future__ import annotations

import re

# Transient errors worth retrying: upstream load/limits (503/429) AND connection-level
# flakiness (proxy hiccups when calling Vertex/Phoenix through a local proxy).
RETRYABLE = (
    "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "502", "504", "500", "INTERNAL",
    "ConnectError", "ConnectTimeout", "ReadTimeout", "Read timed out",
    "Connection refused", "Connection reset", "RemoteProtocolError",
    "ServerDisconnected", "peer closed connection",
)


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
