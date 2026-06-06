"""Heuristic (non-LLM) router -- a zero-cost drop-in for the LlmAgent router.

The LlmAgent router spends one LLM call per turn just to emit "sql"/"other". On
Spider every question is "sql", so in batch eval that call is ~1/3 of the per-turn
LLM budget spent on a near-constant answer -- the free-tier quota bottleneck called
out in PROGRESS (Day 2/3).

This agent classifies by keywords instead (greetings/chit-chat/meta -> "other",
everything else -> "sql", biased to "sql" since the system is a DB QA tool). It
writes `intent` to state exactly like the LLM router and keeps the name "router",
so the Phoenix span tree (router -> schema_linker -> ...) is unchanged -- only the
LLM call disappears.

Select via build_pipeline / env SQLOOP_ROUTER=heuristic (or "skip"). Default stays
"llm" for backward compatibility.
"""

from __future__ import annotations

import re
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

# Obvious non-database turns. Everything else is treated as a DB question.
_CHITCHAT = {"hi", "hello", "hey", "yo", "hiya", "thanks", "thank", "thx",
             "bye", "goodbye", "ok", "okay", "cool", "nice"}
_META = ("who are you", "what can you do", "what do you do", "how do you work",
         "what are you", "help me with")


def classify_intent(question: str) -> str:
    """"sql" for database questions, "other" for greetings/chit-chat/meta."""
    s = (question or "").strip().lower()
    if not s:
        return "other"
    if any(m in s for m in _META):
        return "other"
    toks = re.findall(r"[a-z']+", s)
    if toks and all(t in _CHITCHAT for t in toks):  # pure greeting / acknowledgement
        return "other"
    return "sql"


class HeuristicRouter(BaseAgent):
    """Write `intent` to state by keyword rules -- no LLM call, same span as router."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        question = ctx.session.state.get("question", "")
        intent = classify_intent(question)
        yield Event(author=self.name, actions=EventActions(state_delta={"intent": intent}))
