"""Populate the history memory from VERIFIED-correct solutions (Day-7+ feature).

Runs the pipeline on a training slice, keeps only the rows whose SQL execution-
matches the gold, and stores (question -> correct SQL) in data/memory.json. The
live dashboard then reuses / retrieves from this memory. We populate only from
training questions (never the demo/held-out ones) so reuse is never leakage.

Usage:
  LLM_BACKEND=deepseek uv run python build_memory.py            # concert_singer [0:20]
  LLM_BACKEND=deepseek uv run python build_memory.py 40 --multidb
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from sqloop.config import baseline_config
from sqloop.instrumentation import flush_tracing, setup_tracing
from sqloop.loop import eval_rows
from sqloop.memory import Memory
from sqloop.spider import dev_examples
from run_loop import PRESETS, _slices


async def main_async(n: int, multidb: bool) -> None:
    setup_tracing()
    if multidb:
        ex = list(dev_examples())
        random.Random(7).shuffle(ex)  # different seed than the eval slices
        examples = ex[:n]
    else:
        examples = dev_examples()[:n]  # concert_singer train block

    # Leakage guard: ENFORCE (not just promise) that we never populate memory from
    # the held-out / val / reflect slices the loop evaluates on. Different shuffle
    # seeds alone don't guarantee disjointness -- both draw from the same pool.
    reflect, val, held = _slices(*PRESETS["multidb" if multidb else "full"], multidb=multidb)
    eval_keys = {(e["db_id"], e["question"]) for e in reflect + val + held}
    before = len(examples)
    examples = [e for e in examples if (e["db_id"], e["question"]) not in eval_keys]
    print(f"leakage guard: excluded {before - len(examples)} examples overlapping the "
          f"eval slices; {len(examples)} train candidates left")
    if not examples:
        print("  -> no disjoint training data (this preset's eval covers the whole pool); "
              "use --multidb or a Spider train split for a leak-free memory.")

    rows = await eval_rows(baseline_config(), examples) if examples else []
    mem = Memory()
    added = sum(bool(_add(mem, r)) for r in rows)
    mem.save()
    print(f"memory populated: +{added} correct solutions, total {len(mem)} -> {mem.path}")

    # New additions are clean (excluded above). Surface any PRE-EXISTING leaky entries
    # loudly (e.g. a concert_singer store built before this guard) without crashing.
    clash = mem.overlap_with(reflect + val + held)
    if clash:
        print(f"⚠️  {len(clash)} pre-existing memory entries overlap the eval slices "
              f"(leaky from before this guard). Delete {mem.path} and rebuild for a clean store. "
              "Eval harnesses can call Memory.assert_disjoint_from() to hard-fail on this.")


def _add(mem: Memory, row: dict) -> bool:
    if row["correct"] and row["pred_sql"].strip():
        mem.add(row["question"], row["pred_sql"], row["db_id"])
        return True
    return False


def main() -> None:
    n = next((int(a) for a in sys.argv[1:] if a.isdigit()), 20)
    multidb = "--multidb" in sys.argv
    try:
        asyncio.run(main_async(n, multidb))
    finally:
        flush_tracing()


if __name__ == "__main__":
    main()
