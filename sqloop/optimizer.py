"""Improvement-plane: the Optimizer (Day 4).

Reads SQLoop's OWN eval results from Phoenix via the Phoenix MCP server, clusters
the failures into named failure modes, and writes a report -- the input to the
Day 5 propose-and-validate loop.

Two phases, so the dev-network proxy never carries the MCP-server connection and
the Gemini connection at the same time (it can't -- they contend and Gemini gets
ConnectError):
  1. FETCH (MCP, no LLM): list-datasets -> list-experiments-for-dataset ->
     get-experiment-by-id, parsed into per-example {question, gold, pred, correct}.
  2. ANALYZE (one Gemini call, no MCP): cluster the failures, emit the report.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_MODEL = os.environ.get("OPTIMIZER_MODEL", os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest"))

# Where run_optimizer.py saves the MCP-derived failure-mode reports. Owned here so
# both the writer (run_optimizer.py) and the reader (the improvement loop) agree.
REPORTS_DIR = Path(__file__).resolve().parent.parent / "data" / "optimizer_reports"


def _mcp_server_params() -> StdioServerParameters:
    """Phoenix MCP server stdio params.

    Phoenix Cloud needs the proxy (direct from CN hits a 404 edge). phoenix-mcp is
    a Node app and Node's fetch ignores HTTP(S)_PROXY unless NODE_USE_ENV_PROXY=1
    (Node 20.13+/24), so we set it explicitly -- otherwise the server connects
    direct and every Phoenix call 404s.
    """
    base = os.environ["PHOENIX_COLLECTOR_ENDPOINT"]
    key = os.environ["PHOENIX_CLIENT_HEADERS"].split("api_key=", 1)[1].strip()
    return StdioServerParameters(
        command="npx",
        args=["-y", "@arizeai/phoenix-mcp@latest", "--baseUrl", base, "--apiKey", key],
        env={**os.environ, "NODE_USE_ENV_PROXY": "1"},
    )


def _text(res) -> str:
    return "\n".join(getattr(c, "text", "") for c in res.content)


async def _call(session: ClientSession, name: str, args: dict, tries: int = 6):
    """Call an MCP tool, parse JSON, retry on the transient 'fetch failed'."""
    last = ""
    for _ in range(tries):
        last = _text(await session.call_tool(name, args))
        if "fetch failed" not in last and last.strip():
            try:
                return json.loads(last)
            except json.JSONDecodeError:
                pass
        await asyncio.sleep(3)
    raise RuntimeError(f"MCP tool {name} failed: {last[:160]}")


# ---- Phase 1: fetch experiment results via Phoenix MCP ----------------------

async def fetch_experiment_rows(dataset_prefix: str = "spider-dev") -> tuple[dict, list[dict]]:
    """Pull the most recent spider-dev experiment's per-example results via MCP."""
    async with stdio_client(_mcp_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            datasets = await _call(session, "list-datasets", {})
            cands = [d for d in datasets if d.get("name", "").startswith(dataset_prefix)]
            if not cands:
                raise RuntimeError(f"no dataset starting with '{dataset_prefix}' in Phoenix")
            cands.sort(key=lambda d: d.get("created_at", ""), reverse=True)
            ds = cands[0]

            exps = await _call(session, "list-experiments-for-dataset", {"dataset_name": ds["name"]})
            if not exps:
                raise RuntimeError(f"no experiments for dataset {ds['name']}")
            exps.sort(key=lambda e: e.get("created_at", ""), reverse=True)
            exp = exps[0]

            full = await _call(session, "get-experiment-by-id", {"experiment_id": exp["id"]})
            results = full.get("experimentResult") or full.get("experiment_result") or []

            rows = []
            for run in results:
                ann = run.get("annotations") or []
                score = next((a.get("score") for a in ann if a.get("name") == "execution_accuracy"), None)
                rows.append({
                    "question": (run.get("input") or {}).get("question", ""),
                    "gold_sql": (run.get("reference_output") or {}).get("gold_sql", ""),
                    "pred_sql": (run.get("output") or {}).get("pred_sql", ""),
                    "correct": bool(score),
                })
            meta = {"dataset": ds["name"], "experiment_id": exp["id"], "total": len(rows)}
            return meta, rows


# ---- Phase 2: analyze failures with one Gemini call -------------------------

_ANALYSIS_PROMPT = """You are the Optimizer for SQLoop, a text-to-SQL agent. Below are
SQLoop's OWN failing runs (the natural-language question, the GOLD SQL, and SQLoop's
PREDICTED SQL), pulled from a Phoenix experiment. Cluster them into named failure modes
and write a report. Base everything strictly on the data -- never invent examples.

Assign each failure ONE mode from this fixed taxonomy:
- MISSING_JOIN            : needed a JOIN but didn't (or joined wrong tables)
- WRONG_COLUMN            : selected/filtered the wrong or non-existent column
- AGGREGATION_ERROR       : wrong/extra aggregate (COUNT/AVG/MAX...) or misuse
- GROUPBY_ERROR           : missing/incorrect GROUP BY
- ORDER_LIMIT_ERROR       : wrong ORDER BY / LIMIT (e.g. top-1 logic)
- VALUE_FILTER_ERROR      : wrong WHERE value/condition
- SYNTAX_OR_RUNTIME_ERROR : query failed to execute
- OTHER                   : none of the above

Output Markdown with EXACTLY these sections:

## Summary
- experiment: {experiment_id} | total runs: {total} | failed: {nfail} | execution accuracy: {acc}

## Failure modes (clustered)
For each mode that occurred:
### <MODE> — <count>
- Q: <question>
  - gold: <gold sql>
  - pred: <predicted sql>
  - why: <one-line root cause>

## Suggested fixes
- 2-4 concrete changes to the SQL Generator prompt or few-shot examples that would
  address the most common failure modes above.

=== FAILURES ===
{failures}
"""


async def analyze(meta: dict, rows: list[dict]) -> str:
    """One Gemini call: cluster the failures and produce the report."""
    from google import genai

    failures = [r for r in rows if not r["correct"]]
    fail_text = "\n".join(
        f"- Q: {f['question']}\n  gold: {f['gold_sql']}\n  pred: {f['pred_sql'] or '(empty)'}"
        for f in failures
    ) or "(no failures)"
    acc = f"{(meta['total'] - len(failures)) / meta['total']:.0%}" if meta["total"] else "n/a"
    prompt = _ANALYSIS_PROMPT.format(
        experiment_id=meta["experiment_id"], total=meta["total"],
        nfail=len(failures), acc=acc, failures=fail_text,
    )
    client = genai.Client()
    resp = await client.aio.models.generate_content(model=_MODEL, contents=prompt)
    return (resp.text or "").strip()


# ---- Bridge: feed the MCP-derived diagnosis INTO the improvement loop -------
# Without this, the Optimizer's report was a dead-end .md file. These let
# sqloop/loop.py consume it so trace-introspection actually drives the prompt
# revision (closing the self-improvement loop the project claims).

def latest_saved_report() -> str | None:
    """Text of the most recent failure-mode report (offline path), or None.

    run_optimizer.py reads the agent's OWN traces via Phoenix MCP and writes a
    report into REPORTS_DIR; the loop reads the latest one. This is the robust,
    network-free way the MCP diagnosis reaches propose().
    """
    if not REPORTS_DIR.exists():
        return None
    files = sorted(REPORTS_DIR.glob("report_*.md"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].read_text(encoding="utf-8") if files else None


def distill_report(report: str, max_chars: int | None = None) -> str:
    """Compress a failure-mode report to its high-signal, still-relevant parts.

    Keeps the Summary line, the clustered mode HEADERS (### MODE — count) and the
    Suggested fixes; drops the per-example Q/gold/pred/why dumps. Those dumps are
    (a) redundant -- the reflection prompt already passes the CURRENT failures
    separately -- and (b) stale, since a saved report is from a past experiment.
    The result is a few hundred chars instead of a few KB, so injecting it into the
    proposer call costs far fewer tokens while keeping the transferable diagnosis.

    max_chars (env SQLOOP_REPORT_MAX_CHARS, default 1200) is a hard safety cap;
    truncation snaps back to a line boundary so a fix bullet is never half-cut.
    """
    if not report:
        return ""
    if max_chars is None:
        max_chars = int(os.environ.get("SQLOOP_REPORT_MAX_CHARS", "1200"))
    out, section = [], None
    for line in report.splitlines():
        s = line.strip()
        if s.startswith("## "):
            section = s.lower()
            out.append(line)
        elif s.startswith("### "):          # failure-mode header + count: keep
            out.append(line)
        elif section and ("summary" in section or "suggested fixes" in section):
            if s:                            # keep summary line + fix bullets
                out.append(line)
        # else: under "Failure modes" -> per-example dump -> drop
    distilled = "\n".join(out).strip()
    if len(distilled) > max_chars:          # snap back to last whole line
        distilled = distilled[:max_chars].rsplit("\n", 1)[0].rstrip()
    return distilled


async def build_failure_report(dataset_prefix: str = "spider-dev") -> str | None:
    """Live path: read own traces via Phoenix MCP, cluster, return the report.

    Best-effort -- returns None if Phoenix/MCP is unreachable, so the loop still
    runs offline. Used by run_loop when SQLOOP_OPTIMIZER_LIVE=1.
    """
    try:
        meta, rows = await fetch_experiment_rows(dataset_prefix)
        return await analyze(meta, rows)
    except Exception:  # noqa: BLE001 - introspection is best-effort
        return None
