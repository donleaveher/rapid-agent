"""Repair step: a dedicated ReAct fixer that runs only when SQL execution failed.

The SQL Generator makes a single attempt. If execute_sql returned "SQL_ERROR:",
this step's child LlmAgent reads the question, schema, failed SQL and error from
session state, diagnoses the cause, and rewrites + re-executes the query. When the
first attempt already succeeded, the step is a no-op (no extra LLM call), so it
stays cheap and shows up in the Phoenix trace only when it actually does work.
"""

from __future__ import annotations

from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event


class RepairAgent(BaseAgent):
    """Conditionally delegate to the child repair LlmAgent (sub_agents[0])."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        last = ctx.session.state.get("last_result", "") or ""
        if not last.startswith("SQL_ERROR"):
            return  # first attempt worked (or empty result) -> nothing to repair
        async for event in self.sub_agents[0].run_async(ctx):
            yield event
