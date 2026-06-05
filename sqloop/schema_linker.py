"""Schema Linker: load the question-relevant DB schema into session state.

Retrieves the tables relevant to the question (+ their foreign-key neighbours)
rather than dumping the whole schema -- see sqloop/schema_link.py. Falls back to
the full schema for small databases. Implemented as a custom (non-LLM) ADK agent
so it shows up as its own span in Phoenix: agent_run [schema_linker].
"""

from __future__ import annotations

from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from sqloop.db import default_db_path
from sqloop.schema_link import linked_schema


class SchemaLinker(BaseAgent):
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        db_path = state.get("db_path") or str(default_db_path())
        question = state.get("question", "")
        schema = linked_schema(db_path, question)
        yield Event(
            author=self.name,
            actions=EventActions(state_delta={"schema": schema, "db_path": db_path}),
        )
