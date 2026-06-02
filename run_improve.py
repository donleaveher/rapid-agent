"""Day 5: one self-improvement round (propose -> A/B -> commit-if-better).

- Mining data: the latest baseline eval run in data/eval_runs/ (successes -> few-shots,
  failures -> prompt rewrite).
- Held-out set: concert_singer dev examples [20:45], DISJOINT from the mining slice
  [0:20], so few-shots aren't leaked into the test.
- Compares baseline config vs candidate config by execution accuracy on the held-out
  set; commits the candidate only if it wins. Appends a point to data/improve_log.json
  (the rising accuracy curve for the demo).

Usage:
  uv run python run_improve.py            # one round (LLM prompt rewrite + eval)
  uv run python run_improve.py --no-llm   # deterministic propose (no LLM rewrite)
"""

from __future__ import annotations

import asyncio
import glob
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from sqloop.agent import build_pipeline
from sqloop.config import ACTIVE_PATH, baseline_config
from sqloop.eval import execution_match
from sqloop.instrumentation import flush_tracing, setup_tracing
from sqloop.propose import propose_candidate
from sqloop.spider import db_path_for, dev_examples
from sqloop.throttle import backoff_seconds, is_retryable
from main import run_turn_detailed

HELDOUT = (20, 45)  # concert_singer held-out, disjoint from mining slice [0:20]
GAP_SECONDS = 3
RETRIES = 5
LOG_PATH = Path(__file__).resolve().parent / "data" / "improve_log.json"


def _latest_eval_rows() -> list[dict]:
    files = sorted(glob.glob("data/eval_runs/baseline_n2*_*.json")) or sorted(
        glob.glob("data/eval_runs/*.json")
    )
    if not files:
        raise SystemExit("No eval runs found; run run_eval.py first.")
    return json.loads(Path(files[-1]).read_text())["rows"]


async def eval_config(config, examples) -> tuple[float, int]:
    """Execution accuracy of a GeneratorConfig on the given examples."""
    agent = build_pipeline(config.render_instruction())
    correct = 0
    for ex in examples:
        db_id, gold = ex["db_id"], ex["query"]
        db_path = str(db_path_for(db_id))
        for attempt in range(RETRIES):
            try:
                out = await run_turn_detailed(ex["question"], db_path, db_id, agent=agent)
                correct += execution_match(out["pred_sql"], gold, db_path)
                break
            except Exception as exc:  # noqa: BLE001
                if is_retryable(str(exc)) and attempt < RETRIES - 1:
                    await asyncio.sleep(backoff_seconds(str(exc), attempt))
                    continue
                break  # counts as wrong
        await asyncio.sleep(GAP_SECONDS)
    return correct / len(examples), correct


def _log_round(baseline_acc, candidate_acc, committed, notes) -> None:
    log = json.loads(LOG_PATH.read_text()) if LOG_PATH.exists() else []
    log.append({
        "round": len(log) + 1,
        "baseline_acc": baseline_acc,
        "candidate_acc": candidate_acc,
        "committed": committed,
        "notes": notes,
        "ts": int(time.time()),
    })
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2))


async def main_async(use_llm: bool) -> None:
    setup_tracing()
    rows = _latest_eval_rows()
    heldout = dev_examples()[HELDOUT[0]:HELDOUT[1]]
    print(f"held-out: {len(heldout)} examples (concert_singer [{HELDOUT[0]}:{HELDOUT[1]}])")

    print("\n[1/3] evaluating BASELINE on held-out ...")
    base_acc, base_c = await eval_config(baseline_config(), heldout)
    print(f"  baseline: {base_c}/{len(heldout)} = {base_acc:.1%}")

    print("\n[2/3] proposing CANDIDATE ...")
    candidate = await propose_candidate(rows, k=4, use_llm=use_llm)
    print(f"  candidate ({candidate.version}): {len(candidate.few_shots)} few-shots; {candidate.notes}")

    print("\n[3/3] evaluating CANDIDATE on held-out ...")
    cand_acc, cand_c = await eval_config(candidate, heldout)
    print(f"  candidate: {cand_c}/{len(heldout)} = {cand_acc:.1%}")

    committed = cand_acc > base_acc
    if committed:
        candidate.save(ACTIVE_PATH)
        print(f"\n✅ COMMITTED candidate (+{cand_acc - base_acc:.1%}) -> {ACTIVE_PATH}")
    else:
        print(f"\n↩️  kept baseline (candidate {cand_acc:.1%} <= baseline {base_acc:.1%})")
    _log_round(base_acc, cand_acc, committed, candidate.notes)
    print(f"logged round -> {LOG_PATH}")


def main() -> None:
    use_llm = "--no-llm" not in sys.argv
    try:
        asyncio.run(main_async(use_llm))
    finally:
        flush_tracing()


if __name__ == "__main__":
    main()
