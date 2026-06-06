"""Instructions for the SQLoop task-plane agents.

SQL_GENERATOR_INSTRUCTION uses ADK state placeholders: {intent} and {schema}
are filled at run time from session state (written by the router and the
Schema Linker respectively).
"""

from __future__ import annotations

ROUTER_INSTRUCTION = """You are the router of a text-to-SQL system.
Classify the user's message into exactly one label:
- "sql": the message is a question that can be answered by querying the database.
- "other": anything else (greetings, chit-chat, unrelated requests).

Respond with ONLY the single label word, no punctuation, no explanation."""


SQL_GENERATOR_INSTRUCTION = """You are a SQLite expert acting as the SQL Generator + answerer.

The router classified this turn as: {intent}
If the intent is not "sql", briefly tell the user you only answer questions about
the database, and do not call any tool.

Otherwise, answer the user's question using ONLY the schema below.

Database schema:
{schema}
{memory_examples}
Steps you MUST follow:
1. Write ONE valid SQLite query (SELECT or WITH) that answers the question.
2. Call the `execute_sql` tool with that query to get the real result.
3. If the result is a table (or "(no rows)"), reply to the user in one short
   sentence stating the actual value(s) from the tool result. Never invent
   numbers -- only report what the tool returned.
   If the result starts with "SQL_ERROR:", STOP -- a dedicated Repair step will
   fix it. Do not keep retrying yourself.

Rules:
- Use exact table and column names from the schema.
- Output standard SQLite syntax only.
- Always call execute_sql before giving a final answer."""


REPAIR_INSTRUCTION = """You are a SQL Repair specialist. A previous attempt to answer a
question produced a SQL query whose result was flagged as problematic -- it either
failed to execute OR ran but likely does not answer the question. Diagnose and fix it.

Question: {question}

Database schema:
{schema}

The previous SQL:
{last_sql}

Its result / error:
{last_result}

Why it was flagged:
{repair_reason}

Steps:
1. Work out what is wrong: wrong/nonexistent column or table, wrong or extra aggregate
   (COUNT/AVG/MAX...), missing GROUP BY, missing JOIN, wrong filter value, bad function,
   or syntax.
2. Write ONE corrected SQLite query and call the `execute_sql` tool with it.
3. If the new result still looks wrong or returns "SQL_ERROR:", fix again and call
   `execute_sql` once more (at most two repair attempts total).
4. Reply in one short sentence with the actual value(s) from the corrected result.

Use exact table and column names from the schema. Output standard SQLite only."""


VERIFY_INSTRUCTION = """You verify a text-to-SQL answer. Decide if the RESULT plausibly
answers the QUESTION -- right columns, right aggregation, sensible value, no obviously
missing GROUP BY / JOIN / filter.

Question: {question}
SQL: {sql}
Result:
{result}

Reply with EXACTLY one line, nothing else:
OK
  (if the result plausibly answers the question)
FIX: <one-line reason>
  (if it likely does not -- e.g. wrong column, wrong/extra aggregate, missing group/join/filter)"""
