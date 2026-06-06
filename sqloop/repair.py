"""Repair step: a dedicated ReAct fixer, triggered by more than just hard errors.

The SQL Generator makes a single attempt; execute_sql writes last_sql/last_result
to state. The trigger is layered (env SQLOOP_REPAIR):

  basic   (default): only "SQL_ERROR:" results -> repair. Backward compatible; the
                     cheapest, but misses the most common Spider failures (WRONG_COLUMN
                     / AGGREGATION_ERROR / GROUPBY_ERROR all execute fine and return a
                     wrong value, no error).
  empty            : basic + empty / "(no rows)" results (deterministic, no LLM).
  verify           : empty + a lightweight LLM self-check ("does this result answer the
                     question?") that catches semantic errors which run without error.
                     Costs one extra LLM call per turn, so it is opt-in (use it at serve
                     /demo time, not in quota-bound batch eval).

When triggered, the reason is written to state["repair_reason"] and the child repair
LlmAgent (sub_agents[0]) reads {question}/{schema}/{last_sql}/{last_result}/{repair_reason}
to diagnose and rewrite. When nothing is flagged the step is a no-op (no extra call, no
span noise), exactly as before.
"""

from __future__ import annotations

import os
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from sqloop.prompts import VERIFY_INSTRUCTION


def deterministic_reason(last_result: str, mode: str) -> str | None:
    """Non-LLM repair triggers. Returns a reason string, or None if not flagged."""
    last = (last_result or "").strip()
    if last.startswith("SQL_ERROR"):
        return f"The SQL failed to execute. Error: {last}"
    if mode in ("empty", "verify"):
        if not last:
            return "No SQL result was produced for the question."
        if last == "(no rows)":
            return "The query returned no rows -- likely a wrong filter value or join."
    return None


async def _verify_answer(question: str, sql: str, result: str) -> str | None:
    """LLM self-check: return a one-line reason if the result likely doesn't answer
    the question, else None. Uses the active backend (Gemini or DeepSeek)."""
    from sqloop.propose import _proposer_complete  # reuse the single-completion helper

    prompt = VERIFY_INSTRUCTION.format(question=question, sql=sql or "(none)", result=(result or "")[:1500])
    try:
        out = (await _proposer_complete(prompt)).strip()
    except Exception:  # noqa: BLE001 - verification is best-effort
        return None
    if out.upper().startswith("FIX"):
        return out.split(":", 1)[1].strip() if ":" in out else "result may not answer the question"
    return None


class RepairAgent(BaseAgent):
    """Conditionally delegate to the child repair LlmAgent (sub_agents[0])."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        last = state.get("last_result", "") or ""
        mode = os.environ.get("SQLOOP_REPAIR", "basic").lower()

        reason = deterministic_reason(last, mode)
        if reason is None and mode == "verify" and last.strip() and not last.startswith("SQL_ERROR"):
            reason = await _verify_answer(state.get("question", ""), state.get("last_sql", ""), last)
        if reason is None:
            return  # first attempt is fine -> nothing to repair

        # Hand the reason to the child agent's instruction ({repair_reason}).
        # Set directly (read immediately by the sub-agent) AND record it as a state
        # change for the trace.
        state["repair_reason"] = reason
        yield Event(author=self.name, actions=EventActions(state_delta={"repair_reason": reason}))
        async for event in self.sub_agents[0].run_async(ctx):
            yield event
