"""Day 4: Optimizer produces a failure-mode report from real Phoenix experiment data.

Phase 1 fetches the latest spider-dev experiment's results via the Phoenix MCP
server; phase 2 clusters the failures with one Gemini call. The two phases are
sequential so the MCP-server and Gemini connections never share the proxy at once.

Usage:
  uv run python run_optimizer.py
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from sqloop.optimizer import REPORTS_DIR, analyze, fetch_experiment_rows


async def main_async() -> None:
    print("[phase 1] fetching experiment results via Phoenix MCP ...")
    meta, rows = await fetch_experiment_rows()
    nfail = sum(not r["correct"] for r in rows)
    print(f"  dataset={meta['dataset']} experiment={meta['experiment_id']} "
          f"runs={meta['total']} failures={nfail}")

    print("\n[phase 2] clustering failures with Gemini ...")
    report = await analyze(meta, rows)

    print("\n===== Optimizer failure-mode report =====\n")
    print(report or "(no report produced)")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"report_{meta['experiment_id']}_{int(time.time())}.md"
    out.write_text(report, encoding="utf-8")
    print(f"\nsaved report -> {out}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
