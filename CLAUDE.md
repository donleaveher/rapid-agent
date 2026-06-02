# CLAUDE.md — rapid-agent (SQLoop)

> 这是项目的"宪法"。Claude Code 每次会话自动读取本文件。请严格遵守这里的约束。

## 项目目标

参赛 **Google Cloud Rapid Agent Hackathon — Arize track**(截止 2026-06-11 14:00 PDT / 北京时间 06-12 05:00)。

构建 **SQLoop**:一个**自我改进的 text-to-SQL agent**。核心卖点不是"能把自然语言转 SQL",而是 **agent 能通过 Phoenix MCP server 读取自己的运行 trace,诊断失败模式,自动改进自己的 prompt/few-shot,并用实验验证改进有效**。

Arize track 评分项(务必每条都对应得上):
1. technical implementation
2. **tracing + MCP 的深度使用**
3. **self-improvement loop 的质量** ← 最大差异化点
4. overall impact

## 架构(两个平面 + 一个闭环)

```
任务平面 (Task plane):
  User query
    → Router agent (意图分发)
    → Schema Linker (对 DB schema 做检索/RAG)
    → SQL Generator (Gemini 生成 SQL)
    → Executor (沙箱执行 SQL)
    → Repair (出错/空结果时 ReAct 式重写)
    → Answer
  全程 OpenInference 埋点 → Phoenix

优化平面 (Improvement plane):
  Optimizer agent (挂 Phoenix MCP server 作为工具)
    → 拉失败 spans
    → 按失败模式聚类 (缺 JOIN / 列名幻觉 / 聚合错 ...)
    → 从成功 trace 挖 few-shot 范例 + 改写 generator prompt → 候选
    → 候选 vs baseline 在 held-out 集上跑 Phoenix experiment
    → commit-if-better (赢了才采纳)

闭环目标产物:一条随迭代上升的 execution-accuracy 曲线 (这是 demo 高光帧)
```

数据集:**Spider** text-to-SQL benchmark(有 gold SQL)。
评估指标:**execution accuracy**(执行结果集与 gold 是否一致),纯 code eval。

## 技术栈(锁定,不要替换)

- **Runtime**: Google ADK (Python) —— Arize track 要求 code-owned agent,**禁止用纯低代码 Agent Builder**
- **LLM**: Gemini (`gemini-flash-latest` 开发,正式生成可换 `gemini-2.0-flash` 或更强)
- **Observability**: Arize Phoenix (Cloud 免费版) + OpenInference 自动埋点
- **MCP**: `@arizeai/phoenix-mcp`(Optimizer 的工具集)
- **包管理**: **uv**(不要用 pip;加包用 `uv add`,跑脚本用 `uv run python xxx.py`)
- **DB**: SQLite(Spider 数据集自带 sqlite 库),执行用 `sqlean-py` 或标准 sqlite3
- **UI**(Day 7): Gradio 或 Streamlit,展示问答 + accuracy 曲线 + 失败模式

## LLM 后端:双配置策略

开发期用免费 AI Studio key(省钱),**提交前切 Vertex**(满足"用 Google Cloud + 花 $100 credits"的合规要求)。

由 `.env` 里的 `GOOGLE_GENAI_USE_VERTEXAI` 控制切换:

```
# 开发(免费,默认)
GOOGLE_GENAI_USE_VERTEXAI=FALSE
GOOGLE_API_KEY=<AI Studio key>

# 提交前切换(注释掉上面,启用下面)
# GOOGLE_GENAI_USE_VERTEXAI=TRUE
# GOOGLE_CLOUD_PROJECT=<project-id>
# GOOGLE_CLOUD_LOCATION=us-central1
# (Vertex 模式需先在终端跑 `gcloud auth application-default login`)
```

## 已验证的配置(Day 0 实测通过,直接用)

```
PHOENIX_COLLECTOR_ENDPOINT=https://app.phoenix.arize.com/s/c2303372901
PHOENIX_CLIENT_HEADERS=api_key=<phoenix key>
```

**坑与对策(已踩过,务必遵守):**
1. Phoenix endpoint **必须带 space 路径** `/s/c2303372901`,否则 401 Unauthorized。
2. Phoenix 认证 **必须设 `PHOENIX_CLIENT_HEADERS=api_key=xxx`**,否则 trace header 为空 → 401。
2b. (Day 1 实测)`phoenix.otel.register()` **不会**自动把 `PHOENIX_CLIENT_HEADERS` 应用到 OTLP span 导出器 → 大 span 批次报 `401 Unauthorized` / `Response ended prematurely`(Day 0 那条单条小 span 走的是另一条路径,所以当时没暴露)。**对策:从 env 解析出 key 显式传 `register(api_key=...)`**(见 `sqloop/instrumentation.py::_api_key_from_env`),同时建议显式传 `protocol="http/protobuf"` 消除协议推断告警。
3. genai 自动埋点 + 短生命周期脚本会报 `client has been closed`。对策:正式代码用**持久化的 genai client**(不要每次新建)+ **BatchSpanProcessor**(不要 SimpleSpanProcessor),并在程序退出前 `force_flush()`。
4. 国内网络:`uv` 走清华镜像(已在 pyproject.toml 配 `[[tool.uv.index]]`);Gemini/Phoenix 走各自服务器,镜像帮不上,延迟偏高属正常。
5. (Day 1 实测)免费 AI Studio key 的**可用模型**:`gemini-flash-latest` 持续 503 高负载、`gemini-2.0-flash` quota=0 不可用;`gemini-3.1-flash` **不存在**(只有 `-image`/`-tts`/`-lite` 变体)。实测稳定可用:**`gemini-3.5-flash`(当前默认,最新)/ `gemini-2.5-flash` / `gemini-3-flash-preview` / `gemini-3.1-flash-lite`**。用 `GEMINI_MODEL` 环境变量切换。

## 硬性约束 / Claude Code 工作守则

- **绝不把 `.env` 提交进 git**(含真实 key)。`.gitignore` 必须含 `.env`。提交进 repo 的是 `.env.example`(占位符)。
- repo 必须有 **MIT LICENSE 文件**(提交要求),且在 GitHub About 区可见。
- 每完成一个 Day 的里程碑,在 `PROGRESS.md` 勾掉对应项并简述结果。
- 写代码优先小步可验证:每加一个 agent/模块,先用 1~2 条 query 跑通再继续。
- 不确定 ADK / Phoenix API 用法时,**查官方文档或读 `gemini-hackathon` 脚手架**,不要凭记忆编 API。
- 涉及钱/配额的操作(切 Vertex、跑大批量 eval)先告诉我再执行。
- 用中文跟我交流;代码注释和 commit message 用英文。

## 关键时间节点

- **2026-06-04**:提交 $100 GCP credits 申请表(审批 1-5 工作日)
- **2026-06-09**:Vertex 后端切换 + 端到端复测的最晚日
- **2026-06-11(北京时间晚)**:完成最终提交,不卡点

## 参考资料

- Arize track 资源:https://rapid-agent.devpost.com/details/arize-resources
- 官方脚手架:https://github.com/Arize-ai/gemini-hackathon (traced Gemini + Phoenix MCP + evals)
- ADK 文档:https://google.github.io/adk-docs/
- Phoenix MCP 指南:https://arize.com/docs/phoenix/integrations/phoenix-mcp-server
- Spider 数据集:https://yale-lily.github.io/spider
