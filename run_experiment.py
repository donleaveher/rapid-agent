"""Day 3: run a SQLoop eval as a Phoenix Experiment.

Uploads a small Spider dev sample as a Phoenix dataset, runs the SQLoop pipeline
as the experiment task, and scores execution accuracy as the evaluator. Results
show up in the Phoenix Experiments UI -- the artifact later iterations compare
against (baseline vs candidate).

Runs sequentially (concurrency=1) and each task self-retries, so free-tier rate
limits don't sink the experiment.

Usage:
  uv run python run_experiment.py 10                 # 10 examples
  uv run python run_experiment.py 10 "baseline-v0"   # named experiment
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from phoenix.client import AsyncClient

from sqloop.eval import execution_match
from sqloop.instrumentation import flush_tracing, setup_tracing
from sqloop.spider import db_path_for, dev_examples
from sqloop.throttle import backoff_seconds, is_retryable
from main import run_turn_detailed

RETRIES = 5


def _client() -> AsyncClient:
    parts = os.environ.get("PHOENIX_CLIENT_HEADERS", "").split("api_key=", 1)
    return AsyncClient(api_key=parts[1].strip() if len(parts) > 1 else None)


async def _task(input: dict) -> dict:
    """Experiment task: run the pipeline, with internal rate-limit retries."""
    question, db_id = input["question"], input["db_id"]
    db_path = str(db_path_for(db_id))
    last_exc = None
    for attempt in range(RETRIES):
        try:
            return await run_turn_detailed(question, db_path=db_path, db_id=db_id)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if is_retryable(str(exc)) and attempt < RETRIES - 1:
                await asyncio.sleep(backoff_seconds(str(exc), attempt))
                continue
            break
    return {"answer": "", "pred_sql": "", "error": str(last_exc)}


def execution_accuracy(output: dict, expected: dict) -> bool:
    """Evaluator: does the predicted SQL's result set match the gold SQL's?"""
    pred = (output or {}).get("pred_sql", "")
    gold = (expected or {}).get("gold_sql", "")
    db_id = (expected or {}).get("db_id", "")
    return execution_match(pred, gold, db_path_for(db_id))


async def main_async(n: int, exp_name: str) -> None:
    setup_tracing()
    client = _client()
    examples = dev_examples(limit=n)

    dataset = await client.datasets.create_dataset(
        name=f"spider-dev-n{n}-{int(time.time())}",
        inputs=[{"question": e["question"], "db_id": e["db_id"]} for e in examples],
        outputs=[{"gold_sql": e["query"], "db_id": e["db_id"]} for e in examples],
        dataset_description="Spider dev sample for SQLoop execution-accuracy eval",
    )

    experiment = await client.experiments.run_experiment(
        dataset=dataset,
        task=_task,
        evaluators=[execution_accuracy],
        experiment_name=exp_name,
        concurrency=1,          # sequential -> friendly to free-tier rate limits
        print_summary=True,
    )

    exp_id = None
    if isinstance(experiment, dict):
        exp_id = experiment.get("id") or experiment.get("experiment_id")
    else:
        exp_id = getattr(experiment, "id", None) or getattr(experiment, "experiment_id", None)
    if exp_id:
        try:
            url = await client.experiments.get_experiment_url(experiment_id=exp_id)
            print(f"\nExperiment in Phoenix: {url}")
        except Exception:  # noqa: BLE001 - URL is a nicety, don't fail the run
            print(f"\nExperiment id: {exp_id}")


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    exp_name = sys.argv[2] if len(sys.argv) > 2 else f"sqloop-baseline-n{n}"
    try:
        asyncio.run(main_async(n, exp_name))
    finally:
        flush_tracing()


if __name__ == "__main__":
    main()
