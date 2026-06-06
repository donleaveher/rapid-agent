"""SQLoop dashboard (Day 7): ask the agent + see it self-improve.

Sections:
  1. Ask SQLoop  -- natural language -> generated SQL -> executed result + answer.
  2. Self-improvement -- the rising execution-accuracy curve (Gemini + DeepSeek,
     flash vs pro), the Optimizer's failure-mode report, and what the loop added.

Run:
  uv run python app.py                       # uses LLM_BACKEND (default gemini)
  LLM_BACKEND=deepseek uv run python app.py  # stable local demo (no proxy)
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import gradio as gr
import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from main import run_turn_detailed
from sqloop.config import active_config, baseline_config
from sqloop.memory import Memory
from sqloop.spider import db_path_for, dev_examples

DATA = Path(__file__).resolve().parent / "data"
DB_IDS = sorted({e["db_id"] for e in dev_examples()})
DEFAULT_DB = "concert_singer" if "concert_singer" in DB_IDS else DB_IDS[0]
MEM = Memory()  # history memory: reuse + retrieval few-shots (built by build_memory.py)


# ---- helpers ---------------------------------------------------------------

def _run_sql(db_path: str, sql: str):
    """Execute SQL; return (columns, rows) or (None, error_str)."""
    if not sql.strip():
        return None, "(no SQL generated)"
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        conn.close()
        return cols, rows[:200]
    except Exception as exc:  # noqa: BLE001
        return None, f"SQL error: {exc}"


async def ask(question: str, db_id: str):
    if not question.strip():
        return "", gr.update(value=None), "Type a question first.", ""
    db_path = str(db_path_for(db_id))
    try:
        out = await run_turn_detailed(question, db_path, db_id, memory=MEM)
    except Exception as exc:  # noqa: BLE001
        return "", gr.update(value=None), f"Pipeline error: {str(exc).splitlines()[-1][:200]}", ""
    sql = out.get("pred_sql", "")
    cols, rows = _run_sql(db_path, sql)
    if cols is None:
        table = pd.DataFrame({"result": [rows]})
    else:
        table = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    if out.get("source") == "memory":
        status = f"🧠 **reused from memory** (skipped generation) · memory size: {len(MEM)}"
    else:
        n_shots = len(MEM.retrieve(question, db_id))
        status = (f"⚙️ generated"
                  + (f" · 🧠 {n_shots} similar past solution(s) used as few-shots" if n_shots else "")
                  + f" · memory size: {len(MEM)}")
    return sql or "(none)", table, out.get("answer", ""), status


def schema_text(db_id: str) -> str:
    conn = sqlite3.connect(db_path_for(db_id))
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name"
    ).fetchall()
    conn.close()
    return "\n\n".join(r[0] for r in rows)


def _load_curve(name: str, series: str) -> list[dict]:
    p = DATA / name
    if not p.exists():
        return []
    return [{"round": d["round"], "accuracy": round(d["held_out_acc"] * 100, 1),
             "model": series} for d in json.loads(p.read_text())]


def curve_df() -> pd.DataFrame:
    # Both backends: Gemini (submission, on Vertex) + DeepSeek (dev cross-check).
    rows = (_load_curve("curve.vertex.json", "Gemini flash (submission)") +
            _load_curve("curve.vertex_pro.json", "Gemini pro") +
            _load_curve("curve.flash.json", "DeepSeek flash (dev)") +
            _load_curve("curve.pro.json", "DeepSeek pro (dev)"))
    return pd.DataFrame(rows or [{"round": 0, "accuracy": 0, "model": "n/a"}])


def latest_report() -> str:
    reports = sorted((DATA / "optimizer_reports").glob("*.md")) if (DATA / "optimizer_reports").exists() else []
    if not reports:
        return "_No optimizer report yet. Run `run_optimizer.py`._"
    return reports[-1].read_text(encoding="utf-8")


def config_diff() -> str:
    base, act = baseline_config(), active_config()
    if act.version == base.version and not act.few_shots:
        return "_No committed improvement yet (active config = baseline). Run `run_loop.py`._"
    lines = [f"**Baseline (v0)** → **Active (`{act.version}`)**  ·  {act.notes}", "",
             f"- Few-shot examples added: **{len(act.few_shots)}**",
             f"- Prompt guidance/rules appended: **{'yes' if len(act.prompt) > len(base.prompt) else 'no'}**", ""]
    if act.few_shots:
        lines.append("Few-shots the loop mined from successful runs:")
        for fs in act.few_shots[:6]:
            lines.append(f"- Q: {fs['question']}\n  - SQL: `{fs['sql']}`")
    return "\n".join(lines)


# ---- UI --------------------------------------------------------------------

# Gradio applies dark mode via a `dark` class on <body>; toggle it in place
# (no page reload, works instantly).
_THEME_TOGGLE_JS = "() => { document.body.classList.toggle('dark'); }"

with gr.Blocks(title="SQLoop") as demo:
    with gr.Row():
        gr.Markdown("# 🔁 SQLoop — a self-improving text-to-SQL agent\n"
                    "Router → Schema Linker → SQL Generator → Executor, fully traced to Phoenix; "
                    "an Optimizer reads its own traces and improves the prompt.")
        theme_btn = gr.Button("🌗 Light / Dark", scale=0, min_width=130)
    theme_btn.click(None, None, None, js=_THEME_TOGGLE_JS)

    with gr.Tab("Ask SQLoop"):
        with gr.Row():
            db_in = gr.Dropdown(DB_IDS, value=DEFAULT_DB, label="Database (Spider)")
        q_in = gr.Textbox(label="Question", placeholder="How many singers do we have?", lines=2)
        ask_btn = gr.Button("Ask", variant="primary")
        status_out = gr.Markdown()
        sql_out = gr.Code(label="Generated SQL", language="sql")
        ans_out = gr.Textbox(label="Answer")
        res_out = gr.Dataframe(label="Query result")
        with gr.Accordion("Database schema", open=False):
            schema_out = gr.Code(value=schema_text(DEFAULT_DB), language="sql")
        gr.Examples(
            [["How many singers do we have?", "concert_singer"],
             ["What are the names and release years for all the songs of the youngest singer?", "concert_singer"]],
            inputs=[q_in, db_in])

        ask_btn.click(ask, [q_in, db_in], [sql_out, res_out, ans_out, status_out])
        db_in.change(schema_text, db_in, schema_out)

    with gr.Tab("Self-improvement"):
        gr.Markdown("### Execution-accuracy curve (held-out, 100 questions / 20 DBs)\n"
                    "Each round: read failures → propose prompt/few-shot candidates → "
                    "validate → commit only if better. **Gemini** is the submission backend "
                    "(on Vertex); **DeepSeek** is the dev cross-check. Weak models climb as they "
                    "self-improve; a strong model already near the ceiling (Gemini pro, 87%) stays "
                    "flat — and the commit gate honestly commits nothing rather than faking a rise.")
        gr.LinePlot(curve_df, x="round", y="accuracy", color="model",
                    x_title="self-improvement round", y_title="execution accuracy (%)",
                    height=360, every=None)
        with gr.Row():
            with gr.Column():
                gr.Markdown("### What the loop changed (baseline → active)")
                gr.Markdown(config_diff)
            with gr.Column():
                gr.Markdown("### Optimizer failure-mode report (from real Phoenix traces)")
                gr.Markdown(latest_report)


if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7860)
