"""Day 5/6: run ONE improvement round, continuing from the current active config.

Use this to take a single incremental step (reflect -> propose pool -> validate ->
A/B vs the current best -> commit-if-better). For the full multi-round rising
curve from baseline, use run_loop.py instead.

Usage:
  uv run python run_improve.py            # one round, full slices
  uv run python run_improve.py --tiny     # one round, tiny slices (cheap)
  uv run python run_improve.py --no-llm   # skip the LLM reflective candidate
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from sqloop.config import ACTIVE_PATH, active_config
from sqloop.instrumentation import flush_tracing, setup_tracing
from sqloop.loop import run_round
from run_loop import IMPROVE_LOG, PRESETS, _slices


async def main_async(use_llm: bool, preset: str) -> None:
    setup_tracing()
    incumbent = active_config()
    reflect, val, held = _slices(*PRESETS[preset], multidb=(preset == "multidb"))
    print(f"incumbent={incumbent.version} ({len(incumbent.few_shots)} few-shots) | "
          f"reflect={len(reflect)} val={len(val)} held-out={len(held)}")

    res = await run_round(incumbent, reflect_examples=reflect, val_examples=val,
                          held_examples=held, use_llm=use_llm)

    print(f"\nvalidation={res['validation']} -> selected {res['selected']}")
    print(f"held-out: incumbent {res['incumbent_held_acc']:.1%} vs candidate "
          f"{res['candidate_held_acc']:.1%}")
    if res["committed"]:
        res["new_incumbent"].save(ACTIVE_PATH)
        print(f"✅ COMMITTED {res['selected']} "
              f"(+{res['candidate_held_acc'] - res['incumbent_held_acc']:.1%}) -> {ACTIVE_PATH}")
    else:
        print("↩️  kept incumbent (candidate not better)")

    log = json.loads(IMPROVE_LOG.read_text()) if IMPROVE_LOG.exists() else []
    log.append({"round": len(log) + 1, **{k: res[k] for k in
               ("validation", "selected", "incumbent_held_acc", "candidate_held_acc",
                "committed", "notes")}, "ts": int(time.time())})
    IMPROVE_LOG.parent.mkdir(parents=True, exist_ok=True)
    IMPROVE_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2))
    print(f"logged -> {IMPROVE_LOG}")


def main() -> None:
    args = sys.argv[1:]
    use_llm = "--no-llm" not in args
    preset = "tiny" if "--tiny" in args else ("multidb" if "--multidb" in args else "full")
    try:
        asyncio.run(main_async(use_llm, preset))
    finally:
        flush_tracing()


if __name__ == "__main__":
    main()
