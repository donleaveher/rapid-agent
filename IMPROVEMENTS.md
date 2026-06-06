# IMPROVEMENTS.md — SQLoop 改进需求清单

> 与 `CLAUDE.md` / `PROGRESS.md` 配套。记录已识别但尚未（全部）落地的改进项，
> 按优先级排列。每项含：**问题 / 位置 / 为什么重要 / 建议 / 状态**。
> 原则不变：先保闭环与诚实叙事，再加花活；涉及配额/钱的大跑先确认。

**状态图例**：✅ 已完成 ｜ 🟡 部分完成/进行中 ｜ ⬜ 未开始

---

## 0. 本次会话已完成（上下文）

- ✅ **#1 MCP 报告接回闭环**：`optimizer` 经 Phoenix MCP 产出的失败分析报告，
  现在通过 `propose_candidates(..., failure_report=)` → `_llm_reflective_guidance`
  真正注入 reflect 候选的 prompt。新增桥接 `optimizer.latest_saved_report()` /
  `build_failure_report()`；`run_loop.py` 启动时加载（`SQLOOP_OPTIMIZER_LIVE=1` 走 live MCP，
  否则读最新存档）。**闭环已 live 跑通**。
  - ⚠️ 诚实结论：两臂 A/B（pro & flash，各 100 题）显示**接通正确但未提分**
    （flash 74 vs 73，pro 78 vs 77，均在噪声内）。原因：① 域不匹配（concert_singer 报告
    喂多库 eval）；② 冗余（本地反思已读当前失败，叠过时报告是负担）。
    → 报告要有非冗余价值，应走"跨实验聚合失败规律"，且与 memory 用 2×2 切分（见下）。
- ✅ **报告蒸馏**：`optimizer.distill_report()` 只留 Summary + 失败模式名&计数 + Suggested fixes，
  丢弃过时/冗余的逐条 SQL dump。注入体积 **减 ~60%**（2559→~1016 chars），信号更纯。
  上限 `SQLOOP_REPORT_MAX_CHARS`（默认 1200），按行边界截断不切半句。
- ✅ **demos / memory 职责切分（2×2）**：`SQLOOP_PROPOSE_DEMOS=off` 时 `propose` 退出
  "成功×实例"格（不挖 few-shot、不产 demos 候选），把成功范例通道让给 `memory.py`
  的动态检索；propose 专注"失败×抽象"（rules/reflect）。**默认 on，向后兼容**。
  已离线 + tiny live 验证：无 demos 候选、配置零 few-shot、端到端不崩、commit 守护完好。

### 2×2 职责划分（避免 memory 与 optimizer 重叠的设计基准）

| | 实例级（per-query 检索、动态） | 抽象级（prompt 级规则、静态） |
|---|---|---|
| **成功数据** | **memory**（检索相似解做 few-shot / 复用） | —（已让 propose 退出，避免双份 few-shot） |
| **失败数据** | （未来可选：相似题的"你曾错+正解"） | **optimizer/propose**（失败聚类 → 规则/反思） |

---

## 🔴 高优先级（直接影响"loop 质量"与"任务平面"评分）

### #2 commit-if-better 用单点严格 `>`，没用上自己算的置信区间

- **问题**：commit 决策是单点比较，n=100 上 0.78→0.79 只是 1 道题的差，纯噪声也会 commit。
  明明已实现 `wilson_ci`，却只用来画误差棒、没进决策——与"评测严谨化"的叙事自相矛盾。
- **位置**：`sqloop/loop.py:106` → `committed = cand_held_acc > incumbent_held_acc`；
  CI 函数在 `sqloop/eval.py::wilson_ci`（仅被 `run_loop.py`/`run_eval.py` 用于显示）。
- **为什么重要**：评委一眼能看出 0.78→0.79 是噪声，反而扣"loop 质量"分。
  前两轮 A/B 已实测噪声达 ±9%（pro）/ ±1 题（flash），单点比较不可信。
- **建议**：commit 门槛加 margin 或显著性判据。可选：
  1. 要求 `候选点估计 > incumbent CI 上界`（最严）；
  2. 要求 `候选 CI 下界 > incumbent 点估计`；
  3. 至少 +N 题的绝对增量（如 N≥3，简单可解释）；
  4. 两比例 z 检验 / Wilson 差值区间不含 0。
  推荐先上 (3) 或 (2)（实现简单、叙事清晰），暴露为 `SQLOOP_COMMIT_MARGIN`。
- **状态**：⬜ 未开始（用户标记：可之后再做）。

### #3 Repair 只在 `SQL_ERROR` 触发，但多数失败根本不报错

- **问题**：Repair 仅在 `last_result.startswith("SQL_ERROR")` 时触发。但 optimizer 的失败分类法里
  `WRONG_COLUMN` / `AGGREGATION_ERROR` / `GROUPBY_ERROR` 这些**都能正常执行、不报错**——
  SQL 跑出一个数，只是数错了。结果：Repair 抓不到最常见的失败模式，只能修语法/运行时错（少数派），
  形同摆设。
- **位置**：`sqloop/repair.py:26`（`if not last.startswith("SQL_ERROR"): return`）。
- **为什么重要**：这是**任务平面能实打实提分**的点，也让 Repair 这个 agent 名副其实。
- **建议**：给 Repair 加"结果合理性"触发，而非只看报错：
  - 空结果 / `(no rows)` 时触发重审；
  - 一个轻量 LLM 自检"这个结果回答了问题吗？"（cross-check question vs result）；
  - 注意成本：自检是每题额外调用，需与 #4（省配额）权衡，或仅在 eval 外的 serve 模式开。
  - 防死循环硬上限 `SQLOOP_MAX_LLM_CALLS` 已在，扩触发不会跑飞。
- **状态**：⬜ 未开始。

---

## 🟡 中优先级（成本 / 鲁棒性）

### #4 Router 每题烧一次 LLM，但 eval 里全是 "sql" —— 浪费 ~1/3 配额

- **问题**：每条 Spider 问题都是 sql，router 几乎恒定输出 "sql"，却占每题约 1/3 的 LLM 调用。
  批量 eval 时是纯浪费。免费层配额是反复强调的头号瓶颈（PROGRESS Day 2/3）。
- **位置**：`sqloop/agent.py`（router=LlmAgent，pipeline 首环）；调用发生在每次 `run_turn_detailed`。
- **为什么重要**：能立刻把批量 eval 的配额/耗时**砍掉约 1/3**，正好缓解最痛的瓶颈。
- **建议**：
  - eval 模式下跳过 router（直接置 `state["intent"]="sql"`），用 env 如 `SQLOOP_SKIP_ROUTER=1` 控制；
  - 或把 router 降级成关键词启发式（非 LLM），serve 时仍保留意图分流能力。
  - 注意保持 Phoenix span 结构叙事（可保留一个轻量非 LLM router span）。
- **状态**：⬜ 未开始。

### #5 Schema Linker 纯词法 Jaccard，遇生僻列名失效

- **问题**：靠问题词与表/列名的 token 重叠打分。问题说 "artist" 而列叫 `singer`、
  或 "how many" 要映射到 `COUNT`，都匹配不到。真实库列名常是缩写/代码，会退化成"塞全库"。
- **位置**：`sqloop/schema_link.py:57`（`scored = {t: len(qtok & ...)}`）。
- **为什么重要**：schema linking 失效 → 大库塞全 schema → prompt 膨胀 + 噪声 + 更易选错列
  （正好喂大 #3 的 WRONG_COLUMN）。
- **建议**：
  - 复用 `memory.py` 已有的可插拔 dense ranker / reranker 钩子（`set_dense_ranker`/`set_reranker`）做语义匹配；
  - 或至少把列的**样本值**纳入打分（值匹配能救"artist→singer"类同义）；
  - 保持小库回退全量的现有安全网（`SQLOOP_SCHEMA_MIN_TABLES`）。
- **状态**：⬜ 未开始。

### #6 Phoenix 用得还不够深 —— 多轮曲线只落本地 json

- **问题**：已有 `run_experiment.py`（能建 Phoenix experiment + evaluator），但多轮闭环的曲线
  只写 `data/curve.json`。"tracing 深度"这项的说服力打折。
- **位置**：`run_loop.py`（写 `data/curve.json`）vs `run_experiment.py`（Phoenix experiments，未接入 loop）。
- **为什么重要**：Arize track 单列"tracing + MCP 深度"评分项。每轮 A/B 落 Phoenix 比本地 json 更有说服力。
- **建议**：
  - 多轮 A/B 每轮作为一个 Phoenix experiment 落库；
  - 用 Phoenix annotations 存 `execution_accuracy`；
  - 曲线直接在 Phoenix Experiments UI 看（dashboard 可嵌链接）。
  - 注意：批量落库会增加网络/配额压力，与已知代理不稳的坑权衡；可只在正式 Vertex 跑时开。
- **状态**：⬜ 未开始。

---

## 🟢 低优先级（代码 / 产物卫生）

### #7 产物不一致：curve.json(flash) 与 active.json/improve_log.json(pro) 对不上

- **问题**：dashboard 展示的曲线（`curve.json`=flash 跑）与它旁边展示的 config before/after
  （`active.json`/`improve_log.json`=pro 跑）来自两次不同运行，兜不到一起。
- **位置**：`run_loop.py` 每次覆盖 `active.json`/`curve.json`/`improve_log.json` 三个固定路径；
  强弱对照时 flash/pro 各跑一次导致错位。
- **建议**：提交前用**同一次跑**统一三个产物（或 dashboard 明确标注两条线各对应哪套 config）。
- **状态**：⬜ 未开始。

### #8 `_cols_match` 超 5 列按原序比，宽结果会假阴

- **问题**：列数 > `_MAX_PERM_COLS`(5) 时不做列排列、直接按原序比，宽结果集若列序不同会假阴性。
- **位置**：`sqloop/eval.py:69`。
- **建议**：Spider 少见宽结果，可暂留；若要修，可对宽结果改用"按列内容多重集匹配"而非全排列（避免阶乘爆炸）。
- **状态**：⬜ 未开始（已知即可）。

### #9 eval 每次比较都重新执行 gold SQL

- **问题**：held 集每轮被多次评测，gold 结果集每次重算，浪费。
- **位置**：`sqloop/eval.py::execution_match` → `_run_sql(db_path, gold_sql)` 每次调用都执行。
- **建议**：按 `(db_path, gold_sql)` 缓存 gold 结果集，小幅提速。注意只读、结果可安全缓存。
- **状态**：⬜ 未开始。

### #10 memory.reuse 复用 SQL 不经执行校验直接返回

- **问题**：近乎相同的历史题直接返回存储 SQL，不重新执行校验，靠"held 为 novel"防错。
  逻辑成立，但一旦有人误把 held 数据灌进 memory 就会**静默泄漏**。
- **位置**：`main.py:62`（`reused = memory.reuse(...)` 直接返回）；填充在 `build_memory.py`。
- **建议**：加护栏——如 `build_memory.py` 断言只从 train 切片填充、且与当前 held 集零交集；
  或 reuse 路径加一个轻量执行校验开关。
- **状态**：⬜ 未开始。

---

## 建议的落地顺序

1. **#2 commit margin**（最小改动、直接补"严谨化"叙事漏洞，且不依赖网络/大跑）。
2. **#4 跳过 router**（立省 1/3 配额，缓解最痛瓶颈，利于后续所有大跑）。
3. **#3 Repair 结果合理性触发**（任务平面真实提分点，但需与 #4 权衡成本）。
4. **#7 产物统一**（提交前必做的卫生项）。
5. 其余（#5/#6/#8/#9/#10）按时间与 Vertex 到账情况安排。
