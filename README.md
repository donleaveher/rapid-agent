# 🔁 SQLoop — a self-improving text-to-SQL agent

SQLoop turns natural-language questions into SQL **and improves itself**. It isn't
just "NL → SQL": an Optimizer reads the agent's *own* execution traces through the
**Arize Phoenix MCP server**, clusters its failure modes, proposes prompt / few-shot
changes, A/B-validates them on a held-out set, and **keeps a change only if it wins**.
The result is a rising execution-accuracy curve produced entirely by the agent.

Built for the **Google Cloud Rapid Agent Hackathon (Arize track)** with Google ADK +
Gemini, OpenInference tracing to Phoenix, and the Phoenix MCP server.

## Why it's interesting

- **A real closed loop**, not a single pass: trace → diagnose → propose → validate → commit-if-better.
- **Deep tracing + MCP**: every Router/Schema-Linker/Generator/Executor/Repair step is a Phoenix span; the Optimizer pulls its own experiment results back *via MCP*.
- **Evidence, not vibes**: Spider-style execution accuracy with 95% confidence intervals.

Self-improvement result (DeepSeek-flash dev validation, 100 held-out questions / 20 DBs, greedy decoding):

| round | 0 (baseline) | 1 | 2 | 3 |
|------|------|------|------|------|
| execution accuracy | 60% (CI 50–69) | **77%** (CI 68–84) | 77% | 77% |

The weaker model gains **+17%** from self-improvement and nearly catches a stronger
model (DeepSeek-pro: 67 → 79). Improvement comes from *transferable* learned rules /
retrieved few-shots, so it generalizes across databases.

## Architecture

![SQLoop architecture](docs/architecture.svg)

- **Dataset / metric**: Spider; execution accuracy (compare result sets of predicted vs gold SQL).
- **Self-improvement extras**: history memory (adaptive hybrid retrieval), real schema linking, ReAct repair with a loop guard, rigorous eval with confidence intervals.

## Quickstart

```bash
# 1. install (uv). If the configured mirror times out, append:
#    --index-url https://pypi.org/simple
uv sync

# 2. configure secrets
cp .env.example .env        # fill GOOGLE_API_KEY + PHOENIX_* (see comments)

# 3. download the Spider dataset (~200 MB) into data/spider/
uv run gdown 1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J -O data/spider.zip
unzip -q data/spider.zip -d data/_x && mv data/_x/spider_data data/spider && rm -rf data/_x data/spider.zip

# (optional, for the Day-1 toy DB used by `main.py` with no db_id)
uv run python seed_db.py
```

## Usage

```bash
# Ask one question (NL → SQL → result), traced to Phoenix
uv run python main.py "How many singers do we have?" concert_singer

# Baseline execution accuracy on a Spider sample (+95% CI)
uv run python run_eval.py 20

# Log an experiment to Phoenix Experiments (dataset + task + evaluator)
uv run python run_experiment.py 20 "baseline"

# Optimizer: read your own failures via Phoenix MCP → failure-mode report
uv run python run_optimizer.py

# One self-improvement round (propose → validate → A/B → commit-if-better)
uv run python run_improve.py

# Full multi-round rising curve  (writes data/curve.json)
uv run python run_loop.py 3 --multidb

# Dashboard: Q&A + the accuracy curve + failure-mode report
uv run python app.py            # http://127.0.0.1:7860
```

### Phoenix MCP usage

The Optimizer (`sqloop/optimizer.py`) launches the Phoenix MCP server
(`npx @arizeai/phoenix-mcp`) and uses its tools — `list-datasets`,
`list-experiments-for-dataset`, `get-experiment-by-id` — to pull the agent's own
experiment results, then a single Gemini call clusters the failures into a report.
Fetch (MCP) and analysis (LLM) run in two phases so the agent never has to hold
both connections at once.

> Note: `@arizeai/phoenix-mcp` is a Node app; set `NODE_USE_ENV_PROXY=1` if you are
> behind an HTTP proxy (handled automatically in `optimizer.py`).

## LLM backend

- **Default = Gemini** (the submission backend; runs on Vertex AI — see `.env.example`).
- **`LLM_BACKEND=deepseek`** routes through LiteLLM to DeepSeek for *dev validation
  only* (reachable without a proxy, no free-tier rate limit). All submission numbers
  are produced on Gemini.

## Project layout

```
sqloop/
  agent.py          task pipeline (build_pipeline); Gemini/DeepSeek backend switch
  schema_linker.py  + schema_link.py   relevant-table retrieval (+FK closure)
  db.py             SQLite executor (execute_sql tool)
  repair.py         conditional ReAct repair step
  memory.py         history memory: reuse + adaptive hybrid retrieval
  optimizer.py      Optimizer: Phoenix MCP fetch + failure-mode analysis
  propose.py        candidate-pool proposer (GEPA/MIPRO-style, additive)
  loop.py           one self-improvement round
  eval.py           Spider-style execution match + Wilson CI
  config.py         swappable GeneratorConfig (prompt + few-shots)
  instrumentation.py  Phoenix tracing setup
main.py  run_eval.py  run_experiment.py  run_optimizer.py  run_improve.py  run_loop.py
build_memory.py  app.py  seed_db.py
```

## License

MIT — see [LICENSE](LICENSE).
