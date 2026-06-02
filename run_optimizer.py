"""Day 4: run the Optimizer to produce a failure-mode report from real traces.

Usage:
  uv run python run_optimizer.py                       # analyze the latest experiment
  uv run python run_optimizer.py "baseline-v0"         # analyze a named experiment
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from google.adk.runners import InMemoryRunner
from google.genai import types

from sqloop.instrumentation import flush_tracing, setup_tracing
from sqloop.optimizer import build_optimizer

APP_NAME = "sqloop-optimizer"


async def main_async(experiment_hint: str) -> None:
    setup_tracing()
    agent, toolset = build_optimizer()
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    user_id, session_id = "optimizer", secrets.token_hex(8)
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )

    target = f'the experiment named "{experiment_hint}"' if experiment_hint else "the most recent experiment"
    prompt = (
        f"Analyze {target} for the spider-dev dataset in Phoenix and produce the "
        "failure-mode report as instructed."
    )

    final_text = ""
    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(p.text or "" for p in event.content.parts)
    finally:
        await toolset.close()

    print("\n===== Optimizer failure-mode report =====\n")
    print(final_text or "(no report produced)")


def main() -> None:
    hint = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        asyncio.run(main_async(hint))
    finally:
        flush_tracing()


if __name__ == "__main__":
    main()
