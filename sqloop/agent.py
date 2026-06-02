"""SQLoop task-plane agents: Router -> Schema Linker -> SQL Generator -> Executor.

Wired as an ADK SequentialAgent so Phoenix shows a clean span tree:
  sqloop_pipeline
    router          (LLM -> writes `intent` to state)
    schema_linker   (custom agent -> writes `schema` to state for the target db)
    sql_generator   (LLM, instruction reads {intent}/{schema} from state)
      execute_sql   (tool span = the Executor; runs against state["db_path"])

The SQL Generator's instruction comes from a GeneratorConfig (see sqloop/config.py),
so the same pipeline can be rebuilt with a baseline or candidate config for A/B
testing in the self-improvement loop.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.tools import FunctionTool

from sqloop.config import active_config
from sqloop.db import execute_sql
from sqloop.prompts import ROUTER_INSTRUCTION
from sqloop.schema_linker import SchemaLinker

# Dev default: gemini-flash-lite-latest -- lite models have higher free-tier
# daily quota, which matters for batch eval before the Vertex switch. Override
# with GEMINI_MODEL. Final submission runs Gemini on Vertex.
_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")


def build_pipeline(generator_instruction: str | None = None) -> SequentialAgent:
    """Build the task pipeline. If no instruction is given, use the active config."""
    if generator_instruction is None:
        generator_instruction = active_config().render_instruction()

    router = LlmAgent(
        model=_MODEL,
        name="router",
        instruction=ROUTER_INSTRUCTION,
        output_key="intent",  # stored in session state, read by sql_generator
    )
    schema_linker = SchemaLinker(name="schema_linker")
    sql_generator = LlmAgent(
        model=_MODEL,
        name="sql_generator",
        instruction=generator_instruction,  # {intent}/{schema} filled from state
        tools=[FunctionTool(func=execute_sql)],
    )
    return SequentialAgent(name="sqloop_pipeline", sub_agents=[router, schema_linker, sql_generator])


# Default pipeline used by main.py / run_eval.py / run_spider.py (active config).
root_agent = build_pipeline()
