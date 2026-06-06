"""Log the self-improvement curve to Phoenix Experiments (#6).

Each round's held-out evaluation is recorded as a Phoenix experiment on a shared
held-out dataset, with execution_accuracy stored as the experiment's eval -- so the
rising curve is visible in the Phoenix Experiments UI, not just data/curve.json.

To avoid re-running the pipeline (the loop already computed each example's
pred_sql), the experiment task is a LOOKUP into the precomputed rows: no extra LLM
calls, just the evaluator re-executing pred vs gold SQL. Off by default
(SQLOOP_PHOENIX_EXPERIMENTS=on) and fully best-effort, so the loop stays robust
offline / under the flaky dev proxy.
"""

from __future__ import annotations

import os
import time

from sqloop.eval import execution_match
from sqloop.spider import db_path_for


def phoenix_enabled() -> bool:
    return os.environ.get("SQLOOP_PHOENIX_EXPERIMENTS", "off").lower() == "on"


def _client():
    from phoenix.client import AsyncClient

    parts = os.environ.get("PHOENIX_CLIENT_HEADERS", "").split("api_key=", 1)
    return AsyncClient(api_key=parts[1].strip() if len(parts) > 1 else None)


def execution_accuracy(output: dict, expected: dict) -> bool:
    """Phoenix evaluator: does the (precomputed) pred SQL match gold's result set?"""
    pred = (output or {}).get("pred_sql", "")
    gold = (expected or {}).get("gold_sql", "")
    db_id = (expected or {}).get("db_id", "")
    return execution_match(pred, gold, db_path_for(db_id))


async def make_held_dataset(held_examples: list[dict], tag: str = ""):
    """Create the shared held-out dataset once; reuse across rounds."""
    client = _client()
    return await client.datasets.create_dataset(
        name=f"sqloop-held-{tag}{int(time.time())}",
        inputs=[{"question": e["question"], "db_id": e["db_id"]} for e in held_examples],
        outputs=[{"gold_sql": e["query"], "db_id": e["db_id"]} for e in held_examples],
        dataset_description="SQLoop held-out set for the self-improvement curve",
    )


def _exp_url(client, experiment) -> str | None:
    if isinstance(experiment, dict):
        exp_id = experiment.get("id") or experiment.get("experiment_id")
    else:
        exp_id = getattr(experiment, "id", None) or getattr(experiment, "experiment_id", None)
    return str(exp_id) if exp_id else None


async def log_round(dataset, round_idx: int, rows: list[dict], selected: str = "") -> str | None:
    """Record one round's held-out result as a Phoenix experiment (no LLM: the task
    just looks up the precomputed pred_sql; the evaluator scores it)."""
    client = _client()
    idx = {(r["question"], r.get("db_id", "")): r.get("pred_sql", "") for r in rows}

    async def task(input: dict) -> dict:
        return {"pred_sql": idx.get((input["question"], input.get("db_id", "")), "")}

    name = f"sqloop-loop-r{round_idx}" + (f"-{selected}" if selected else "")
    experiment = await client.experiments.run_experiment(
        dataset=dataset, task=task, evaluators=[execution_accuracy],
        experiment_name=name, concurrency=4, print_summary=False,
    )
    exp_id = _exp_url(client, experiment)
    if not exp_id:
        return None
    try:
        return await client.experiments.get_experiment_url(experiment_id=exp_id)
    except Exception:  # noqa: BLE001 - URL is a nicety
        return f"id={exp_id}"
