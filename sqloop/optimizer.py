"""Improvement-plane: the Optimizer agent (Day 4).

The Optimizer introspects SQLoop's own runs through the Phoenix MCP server: it
navigates datasets -> experiments -> failed runs, then clusters the failures
into named failure modes and writes a report. That report is the input to the
Day 5 "propose-and-validate" loop.

The Phoenix MCP server (`@arizeai/phoenix-mcp`) is wired in as an ADK toolset, so
every MCP call the Optimizer makes is itself traced.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

# Reasoning + multi-step tool use; override with OPTIMIZER_MODEL. Post-Vertex,
# use a stronger model here for better failure-mode clustering.
_MODEL = os.environ.get("OPTIMIZER_MODEL", os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest"))


# Only the tools the Optimizer needs. Phoenix MCP exposes 27 tools; exposing all
# of them bloats every LLM request (27 schemas) and hurts reliability on a flaky
# link, so we filter down to the dataset/experiment-reading tools.
_PHOENIX_TOOLS = [
    "list-datasets",
    "get-dataset",
    "get-dataset-examples",
    "list-experiments-for-dataset",
    "get-experiment-by-id",
]


def make_phoenix_toolset() -> McpToolset:
    """Phoenix MCP server as an ADK toolset (reads env; call after load_dotenv)."""
    base = os.environ["PHOENIX_COLLECTOR_ENDPOINT"]
    key = os.environ["PHOENIX_CLIENT_HEADERS"].split("api_key=", 1)[1].strip()
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="npx",
                args=["-y", "@arizeai/phoenix-mcp@latest", "--baseUrl", base, "--apiKey", key],
            )
        ),
        tool_filter=_PHOENIX_TOOLS,
    )


OPTIMIZER_INSTRUCTION = """You are the Optimizer for SQLoop, a text-to-SQL agent.
Your job is to read SQLoop's OWN evaluation results from Phoenix (via the Phoenix
MCP tools) and produce a failure-mode report grounded in real data. Never invent
examples -- every failure you cite must come from a tool result.

Use the Phoenix MCP tools in this order:
1. `list-datasets` -> find the dataset whose name starts with "spider-dev"
   (if several, use the most recent one).
2. `list-experiments-for-dataset` for that dataset -> pick the experiment the
   user names, or the most recent one.
3. `get-experiment-by-id` -> get the experiment runs. Each run has the example
   input (the natural-language question), the example output (the GOLD SQL), the
   task output (SQLoop's PREDICTED SQL), and the `execution_accuracy` evaluation.
4. Keep only the runs where execution_accuracy is false/0 (the failures).

For each failure, compare the question, predicted SQL, and gold SQL, and assign
ONE failure mode from this fixed taxonomy:
- MISSING_JOIN          : needed a JOIN but didn't join (or joined wrong tables)
- WRONG_COLUMN          : selected/filtered the wrong or non-existent column
- AGGREGATION_ERROR     : wrong/extra aggregate (COUNT/AVG/MAX...) or misuse
- GROUPBY_ERROR         : missing/incorrect GROUP BY
- ORDER_LIMIT_ERROR     : wrong ORDER BY / LIMIT (e.g. top-1 logic)
- VALUE_FILTER_ERROR    : wrong WHERE value/condition
- SYNTAX_OR_RUNTIME_ERROR : query failed to execute
- OTHER                 : none of the above

Then output a Markdown report with EXACTLY these sections:

## Summary
- experiment: <name/id>, total runs: <n>, failed: <k>, execution accuracy: <pct>

## Failure modes (clustered)
For each failure mode that occurred, a subsection:
### <MODE> — <count>
- Q: <question>
  - gold: <gold sql>
  - pred: <predicted sql>
  - why: <one-line root cause>

## Suggested fixes
- 2-4 concrete, specific changes to the SQL Generator prompt or few-shot
  examples that would address the most common failure modes above.

Be concise and concrete. Base everything strictly on the tool results."""


def build_optimizer() -> tuple[LlmAgent, McpToolset]:
    """Construct the Optimizer agent and its Phoenix MCP toolset.

    Returns (agent, toolset); the caller should close the toolset when done.
    """
    toolset = make_phoenix_toolset()
    agent = LlmAgent(
        model=_MODEL,
        name="optimizer",
        instruction=OPTIMIZER_INSTRUCTION,
        tools=[toolset],
    )
    return agent, toolset
