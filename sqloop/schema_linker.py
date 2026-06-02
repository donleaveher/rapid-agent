"""Schema Linker: load the target DB's schema into session state.

Day 2 minimal version injects the FULL schema of the database named by
state["db_path"] (Spider databases are small enough to fit). Keyword-based
table/column selection is a deliberate later improvement -- a natural knob for
the self-improvement loop.

Implemented as a custom (non-LLM) ADK agent so it shows up as its own span in
the Phoenix trace: agent_run [schema_linker].
"""

from __future__ import annotations

from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from sqloop.db import default_db_path, get_schema


class SchemaLinker(BaseAgent):
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        db_path = ctx.session.state.get("db_path") or str(default_db_path())
        schema = get_schema(db_path)
        yield Event(
            author=self.name,
            actions=EventActions(state_delta={"schema": schema, "db_path": db_path}),
        )
