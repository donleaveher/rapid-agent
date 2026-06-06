"""Run one SQLoop turn end-to-end with Phoenix tracing.

Usage:
  uv run python main.py "How many users are there?"
  uv run python main.py "How many singers are there?" concert_singer
      (second arg = Spider db_id; default uses the Day 1 test db)
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing anything that reads env (model name, Phoenix auth).
load_dotenv(Path(__file__).resolve().parent / ".env")

from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.runners import InMemoryRunner
from google.genai import types

# Hard cap on LLM calls per turn -- prevents any agent (incl. the repair ReAct
# loop) from spinning forever. The prompt's "at most two attempts" is only a soft
# hint; this is the real ceiling. Override with SQLOOP_MAX_LLM_CALLS.
_MAX_LLM_CALLS = int(os.environ.get("SQLOOP_MAX_LLM_CALLS", "12"))

from sqloop.agent import root_agent
from sqloop.db import default_db_path
from sqloop.instrumentation import flush_tracing, setup_tracing
from sqloop.spider import db_path_for

APP_NAME = "sqloop"


def _format_memory_examples(retrieved: list[dict]) -> str:
    """Render retrieved past solutions as a few-shot block for the generator."""
    if not retrieved:
        return ""
    lines = ["", "Similar past questions you solved correctly (reuse their patterns):"]
    for r in retrieved:
        lines.append(f"Q: {r['question']}\nSQL: {r['sql']}")
    return "\n".join(lines) + "\n"


async def run_turn_detailed(user_text: str, db_path: str, db_id: str = "", agent=None, memory=None) -> dict:
    """Run the pipeline once; return {"answer", "pred_sql", "source"}.

    pred_sql is the last SQL the model passed to the execute_sql tool. `agent`
    defaults to the active-config pipeline. If `memory` is given:
      - a near-identical past question (same db) is REUSED directly (no LLM call);
      - otherwise the most similar past solutions are injected as few-shots via
        the {memory_examples} state slot.
    Leakage is the caller's responsibility: only populate `memory` from training
    examples, never from the held-out set being evaluated.
    """
    zero_tokens = {"prompt": 0, "completion": 0, "total": 0, "by_agent": {}}
    if memory is not None:
        reused = memory.reuse(user_text, db_id)
        if reused:  # reuse skips all LLM calls -> zero tokens
            return {"answer": f"(from memory) {reused}", "pred_sql": reused,
                    "source": "memory", "tokens": zero_tokens}

    setup_tracing()
    memory_examples = _format_memory_examples(memory.retrieve(user_text, db_id) if memory else [])
    user_id, session_id = "local_user", secrets.token_hex(8)
    runner = InMemoryRunner(agent=agent or root_agent, app_name=APP_NAME)
    await runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
        state={"db_path": db_path, "db_id": db_id, "question": user_text,
               "memory_examples": memory_examples},
    )

    final_text, pred_sql, source = "", "", "generated"
    tokens = {"prompt": 0, "completion": 0, "total": 0, "by_agent": {}}
    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=user_text)]),
            run_config=RunConfig(max_llm_calls=_MAX_LLM_CALLS),
        ):
            um = getattr(event, "usage_metadata", None)
            if um is not None:  # per-LLM-call token usage (Gemini + DeepSeek/LiteLlm)
                p = getattr(um, "prompt_token_count", 0) or 0
                c = getattr(um, "candidates_token_count", 0) or 0
                t = getattr(um, "total_token_count", 0) or 0
                tokens["prompt"] += p
                tokens["completion"] += c
                tokens["total"] += t
                a = event.author or "?"
                tokens["by_agent"][a] = tokens["by_agent"].get(a, 0) + t
            if event.content and event.content.parts:
                for part in event.content.parts:
                    fc = getattr(part, "function_call", None)
                    if fc and fc.name == "execute_sql":
                        pred_sql = (fc.args or {}).get("sql", pred_sql)
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(p.text or "" for p in event.content.parts)
    except LlmCallsLimitExceededError:
        # Hit the per-turn ceiling (runaway loop guard); keep what we captured.
        source = "capped"
    return {"answer": final_text, "pred_sql": pred_sql, "source": source, "tokens": tokens}


async def run_turn(user_text: str, db_path: str, db_id: str = "") -> str:
    """Run the pipeline once against `db_path`; returns the final answer text."""
    result = await run_turn_detailed(user_text, db_path, db_id)
    return result["answer"]


def main() -> None:
    msg = sys.argv[1] if len(sys.argv) > 1 else "How many users are there?"
    db_id = sys.argv[2] if len(sys.argv) > 2 else ""
    db_path = str(db_path_for(db_id)) if db_id else str(default_db_path())
    try:
        answer = asyncio.run(run_turn(msg, db_path=db_path, db_id=db_id))
        print("\n=== SQLoop answer ===")
        print(answer or "(no final response)")
    finally:
        flush_tracing()  # ensure spans reach Phoenix before exit (trap #3)


if __name__ == "__main__":
    main()
