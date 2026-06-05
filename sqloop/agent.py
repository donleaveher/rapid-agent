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
from google.genai import types

from sqloop.config import active_config
from sqloop.db import execute_sql
from sqloop.prompts import REPAIR_INSTRUCTION, ROUTER_INSTRUCTION
from sqloop.repair import RepairAgent
from sqloop.schema_linker import SchemaLinker

# LLM backend switch. Default = Gemini (the submission backend, runs on Vertex).
# LLM_BACKEND=deepseek routes through LiteLlm to DeepSeek's OpenAI-compatible API
# -- used ONLY for dev validation of the loop mechanics (DeepSeek is reachable from
# CN without a proxy and has no free-tier rate limit). All submission numbers and
# the committed config are produced on Gemini; DeepSeek runs are disposable.
_BACKEND = os.environ.get("LLM_BACKEND", "gemini").lower()
_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

# Greedy decoding: text-to-SQL eval must be reproducible, otherwise execution
# accuracy swings run-to-run (seen 80% vs 96% on the same 25 held-out examples)
# and A/B comparisons just measure sampling noise.
_GEN_CONFIG = types.GenerateContentConfig(temperature=0.0)


def _make_model():
    """Model for an LlmAgent: a Gemini model-id string, or a LiteLlm DeepSeek model."""
    if _BACKEND == "deepseek":
        from google.adk.models.lite_llm import LiteLlm

        return LiteLlm(
            model=f"openai/{_DEEPSEEK_MODEL}",
            api_base="https://api.deepseek.com",
            api_key=os.environ["DEEPSEEK_API_KEY"],
            temperature=0.0,  # reproducible; LiteLlm passes this through
        )
    return _MODEL


def _gen_config():
    # Gemini takes temperature via generate_content_config; DeepSeek/LiteLlm bakes
    # it into the model kwargs above, so leave the ADK config unset there.
    return None if _BACKEND == "deepseek" else _GEN_CONFIG


def build_pipeline(generator_instruction: str | None = None) -> SequentialAgent:
    """Build the task pipeline. If no instruction is given, use the active config."""
    if generator_instruction is None:
        generator_instruction = active_config().render_instruction()

    router = LlmAgent(
        model=_make_model(),
        name="router",
        instruction=ROUTER_INSTRUCTION,
        output_key="intent",  # stored in session state, read by sql_generator
        generate_content_config=_gen_config(),
    )
    schema_linker = SchemaLinker(name="schema_linker")
    sql_generator = LlmAgent(
        model=_make_model(),
        name="sql_generator",
        instruction=generator_instruction,  # {intent}/{schema} filled from state
        tools=[FunctionTool(func=execute_sql)],
        generate_content_config=_gen_config(),
    )
    repair_llm = LlmAgent(
        model=_make_model(),
        name="repair_llm",
        instruction=REPAIR_INSTRUCTION,  # {question}/{schema}/{last_sql}/{last_result} from state
        tools=[FunctionTool(func=execute_sql)],
        generate_content_config=_gen_config(),
    )
    repair = RepairAgent(name="repair", sub_agents=[repair_llm])
    return SequentialAgent(
        name="sqloop_pipeline",
        sub_agents=[router, schema_linker, sql_generator, repair],
    )


# Default pipeline used by main.py / run_eval.py / run_spider.py (active config).
root_agent = build_pipeline()
