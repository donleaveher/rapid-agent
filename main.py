"""Run one SQLoop turn end-to-end with Phoenix tracing.

Usage:
  uv run python main.py "How many users are there?"
  uv run python main.py "How many singers are there?" concert_singer
      (second arg = Spider db_id; default uses the Day 1 test db)
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing anything that reads env (model name, Phoenix auth).
load_dotenv(Path(__file__).resolve().parent / ".env")

from google.adk.runners import InMemoryRunner
from google.genai import types

from sqloop.agent import root_agent
from sqloop.db import default_db_path
from sqloop.instrumentation import flush_tracing, setup_tracing
from sqloop.spider import db_path_for

APP_NAME = "sqloop"


async def run_turn_detailed(user_text: str, db_path: str, db_id: str = "", agent=None) -> dict:
    """Run the pipeline once; return {"answer", "pred_sql"}.

    pred_sql is the last SQL the model passed to the execute_sql tool (the
    prediction used for execution-accuracy scoring). Captured from the event
    stream so we don't have to mutate tool/state code. `agent` defaults to the
    active-config pipeline; pass a candidate pipeline for A/B testing.
    """
    setup_tracing()
    user_id, session_id = "local_user", secrets.token_hex(8)
    runner = InMemoryRunner(agent=agent or root_agent, app_name=APP_NAME)
    await runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
        state={"db_path": db_path, "db_id": db_id},
    )

    final_text, pred_sql = "", ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=user_text)]),
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                fc = getattr(part, "function_call", None)
                if fc and fc.name == "execute_sql":
                    pred_sql = (fc.args or {}).get("sql", pred_sql)
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(p.text or "" for p in event.content.parts)
    return {"answer": final_text, "pred_sql": pred_sql}


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
