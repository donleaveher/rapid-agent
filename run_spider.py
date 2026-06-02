"""Day 2 smoke runner: run SQLoop over the first N Spider dev questions.

Goal for Day 2 is robustness, NOT correctness: every question should produce
*some* answer without the pipeline crashing. Execution-accuracy scoring is Day 3.

Usage:
  uv run python run_spider.py            # first 20 dev questions
  uv run python run_spider.py 30         # first 30
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from sqloop.instrumentation import flush_tracing, setup_tracing
from sqloop.spider import db_path_for, dev_examples
from sqloop.throttle import backoff_seconds, is_retryable
from main import run_turn

# Seconds between questions on the free tier (each question = ~3 LLM calls).
GAP_SECONDS = 5
RETRIES = 5


async def run_one(question: str, db_id: str) -> tuple[bool, str]:
    """Run one question with adaptive backoff on transient errors."""
    for attempt in range(RETRIES):
        try:
            answer = await run_turn(question, db_path=str(db_path_for(db_id)), db_id=db_id)
            return bool(answer.strip()), answer.strip() or "(empty answer)"
        except Exception as exc:  # noqa: BLE001 - Day 2 wants no crashes
            msg = str(exc)
            if is_retryable(msg) and attempt < RETRIES - 1:
                await asyncio.sleep(backoff_seconds(msg, attempt))
                continue
            return False, f"ERROR: {msg.splitlines()[-1][:160]}"
    return False, "ERROR: exhausted retries"


async def main_async(n: int) -> None:
    setup_tracing()
    examples = dev_examples(limit=n)
    ok_count = 0
    for i, ex in enumerate(examples, 1):
        q, db_id = ex["question"], ex["db_id"]
        ok, answer = await run_one(q, db_id)
        ok_count += ok
        flag = "OK " if ok else "FAIL"
        print(f"[{i:>2}/{len(examples)}] {flag} ({db_id}) {q}")
        print(f"        -> {answer}")
        await asyncio.sleep(GAP_SECONDS)  # space out calls to ease free-tier RPM
    print(f"\n=== Day 2 smoke: {ok_count}/{len(examples)} produced an answer (no crash) ===")


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    try:
        asyncio.run(main_async(n))
    finally:
        flush_tracing()


if __name__ == "__main__":
    main()
