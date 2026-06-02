# PROGRESS.md — SQLoop 开发进度看板

> 与 CLAUDE.md 配套。每完成一项打勾 `[x]` 并在后面补一句结果。
> 剩余时间紧(~11 天),原则:**先保闭环跑通,再加花活**。任何一天卡住,优先砍任务平面的复杂度(Repair / 多分支 Router),保住 self-improvement loop——它是单列打分项。

---

## Day 0 — 环境与可行性验证 ✅ 已完成 (2026-05-31)

- [x] uv 项目初始化 + 依赖安装(走清华镜像解决超时)
- [x] ① Gemini 可达(AI Studio key,返回 pong)
- [x] ② Phoenix trace 链路通(kill-switch-test span 已入库)
- [x] ③ Phoenix MCP server 能启动(running on stdio)
- [x] `.env` + `.gitignore` 配好(key 持久化,不进 git)
- [x] clone 官方脚手架(2026-06-02):clone 到 repo 外部 `/Users/tourbillion/gemini-hackathon-ref`,不污染本 repo git。已读懂范式:① ADK 单轮跑法 `InMemoryRunner(agent) → create_session → run_async(types.Content(role="user"))`(`agent/main.py`);② Phoenix 注册 `register(project_name, batch=False, auto_instrument=True)`(`agent/instrumentation.py`,用全局 `_provider` 做幂等);③ Agent 定义 `Agent(model, name, instruction, tools=[FunctionTool(func=...)])`(`agent/shopping_demo/agent.py`),tool 即带 docstring+类型注解的 async 函数;④ MCP server 接法在 `.gemini/settings.json`(`npx @arizeai/phoenix-mcp@latest --baseUrl ... --apiKey ...`)。⚠️ 注意:脚手架 `batch=False` 适合长跑 CLI,我们批量 eval 要按坑#3 改用 BatchSpanProcessor+force_flush;脚手架还依赖 `python-dotenv`(我们 pyproject 暂缺,Day1 需 `uv add`)。

---

## Day 1 — ADK 任务骨架 (目标:单条 query 能跑出 SQL 并执行) ✅ 已完成 (2026-06-02)

- [x] 用 ADK 搭最小 agent:Router → SQL Generator → Executor(`SequentialAgent`,见 `sqloop/agent.py`)。Router=LlmAgent(output_key=intent);sql_generator=LlmAgent 挂 `execute_sql` 工具;Executor=`sqloop/db.py::execute_sql`(只读 SELECT/WITH)。
- [x] 本地 SQLite 测试库:`seed_db.py` → `data/sqloop_test.db`(users 5 行 / orders 6 行),NL→SQL→执行→返回结果跑通。
- [x] OpenInference ADK 自动埋点 + 持久 provider + **BatchSpanProcessor(batch=True)+ force_flush**(`sqloop/instrumentation.py`)。
- [x] span 在 Phoenix `rapid-agent` 可见,层级完整:`invocation[sqloop] → agent_run[sqloop_pipeline] → {router, sql_generator → call_llm → execute_tool execute_sql}`。
- **验收**:✅ "How many users are there?" → "There are 5 users.";另测 JOIN "UK 用户总订单额" → 295.0(正确)。Phoenix 里有完整 trace 树。
- ⚠️ **本日踩坑(已固化进代码/文档)**:
  1. **坑 #2b**:`phoenix.otel.register()` 不会自动把 `PHOENIX_CLIENT_HEADERS` 应用到 OTLP 导出器 → 大 span 批次报 401 / "Response ended prematurely"。对策:从 env 解析出 key 显式传 `api_key=`(已写进 `instrumentation.py`)。
  2. **模型**:`gemini-flash-latest` 持续 503 高负载;`gemini-2.0-flash` 免费层 quota=0;实测可用的是 **`gemini-2.5-flash`**,已设为默认(可用 `GEMINI_MODEL` 覆盖)。
  3. 需 `python-dotenv`(已 `uv add`)。

## Day 2 — 完整 pipeline (目标:接 Spider,跑通端到端) ✅ 已完成 (2026-06-02)

- [x] 下载 Spider(gdown,官方 file id `1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J`,~196MB)→ `data/spider/`(dev.json 1034 题 / 166 库,已 gitignore)。库路径:`data/spider/database/<db_id>/<db_id>.sqlite`。访问层 `sqloop/spider.py`。
- [x] **架构改造:单库 → 多库**。schema 不再导入时写死,改为按每题 `db_id` 动态选库 + 注入 schema,经 session state 传递(`db_path`/`schema`/`intent`)。execute_sql 通过 `tool_context.state["db_path"]` 决定连哪个库。
- [x] Schema Linker:`sqloop/schema_linker.py`(自定义非 LLM ADK agent,把目标库完整 schema 写进 `state["schema"]`,在 Phoenix 显示为 `agent_run [schema_linker]`)。Day 2 最小版=完整 schema 注入;关键词筛表留作后续改进点。
- [x] Repair:沿用 sql_generator 指令内置的"出错/空结果重试一次"(最小版,未拆独立 agent)。
- [x] 20 条 Spider dev 题冒烟:`run_spider.py`(每题独立 try/except + 按服务端 `retry in Xs` 精确退避 + 重试 5 次 + 题间隔 5s)。
- **验收**:✅ **20/20 出结果、pipeline 不崩**(首轮 18/20,2 个 FAIL 是 429 限流;加强限流策略后达 20/20,证实纯属限流而非代码问题)。Phoenix trace 树含 router/schema_linker/sql_generator/execute_sql 完整层级。
- 默认模型改为 `gemini-flash-lite-latest`(lite 每日额度更高,适合批量;Vertex 到账后可换回 flash 提质)。
- ⚠️ **本日关键发现(影响 Day 3/8)**:**免费层配额是批量 eval 的真正瓶颈**。每题 ~3 次 LLM 调用,`gemini-2.5-flash` 当天 **PerDay 配额被打爆**;改用 lite 模型(`gemini-flash-lite-latest`,每日额度更高)才跑通 18/20。**Day 3 baseline(50~100 题)和之后多轮 eval 大概率必须靠模型轮换/重限速,或提前切 Vertex**——这正是要 $100 credits 的原因。

## Day 3 — Eval harness + baseline (目标:有一个可量化的基准分) 🟡 小样本验证完成 (2026-06-02)

- [x] execution-accuracy 评估:`sqloop/eval.py::execution_match`(执行 pred SQL vs gold SQL,结果集做"顺序无关 + 类型无关"的多重集比较)。SQL 抓取靠 `main.py::run_turn_detailed`(从事件流里取 execute_sql 的入参,不改 tool 代码)。
- [x] 本地 baseline harness:`run_eval.py`(小样本 + 限流退避,结果存 `data/eval_runs/*.json`)。**n=20 baseline = 18/20 = 90.0%**。2 个错都是真实失败模式(选错列 / 聚合误用),正好留给 Day 4 optimizer 诊断,非指标 bug。
- [x] 写进 **Phoenix experiment**:`run_experiment.py`(AsyncClient,上传 Spider dev 子集为 dataset,pipeline 作 task,execution_accuracy 作 evaluator,`concurrency=1` 防限流)。n=5 验证通过,实验已落 Phoenix Experiments UI。
- [ ] **正式 baseline(50~100 题、跨多库)**:待 Vertex credits 到账后跑(免费层配额 + 当前小样本都集中在 concert_singer 库,代表性不足)。
- 公共件抽出:`sqloop/throttle.py`(退避逻辑,run_spider/run_eval/run_experiment 共用)。
- **验收**:🟡 小样本已能打印 baseline(90%)+ Phoenix Experiments 可见;大样本正式 baseline 待 Vertex。
- ⚠️ **提醒:06-04 交 $100 credits 申请表 → 已于 06-02 提交** ✅

## Day 4 — Optimizer + Phoenix MCP (目标:agent 能读自己的 trace) 🟡 代码完成,待一次稳定网络下的 live 运行 (2026-06-02)

- [x] 建 Optimizer agent:`sqloop/optimizer.py`(LlmAgent),内置失败模式分类法
      (MISSING_JOIN / WRONG_COLUMN / AGGREGATION_ERROR / GROUPBY_ERROR /
      ORDER_LIMIT_ERROR / VALUE_FILTER_ERROR / SYNTAX_OR_RUNTIME_ERROR / OTHER)
      + 固定 Markdown 报告格式(Summary / 聚类 / Suggested fixes)。驱动脚本 `run_optimizer.py`。
- [x] 把 Phoenix MCP server 作为 Optimizer 的工具集接入:ADK `McpToolset` + `npx @arizeai/phoenix-mcp@latest`
      (`--baseUrl`/`--apiKey` 从 env 取)。**实测能连、列出 27 个工具**,用 `tool_filter` 收敛到 5 个
      (list-datasets / get-dataset / get-dataset-examples / list-experiments-for-dataset / get-experiment-by-id),
      减小请求体、更聚焦、更稳。
- [x] 待分析的真实数据已就位:Phoenix 里建好 `baseline-v0` 实验(n=20,含 2 个真实失败:选错列、聚合误用)。
- [ ] **Optimizer 实际产出"失败模式分类报告"**:唯一未完成项。**卡在开发网络对 Gemini 不稳**
      (`httpcore async http_proxy ConnectError` 抖动;裸 genai 偶尔 10/10,但 ADK 连续多次异步调用必中一次失败)。
      **非代码问题,切 Vertex 后(GCP 内网无需代理)自动消失**;或等本地 VPN 稳定窗口重跑一次即可。
- [x] **$100 GCP credits 申请表 → 已于 06-02 提交** ✅
- **验收**:🟡 代码 + MCP 链路就绪;待稳定网络下 `uv run python run_optimizer.py "baseline-v0"` 成功产出报告。
- ⚠️ **本日踩坑**:① 27 个 MCP 工具全暴露会撑大每次请求、弱网下更易失败 → 用 `tool_filter` 收敛;
      ② ADK 全异步,经本地代理(Clash 7897)的 httpx 异步连接不稳是 live 运行失败的根因(环境问题,非逻辑)。

## Day 5 — 自我改进第一轮 (目标:闭环跑通,分数真的涨) 🟡 骨架完成 + 离线验证,待稳定网络跑 live 一轮 (2026-06-02)

- [x] **可替换配置骨架**:`sqloop/config.py::GeneratorConfig`(prompt 模板 + few-shots + version),save/load JSON;
      `active_config()` 按 env `SQLOOP_CONFIG` → `data/configs/active.json` → baseline 解析。
      `sqloop/agent.py::build_pipeline(instruction)` 让同一 pipeline 按任意配置重建(baseline=v0 / 候选=v1 干净 A/B)。
      `main.py::run_turn_detailed(..., agent=)` 支持传指定配置的 pipeline。
- [x] **Propose(混合式)**:`sqloop/propose.py`。few-shot 从成功 eval 行**确定性按 SQL 形态多样化挖取**(离线可靠);
      prompt 由 **LLM proposer 读真实失败后改写**,带降级(LLM 不可用/丢占位符 → 追加确定性规则)。
- [x] **commit-if-better + 曲线日志**:`run_improve.py` 编排一轮 propose→A/B→择优;held-out 用 concert_singer [20:45]
      (与挖 few-shot 的 [0:20] **不重叠,无泄漏**);胜出才写 `active.json`;每轮追加 `data/improve_log.json`(上升曲线点)。
- [x] **离线验证通过**:挖 few-shot / propose(--no-llm) / 渲染(占位符完好)/ build_pipeline / save-load 往返 全 OK。
- [ ] **live 跑一轮**:`uv run python run_improve.py`(baseline vs 候选各评 25 题 held-out ≈ 150 次调用)。**待稳定网络/Vertex**
      (与 Day 4 同因:白天本地代理对 ADK 异步连接不稳)。
- **验收**:🟡 闭环代码全通、离线验证 OK;待 live 跑出"候选 > baseline"的可见提升(哪怕 +3%)。

## Day 6 — 多轮迭代 (目标:产出上升曲线)

- [ ] 跑满 2~3 轮自我改进
- [ ] 收集每轮的 accuracy,整理成一条上升曲线的数据
- [ ] 处理边界:某轮没涨怎么办(保留上一轮最优)
- **验收**:有一组 [baseline, 轮1, 轮2, 轮3] 的真实分数,呈上升趋势

## Day 7 — UI / Dashboard (目标:demo 能看的界面)

- [ ] Gradio/Streamlit:问答框(输入自然语言 → 显示 SQL + 结果)
- [ ] Dashboard 区:accuracy 曲线 + 发现的失败模式 + before/after 改进示例
- **验收**:浏览器打开能完整演示一遍

## Day 8 — 部署 + repo 整理 (目标:hosted URL + 干净仓库)

- [ ] 部署到 Cloud Run,拿到公开 hosted URL
- [ ] **切 Vertex 后端**(改 .env,跑 `gcloud auth application-default login`),干净环境端到端复测
- [ ] README:项目简介 + 架构图 + 安装/运行说明 + MCP 用法说明
- [ ] 加 **MIT LICENSE** 文件,确认 GitHub About 区显示 license
- [ ] `.env.example` 就位,确认 `.env` 没被提交
- **验收**:别人 clone + 照 README 能跑起来;hosted URL 可访问

## Day 9 — Demo 视频 (≤3 分钟)

- [ ] 脚本:问题背景 → 架构 → 实跑一条 query → 自我改进闭环 → accuracy 曲线爬升
- [ ] 录制 + 传 YouTube(英文讲解或带英文字幕)
- **验收**:视频 ≤3 分钟,公开可播放,清楚展示"自我改进"这个核心卖点

## Day 10 — Devpost 提交文案 + buffer

- [ ] 填 Devpost:Project name / Elevator pitch / What it does / How we built it / **MCP 用法** / Challenges / What we learned
- [ ] 关联 GitHub repo + hosted URL + YouTube 链接
- [ ] 选定 **Arize track**
- [ ] 留时间修 bug
- **验收**:提交表单除"点击 submit"外全部填完,预览无误

## Day 11 — 最终提交 (2026-06-11 北京时间晚上)

- [ ] 最后通读一遍提交内容
- [ ] 点击 Submit
- [ ] 截图留证
- **验收**:Devpost 显示已提交,状态不再是 DRAFT

---

## 简历 bullet(无论是否获奖都能用)

> Built **SQLoop**, a self-improving text-to-SQL agent (Google ADK + Gemini) with full OpenInference tracing; implemented a closed-loop optimizer that introspects its own traces via the **Phoenix MCP server**, clusters failure modes, mines few-shot exemplars from successful trajectories, and A/B-validates prompt revisions — raising execution accuracy from X% to Y% over N iterations.

(提交后把 X/Y/N 换成真实数字)

## 风险登记

- **最易爆的天**:Day 4-6(Optimizer + MCP 接入 + experiment 对比逻辑)。卡住就砍任务平面复杂度,保闭环。
- **网络**:Gemini/Phoenix 国内延迟高,留出重试余量;Vertex 切换可能遇区域/权限问题,Day 8 别拖到最后。
- **credits**:06-04 必须交表,否则 Day 8 切 Vertex 时可能没额度。
