"""Day 6: run N self-improvement rounds and produce a rising accuracy curve.

Each round's incumbent is the previous round's committed config (round 0 = the
baseline). Every round is scored on the SAME held-out slice, so the points are
comparable. We only commit improvements, so the curve is monotonic non-decreasing
("a round that doesn't beat the incumbent keeps the incumbent").

Slices (all concert_singer, mutually disjoint, no leakage):
  reflect    = dev [0:R)     (incumbent's current failures -> propose)
  validation = dev [R:R+V)   (rank candidates)
  held-out   = dev [R+V:45)  (the curve; commit decision)

Writes:
  data/configs/active.json   -- the final best config
  data/improve_log.json      -- per-round detail
  data/curve.json            -- [{round, held_out_acc, committed, selected}] for the dashboard

Usage:
  uv run python run_loop.py            # 3 rounds, default slices
  uv run python run_loop.py 2 --tiny   # 2 rounds, tiny slices (cheap smoke)
  uv run python run_loop.py 3 --no-llm # no LLM reflective candidate
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# Batch eval: every Spider question is "sql", so skip the per-question router LLM
# call by default (#4) -- saves ~1/3 of LLM calls. Override with SQLOOP_ROUTER=llm.
os.environ.setdefault("SQLOOP_ROUTER", "heuristic")

from sqloop.config import ACTIVE_PATH, baseline_config
from sqloop.eval import wilson_ci
from sqloop.experiment import log_round, make_held_dataset, phoenix_enabled
from sqloop.instrumentation import flush_tracing, setup_tracing
from sqloop.loop import accuracy, eval_rows, run_round
from sqloop.optimizer import build_failure_report, latest_saved_report
from sqloop.spider import dev_examples


def _point(rnd: int, acc: float, n: int, committed: bool, selected: str) -> dict:
    lo, hi = wilson_ci(round(acc * n), n)
    return {"round": rnd, "held_out_acc": round(acc, 4), "n": n,
            "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
            "committed": committed, "selected": selected}

DATA = Path(__file__).resolve().parent / "data"
IMPROVE_LOG = DATA / "improve_log.json"
CURVE = DATA / "curve.json"

# (reflect_n, validation_n, held_n), disjoint.
#  tiny/full  -> single db (concert_singer, 45 examples)
#  multidb    -> sampled across all 20 dev dbs; large held-out averages out noise
#               and improvements come from transferable rules/guidance, not db-specific
#               few-shots. Pair with DeepSeek (SQLOOP_EVAL_CONCURRENCY>1, gap 0).
PRESETS = {"tiny": (6, 6, 10), "full": (15, 10, 20), "multidb": (40, 30, 100)}


def _slices(reflect_n: int, val_n: int, held_n: int, multidb: bool):
    if multidb:
        ex = list(dev_examples())  # all 1034 across 20 dbs
    else:
        # Spider orders concert_singer by difficulty (easy counts first, joins later);
        # shuffle so reflect/val/held each get a mix of easy and hard questions.
        ex = dev_examples()[:45]
    random.Random(13).shuffle(ex)
    reflect = ex[:reflect_n]
    val = ex[reflect_n:reflect_n + val_n]
    held = ex[reflect_n + val_n:reflect_n + val_n + held_n]
    return reflect, val, held


async def _load_failure_report() -> str | None:
    """The Optimizer's MCP-derived failure-mode report that grounds the reflective
    candidate. SQLOOP_OPTIMIZER_LIVE=1 reads the agent's own traces via Phoenix MCP
    now; otherwise use the latest report run_optimizer.py already saved. Either way
    None is fine -- reflection just runs unguided (previous behaviour)."""
    report = None
    if os.environ.get("SQLOOP_OPTIMIZER_LIVE", "0") == "1":
        print("[optimizer] reading own traces via Phoenix MCP ...")
        report = await build_failure_report()
    if report is None:
        report = latest_saved_report()
    print(f"[optimizer] failure-mode report: "
          f"{f'loaded ({len(report)} chars) -> grounds reflect candidate' if report else 'none -> reflect runs unguided'}")
    return report


async def main_async(rounds: int, use_llm: bool, preset: str) -> None:
    setup_tracing()
    failure_report = await _load_failure_report()
    reflect, val, held = _slices(*PRESETS[preset], multidb=(preset == "multidb"))
    n_dbs = len({e["db_id"] for e in reflect + val + held})
    print(f"preset={preset} | reflect={len(reflect)} val={len(val)} held-out={len(held)} "
          f"| dbs={n_dbs} | rounds={rounds}")

    incumbent = baseline_config()
    held_rows = await eval_rows(incumbent, held)          # keep rows for optional Phoenix log
    held_acc = accuracy(held_rows)
    lo, hi = wilson_ci(round(held_acc * len(held)), len(held))
    print(f"\n[round 0] baseline held-out accuracy = {held_acc:.1%}  (95% CI {lo:.0%}-{hi:.0%})")
    curve = [_point(0, held_acc, len(held), True, "baseline")]
    rounds_log = []

    # Optional: log each round's held-out eval to Phoenix Experiments (#6), no extra
    # LLM (precomputed preds). Best-effort -- never break the loop.
    ph_dataset = None
    if phoenix_enabled():
        try:
            ph_dataset = await make_held_dataset(held, tag=f"{preset}-")
            url = await log_round(ph_dataset, 0, held_rows, "baseline")
            print(f"[phoenix] round 0 experiment -> {url}")
        except Exception as exc:  # noqa: BLE001
            print(f"[phoenix] logging disabled (error: {exc})")
            ph_dataset = None

    for r in range(1, rounds + 1):
        print(f"\n===== round {r} =====")
        res = await run_round(
            incumbent, reflect_examples=reflect, val_examples=val, held_examples=held,
            incumbent_held_acc=held_acc, incumbent_held_rows=held_rows,
            use_llm=use_llm, failure_report=failure_report,
        )
        print(f"  reflect acc={res['reflect_acc']:.1%} | validation={res['validation']} "
              f"-> selected {res['selected']}")
        print(f"  held-out: incumbent {res['incumbent_held_acc']:.1%} vs candidate "
              f"{res['candidate_held_acc']:.1%} -> {'COMMIT' if res['committed'] else 'keep'} "
              f"[gate: {res['commit_rule']}]")
        incumbent = res["new_incumbent"]
        held_acc = res["new_held_acc"]
        held_rows = res["new_held_rows"]
        if ph_dataset is not None:
            try:
                url = await log_round(ph_dataset, r, held_rows, res["selected"])
                print(f"[phoenix] round {r} experiment -> {url}")
            except Exception as exc:  # noqa: BLE001
                print(f"[phoenix] round {r} log failed: {exc}")
        curve.append(_point(r, held_acc, len(held), res["committed"], res["selected"]))
        rounds_log.append({"round": r, **{k: res[k] for k in
                          ("reflect_acc", "validation", "selected", "incumbent_held_acc",
                           "candidate_held_acc", "committed", "commit_rule", "notes")}, "ts": int(time.time())})

    incumbent.save(ACTIVE_PATH)
    DATA.mkdir(parents=True, exist_ok=True)
    IMPROVE_LOG.write_text(json.dumps(rounds_log, ensure_ascii=False, indent=2))
    CURVE.write_text(json.dumps(curve, ensure_ascii=False, indent=2))

    # Provenance stamp (#7): active.json / curve.json / improve_log.json are written
    # together here, so a single run is self-consistent. The earlier mismatch
    # (flash curve vs pro config) came from manually juggling files across separate
    # runs -- this records which run produced the current artifacts so any such
    # mismatch is detectable. Comparison runs should use side paths, not these.
    backend = os.environ.get("LLM_BACKEND", "gemini").lower()
    meta = {
        "ts": int(time.time()),
        "backend": backend,
        "model": (os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro") if backend == "deepseek"
                  else os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")),
        "preset": preset, "rounds": rounds, "seed": 13,
        "router": os.environ.get("SQLOOP_ROUTER", "llm"),
        "commit_rule": rounds_log[-1]["commit_rule"] if rounds_log else "n/a",
        "n_held": len(held), "dbs": n_dbs,
        "final_version": incumbent.version, "final_few_shots": len(incumbent.few_shots),
        "curve": [p["held_out_acc"] for p in curve],
    }
    (DATA / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    pts = " -> ".join(f"r{p['round']}:{p['held_out_acc']:.0%}" for p in curve)
    print(f"\n=== accuracy curve (held-out) ===\n{pts}")
    print(f"final config: {incumbent.version} ({len(incumbent.few_shots)} few-shots) -> {ACTIVE_PATH}")
    print(f"curve -> {CURVE} | provenance -> {DATA / 'run_meta.json'}")


def main() -> None:
    args = [a for a in sys.argv[1:]]
    rounds = next((int(a) for a in args if a.isdigit()), 3)
    use_llm = "--no-llm" not in args
    preset = "tiny" if "--tiny" in args else ("multidb" if "--multidb" in args else "full")
    try:
        asyncio.run(main_async(rounds, use_llm, preset))
    finally:
        flush_tracing()


if __name__ == "__main__":
    main()
