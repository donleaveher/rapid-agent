"""Phoenix tracing setup for SQLoop.

Differs from the official scaffold on purpose: SQLoop runs short-lived /
batched eval scripts, so we use a BatchSpanProcessor (batch=True) and an
explicit force_flush() before exit. Otherwise spans get dropped and genai's
auto-instrumented client raises "client has been closed" (see CLAUDE.md trap #3).

Required env (loaded from .env by the caller):
  PHOENIX_COLLECTOR_ENDPOINT  -- must include the /s/<space-id> path (trap #1)
  PHOENIX_CLIENT_HEADERS      -- "api_key=<key>" (trap #2)
Optional:
  PHOENIX_PROJECT_NAME        -- defaults to "rapid-agent"

Trap #2b (found Day 1): register() does NOT auto-apply PHOENIX_CLIENT_HEADERS to
the OTLP span exporter -> exports 401. We parse the key out and pass api_key=
explicitly. (The kill-switch span in Day 0 used a different code path.)
"""

from __future__ import annotations

import atexit
import os
from typing import Any, Optional

from phoenix.otel import register

_provider: Optional[Any] = None


def _api_key_from_env() -> Optional[str]:
    """Extract the Phoenix api_key from PHOENIX_CLIENT_HEADERS ("api_key=<key>")."""
    headers = os.environ.get("PHOENIX_CLIENT_HEADERS", "")
    if "api_key=" in headers:
        return headers.split("api_key=", 1)[1].strip()
    return None


def setup_tracing() -> Optional[Any]:
    """Register Phoenix tracing once (idempotent). Returns the provider or None."""
    global _provider
    if _provider is not None:
        return _provider
    if not (os.environ.get("PHOENIX_COLLECTOR_ENDPOINT") or "").strip():
        # No endpoint configured -> run without tracing rather than crashing.
        return None
    _provider = register(
        project_name=os.environ.get("PHOENIX_PROJECT_NAME", "rapid-agent"),
        batch=True,                  # BatchSpanProcessor (trap #3)
        protocol="http/protobuf",    # Phoenix Cloud; avoids "could not infer protocol"
        api_key=_api_key_from_env(), # must pass explicitly or exporter 401s (trap #2b)
        auto_instrument=True,        # picks up installed openinference adk + genai instrumentors
        verbose=False,
    )
    atexit.register(flush_tracing)
    return _provider


def flush_tracing() -> None:
    """Flush pending spans. Call before process exit; also runs via atexit."""
    if _provider is not None:
        try:
            _provider.force_flush()
        except Exception:
            pass
