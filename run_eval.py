"""Day 3 eval harness: execution accuracy on a small Spider dev sample.

Runs the SQLoop pipeline on the first N dev questions, compares the predicted
SQL's result set against the gold SQL, and prints a baseline accuracy. Writes a
JSON record so baselines/iterations can be compared later.

Usage:
  uv run python run_eval.py            # first 20 dev questions
  uv run python run_eval.py 10         # first 10
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from sqloop.eval import execution_match, wilson_ci
from sqloop.instrumentation import flush_tracing, setup_tracing
from sqloop.spider import db_path_for, dev_examples
from sqloop.throttle import backoff_seconds, is_retryable
from main import run_turn_detailed

GAP_SECONDS = 5
RETRIES = 5
RESULTS_DIR = Path(__file__).resolve().parent / "data" / "eval_runs"


async def eval_one(question: str, db_id: str, gold_sql: str) -> dict:
    """Run one question with backoff; score execution match. Never raises."""
    db_path = str(db_path_for(db_id))
    for attempt in range(RETRIES):
        try:
            out = await run_turn_detailed(question, db_path=db_path, db_id=db_id)
            pred_sql = out["pred_sql"]
            correct = execution_match(pred_sql, gold_sql, db_path)
            return {"db_id": db_id, "question": question, "gold_sql": gold_sql,
                    "pred_sql": pred_sql, "correct": correct, "error": ""}
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if is_retryable(msg) and attempt < RETRIES - 1:
                await asyncio.sleep(backoff_seconds(msg, attempt))
                continue
            return {"db_id": db_id, "question": question, "gold_sql": gold_sql,
                    "pred_sql": "", "correct": False, "error": msg.splitlines()[-1][:160]}
    return {"db_id": db_id, "question": question, "gold_sql": gold_sql,
            "pred_sql": "", "correct": False, "error": "exhausted retries"}


async def main_async(n: int) -> None:
    setup_tracing()
    examples = dev_examples(limit=n)
    rows = []
    for i, ex in enumerate(examples, 1):
        r = await eval_one(ex["question"], ex["db_id"], ex["query"])
        rows.append(r)
        mark = "✓" if r["correct"] else ("E" if r["error"] else "✗")
        print(f"[{i:>2}/{len(examples)}] {mark} ({r['db_id']}) {r['question']}")
        print(f"        gold: {r['gold_sql']}")
        print(f"        pred: {r['pred_sql'] or r['error'] or '(none)'}")
        time.sleep(GAP_SECONDS)

    correct = sum(r["correct"] for r in rows)
    acc = correct / len(rows) if rows else 0.0
    lo, hi = wilson_ci(correct, len(rows))
    print(f"\n=== baseline execution accuracy: {correct}/{len(rows)} = {acc:.1%} "
          f"(95% CI {lo:.1%}-{hi:.1%}) ===")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"baseline_n{len(rows)}_{int(time.time())}.json"
    out_path.write_text(json.dumps(
        {"n": len(rows), "correct": correct, "accuracy": acc,
         "ci_lo": lo, "ci_hi": hi, "rows": rows},
        ensure_ascii=False, indent=2))
    print(f"saved: {out_path}")


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    try:
        asyncio.run(main_async(n))
    finally:
        flush_tracing()


if __name__ == "__main__":
    main()
