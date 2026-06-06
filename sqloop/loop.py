"""Core of the self-improvement loop: evaluate a config, run one round.

One round (run_round):
  1. REFLECT  -- evaluate the incumbent on a reflection slice -> its current rows
     (successes seed few-shots, failures seed guidance).
  2. PROPOSE  -- a pool of additive candidates extending the incumbent.
  3. RANK     -- score each candidate on a validation slice; pick the best
                 (accuracy, tie-break shorter instruction -- Pareto-ish).
  4. A/B      -- best vs incumbent on a held-out slice; commit-if-better.

Slices are disjoint (no leakage). Generation is greedy (temperature=0) so the
numbers are reproducible. run_loop.py chains rounds into a rising curve.
"""

from __future__ import annotations

import asyncio
import os

from sqloop.agent import build_pipeline
from sqloop.config import GeneratorConfig
from sqloop.eval import execution_match, should_commit
from sqloop.propose import propose_candidates
from sqloop.spider import db_path_for
from sqloop.throttle import backoff_seconds, is_retryable
from main import run_turn_detailed

# Seconds between calls. Default 3 spaces out Gemini's free-tier RPM; set
# SQLOOP_EVAL_GAP=0 for DeepSeek (no rate limit) to run at full speed.
GAP_SECONDS = float(os.environ.get("SQLOOP_EVAL_GAP", "3"))
# Parallel evals. 1 = sequential (Gemini free tier). DeepSeek (concurrency 500,
# no rate limit) can use ~10 to make multi-db / hundreds-of-examples runs fast.
CONCURRENCY = int(os.environ.get("SQLOOP_EVAL_CONCURRENCY", "1"))
RETRIES = 5


async def _eval_one(agent, ex: dict) -> dict:
    db_id, gold = ex["db_id"], ex["query"]
    db_path = str(db_path_for(db_id))
    pred, tokens = "", 0
    for attempt in range(RETRIES):
        try:
            res = await run_turn_detailed(ex["question"], db_path, db_id, agent=agent)
            pred = res["pred_sql"]
            tokens = (res.get("tokens") or {}).get("total", 0)  # tokens for this SQL generation
            break
        except Exception as exc:  # noqa: BLE001
            if is_retryable(str(exc)) and attempt < RETRIES - 1:
                await asyncio.sleep(backoff_seconds(str(exc), attempt))
                continue
            break  # counts as wrong (pred stays "")
    return {"question": ex["question"], "db_id": db_id, "gold_sql": gold, "pred_sql": pred,
            "correct": execution_match(pred, gold, db_path), "tokens": tokens}


async def eval_rows(config: GeneratorConfig, examples: list[dict]) -> list[dict]:
    """Run a config over examples; return per-example {question, gold, pred, correct}.

    Runs CONCURRENCY examples at a time (order preserved). With concurrency=1 it is
    sequential with GAP_SECONDS spacing (Gemini free tier).
    """
    agent = build_pipeline(config.render_instruction())
    sem = asyncio.Semaphore(CONCURRENCY)

    async def guarded(ex):
        async with sem:
            row = await _eval_one(agent, ex)
            if CONCURRENCY == 1 and GAP_SECONDS:
                await asyncio.sleep(GAP_SECONDS)
            return row

    return list(await asyncio.gather(*(guarded(ex) for ex in examples)))


def accuracy(rows: list[dict]) -> float:
    return sum(r["correct"] for r in rows) / len(rows) if rows else 0.0


async def eval_accuracy(config: GeneratorConfig, examples: list[dict]) -> float:
    return accuracy(await eval_rows(config, examples))


async def run_round(
    incumbent: GeneratorConfig,
    *,
    reflect_examples: list[dict],
    val_examples: list[dict],
    held_examples: list[dict],
    incumbent_held_acc: float | None = None,
    incumbent_held_rows: list[dict] | None = None,
    use_llm: bool = True,
    k: int = 4,
    failure_report: str | None = None,
    mine_demos: bool | None = None,
) -> dict:
    """Run one improvement round; return metrics + the (possibly new) incumbent.

    `failure_report` (optional) is the Optimizer's failure-mode analysis, clustered
    from the agent's own Phoenix traces via MCP; when given it grounds the reflective
    candidate so trace-introspection actually drives the prompt revision.

    `mine_demos` (default None -> env SQLOOP_PROPOSE_DEMOS) gates static success
    few-shot mining; set False when the History Memory owns that lane, so propose
    only contributes abstract rules/guidance (no duplicate few-shot channel).
    """
    refl_rows = await eval_rows(incumbent, reflect_examples)
    candidates = await propose_candidates(
        refl_rows, incumbent, k=k, use_llm=use_llm, failure_report=failure_report,
        mine_demos=mine_demos)

    ranked = []
    for c in candidates:
        acc = await eval_accuracy(c, val_examples)
        ranked.append((acc, len(c.render_instruction()), c))
    ranked.sort(key=lambda t: (-t[0], t[1]))  # best accuracy, then shorter prompt
    best = ranked[0][2]

    # Keep per-example held rows so run_loop can log them to Phoenix (#6) without a
    # re-eval. accuracy() of these rows == the old eval_accuracy(), so numbers are
    # unchanged.
    if incumbent_held_rows is not None:
        incumbent_held_acc = accuracy(incumbent_held_rows)
    elif incumbent_held_acc is None:
        incumbent_held_rows = await eval_rows(incumbent, held_examples)
        incumbent_held_acc = accuracy(incumbent_held_rows)
    cand_held_rows = await eval_rows(best, held_examples)
    cand_held_acc = accuracy(cand_held_rows)

    # Commit only on a meaningful gain, not single-point noise (see eval.should_commit).
    rule = os.environ.get("SQLOOP_COMMIT_RULE", "margin")
    min_gain = int(os.environ.get("SQLOOP_COMMIT_MIN_GAIN", "3"))
    committed = should_commit(cand_held_acc, incumbent_held_acc, len(held_examples),
                              rule=rule, min_gain=min_gain)

    return {
        "reflect_acc": accuracy(refl_rows),
        "validation": {c.version: round(a, 4) for a, _, c in ranked},
        "selected": best.version,
        "incumbent_held_acc": incumbent_held_acc,
        "candidate_held_acc": cand_held_acc,
        "committed": committed,
        "commit_rule": f"{rule}(min_gain={min_gain})" if rule == "margin" else rule,
        "new_incumbent": best if committed else incumbent,
        "new_held_acc": cand_held_acc if committed else incumbent_held_acc,
        "new_held_rows": cand_held_rows if committed else incumbent_held_rows,
        "notes": best.notes,
    }
