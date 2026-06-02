"""Propose step of the self-improvement loop (hybrid).

- few-shots: deterministically mined from successful eval rows (offline, reliable).
- prompt: rewritten by an LLM proposer that reads the real failures (intelligent),
  with a deterministic rule-append fallback if the LLM is unavailable or drops the
  required {intent}/{schema} placeholders.

Produces a candidate GeneratorConfig (version "v1") for A/B testing.
"""

from __future__ import annotations

import os
import re

from sqloop.config import GeneratorConfig
from sqloop.prompts import SQL_GENERATOR_INSTRUCTION

_PROPOSER_MODEL = os.environ.get("PROPOSER_MODEL", os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest"))


def _sql_shape(sql: str) -> tuple[str, ...]:
    s = (sql or "").lower()
    feats = []
    if " join " in s:
        feats.append("join")
    if "group by" in s:
        feats.append("groupby")
    if "order by" in s:
        feats.append("orderby")
    if "limit" in s:
        feats.append("limit")
    if re.search(r"\b(count|avg|sum|max|min)\s*\(", s):
        feats.append("agg")
    if " where " in s:
        feats.append("where")
    return tuple(sorted(set(feats)))


def mine_few_shots(rows: list[dict], k: int = 4) -> list[dict]:
    """Pick up to k diverse correct examples (by SQL shape) as few-shots (gold SQL)."""
    correct = [r for r in rows if r.get("correct")]
    picked, seen = [], set()
    for r in correct:  # first pass: one per distinct SQL shape
        shape = _sql_shape(r["gold_sql"])
        if shape not in seen:
            seen.add(shape)
            picked.append({"question": r["question"], "sql": r["gold_sql"]})
        if len(picked) >= k:
            return picked
    for r in correct:  # second pass: fill remaining slots
        fs = {"question": r["question"], "sql": r["gold_sql"]}
        if fs not in picked:
            picked.append(fs)
        if len(picked) >= k:
            break
    return picked


def _deterministic_rules() -> str:
    return (
        "\n\nCommon mistakes to avoid:\n"
        "- Select EXACTLY the columns the question asks for (e.g. the SONG name, not the"
        " singer's name); never substitute a similarly named column.\n"
        "- If a column is already a stored value (e.g. one literally named 'Average'),"
        " return it directly; do not wrap it in another aggregate like AVG().\n"
        "- For superlatives ('youngest', 'highest'), use ORDER BY <col> [DESC] LIMIT 1.\n"
        "- JOIN the needed tables when the answer spans more than one table."
    )


async def _llm_rewrite_prompt(base_prompt: str, failures: list[dict]) -> str:
    """One LLM call: rewrite the generator prompt to address real failures."""
    from google import genai

    fail_text = "\n".join(
        f"- Q: {f['question']}\n  gold: {f['gold_sql']}\n  pred: {f['pred_sql']}"
        for f in failures[:8]
    )
    meta = (
        "You improve the system prompt of a text-to-SQL generator. Below is the CURRENT "
        "prompt, then real FAILURES (the question, the correct gold SQL, and the wrong "
        "predicted SQL).\n\n"
        "Rewrite the prompt so the generator would avoid these mistakes. Keep it concise. "
        "You MUST keep the literal placeholders {intent} and {schema} exactly where they "
        "make sense. Output ONLY the new prompt text, nothing else.\n\n"
        f"=== CURRENT PROMPT ===\n{base_prompt}\n\n=== FAILURES ===\n{fail_text}\n"
    )
    client = genai.Client()
    resp = await client.aio.models.generate_content(model=_PROPOSER_MODEL, contents=meta)
    return (resp.text or "").strip()


async def propose_candidate(rows: list[dict], k: int = 4, use_llm: bool = True) -> GeneratorConfig:
    """Build a candidate GeneratorConfig from eval rows (few-shots + improved prompt)."""
    base = SQL_GENERATOR_INSTRUCTION
    few = mine_few_shots(rows, k)
    failures = [r for r in rows if not r.get("correct")]
    note = f"mined {len(few)} few-shots; "

    prompt = base
    if use_llm and failures:
        try:
            rewritten = await _llm_rewrite_prompt(base, failures)
            if "{schema}" in rewritten and "{intent}" in rewritten:
                prompt = rewritten
                note += "prompt rewritten by LLM proposer"
            else:  # LLM dropped placeholders -> unsafe, fall back
                prompt = base + _deterministic_rules()
                note += "LLM output missing placeholders -> deterministic rules"
        except Exception as exc:  # noqa: BLE001
            prompt = base + _deterministic_rules()
            note += f"LLM rewrite failed ({type(exc).__name__}) -> deterministic rules"
    else:
        prompt = base + _deterministic_rules()
        note += "deterministic rules"

    return GeneratorConfig(prompt=prompt, few_shots=few, version="v1", notes=note)
