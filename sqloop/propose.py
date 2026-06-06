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


async def _proposer_complete(prompt: str) -> str:
    """Single LLM completion for the proposer, on the active backend.

    DeepSeek (dev validation) avoids the proxy; Gemini is the default/submission.
    """
    if os.environ.get("LLM_BACKEND", "gemini").lower() == "deepseek":
        import litellm

        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
        resp = await litellm.acompletion(
            model=f"openai/{model}",
            api_base="https://api.deepseek.com",
            api_key=os.environ["DEEPSEEK_API_KEY"],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
        return resp.choices[0].message.content or ""
    from google import genai

    client = genai.Client()
    resp = await client.aio.models.generate_content(model=_PROPOSER_MODEL, contents=prompt)
    return resp.text or ""


async def _llm_reflective_guidance(failures: list[dict], failure_report: str | None = None) -> str:
    """One LLM call: a SHORT additive 'guidance' block reflecting on real failures.

    GEPA-style reflection, but ADDITIVE (appended to the proven base prompt) rather
    than a full rewrite -- a full rewrite regressed 80%->68% in an earlier round.

    If `failure_report` is given (the Optimizer's failure-mode analysis, clustered
    from the agent's OWN Phoenix traces via MCP), it is injected as grounding so the
    rules reflect the MCP-derived diagnosis -- this is what closes the trace ->
    introspection -> prompt-revision loop.
    """
    fail_text = "\n".join(
        f"- Q: {f['question']}\n  gold: {f['gold_sql']}\n  pred: {f['pred_sql'] or '(empty)'}"
        for f in failures[:8]
    )
    meta = (
        "You tune a text-to-SQL generator. Below are real FAILURES (question, correct "
        "gold SQL, wrong predicted SQL). Write a SHORT block of 3-6 concrete bullet rules "
        "that would prevent these specific mistakes. Output ONLY the bullets, no preamble. "
        "Do NOT use the characters '{' or '}'.\n\n"
    )
    if failure_report:
        from sqloop.optimizer import distill_report  # local import: avoid load cycle

        digest = distill_report(failure_report)  # modes + fixes only (token-lean)
        if digest:
            meta += (
                "A failure-mode analysis of this agent's OWN past runs -- clustered from its "
                "Phoenix traces via the MCP server -- is provided below. Ground your rules in "
                "its named failure modes and suggested fixes.\n\n"
                f"=== FAILURE-MODE ANALYSIS (from traces via Phoenix MCP) ===\n{digest}\n\n"
            )
    meta += f"=== FAILURES ===\n{fail_text}\n"
    bullets = (await _proposer_complete(meta)).strip()
    if not bullets or "{" in bullets:
        return ""
    return "\n\nAdditional guidance (learned from past failures):\n" + bullets


_RULES_MARKER = "Common mistakes to avoid"


async def propose_candidates(
    rows: list[dict], incumbent: GeneratorConfig | None = None, k: int = 4, use_llm: bool = True,
    failure_report: str | None = None, mine_demos: bool | None = None,
) -> list[GeneratorConfig]:
    """Generate a diverse POOL of candidate configs (GEPA/MIPRO style).

    Candidates EXTEND the incumbent (the current best config), additively -- never a
    full rewrite (that regressed 80%->68%). The caller ranks candidates on a
    validation set and keeps the best (see sqloop/loop.py).

    `rows` are the incumbent's current eval rows: successes seed new few-shots,
    failures seed the reflective guidance.

    mine_demos owns the success/example lane: when True (default, or env
    SQLOOP_PROPOSE_DEMOS=on) successes are mined into static few-shots and a `demos`
    candidate is produced. Set False (SQLOOP_PROPOSE_DEMOS=off) when the deployment
    uses the History Memory (sqloop/memory.py) instead -- memory retrieves success
    examples per-question (instance-level, dynamic), which subsumes static mining, so
    propose stays in the ABSTRACT lane (rules + reflective guidance from FAILURES) and
    the two don't duplicate the few-shot channel. See the 2x2: memory = success x
    instance, propose/optimizer = failure x abstract.
    """
    if mine_demos is None:
        mine_demos = os.environ.get("SQLOOP_PROPOSE_DEMOS", "on").lower() != "off"
    if incumbent is None:
        incumbent = GeneratorConfig(prompt=SQL_GENERATOR_INSTRUCTION, few_shots=[], version="v0")
    base_prompt = incumbent.prompt

    if mine_demos:
        # New few-shots from current successes, excluding ones the incumbent already has.
        have = {fs["question"] for fs in incumbent.few_shots}
        mined = [fs for fs in mine_few_shots(rows, k * 2) if fs["question"] not in have][:k]
    else:
        mined = []  # memory owns success-example retrieval; don't duplicate the lane
    few = incumbent.few_shots + mined
    failures = [r for r in rows if not r.get("correct")]

    candidates = []
    if mine_demos:
        candidates.append(GeneratorConfig(prompt=base_prompt, few_shots=few, version="demos",
                          notes=f"+{len(mined)} few-shots (MIPRO-style demos)"))
    if _RULES_MARKER not in base_prompt:  # don't append the rules block twice
        candidates.append(GeneratorConfig(prompt=base_prompt + _deterministic_rules(), few_shots=few,
                          version="rules", notes=f"+{len(mined)} few-shots + deterministic rules"))
    if use_llm and failures:
        try:
            block = await _llm_reflective_guidance(failures, failure_report=failure_report)
            if block:
                src = " (guided by Phoenix-MCP failure report)" if failure_report else ""
                candidates.append(GeneratorConfig(prompt=base_prompt + block, few_shots=few,
                                  version="reflect",
                                  notes=f"+{len(mined)} few-shots + LLM reflective guidance{src}"))
        except Exception:  # noqa: BLE001 - reflection is best-effort
            pass
    if not candidates:  # e.g. demos off + rules already present + no failures this round
        candidates.append(GeneratorConfig(prompt=base_prompt, few_shots=few, version="incumbent",
                          notes="no new abstract candidate this round"))
    return candidates
