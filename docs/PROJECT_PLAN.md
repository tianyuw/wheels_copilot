# Wheels Copilot — 项目方案

**版本：** v1.1（2026-05-18 应用 council-review consensus 后的修订）
**日期：** 2026-05-18
**状态：** 已通过 2 道 council（specialist 起草 + 外部 LLM review），等用户 review → kickoff
**输入：** [`docs/RESEARCH.md`](RESEARCH.md)、用户已锁定的 4 条决策、4 人 specialist council、外部 council-review（GPT-5.4 + Gemini-3.1-Pro）

**Changelog：**
- **v1.0 → v1.1**（2026-05-18）应用 `~/.claude/skills/council-review.md` 的 consensus 修订：FSM 加 `PENDING_ROLL` / `CORPORATE_ACTION`；多次 daily reconcile + broker activity stream；live 切 Postgres；per-ticker 8%→6%、SPY/QQQ 12%→10%；put delta 0.22→0.18、call 0.25→0.20、DTE target 35→42；max_roll_count 2→1；earnings blackout ±7→±10；新增 single-name gap CB；drawdown CB 8%→6%；5-model council 缩为 3-model；LLM 评估改 shadow A/B；live 阶梯每档延长至 ≥20 交易日或 1 个完整 cycle；加 Live-μ micro-test；wash sale 追踪、ex-div 提前行权处理、MLEG partial fill 处理纳入正文。原始 GPT-5.4 + Gemini review 见末尾附录。

---

## 0. 执行摘要

`wheels_copilot` 是一个 Python daemon，在 $500K USD 的 Alpaca 账户上每日自动跑**单一策略——Wheel**，并在 *定性* 决策上有选择地调用 LLM（选股、基本面分析、前景分析、市场环境分析）。它是 `options-copilot` 的姊妹项目，复用其约 60% 的基础设施。

**本方案的 5 条核心信念：**

1. **Wheel 是状态机，不是策略。** 每个 ticker 是一台独立 FSM（`CASH → CSP_OPEN → ASSIGNED → CC_OPEN → ...`）。系统里每个决策的本质都是"这台状态机今天该做什么"。
2. **Validator-first，默认 mechanical。** 行权价、DTE、profit-take、roll、stop-loss、sizing —— 全部 deterministic、code-enforced。LLM 只做 *定性* 决策（watchlist 维护、基本面深读、救援决策）。LLM 层断了交易也继续。
3. **Watchlist 是 policy；每日交易是 policy 的执行。** Watchlist 每周日晚上 18:00 通过 5 模型 LLM council 重做一次；每日 cycle 机械地按这个 frozen watchlist 执行。
4. **Forward test 是硬 gate，不是走过场。** 10 周 Alpaca paper、4 个 phase。所有 go-live gate 全绿才上 live。资金分 4 步上：$50K → $150K → $300K → $500K。
5. **从第 1 天就按 $500K 真钱设计。** 保守的默认值（per-ticker 上限 **6%**、SPY/QQQ **10%**、日亏 CB -1.5%、drawdown CB **6%**、max_roll_count **1**）。合理回报预期：**年化净 5–8%**，Sharpe 0.8–1.2，max DD 大约 6–10%。**不追求**在 bull market beat SPY —— 追求的是可生存性 + 稳定现金流。

**Council 成员（本 plan 是两道 council 联合产出）：**

| 角色 | 负责 | 章节 |
|------|------|------|
| System Architect | 代码结构、模块、DB、daemon、config | §2 |
| Quant / Risk | 参数、标的池、风险预算、circuit breaker | §3 |
| AI/LLM Integration | LLM 接入点、prompt、成本、降级 | §4 |
| Forward Test Strategist | 10 周 test 方案、go-live gate、资金阶梯 | §5 |
| **External council-review**（GPT-5.4 + Gemini-3.1-Pro） | v1.0 → v1.1 修订点（在本文档各章节标记） | 全文 |

**跨章节冲突的 reconcile：**
- Watchlist 刷新：**周日 18:00 ET**（LLM agent 的方案——数据更全、避开工作日波动）
- VIX gating：分级（≤22 两个 tier 都开 / 22–28 仅 Tier 1 / >28 全冻），而不是单一阈值
- 日亏 circuit breaker：**-1.5% equity（约 $7.5K）**——两个方案中更保守的那个
- Watchlist 权威源：**DB**，`tickers.yaml` 仅做一次性 seed
- LLM 模型分配：**3 模型 council**（v1.1 从 5 减为 3）用于 watchlist + rescue；单 Opus 用于 fundamentals；单 Sonnet 用于 outlook + regime
- DB 存储：**paper 用 SQLite；live 切 Postgres 15+**（v1.1，外部 council 提出真钱场景下 SQLite 的崩溃/磁盘恢复脆弱）
- Reconcile 频率：**每日 3 次**（08:00 / 09:20 / 16:15 ET）+ broker activity stream（v1.1）
- max_roll_count：**1**（v1.1 从 2 减为 1，防止 roll-down 死亡螺旋）

**时间表（从 2026-05-19 kickoff 起算，v1.1 应用 council 建议后延长）：**

| 窗口 | 周次 | 阶段 |
|------|------|------|
| 2026-05-19 → 2026-06-15 | -4 至 0 | 工程建设：M0 shared 层 → M2 多 ticker（达到 Phase 1 entry 标准） |
| 2026-06-16 → 2026-06-29 | 1-2 | Paper Phase 1：单 ticker (SPY)，纯 mechanical |
| 2026-06-30 → 2026-07-13 | 3-4 | Paper Phase 2：5 个 ticker，mechanical |
| 2026-07-14 → 2026-08-10 | 5-8 | Paper Phase 3：完整 watchlist + LLM 接入 |
| 2026-08-11 → 2026-08-24 | 9-10 | Paper Phase 4：压测 + 边缘场景 |
| 2026-08-25 → 2026-09-21 | 11-14 | 🆕 **Live-μ micro-test**：$5-10K，SPY 1 个，≥20 交易日 / 1 完整 cycle |
| 2026-09-22 → 2026-10-19 | 15-18 | Live-α：$50K，2 个 ticker，≥20 交易日 / 1 完整 cycle |
| 2026-10-20 → 2026-11-16 | 19-22 | Live-β：$150K，4 个 ticker，≥20 交易日 / 1 完整 cycle |
| 2026-11-17 → 2026-12-14 | 23-26 | Live-γ：$300K，6-8 个 ticker，≥20 交易日 / 1 完整 cycle |
| 2026-12-15 起 | 27+ | Live-1.0：$500K，完整 watchlist |

**总计：kickoff 到 $500K 满仓 live 大约 7 个月**（v1.1 比 v1.0 延长 2 个月，主要是 Live-μ 加入 + 每个 live stage 从 2 周延至 ≥4 周或 1 个完整 cycle —— 外部 council 一致认为 v1.0 上线节奏太快，单 cycle 都跑不完就升档）。

---

## 1. 已锁定的决策

来自用户，是后续所有章节的前提：

1. **账户：** $500,000 USD Alpaca 账户。Forward test 用 paper → gate 通过后转 live。
2. **策略：** 只做 Wheel，不在这个 codebase 里混入其他 options 策略。
3. **LLM 用途：** 选股 (watchlist)、基本面分析、前景/outlook 分析、市场环境分析；外加 code-based screener 喂数据给 LLM。
4. **Broker：** Alpaca（与 `options-copilot` 保持一致）。
5. **Forward test：** 8–12 周 paper，本方案 lock 在 10 周 + 2 周 buffer。
6. **参考项目：** `/Users/tianyuwang/Projects/options-copilot/` —— vendor-copy 约 15 个文件到 `shared/`，用 SHA 跟踪的 sync script 维护。

规划过程中做出的其他 framing 决策（可调整，详见 §7）：
- 账户类型：暂按 **taxable** 推进。IRA 推迟（Alpaca 当前不支持 IRA）。
- 复用方式：**先 vendor-copy → 后续提取为 pip-installable `copilot-core`**，不做 monorepo。
- Daemon 模型：APScheduler `BlockingScheduler`，与 `options-copilot` 一致。

---

## 2. 架构

### 2.1 代码复用策略 —— 现在 vendor-copy，未来抽 package

**决策：把 `options-copilot` 中筛选过的一批文件 vendor-copy 到 `wheels_copilot/shared/`**，留好后续抽 pip-installable `copilot-core` 的迁移路径（预计 3-4 个月后）。

理由：
- **现在做 monorepo 错。** `options-copilot` 是 60K+ LOC 的生产代码，有自己的 DB、.venv、daemon plist、workspace artifacts。合并意味着重写几十个 skill 的路径、打断正在运行的 paper account。爆炸半径巨大、收益为零。
- **抽 pip package 是对的，但太早。** 在我们还不知道哪些文件真正需要共享（有些 adapter 会 fork、有些不会）之前，过早抽出 `copilot-core` 会制造一个虚假的 API 边界，我们会反复折腾。
- **Vendor-copy 一周就能 ship。** 复制约 15 个文件到 `shared/`，写一个 `SHARED_PROVENANCE.md` 记录每个文件的 git SHA，再写一个 `scripts/sync_shared.sh` 用 3-way diff 暴露上游 drift。

**后续路径：** 两个 repo 都稳定运行一个季度后，把 `shared/` 抽到独立 repo `copilot-core`，两个项目都用 `pip install -e ../copilot-core` 引用。

### 2.2 目录结构

```
wheels_copilot/
├── README.md
├── pyproject.toml
├── config.yaml                          # 账户级配置（risk、LLM 开关、schedule）
├── tickers.yaml                         # 仅作为 watchlist 初始 seed
├── .env                                 # ALPACA_*、OPENROUTER_*、FINNHUB_*、FRED_*
├── com.wheels-copilot.daemon.plist      # launchd unit
├── wheels_daemon.py                     # APScheduler 入口
├── wheels_copilot.db                    # SQLite，WAL 模式
│
├── shared/                              # 从 options-copilot vendor 过来
│   ├── SHARED_PROVENANCE.md             # 每个文件的 git SHA
│   ├── adapters/                        # alpaca_client、openrouter、finnhub、fred、edgar
│   ├── db/base.py                       # connection、job_runs、heartbeat、hash
│   ├── engines/                         # alpaca_health、market_clock、technicals、vol_features、oms
│   └── skills_common/                   # daily-report base、workspace 原语
│
├── db/
│   ├── schema.py                        # wheels 特有表 + migration
│   ├── wheel_state_repo.py              # wheel_states CRUD
│   ├── watchlist_repo.py
│   ├── cost_basis_repo.py
│   └── cycle_log_repo.py
│
├── engines/                             # WHEELS 领域逻辑 —— 全新
│   ├── wheel_state_machine.py           # FSM
│   ├── transitions.py                   # 状态转换表 + guard
│   ├── csp_selector.py                  # 行权价 / DTE —— deterministic
│   ├── cc_selector.py                   # 同上，含 cost_basis floor 硬约束
│   ├── roll_decider.py                  # roll / close / take-assignment
│   ├── assignment_lifecycle.py          # 处理 assigned 事件
│   ├── rescue_engine.py                 # bag-holder 升级处置
│   ├── risk_budget.py                   # account → ticker → trade 三级风险预算
│   ├── portfolio_risk.py                # 净 delta、行业集中度
│   ├── wheel_exit_plan.py               # per-state lifecycle 退出规则
│   ├── wheel_gates.py                   # 入场前硬性约束
│   └── earnings_calendar.py             # Finnhub wrapper + 缓存
│
├── skills/                              # LLM 驱动、低频
│   ├── watchlist-curate/                # 每周日 18:00 ET
│   ├── ticker-evaluate/                 # 新加入时做一次基本面深读
│   ├── market-outlook/                  # 每日 regime read
│   ├── rescue-decide/                   # 事件触发的 bag-holder 救援
│   ├── code-screener/                   # 4 阶 cheap-first screener
│   ├── wheel-cycle/                     # per-ticker 决策 dispatcher（主要 deterministic）
│   └── daily-report/                    # 针对 wheel 的邮件日报
│
├── schemas/
│   ├── wheel_position.py                # WheelPosition、WheelLeg、CostBasis
│   ├── wheel_state.py                   # WheelStateEnum、WheelTransition
│   ├── watchlist.py                     # WatchlistEntry
│   ├── decisions.py                     # CSPProposal、CCProposal、RollDecision、RescuePlan
│   └── llm_outputs.py                   # 各 LLM touchpoint 的 Pydantic schema
│
├── scripts/
│   ├── init_db.py
│   ├── seed_watchlist.py
│   ├── reconcile_alpaca.py
│   ├── force_close.py
│   ├── replay_state.py                  # 从 fills 重建 wheel_states（审计用）
│   ├── dry_run_cycle.py
│   ├── sync_shared.sh                   # diff shared/ vs options-copilot 上游
│   └── stress/                          # forward-test 压测注入脚本
│
├── workspace/                           # YYYY-MM-DD/<ticker>/ —— LLM 输入输出、决策
│
├── tests/
│   ├── unit/                            # state_machine 转换、gate、selector
│   ├── integration/                     # 用 synthetic broker 跑完整 cycle
│   └── fixtures/                        # 罐头 chain、account snapshot、fill 事件
│
├── docs/
│   ├── RESEARCH.md                      # 已存在
│   ├── PROJECT_PLAN.md                  # 本文件
│   ├── RUNBOOK.md                       # 操作员每日 / 每周手册（待写）
│   └── decisions/                       # 非平凡决策的 ADR
│
└── logs/                                # daemon.log、fills.log、decisions.log
```

### 2.3 模块归属边界

| 模块 | 处置方式 | 源（options-copilot） | 目的地（wheels_copilot） | 备注 |
|---|---|---|---|---|
| Alpaca client | REUSE | `adapters/alpaca_client.py` | `shared/adapters/alpaca_client.py` | 已支持 CSP/CC/MLEG |
| OpenRouter | REUSE | `adapters/openrouter.py` | `shared/adapters/openrouter.py` | 5 模型编排可直接用 |
| Finnhub | REUSE | `adapters/finnhub.py` | `shared/adapters/finnhub.py` | Earnings + 基本面 |
| FRED | REUSE | `adapters/fred.py` | `shared/adapters/fred.py` | 宏观日历 |
| EDGAR | REUSE | `adapters/edgar.py` | `shared/adapters/edgar.py` | 10-K / 10-Q 给基本面 |
| Unusual Whales | SKIP | `adapters/unusual_whales.py` | — | Flow 数据偏方向性，wheel 暂不需要 |
| DB 基础 | REUSE | `db/database.py`（拆分） | `shared/db/base.py` | Connection、heartbeat、hash |
| OMS | REUSE | `engines/oms.py` | `shared/engines/oms.py` | Broker-generic |
| Market clock | REUSE | `engines/market_clock.py` | `shared/engines/market_clock.py` | 半日 / 节假日 |
| Alpaca health | REUSE | `engines/alpaca_health.py` | `shared/engines/alpaca_health.py` | 降级模式 |
| Technicals | REUSE | `engines/technicals.py` | `shared/engines/technicals.py` | TA-Lib wrapper |
| Vol features | REUSE | `engines/vol_features.py` | `shared/engines/vol_features.py` | IV rank、VRP、skew |
| Daily-report 基础 | REUSE | `skills/daily-report/scripts/report.py` | `shared/skills_common/report_base.py` | SES 邮件 + 模板 |
| Workspace 审计 | REUSE | (inline pattern) | `shared/skills_common/workspace.py` | 把 inline 模式抽成 helper |
| Exit-plan engine | EXTEND | `engines/exit_plan.py` | `engines/wheel_exit_plan.py` | Per-state lifecycle；CSP 退出衔接 CC 入场 |
| Economics gate | EXTEND | `engines/economics_gate.py` | `engines/wheel_gates.py` | 作为 FSM transition 的 guard 函数 |
| Strategy schema | EXTEND | `schemas/strategies.py` | `schemas/decisions.py` | CSPProposal/CCProposal/RollDecision 各自独立 |
| Portfolio gate | EXTEND | `engines/portfolio_gates.py` | `engines/portfolio_risk.py` | 加入净 stock delta 跟踪 |
| Ticker discovery | REPLACE | `skills/ticker-discovery/` | `skills/code-screener/` + `skills/watchlist-curate/` | 问题不同：筛 *eligible pool* 而非 *今日最佳交易* |
| Position adjust | REPLACE | `skills/position-adjust/` | `engines/roll_decider.py` + `engines/rescue_engine.py` | Wheel 的退出是 deterministic |
| WheelStateMachine | NEW | — | `engines/wheel_state_machine.py` | 核心领域 |
| AssignmentLifecycle | NEW | — | `engines/assignment_lifecycle.py` | 检测 → 修改状态 → 计算 cost basis |
| Watchlist | NEW | — | `db/watchlist_repo.py` + `skills/watchlist-curate/` | 持久化、每周 LLM 维护 |
| Risk budget | NEW | — | `engines/risk_budget.py` | 三级分配 |
| Cycle log | NEW | — | `db/cycle_log_repo.py` | 闭合 cycle 的分析层 |

### 2.4 核心领域原语

**WheelPosition (Pydantic 草图)：**

```python
class WheelLeg(BaseModel):
    role: Literal["short_put", "short_call", "long_stock"]
    occ_symbol: str | None
    strike: Decimal | None
    expiration: date | None
    contracts: int                # 1 张 = 100 股
    entry_credit: Decimal | None
    alpaca_order_id: str
    alpaca_position_id: str | None

class WheelPosition(BaseModel):
    id: int
    ticker: str
    state: WheelState
    state_entered_at: datetime
    current_leg: WheelLeg | None
    cost_basis: Decimal | None    # 在 state=ASSIGNED 时写入
    shares_owned: int
    roll_count: int               # 当前 option 期内的 roll 次数
    cycle_id: int
    realized_pnl_cycle: Decimal
    last_reconciled_at: datetime
```

**状态机：手写 enum + transition 表的 FSM**，不引入库。

```
       ┌────────────────────┐
       │       CASH         │◄──────────────────────┐
       │  (无仓位)          │                        │
       └──────────┬─────────┘                        │
                  │ sell_csp (gate.csp_open_ok)      │ csp_expired_otm
                  ▼                                  │ 或 csp_closed_50%tp
       ┌────────────────────┐    submit_roll         │
       │     CSP_OPEN       │──────────────┐         │
       │  (short put 在手)  │              ▼         │
       └──────────┬─────────┘   ┌─────────────────┐  │
                  │             │  PENDING_ROLL   │──┤ roll filled completely
                  │             │ (MLEG 部分成交) │  │ → 回 CSP_OPEN / CC_OPEN
                  │ csp_assigned└─────────────────┘  │
                  │ (broker 事件)        │           │
                  │                      │ partial   │
                  ▼                      ▼ unwind    │
       ┌────────────────────┐    ┌───────────────┐  │
       │      ASSIGNED      │    │ MANUAL_REVIEW │  │
       │ (100×N 股)         │    └───────────────┘  │
       └──────────┬─────────┘                       │
                  │ sell_cc (gate.cc_strike_floor_ok)│
                  ▼                                  │
       ┌────────────────────┐    submit_roll         │
       │      CC_OPEN       │──────────────┐         │
       │ (short call 在手) │              │         │
       └──────────┬─────────┘             ▼         │
                  │              ┌─────────────────┐│
                  │              │  PENDING_ROLL   ││
                  │ cc_called    └─────────────────┘│
                  │ _away                            │
                  ▼                                  │
       ┌────────────────────┐                        │
       │  CYCLE_COMPLETE    │── 写入 cycle_log ─────┘
       └────────────────────┘

       ┌────────────────────────┐
       │   CORPORATE_ACTION     │  ◄── 任何状态都可转入此
       │  (split / spin-off /   │      由公司行动监控触发
       │   special div / M&A)   │      暂停所有自动操作
       └────────┬───────────────┘      → 操作员 review 后明确转回
                │ 人工 unfreeze
                ▼  回到上一状态或 CASH

       ┌────────────────────────┐
       │    ASSIGNED_HELD       │  ◄── ASSIGNED 状态下股价 < cost_basis × 0.85
       │  (停止自动 CC)         │      由 rescue 引擎接管
       └────────────────────────┘
```

**子状态说明：**
- **`PENDING_ROLL`**（v1.1 新增）—— MLEG roll 订单提交后到完全成交之间的过渡态。Partial fill 在 `options_chain` 流动性差时真实发生（Gemini 提出）；此状态下：(a) 该 ticker 上不允许其他并发决策，(b) 5 分钟内未完全成交触发 cancel-and-rebuild，(c) 完全成交后回到目标新状态（CSP_OPEN 或 CC_OPEN）。
- **`CORPORATE_ACTION`**（v1.1 新增）—— 检测到底层股票发生 split、spin-off、special dividend、M&A 等公司行动时，**任何**当前状态都可转入此 freeze 态。期权合约会被 OCC 调整，自动操作此时会出错。EDGAR + corporate action API 监控触发；操作员手动 `unfreeze` 后转回上一状态或 CASH。
- **`ASSIGNED_HELD`** —— bag-holder 子状态（股价 < cost_basis × 0.85）：停止自动 CC，由 rescue 引擎接管。
- **`CYCLE_COMPLETE`** —— 瞬时状态，原子写入 `cycle_log` 后回到 `CASH`。
- **`MANUAL_REVIEW`** —— `PENDING_ROLL` 5 分钟未完成或 cancel 失败时的兜底状态，等待操作员明确处置。

**Watchlist 持久化：** 详见下面的 DB schema。**DB 是权威源**。`tickers.yaml` 只用于一次性 bootstrap，之后所有变更都通过 `skills/watchlist-curate/` → `db/watchlist_repo.py` 走。

**Cost basis 与 cycle 历史：** 两张表。`cost_basis_history` 是 append-only 日志（初始 assignment、CC 收的 premium、分红）。`cycle_log` 是闭合 cycle 的账本（每个完整 CASH→CASH 周期一行）。

### 2.5 Daemon 调度

**单一 APScheduler `BlockingScheduler` daemon。** 三个调度 cycle + 三次每日对账 + 长连 broker activity stream，都按 wheel 的节奏量身设计。

**🆕 v1.1：三次 reconcile + broker activity stream**

只在 morning 对账（v1.0）在真钱场景下不够：early assignment（特别是 ex-div CC）、partial fill、cancel-replace ack 延迟都需要快速捕获。v1.1 引入：

- **每日 3 次 reconcile**：08:00（盘前；捕获隔夜 assignment / exercise）/ 09:20（开盘前最后校验）/ 16:15（EOD）
- **Broker activity stream**（Alpaca trade events websocket）：长连，捕获 `fill`、`partial_fill`、`canceled`、`expired`、`assigned`、`exercised` 事件 → 实时驱动 OMS 状态转换 + FSM 转移
- 任何 reconcile 发现 broker ≠ DB → 进入 CB7（仓位漂移）+ push 告警

**Pre-market reconcile —— 08:00 ET（周一至周五）：**
1. 与 Alpaca 全量对账（positions / orders / activities since last close）。
2. 处理隔夜事件：assignment、exercise、expiration、corporate action（split / dividend / spin-off）→ FSM 转移。
3. 公司行动监控：若任一 ticker 有 corporate action announcement，进入 `CORPORATE_ACTION` 状态。
4. 不交易。

**Open-validate reconcile —— 09:20 ET（盘前 10 分钟）：**
1. 二次 reconcile + 检查 Alpaca health + 检查数据新鲜度。
2. 计算今日 risk-off 评估（VIX、宏观、`market-outlook` LLM）。
3. 不交易，准备 morning cycle。

**Morning cycle —— 09:45 ET（周一至周五）：**
1. 第三次 reconcile（防止 09:20 之后的 ack 延迟）。
2. 处理 risk-off：如果 risk-off，跳过第 4 步。
3. 遍历每一行 `wheel_state`，跑 gate：`CSP_OPEN`/`CC_OPEN` → 检查 50%TP / 21DTE / delta breach。`ASSIGNED` → 提出 CC（强制 ex-div 检查，详见 §2.7）。`CASH` → 如果 watchlist 有效且 gate 通过，提出 CSP。
4. 通过 OMS 提交订单（idempotency key + open-order inventory lock，详见 §2.7）。

09:45 比 `options-copilot` 的 14:30 早，是因为 wheel 不需要 intraday flow 数据 —— 我们要早点拿到 fill 以获得更好流动性。

**Intraday cycle —— 每 15 分钟，10:00-15:45 ET：**
1. 主要靠 broker activity stream 驱动，调度只是兜底。
2. 跑机械退出规则：short option delta > 0.65 → 把 roll 决策入队等 EOD 处理。underlying 盘中跌穿 strike 超过 10% → 立即触发 rescue 评估。
3. 检查 `PENDING_ROLL` 状态：超过 5 分钟未完全成交 → cancel-and-rebuild。
4. 无 LLM call，无新仓位。

**EOD cycle —— 16:15 ET（周一至周五）：**
1. 第三次每日 reconcile（pre-market 之外的最后一次）。捕获 `account_snapshot`。检测 expiry。
2. 处理 expiry：short option OTM 失效 → premium 落袋、状态切换。ITM → assignment 已 posted，明早 pre-market reconcile 处理。
3. 跑 intraday 入队的 roll 决策。
4. **Ex-div 预检：** 找出明日 ex-div 且有 short call 的 ticker，若 short call deep ITM 且 extrinsic < dividend → 强制 close（详见 §2.7 ex-div 处理）。
5. 如果今天是周日 EOD → 触发 `skills/watchlist-curate/`（周日特殊，跑在 18:00 ET 而不是 16:15）。
6. 计算每日指标、生成日报。

**Heartbeat —— 每 30 分钟。** 与 `options-copilot` 完全一致。

### 2.6 DB Schema —— 仅列新增表

**🆕 v1.1：存储后端按环境分层**

- **Paper：** SQLite（WAL 模式）—— 与 `options-copilot` 一致，开发迭代快、易于本地审计
- **Live：** Postgres 15+ —— 真钱场景下，SQLite 在进程崩溃 / 磁盘满 / 文件锁场景下的恢复脆弱性是不可接受的（外部 council 提出）
- DB 访问通过抽象层（SQLAlchemy core 或薄 DAO），M0 阶段就要按"抽象 + 双 backend"设计，paper-to-live 切换时只换 connection string + 跑一次 schema migration
- Live 切换前要做：一次 paper → Postgres 的全量数据迁移演练（M6 阶段）

直接复用 `options-copilot` 的表（schema 不动）：`account_snapshots`、`positions`、`orders`、`fills`、`signals`、`model_outputs`、`job_runs`、`daemon_status`、`daily_metrics`。

**Wheels 特有的新增表：**

```sql
-- 每个被纳入 wheel 流程的 ticker 一行。FSM 状态住这里。
CREATE TABLE wheel_states (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL UNIQUE,
    state               TEXT NOT NULL,    -- 'CASH','CSP_OPEN','ASSIGNED','ASSIGNED_HELD','CC_OPEN'
    state_entered_at    TEXT NOT NULL,
    current_position_id INTEGER REFERENCES positions(id),
    cycle_id            INTEGER REFERENCES cycle_log(id),
    cost_basis          REAL,
    shares_owned        INTEGER NOT NULL DEFAULT 0,
    roll_count          INTEGER NOT NULL DEFAULT 0,
    last_reconciled_at  TEXT NOT NULL,
    notes               TEXT,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE watchlist (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                  TEXT NOT NULL UNIQUE,
    status                  TEXT NOT NULL DEFAULT 'ACTIVE',  -- 'ACTIVE','FROZEN','RETIRED'
    tier                    TEXT NOT NULL,                   -- 'core','satellite','probation','drop'
    sector                  TEXT,
    max_contracts           INTEGER NOT NULL DEFAULT 1,
    target_put_delta_low    REAL DEFAULT 0.20,
    target_put_delta_high   REAL DEFAULT 0.30,
    target_call_delta_low   REAL DEFAULT 0.20,
    target_call_delta_high  REAL DEFAULT 0.30,
    target_dte_low          INTEGER DEFAULT 30,
    target_dte_high         INTEGER DEFAULT 45,
    last_curated_at         TEXT,
    last_curation_score     REAL,
    last_curation_json      TEXT,                            -- LLM 评估的 payload
    added_at                TEXT NOT NULL DEFAULT (datetime('now')),
    retired_at              TEXT,
    retire_reason           TEXT
);

CREATE TABLE cost_basis_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL,
    cycle_id            INTEGER REFERENCES cycle_log(id),
    event_type          TEXT NOT NULL,    -- 'assignment','csp_credit','cc_credit','dividend','adjust'
    event_date          TEXT NOT NULL,
    delta_per_share     REAL NOT NULL,
    delta_total         REAL NOT NULL,
    shares_affected     INTEGER NOT NULL,
    resulting_basis     REAL NOT NULL,
    source_position_id  INTEGER REFERENCES positions(id),
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE cycle_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    outcome             TEXT,         -- 'called_away','expired_to_cash','closed_loss','manual','rescued'
    total_premium       REAL DEFAULT 0,
    total_dividends     REAL DEFAULT 0,
    capital_gain_loss   REAL DEFAULT 0,
    total_pnl           REAL DEFAULT 0,
    capital_tied_avg    REAL,
    annualized_yield    REAL,
    days_held           INTEGER,
    csp_count           INTEGER DEFAULT 0,
    cc_count            INTEGER DEFAULT 0,
    roll_count          INTEGER DEFAULT 0,
    assignment_count    INTEGER DEFAULT 0,
    max_drawdown_pct    REAL,
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE roll_decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id         INTEGER NOT NULL REFERENCES positions(id),
    ticker              TEXT NOT NULL,
    decision_at         TEXT NOT NULL,
    trigger             TEXT NOT NULL,    -- '21dte','delta_breach','tp_50','operator'
    decision            TEXT NOT NULL,    -- 'roll_out','roll_down','roll_up','close','take_assignment'
    roll_count_before   INTEGER NOT NULL,
    proposed_legs_json  TEXT,
    llm_consulted       INTEGER NOT NULL DEFAULT 0,
    llm_output_id       INTEGER REFERENCES model_outputs(id),
    executed_order_id   INTEGER REFERENCES orders(id),
    rationale           TEXT
);

CREATE INDEX idx_wheel_states_state ON wheel_states(state);
CREATE INDEX idx_watchlist_status ON watchlist(status);
CREATE INDEX idx_cost_basis_ticker ON cost_basis_history(ticker, event_date);
CREATE INDEX idx_cycle_log_ticker ON cycle_log(ticker, started_at);

-- v1.1 新增：Wash Sale 追踪表（taxable 账户必备）
-- IRS rule: 30 天内同一 "substantially identical" 标的的损失不能立即抵税，
-- 而是延迟到新仓位 cost basis 里。Wheel 高频换手 + 同标的反复交易 = 高发场景
CREATE TABLE wash_sale_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL,
    closing_position_id INTEGER REFERENCES positions(id),  -- 触发亏损的平仓
    closing_loss        REAL NOT NULL,                     -- 该笔亏损金额（负）
    closing_date        TEXT NOT NULL,
    opening_position_id INTEGER REFERENCES positions(id),  -- 30 天窗口内的"替代"仓位
    opening_date        TEXT,
    disallowed_loss     REAL,                              -- 被推迟的亏损金额
    adjusted_basis      REAL,                              -- 调整后的新仓位 cost basis
    status              TEXT NOT NULL DEFAULT 'PENDING',   -- 'PENDING','APPLIED','EXPIRED'
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- v1.1 新增：公司行动监控
CREATE TABLE corporate_actions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL,
    action_type         TEXT NOT NULL,    -- 'split','reverse_split','spinoff','special_div','merger','tender_offer'
    announced_at        TEXT NOT NULL,
    effective_date      TEXT NOT NULL,
    detail_json         TEXT,
    handled             INTEGER NOT NULL DEFAULT 0,
    freeze_set_at       TEXT,
    freeze_lifted_at    TEXT,
    operator_notes      TEXT
);

CREATE INDEX idx_wash_sale_ticker ON wash_sale_events(ticker, closing_date);
CREATE INDEX idx_corp_action_ticker ON corporate_actions(ticker, effective_date);
```

### 2.7 订单在 OMS 中的流转

**共享 OMS 直接复用，不改。** 它的状态机是 broker-generic（PENDING → SUBMITTED → FILLED/CANCELED/REJECTED）。Wheels 特有的 lifecycle 住在更上一层的 `wheel_state_machine.py` 里，它观察 OMS 状态变化并做出反应。

**🆕 v1.1：真钱必备的三个 OMS 加固**（外部 council 提出）

1. **Idempotency key**：每个提交订单都必须带 `idempotency_key = sha256(account_id + ticker + legs + side + intent + timestamp_minute)`。OMS 拒绝重复 key 的提交。防止网络重试、daemon 重启、并发 cycle 重复下单。
2. **Open-order inventory lock**（per-ticker mutex）：一个 ticker 上同时只允许一个未完成订单。新订单提交前检查 `pending_orders[ticker]`，存在则跳过 + log。这就是 $500K 上"多卖一倍合约"的最终防线。
3. **Duplicate-submit 防护**：OMS 在提交前查询 broker 现有 open orders（按 `client_order_id` 匹配）。已存在 → 同步 broker → 不再提交。重启后第一次提交时这个检查尤其关键。

**🆕 v1.1：Ex-div 提前行权处理**（外部 council 双方都点名）

ITM 的 short call 在 ex-dividend 日**前一天**经常被对手方提前 exercise（exerciser 抢分红）。会导致：你以为还有 CC 在手，实际已经被 called away，账户多了 cash 少了 shares。

- **检测：** EOD cycle 跑 ex-div 预检 —— 列出明日 ex-div 且 wheels 持有 short CC 的 ticker
- **判断 ITM 提前行权风险：** 如果 `(stock_price - strike) > (extrinsic_value + dividend)`，则提前行权对对手方有利，**强制 close CC** 或 buy-back-and-rewrite
- **检测漏掉的情况：** 第二天 pre-market reconcile（08:00 ET）发现 shares 消失 + CC 消失 + cash 多了 → 由 `assignment_lifecycle.process_called_away` 处理，但要 flag 为"ex-div 提前行权"，因为这种 case 收到的是 strike 而不是 strike + dividend（dividend 归对手方）

**🆕 v1.1：MLEG Roll Partial Fill 处理**（Gemini 提出）

Roll 是 close + open 的 MLEG（multi-leg）原子订单。在流动性差的合约上**可能只成交一条腿**，留下"裸"敞口。

- **FSM 子状态 `PENDING_ROLL`**：roll 提交时进入；该 ticker 锁定（不允许其他决策）
- **5 分钟超时**：未完全成交 → cancel-and-rebuild（取消未成交那条腿，单独发新单完成）
- **如果 cancel 也失败**（broker 已部分 fill）→ 进入 `MANUAL_REVIEW` + critical 告警，操作员手动决定如何 unwind

**Flow A —— CSP 提交 → fill → OTM 到期 → 回到 cash：**
1. Morning cycle：`csp_selector.propose(ticker)` 返回 leg + limit price。`wheel_gates.csp_open_ok()` 校验。
2. `oms.create_order(...)`。`positions` 表写入一行，`effective_exit_plan` 此时 freeze（TP=50%、time-stop=21DTE、stop-loss=delta 0.65）。状态：CASH → CSP_OPEN。
3. `oms.submit()` → Alpaca。PENDING → SUBMITTED。
4. Intraday：`oms.reconcile_orders()` 拉 fill。SUBMITTED → FILLED。`cost_basis_history` 写入一行 `csp_credit`。
5. 到期（OTM）：EOD 检测到 expiry。`positions.status='CLOSED'`、`close_reason='expired_otm'`。状态：CSP_OPEN → CASH。

**Flow B —— CSP → fill → assignment → cash 变成 shares → CC → 被 called away：**
1-4. 同 Flow A 到 fill。
5. 到期 (ITM) 或更早：Alpaca posted assignment。次日 morning 的 reconcile 检测到"多了 shares、少了 short put"。
6. `assignment_lifecycle.process(ticker)`：
   - 新写一行 `positions`，`strategy_template='long_stock'`，qty = 100 × contracts。
   - 关闭 CSP 行：`status='ASSIGNED'`、`close_reason='assigned'`。
   - 写 `cost_basis_history`：先 `assignment` 事件（+strike），再为已收的 csp_credit 写负号事件。结果 basis = strike − total_premium。
   - 状态：CSP_OPEN → ASSIGNED。`cost_basis`、`shares_owned`、`current_position_id` 在一个事务里原子更新。
   - 如果股价 < cost_basis × 0.85 → 状态变 ASSIGNED_HELD，发出 rescue 告警。
7. Morning cycle：`cc_selector.propose(ticker)` **在代码里硬约束 `strike ≥ cost_basis`，不靠 LLM**。在 strike ≥ min AND delta ∈ [0.20, 0.35] AND DTE ∈ [30, 45] 中挑。如果没有满足的合约，返回 None，记录 `cc_no_viable_strike`，状态保持 ASSIGNED。
8. OMS 提交 CC。状态：ASSIGNED → CC_OPEN。
9. 数日后：被 called away（deep ITM 或到期）。Reconcile 检测到 shares + call 都消失，cash 回到账户。
10. `assignment_lifecycle.process_called_away(ticker)`：
    - 关 long_stock 仓位。Capital gain = (strike − cost_basis) × shares。
    - 关 short_call 仓位。
    - 状态：CC_OPEN → CYCLE_COMPLETE → CASH。
    - `cycle_log` 这行最终落盘：outcome='called_away'，根据 cost_basis_history 和 fill 算出总额、`annualized_yield`。

**关键性质：OMS 不知道有 wheel 这回事。** 它只把订单从 PENDING 推到 FILLED。Wheel 引擎通过轮询 `reconcile_orders` 结果订阅 OMS 状态变化，再去改 `wheel_states`。这保留了未来再插一个第三种策略而不耦合的可能性。

### 2.8 配置 Schema —— YAML，两个文件

**`config.yaml`**（账户级，手工编辑）：

```yaml
account:
  broker: alpaca
  mode: paper                   # paper | live
  account_id: WHEELS-PAPER-1    # 用于 idempotency key
  total_capital_usd: 500000
  cash_reservation_pct: 0.70    # CSP 抵押用的 cash 上限占比

storage:                        # v1.1 新增
  paper_backend: sqlite         # WAL 模式
  live_backend: postgres        # 切 live 前强制
  postgres_url_env: WHEELS_PG_URL

schedule:                       # v1.1：3 次 reconcile + activity stream
  premarket_reconcile_et: "08:00"
  open_validate_reconcile_et: "09:20"
  morning_cycle_et: "09:45"
  intraday_interval_min: 15
  eod_cycle_et: "16:15"
  watchlist_refresh_day: sunday
  watchlist_refresh_time_et: "18:00"
  broker_activity_stream: true  # Alpaca trade events websocket

risk:
  per_ticker_max_pct: 0.06              # v1.1: 0.08 → 0.06
  per_ticker_etf_max_pct: 0.10          # v1.1: 0.12 → 0.10 (SPY/QQQ)
  sector_max_pct: 0.25                  # Tier 1 可放宽到 0.35
  daily_loss_cb_pct: 0.015              # -1.5%（保持）
  weekly_loss_cb_pct: 0.04
  drawdown_cb_pct: 0.06                 # v1.1: 0.08 → 0.06
  catastrophic_drawdown_pct: 0.15
  per_position_stop_loss_delta: 0.65
  rescue_trigger_drawdown_pct: 0.15
  max_roll_count: 1                     # v1.1: 2 → 1
  single_name_gap_freeze_pct: 0.12      # v1.1 新增
  single_name_gap_freeze_days: 5        # v1.1 新增

defaults:
  put_delta: [0.15, 0.22]               # v1.1: [0.20, 0.30] → [0.15, 0.22]
  put_delta_target: 0.18                # v1.1: 0.22 → 0.18
  call_delta: [0.15, 0.25]              # v1.1: [0.20, 0.35] → [0.15, 0.25]
  call_delta_target: 0.20               # v1.1: 0.25 → 0.20
  dte: [35, 50]                         # v1.1: [28, 45] → [35, 50]
  dte_target: 42                        # v1.1: 35 → 42
  iv_rank_range: [0.25, 0.65]
  earnings_blackout_days: 10            # v1.1: 7 → 10
  macro_blackout_days: 2
  profit_take_pct: 0.50
  time_stop_dte: 21                     # 固定不随 IVR 漂移（外部 council 修正）

gates:
  vix_freeze_above: 28
  vix_tier1_only_above: 22
  min_open_interest: 500
  max_spread_pct_of_mid: 0.05
  min_avg_option_volume: 200
  min_credit_pct_of_strike_csp: 0.005
  min_credit_pct_of_strike_cc: 0.004
  max_daily_new_entries: 3
  pending_roll_timeout_sec: 300         # v1.1 新增
  ex_div_cc_close_extrinsic_lt_div: true  # v1.1：extrinsic < dividend 时强 close

llm:
  watchlist_curate:
    enabled: true
    pattern: council                     # v1.1：3 模型 propose + blind-score
    models:                              # 3 模型组合
      - "anthropic/claude-opus-4-7"
      - "openai/gpt-5.4"
      - "google/gemini-3.1-pro-preview"
    frequency: weekly
    promote_to_core_threshold: 3         # 3/3 一致才进 core
  fundamentals_deepdive:
    enabled: true
    pattern: single
    model: "anthropic/claude-opus-4-7"
    cache_quarters: 1
  outlook_analysis:
    enabled: true
    pattern: single
    model: "anthropic/claude-sonnet-4-7"
  market_outlook:                        # 每日 regime read
    enabled: true
    pattern: single
    model: "anthropic/claude-sonnet-4-7"
  rescue_decide:
    enabled: true
    pattern: council_vote                # v1.1：3 模型 vote
    models: [...]                        # 同 watchlist 的 3 个
    tie_default: "escalate_to_human"
  code_screener:
    enabled: true                        # 纯代码，无 LLM call
  # 显式 NO-LLM 列表：strike/DTE/delta、order quantity、profit-take、stop-loss

reporting:
  email_to: "tianyuw@icloud.com"
  ses_region: us-east-1
  push_notify_url: "https://ntfy.sh/wheels-copilot-tianyu"   # 关键事件推送
  include_cycle_log: true
  include_cost_basis_history: false

dry_run: false                 # 全局 kill switch
```

**`tickers.yaml`**（仅作为 seed，只读一次）：

```yaml
core:
  - {ticker: SPY,   max_contracts: 5, sector: index}
  - {ticker: QQQ,   max_contracts: 3, sector: index}
  - {ticker: IWM,   max_contracts: 3, sector: index}
  - {ticker: AAPL,  max_contracts: 3, sector: tech}
  - {ticker: MSFT,  max_contracts: 3, sector: tech}
  - {ticker: GOOGL, max_contracts: 2, sector: tech}
  - {ticker: KO,    max_contracts: 4, sector: cons_staples}

satellite:
  - {ticker: AMZN, max_contracts: 2, sector: cons_disc}
  - {ticker: META, max_contracts: 1, sector: tech}
  - {ticker: NVDA, max_contracts: 1, sector: semi, put_delta: [0.15, 0.25]}
  - {ticker: AMD,  max_contracts: 1, sector: semi}
  - {ticker: JPM,  max_contracts: 1, sector: financials}
  - {ticker: V,    max_contracts: 1, sector: financials}
  - {ticker: XOM,  max_contracts: 3, sector: energy}
  - {ticker: UNH,  max_contracts: 1, sector: healthcare}
```

Seed 完之后，**DB 中的 watchlist 表是权威源**。YAML 仅作为 bootstrap / 归档。

---

## 3. 交易参数与风险管理

下面所有默认值都偏 **保守**，因为这是真实 $500K。每个参数都可以配置覆盖，但默认就是 day-1 跑的值。

### 3.1 资金分配 —— $500,000

| 桶 | % | $ | 说明 |
|---|---|---|---|
| **硬保留 cash（不可碰）** | 20% | $100,000 | SGOV / cash。永远不能给 CSP 抵押用。用于 margin call、broker 故障、rescue capital |
| **Active CSP cash 上限** | 70% | $350,000 | 所有未平 short put 抵押 cash 的总额硬上限 |
| **工作 buffer** | 10% | $50,000 | 结算中现金、premium float、在途订单、手续费 |
| **总计** | 100% | $500,000 | |

**单 ticker 最大敞口：6% 账户 = $30,000** 名义值（v1.1 从 8% 收紧；所有未平 CSP + 已 assigned shares 按 strike 计）。$30K 上限意味着即使某只单股 50% 跳空跌停，账户也只损失 3%。**例外：SPY/QQQ 上限 $50K (10%)**（v1.1 从 12% 收紧），因为它们无 idiosyncratic risk。

**目标并行仓位数：10**（research 给的 8-10 范围的上限）：
- 10 × $28K 平均 = $280K，留 $70K 给 ETF 高溢价和后续加仓
- 10 个名字比 8 个分散得更好
- 少于 10 → 单仓敞口爬太高；多于 12 → earnings 冲突概率非线性升高

**行业集中度：**
- 任一 GICS sector 最多占已部署资金的 **25%**
- Tier 1 sector（指数 + 主导 tech）放宽到 **35%**
- 单一 sub-industry 最多 **2 个名字**（不能 AMD + NVDA 同时开）
- 🆕 v1.1：**Factor / correlation cap**（QQQ + AAPL + MSFT + GOOGL 高度相关，sector cap 不能替代）—— M2 阶段加入 correlation 监控，short-term v1.1 通过 watchlist tier 间接控制（core 中 tech 类不超过 3 个名字）

**满仓示例（10 个仓位，$306K 部署 —— v1.1 重算）：**

| # | Ticker | Strike | Contracts | Reserved $ | Sector |
|---|---|---|---|---|---|
| 1 | SPY | $580 | 1 | $58,000 | Index *(ETF 上限 $50K → 引擎调整为 0 张或换 strike)* |
| 1' | SPY | $480 | 1 | $48,000 | Index *(实际选低一档)* |
| 2 | QQQ | $470 | 1 | $47,000 | Index |
| 3 | AAPL | $230 | 1 | $23,000 | Tech |
| 4 | MSFT | $300 | 1 | $30,000 | Tech *($30K 上限即 1 张)* |
| 5 | GOOGL | $150 | 2 | $30,000 | Tech |
| 6 | AMZN | $150 | 2 | $30,000 | Cons. Disc. |
| 7 | JPM | $230 | 1 | $23,000 | Financials |
| 8 | KO | $70 | 4 | $28,000 | Cons. Staples |
| 9 | XOM | $100 | 3 | $30,000 | Energy |
| 10 | UNH | $300 | 1 | $30,000 | Healthcare *($30K 上限即 1 张)* |
| | | | **合计** | **~$317,000** | |

引擎对超 $30K 的单 ticker 自动缩单或挑更低 strike。Cash 闲置约 $183K = 37%（v1.1 比 v1.0 更保守，留更多 buffer）。

### 3.2 默认交易参数

| 参数 | 默认 | 区间 | 理由 |
|---|---|---|---|
| Put delta (CSP 入场) | **-0.18** *(v1.1)* | -0.15 ~ -0.22 *(v1.1)* | 外部 council：原 0.22 偏激进，$500K 真钱建议更保守端。-0.18 ≈ 82% OTM 概率 |
| Call delta (CC 入场) | **+0.20** *(v1.1)* | +0.15 ~ +0.25 *(v1.1)* | 外部 council：原 0.25 偏激进。-0.20 给被 called away 更宽的余地 |
| DTE target（入场） | **42** *(v1.1)* | 35-50 *(v1.1)* | 外部 council：原 35 离 21-DTE time stop 太近；42 给 50% TP 更充分的时间 |
| 提前关仓 (profit take) | **50% credit** | 40-60% | tastytrade 200K trade 研究。50% 时关（除非 <7 DTE，让它过期） |
| Time stop | **21 DTE 固定** *(v1.1)* | 不随 IVR 漂移 | 外部 council 反对 v1.0 的 IVR-dependent 调整 —— 简单规则更可靠 |
| Stop loss | short delta ≥ 0.65 或 close 要付的 debit ≥ 2× credit | 同时 | 两个冗余触发；股票可能比单一规则更快穿仓 |
| Roll 触发 | strike breached AND short delta ≥ 0.40 AND ≤21 DTE | 三者必须同时 *(v1.1: 0.50 → 0.40)* | Gemini 提出：0.50 时合约已 ITM、bid-ask 极宽难拿 net credit；0.40 提前防守 |
| 最大 roll 次数 | **1 次** *(v1.1: 2 → 1)* | 硬上限 | 外部 council：防止 roll-down 死亡螺旋。第二次决策：take assignment / close / rescue |
| IV rank 入场 | **25 ≤ IVR ≤ 65** | 硬约束 | <25 太便宜不能补偿 tail risk；>65 通常是 event 在被 price in。**例外：v1.1 允许 IVR > 65 的 ETF / Tier 1 名字 进入**（避免 panic 时砍掉肥尾机会，由 LLM outlook 单独审视个股 idiosyncratic risk） |
| Bid-ask spread | ≤ 5% mid | 硬约束 | Slippage |
| Open interest | ≥ 500 目标行权价 | 硬约束 | 比 research 的 100 紧 —— $500K 规模可能需要 roll size |
| 最低 credit | ≥ 0.5% strike (CSP)、≥ 0.4% strike (CC) | 硬约束 | "这张合约的位置值不值"的底线 |

### 3.3 标的池

**Tier 1 —— Safe Core（7 个名字，约占已部署 60%）：**

| Ticker | 大约价 | 每张预留 | 理由 |
|---|---|---|---|
| SPY | $580 | $58K | S&P 500 ETF，全球最深 option 市场 |
| QQQ | $470 | $47K | Nasdaq 100，与 SPY 互补 |
| IWM | $230 | $23K | Russell 2000，小盘分散 |
| AAPL | $230 | $23K | 顶级 mega-cap |
| MSFT | $420 | $42K | 现金流可预测 |
| GOOGL | $180 | $18K | 股价低 → 仓位粒度细 |
| KO | $70 | $7K | 防御性低 vol，VIX 升高时仍可用 |

**Tier 2 —— Quality Growth（8 个名字，约占 40%）：**

| Ticker | 大约价 | 每张预留 | 说明 |
|---|---|---|---|
| AMZN | $200 | $20K | 巨型 consumer disc. |
| META | $580 | $58K | 单张就够 |
| NVDA | $140 | $14K | 波动大但质量高，最多 1 张 |
| AMD | $160 | $16K | Semi 多样化 —— 永不与 NVDA 同时开 |
| JPM | $230 | $23K | 金融锚 |
| V | $290 | $29K | 支付，margin 稳 |
| XOM | $115 | $11.5K | 能源 |
| UNH | $550 | $55K | 医药锚 |

**硬性排除（永不 wheel）：**
- Meme / 动量股（GME、AMC、TSLA 在极端 IV 时）
- 无收入生物科技 / 90 天内有 FDA catalyst
- 上市 <12 个月的次新股
- 股价 <$20
- 3 倍 leveraged ETF（TQQQ、SQQQ、SOXL...）
- 单商品 ETF（USO、UNG —— contango 慢慢吃你）
- v1 阶段市值 <$20B 都不做
- 60 天内有重大事件（并购投票、反垄断裁决等）

### 3.4 入场前硬性 Gate（CSP 开仓）

代码强制，不接受 LLM debate。**任一**触发 → 拒绝：

| # | Gate | 阈值 |
|---|---|---|
| G1 | Earnings | underlying earnings ≤ ±10 日（v1.1：±7 → ±10；相对入场或到期，任一） |
| G2 | 宏观事件 | FOMC / CPI / NFP / PCE ±2 日内 |
| G3 | VIX | >28 全冻；22-28 仅 Tier 1；≤22 两层都开 |
| G4 | IV rank | <25（始终拒）；>65 仅对 Tier 1 / ETF 例外允许（v1.1） |
| G5 | OI | <500 在目标行权价 |
| G6 | 价差 | >5% mid |
| G7 | 期权 volume | 20 日均 <200 |
| G8 | per-ticker 唯一性 | 同一 ticker 已经有 CSP/CC/shares |
| G9 | CSP cash 上限 | 加上这笔会超 $350K 总预留 |
| G10 | 行业集中度 | 加上后 sector >25%（Tier 1 >35%） |
| G11 | 子行业冲突 | 同一子行业已有未平仓位 |
| G12 | 单 ticker 敞口 | 预留需 >$30K（SPY/QQQ 例外 $50K）（v1.1：从 $40K/$60K 收紧） |
| G13 | 数据陈旧 | quote / chain >5 分钟未更新 |
| G14 | Watchlist | Ticker 不在 ACTIVE watchlist |
| G15 | 待 review 的 rescue | 此 ticker 有未解决的 rescue flag |
| G16 | 当日入场上限 | 当日已开 >3 个新 CSP |
| G17 | Drawdown brake | 账户在 CB 状态 |
| G18 | Broker 健康 | Alpaca health monitor 降级 |
| **G19** | **Single-name gap freeze**（v1.1 新增） | 该 ticker 在过去 5 个交易日内任一日单日跌幅 > 12% → 该 ticker 5 个交易日内禁开新仓 |
| **G20** | **Corporate action**（v1.1 新增） | 该 ticker 在 `CORPORATE_ACTION` 状态 → 拒绝 |
| **G21** | **Wash sale**（v1.1 新增） | 该 ticker 在过去 30 日内有亏损平仓 → 新仓只在评估后开（损失会被 deferred，需要预计 cost basis 调整） |

### 3.5 Assignment / Bag-Holder 管理

被 assigned 时：
1. **Cost basis 公式（v1.1 显式化）：** `Adjusted Basis = strike + Σ(roll debits) − Σ(all credits)`
   - 包含：assignment 行权价 + 历次 roll 支付的 net debit − CSP 期收到的所有 net credits − CC 期收到的所有 net credits − 持有期分红
   - **死亡螺旋防护：** 代码硬性禁止"为了赚 premium 反复 roll-down 导致 effective basis 升高"。详见 §3.2 `max_roll_count = 1`。
2. 预留 cash 变成 shares；重算 sector + ticker 敞口。
3. 只有 escalation tier 允许时才提 CC（见下）。

**硬约束：** 任一 CC 的 strike `K` 必须满足 `K ≥ cost_basis`。代码强制。**全系统最重要的一行代码**。

**🆕 v1.1：CC strike floor 例外路径**（外部 council 提出：极端套牢下硬约束会导致永久僵死）

仅在 **同时满足以下所有条件**时，允许 CC strike < cost_basis（"basis-below CC"）：
1. Ticker 处于 T3/T4 escalation 状态（drawdown ≥ -20%）
2. 已经至少 60 天未能在 K ≥ basis 卖出 CC（连续 4 周 `cc_no_viable_strike` log）
3. **操作员显式人工批准**（不能由 LLM rescue 单独决定）
4. 该笔锁定亏损 ≤ 季度 realized loss budget 上限（默认账户净值 1%/季 = $5K/季）
5. 在 daily report + 推送通知中标红记录

如果上述任一不满足，CC 不开，等待 ticker 价格恢复或被强制退出。

**🆕 v1.1：Wash Sale 追踪**（taxable 账户必备，外部 council 提出）

US IRS 30 天 wash sale 规则：30 日内同一 "substantially identical" 标的的亏损不能立即抵税，而是 deferred 到新仓位的 cost basis。Wheel 高频换手 + 同标的反复交易 = wash sale 高发场景。

**实现：** `db/cost_basis_repo.py` + `wash_sale_events` 表（§2.6 已建）：
- 任一 CSP / CC / shares 平仓有亏损 → 写入 `wash_sale_events` PENDING
- 30 日内同一 ticker 再开新 CSP / CC / 接到 assignment → 把之前的 disallowed_loss 加到新仓位 cost basis 上
- Gate G21 在新开 CSP 前查询 pending wash sales，给出预警（不强制拒绝，但 daily report 标记）
- Cycle log 的 `realized_pnl` 区分 *会计 P&L*（含 wash sale 调整）和 *经济 P&L*（不含）

**按 cost basis drawdown 的分级 escalation：**

| Tier | DD | 动作 |
|---|---|---|
| **T0 Normal** | 0% 到 -5% | 标准 CC：delta 0.20（v1.1）、DTE 35-50、K ≥ basis |
| **T1 Watch** | -5% 到 -10% | delta 降到 0.12-0.18（v1.1：更保守）；K = max(basis, current × 1.05)。日报里标记 |
| **T2 Stress** | -10% 到 -20% | **暂停自动 CC。** 开 rescue review ticket。LLM rescue 评估：继续持有？远期低 delta CC（<0.10）？加仓拉低 basis？ |
| **T3 Critical** | -20% 到 -30% | **此 ticker 全部自动操作暂停。** 强制 LLM rescue review。给人类 3 个选项。**可启用 basis-below CC 例外路径**（需人工批准） |
| **T4 Forced exit** | <-30% | 5 个工作日内人类必须 review。无输入则默认：3 日 TWAP 退出。Ticker 从 watchlist 移除至少 90 天 |

**强制退出（无视 tier）：** underlying <$15/股、宣布破产、审计失败、持有 >365 天且未恢复到 -10% basis 内、市值跌破 $5B。

### 3.6 组合层 Circuit Breaker

| Breaker | 触发 | 动作 | 恢复 |
|---|---|---|---|
| **CB1 日亏** | 日 P&L < -1.5% equity (约 $7.5K) | 冻新仓 24h | 次日自动 |
| **CB2 周亏** | 5 日滚动 P&L < -4% (约 $20K) | 冻新仓；现有 TP 收紧到 30% | 手动解锁 |
| **CB3 Drawdown** | peak-to-trough DD ≥ **6%** (约 $30K)（v1.1：8% → 6%） | 停新；CC 一有利润就平；保护现金 | 手动 + DD <4% 连续 5 日 |
| **CB4 灾难性 DD** | DD ≥ 12%（v1.1：15% → 12%）(约 $60K) | 完全停；任何 option 都不开；只保 shares | 强制全系统 review |
| **CB5 Broker 健康** | 10 分钟内 API 错误 >10% 或 >5 个连续 5xx | 只读模式 | 30 分钟正常后自动 |
| **CB6 数据陈旧** | quote / IV / earnings >4h 未更新 | 跳过本 cycle | 数据新鲜后自动 |
| **CB7 仓位漂移** | Broker vs DB 不一致 >1 cycle | 暂停新仓 | reconcile 通过后自动 |
| **CB8 订单速率** | 单日新订单 >8 | 当日不再开仓 | 次日自动 |
| **CB9 单票 gap**（v1.1 新增） | 单 ticker 单日跌幅 > 12% | 该 ticker 冻结 5 个交易日（也由 G19 在 gate 层捕获） | 5 个交易日后自动；或操作员手动早释 |
| **CB10 LLM 服务降级**（v1.1 新增） | OpenRouter API 错误率 >50% 持续 30 分钟 | 进入 `llm_degraded` 模式：所有 LLM touchpoint 用 last-good cache | LLM 恢复后自动 |

**自动恢复** 用于基础设施问题（CB5/6/7）；**手动解锁** 用于资金问题（CB2/3/4）。资金类违规说明 *框架本身有问题*，不只是连接问题。

### 3.7 真实回报预期（$500K Base Case）

诚实，不画饼：

| 指标 | 基准 | 好年景 | 坏年景 |
|---|---|---|---|
| 月 premium 净收 | 0.6-0.9% equity ($3-4.5K) | 1.0-1.3% ($5-6.5K) | 0.2-0.4% ($1-2K) |
| 毛年化 premium | 8-11% | 12-15% | 3-5% |
| **年化总回报（净）** | **6-9%** | 10-13% | -3% 到 0% |
| Max drawdown | 6-10% | <5% | 12-18% |
| Assignment rate (CSP %) | 15-25% | 10-15% | 30-40% |
| 每年每 ticker cycle 数 | 6-10 | 10-14 | 3-5 |
| CSP 胜率（按 close at profit） | 75-82% | 82-88% | 65-72% |

**诚实校准：**
- 6-9% 净 **低于** S&P 历史 ~10%。这是设计的代价 —— 我们在用"回报"换"路径可预测性 + 较低 DD"。
- 强牛市（SPY +20%）会显著跑输（CC strike 把上行封顶）。这是 wheel 的结构性成本。
- 急熊市（SPY -25%）我们大概率亏钱，但 **比 buy-and-hold 少亏**，premium cushion + 行业分散提供的保护。
- "0.6-0.9% / 月 premium 净收"是 **扣除** TP、roll debit、被 assigned 的亏损 CSP 之后的值。毛 theta 收入大得多；网值才是要看的。
- **Sharpe 目标 0.8-1.2** vs SPY ~0.5-0.7 —— 这才是我们真正在追求的 "edge"，不是绝对收益。

---

## 4. LLM 与 AI 集成

**核心原则：** Deterministic 层（状态机、gate、sizing、order builder）独立完整。LLM 层是 *enrichment*，绝不在 critical path。把所有 LLM 调用注释掉，bot 仍然能用上周的 watchlist + "normal" regime 假设跑 wheel。

### 4.1 LLM 接入点清单

6 个接入点。4 个是用户指定的，2 个是评估后加的。

#### 4.1.1 Regime read —— `market-outlook`（每日，单模型）
- **决策：** 今天是 risk-on / normal / caution / risk-off / crisis？输出控制全局 `new_entries_allowed` gate 和 `size_multiplier`。
- **频率：** 每日，08:30 ET 盘前。
- **输入：** VIX + 5/20 日变化、SPY/QQQ 回报、term structure、DXY、10Y 收益率 + 2s10s、今日 FRED 宏观、Massive 24h 宏观新闻、`vol_features` regime tag。
- **输出 schema：**
  ```json
  {"regime": "normal|caution|risk_off|crisis",
   "confidence": 0.0-1.0,
   "new_entries_allowed": bool,
   "size_multiplier": 0.0|0.5|1.0,
   "reasoning": "...", "key_drivers": ["..."], "expires_at": "ISO"}
  ```
- **为什么用 LLM：** 没有规则能区分"VIX 22 是噪音"和"VIX 22 是信用利差走阔的前兆"。多信号综合。
- **模型：** 单 **Sonnet 4.7**。
- **成本：** 约 $0.02/天 → **约 $0.50/月**。

#### 4.1.2 Watchlist curation —— `watchlist-curate`（每周，5 模型 council）
- **决策：** 在 screener shortlist 之上，给出未来约 4 周的 12-20 个 ticker 排名 + `core/satellite/probation/drop` 分级。
- **频率：** **每周日 18:00 ET。** 故意做成 *稳定* 的（research §5/§9.4 —— "watchlist is the policy, not the trade"）。
- **输入：** 每个候选 ticker 的 dossier —— 60 日 price action、IV rank/percentile/term、基本面 (Finnhub)、下次 earnings + 过去 4 次 surprise、分红时间表、option 流动性、UW 资金流 tag、10-K/10-Q 摘要、Massive 14 天新闻、当前组合、**上周 watchlist + 上周理由**（保持连续性）。
- **输出 schema：**
  ```json
  {"effective_date": "ISO",
   "review_horizon_weeks": 4,
   "tickers": [
     {"symbol": "AAPL", "tier": "core",
      "wheel_fitness_score": 0-100,
      "max_account_pct": 0.0-0.10,
      "willing_to_hold_24mo": true,
      "thesis": "...", "concerns": [...],
      "preferred_iv_rank_range": [int, int],
      "next_review_trigger": "..."}],
   "removed_from_prior": [...], "sector_concentration_note": "..."}
  ```
- **为什么用 LLM：** Watchlist 本质就是问 "我作为长期投资者愿意在这个 strike 买入并持有这家公司吗？"—— 纯定性公司质量问题。Research §2.1：只在你 *愿意持有 2 年以上* 的股票上 wheel。
- **模型：** **3 模型 propose-then-blind-score council**（v1.1：5 → 3，外部 council review 一致认为 5 模型对单一 Wheel 是过度工程）—— 3 模型组合：`anthropic/claude-opus-4-7` + `openai/gpt-5.4` + `google/gemini-3.1-pro-preview`。每个模型独立排序；提案匿名化后再被同 3 模型打分；进入 `core` 需 **3/3 一致**（v1.1：5/5 → 3/3），降级到 `satellite` 或保持现状需 2/3。
- **成本：** ~$2-3/run × 每月 4-5 run = **约 $10-15/月**（v1.1 比 v1.0 节省约 $8）。

#### 4.1.3 基本面深读 —— `fundamentals-deepdive`（每月每 ticker，单 Opus）
- **决策：** 单 ticker 基本面评分卡，作为 watchlist-curate council 的 *evidence* 使用，不单独决策。
- **频率：** 每月对 watchlist 上每个 ticker 做一次；earnings event 时刷新（事件触发）；screener 新候选首次进入时做一次。
- **输入：** 10-K + 最新 10-Q（EDGAR）、最新 earnings transcript、4 个季度 consensus、revision trend、资产负债率、3-5 个同业 peer 对标。
- **输出 schema：** `business_quality(1-10)`、`balance_sheet_strength(1-10)`、`earnings_quality(1-10)`、`valuation_vs_peers(cheap/fair/rich)`、`red_flags(str[])`、`competitive_moat`、`wheel_holdability(1-10)`、`summary`。
- **为什么用 LLM：** 读 10-K → 一页评分卡是 LLM 经典场景。
- **模型：** 单 **Opus 4.7** —— 长上下文文档综合的强项。不用 council；输出是被下游 council 加权的 evidence。
- **成本：** $0.40/ticker × 40 ticker-events/月 = $16/月（无缓存）；**$8/月（季度缓存）**。

#### 4.1.4 Outlook 分析 —— `outlook-analysis`（每周，单 Sonnet）
- **决策：** 每个 watchlist ticker 的前瞻 thesis：未来 1-3 月预期、催化日历、非对称风险标记。
- **频率：** 每周（作为 watchlist-curate 的输入）。加单日 >10% 大跳的事件触发。
- **输入：** 近期新闻聚合（Massive 14 日）、分析师评级变化（Finnhub）、即将到来的催化、UW 异常资金流、行业动量、技术面（RSI、距 SMA200、ATR）。
- **输出 schema：** `directional_lean(bullish/neutral/bearish)`、`catalyst_calendar[]`、`iv_compression_risk`、`tail_risk_flags`、`recommend_pause(bool, reason)`。
- **为什么用 LLM：** 把新闻 + 资金流 + 技术面综合成前瞻 thesis 是判断题。
- **模型：** 单 **Sonnet 4.7**。
- **成本：** $0.08/ticker × 20 watchlist tickers × 每周 1 次 × 4 周 = **约 $6-8/月**。

#### 4.1.5 Bag-holder 救援 —— `rescue-decide`（事件触发，5 模型投票）
- **触发：** Assigned ticker 跌破 basis -15%，或 ASSIGNED 状态 >60 天未成功卖出 CC，或 CC 已 roll 2 次。
- **频率：** 事件触发。稳态：0-3 次/月。
- **输入：** 完整仓位历史、当前 basis vs market、≥basis 的 CC 行权价候选、持有天数、最新基本面、行业 + 宏观背景、保护性 put / collar 合约链。
- **输出 schema：** 恰选一项：`{hold_and_wait, sell_cc_at_basis, sell_cc_below_basis_for_premium, buy_protective_put, convert_to_collar, take_loss_close, escalate_to_human}` + 理由 + 估算 P&L 影响。
- **为什么用 LLM：** Research §5 唯一明确点名 LLM 加价值的地方。高信息密度、多选项、单一规则 base rate 差。
- **模型：** **3 模型 propose-and-vote**（v1.1：5 → 3）—— 同 watchlist 的 3 个模型。无 blind-score（输出是 categorical，不连续）。多数票胜（2/3）。**平票 → `escalate_to_human`**（在 3 模型组合下平票即 1-1-1 三选项全分歧，更需要人介入）。
- **成本：** ~$1-3/次。预期 **<$5/月**（v1.1 比 v1.0 节省约 $5）。

#### 4.1.6 每日 "今天该不该交易" → 已合并到 4.1.1
Regime read 已经处理。单独的 gate 是冗余 + 增加延迟。

#### 4.1.7 Strike / DTE / delta 选择 —— **不用 LLM**
Research §5 + `options-copilot` 的踩坑历史：LLM 在数值边界上不可靠。这些全部 *硬* 写在 `config.yaml` + deterministic gate 引擎里。LLM 永远不"凭空"提出数字 —— 它读结构化输入然后转述。

### 4.2 Screener + LLM 组合工作流

便宜的先做（参考 `wheel-it` 的 4 阶 pipeline + `options-copilot` 的数据 enrichment）：

```
Step 1 —— 代码 SCREENER  (成本：约 $0)
   池子：S&P 500 + NASDAQ 100 + 精选中盘（约 700 个）
   筛子：
     1a. 流动性：日均 vol >2M、市值 >$5B
     1b. Option 流动性：front-month OI >500 在 30-DTE 0.25Δ
     1c. IV rank: 20 ≤ IVR ≤ 70
     1d. Earnings: 14 天内无 earnings
     1e. 行业排除：biotech、cannabis、OTC、次新股 (<2y)
     1f. 价格：$20 ≤ last_close ≤ $400
   → 约 30-60 个候选

Step 2 —— 数据聚合（成本：API 调用，无 LLM）
   每个候选：Alpaca + Finnhub + EDGAR + Massive + UW + FRED
   → dossier（约 50K tokens 每个）

Step 3 —— LLM 评估
   3a：fundamentals-deepdive (Opus 单)  —— 只跑本季度未缓存的约 15 个
   3b：outlook-analysis    (Sonnet 单)  —— 全部 30-60 个
   3c：watchlist council propose + blind-score on combined dossiers

Step 4 —— 综合与持久化
   - Council 多数 → tier
   - 与现有 watchlist 对比：
       core → drop      = probation 一周（不强平，不开新仓）
       drop → 提升       = 先到 satellite，不能直接 core
       new → core       = 必须 5/5 全员一致
   - 写 workspace/YYYY-WW/ 审计 + 更新 DB watchlist
```

**频率：每周日 18:00 ET。** 每日是浪费；双周对 earnings 季节太慢。

### 4.3 Prompt 设计原则

**每个 LLM prompt 都必须包含（standard context 块）：**
1. 账户状态 —— cash、equity、CSP 部署 %、shares 部署 %
2. 当前未平仓位 —— 每个 CSP/CC 的 strike、DTE、P&L、持有天数
3. 当前 watchlist —— 完整 tier 列表 + 上周理由
4. 近期表现 —— 30/90 天 trailing 已实现 P&L、assignment 数、平均 cycle time、最大 DD
5. 系统配置 snapshot —— delta 区间、DTE 区间、profit-take %、所有硬规则（让 LLM 知道代码会强制什么）
6. 日期、市场环境、VIX、最近宏观发布
7. **明示 "你不能提议" 列表** —— 在 system prompt 开头声明

**绝不向 LLM 询问：**
- Strike、DTE、delta、roll trigger 阈值、profit-take %、stop-loss % —— 任何会被 order builder 消费的数字
- 订单数量（来自 `WheelRiskBudget`）
- "今天该不该交易 X" —— deterministic gate 的工作
- Roll 决策（规则给出干净答案的情况下）

**结构化输出强制：**
- 每个 touchpoint 在 `schemas/llm_outputs.py` 有 Pydantic schema
- Prompt 末尾必带 `Return ONLY valid JSON matching this schema: {schema_as_text}`
- 首道防线是 `openrouter.parse_json_from_llm`（5 层 fallback，从 `options-copilot` 实战验证过）
- 解析失败：1 次重试 temp=0.0 + "Your previous response was invalid JSON" 追加。二次失败 → 标记 touchpoint degraded、走 fallback
- 幻觉 ticker 在 post-parse 校验对照 screener-eligible set。未知 ticker drop + 记日志

**Council 模式分配矩阵：**

| 接入点 | 模式 | 理由 |
|---|---|---|
| Watchlist curate | **3 模型** propose + blind-score（v1.1） | 高 stakes；周度 policy；3 模型已经够覆盖盲点，避免过度工程 |
| Rescue decide | **3 模型** propose + vote（v1.1） | Categorical 输出；防止单模型在灾难路径上过度自信 |
| Fundamentals deepdive | 单 Opus | Evidence 不是 verdict；下游 council 加权 |
| Outlook analysis | 单 Sonnet | 调节信号；不直接执行 |
| Regime read | 单 Sonnet | 每日延迟敏感；规则已经盖了最坏情况 |

### 4.4 成本管理

**稳态月成本（v1.1：5 模型 → 3 模型 后）：**

| 接入点 | 频率 | $/run | $/月 |
|---|---|---|---|
| Regime read (Sonnet) | 22 交易日 | $0.02 | ~$0.50 |
| Watchlist council (3×) | 4-5/月 | ~$2.5 | ~$11 |
| Fundamentals (Opus) | 约 40 ticker-events | $0.40 | ~$16（缓存后 ~$8） |
| Outlook (Sonnet) | 约 80 ticker-events | $0.08 | ~$7 |
| Rescue council (3×) | 0-3/月 | ~$2 | ~$3 |
| **总计** | | | **~$25-30/月** |

vs 预期月毛 premium $3-8K → 成本 <1% 收入。不是约束。v1.1 比 v1.0 节省约 $15/月（5→3 模型 council）。

**缓存策略**（巨大乘数）：
- Fundamentals 输出按 (ticker, 最近 10Q filing 日期) 缓存 —— 季度级。$1.60/ticker/年 vs $19.20 无缓存。
- EDGAR 10-K/10-Q 摘要在 adapter 层缓存 90 天
- Outlook 日缓存（同日同 prompt = cache hit）
- **Anthropic prompt caching** (`cache_control: ephemeral`) 用于长上下文 fundamentals —— 静态块缓存，council 打分阶段输入 token 省约 75%

### 4.5 失败模式与防护

| 失败 | 检测 | Fallback |
|---|---|---|
| 幻觉 ticker | post-parse 校验对照 screener-eligible | Drop 未知；若 >25% 无效，标记 run degraded、回上一份好 watchlist |
| JSON 解析失败 | `parse_json_from_llm` → None | temp=0 重试 + 重新格式化；二次失败 → 标记 touchpoint degraded |
| LLM 自信地推错 watchlist | Council 要求 `core` ≥3/5、新入 `core` 5/5 一致 | 分歧自动降到 `satellite` |
| LLM 慢 / 超时 | `openrouter.py` 180s timeout + 2 retry | 每日 flow：regime fallback 4h 内用 last good，否则强制 `risk_off`。每周 flow：保留旧 watchlist、明早重试 |
| LLM 全断 | OpenRouter health monitor | `llm_degraded` 模式：所有 touchpoint 用 last-good cache。**交易继续 deterministic 跑** —— 用现有 watchlist 和机械 gate |
| 输出"清仓"恐慌 | 变化率限制 validator：每周一次 run 最多改 25% 的 watchlist | 超出部分排到下周 |
| Rescue 5-way 不一致 | 无多数 → 强制 `escalate_to_human`（critical 邮件） | 不自动操作；bag-holder freeze 维持 |

### 4.6 数据输入 —— Adapter 生态

**从 options-copilot 复用：**
- Alpaca（chain、价格、technicals）
- Finnhub（基本面、earnings、estimate）
- FRED（宏观）
- Unusual Whales（资金流、IV percentile）
- EDGAR（10-K / 10-Q）
- Massive（新闻）
- yfinance（兜底）

**新增 adapter：**

1. **Earnings 电话会议 transcript** —— `adapters/transcripts.py`。**API Ninjas**（$10-50/月）起步；若覆盖太差再升级到 Seeking Alpha API（$199/月）。M4 需要。
2. **Web fetch** —— `adapters/web_fetch.py`。通用 httpx + readability，处理新闻/filing 里出现的 URL。每次 LLM run 限 5 次 fetch，30 天缓存。M4 需要。
3. **分析师 revision 趋势** —— 推迟。Finnhub 有 estimate 但 revision history 浅。Tipranks / Visible Alpha（$200+/月）过度。
4. **内幕交易** —— 已在 EDGAR adapter（Form 4）。
5. **行业 ETF 动量** —— 用 Alpaca bars 拉 SPDR sector ETF (XLK/XLF/XLE 等) 推。不需要新 adapter。

**新增数据成本：约 $10-50/月。** 加上 LLM ~$40-50：AI 层合计 ~$60-100/月，<月毛 premium 2%。

---

## 5. Forward Test 与上线方案

Alpaca paper 10 周 + 真钱 6 周阶梯上线。

### 5.1 分阶段推进

#### Phase 1 —— 单 ticker，纯 mechanical（Week 1-2，2026-06-16 → 2026-06-29）
- **范围：** 1 个 ticker（SPY），最多 1 张合约，无 LLM。Watchlist 写死。入场/退出全 deterministic。
- **进入条件：** M0-M2 完成；手工 dry-run 至少跑通一次完整 CSP→assign→CC→called-away；`.env` 配好 Alpaca paper creds；heartbeat 在更新；日报邮件能到收件箱。
- **退出条件：** 至少完成 1 个完整 cycle 且无人工介入；0 个 orphan 订单（PENDING/SUBMITTED >10 分钟）；0 个 reconcile drift；日报 ≥8/10 天到达。

#### Phase 2 —— 5 个 ticker，仍 deterministic（Week 3-4，2026-06-30 → 2026-07-13）
- **范围：** SPY、QQQ、AAPL、MSFT、GLD。静态 watchlist。LLM 仍关。Per-ticker risk budget 启用。每 ticker ≤1 张。
- **进入条件：** Phase 1 退出条件全绿；风险预算 config 校验通过；行业集中度的单元测试通过。
- **退出条件：** 5 个都至少开过一次 CSP；至少 2 个完成完整 cycle；0 个 portfolio CB 触发；reconcile 连续 10 天通过；fill slippage <5% vs 理论 mid。

#### Phase 3 —— 完整 watchlist + LLM 开启（Week 5-8，2026-07-14 → 2026-08-10）
- **范围：** 8-12 个 ticker，LLM watchlist curation 每周日 18:00 跑，LLM rescue 触发器启用，regime read 每 cycle 一次。
- **进入条件：** Phase 2 退出全绿；LLM prompt 入库且有 hash；OpenRouter 上线；`model_outputs` 每次调用都记录成本；LLM fallback path 验证过。
- **退出条件：** Watchlist 上至少完成 4 个完整 cycle；LLM-curated watchlist 在 premium yield 上比 Phase-2 baseline 高（≥4 周窗口）；0 个 rescue 决策是用户 review 时会推翻的；LLM 成本 ≤$30/周。

#### Phase 4 —— 压测 + 边缘场景（Week 9-10，2026-08-11 → 2026-08-24）
- **范围：** 不加新功能。注入 §5.4 列出的压测场景 + review Phases 1-3 自然出现的事件。
- **进入条件：** Phase 3 退出全绿；近 14 天 0 个 critical bug。
- **退出条件：** 全部 9 个压测场景都已发生过（自然或注入）且被正确处理；§5.3 所有 go-live gate 全绿。

任一 phase 退出失败 → **重复该 phase 一周**。总 buffer：2 周。Week 12 之后 → 重新规划。

### 5.2 每日追踪的指标

**账户级（每 cycle snapshot，永久保留）：**
- equity、cash、buying_power、margin_used（来自 `account_snapshots`）
- `cash_reserved_for_csp` —— 所有未平 CSP 的 (strike × 100 × contracts) 总和
- day_pnl、cumulative_pnl、cumulative_pnl_pct

**每个未平仓位（每日 snapshot）：**
- delta、theta、gamma、iv（拉自 chain）
- dte、days_held、unrealized_pnl、pct_to_profit_target
- 对 shares：cost_basis、current_price、pct_vs_basis、cc_eligible

**每个 cycle（`cycle_log` 一行）：**
- ticker、started_at/ended_at、cycle_time_days
- total_premium_collected、realized_pnl、unrealized_pnl_at_end
- n_csps、n_assignments、n_ccs、n_rolls
- outcome：csp_closed | called_away | still_holding | force_closed
- annualized_yield、win

**每日聚合（`wheel_daily_metrics`）：**
- assignment_rate（30 日滚动）
- avg_cycle_time_days（30 日滚动）
- avg_premium_yield_per_csp_pct
- win_rate_cycles
- max_drawdown_pct（peak 累计）
- max_single_ticker_drawdown_pct

**系统健康：**
- missed_runs_count（30 天）
- api_error_rate（1 小时滚动）
- avg_cycle_latency_seconds
- reconciliation_drift_count（必须每日 0）

**LLM：**
- llm_calls_today、llm_cost_usd_today、llm_cost_usd_week
- llm_p95_latency_ms、llm_failure_rate
- llm_watchlist_premium_yield_vs_baseline（每周）

**保留策略：** 所有 snapshot 永久存 SQLite。`model_outputs.prompt_text` 保留 90 天后 truncate 为 hash。

### 5.3 上线决策 Gate

所有阈值对 **Phase 3-4 最后 4 周** 的 paper 数据测量。**全部** 绿才能上 live。

**系统可靠性：**
- 近 28 天 0 个 critical bug（数据丢失、错误方向单、ghost 仓位）
- 错过 daemon run <2%（即 ≥98% 的预期 morning+EOD cycle 完成且 `ok`）
- 0 个 reconcile drift >1 个交易日未解决
- Heartbeat：`daemon_status.last_heartbeat` 在开市窗口里从未老于 6 小时

**业绩：**
- 累计 P&L 减去 (commission × 1.5) 在 10 周中 >0
- Cycle 胜率 ≥60%，至少 8 个完整 cycle
- Max drawdown <5% 起始资金（$25K on $500K）
- 年化 premium yield (paper) ≥8% —— 低于此则策略不值得操作风险

**风险：**
- 第 7-10 周 0 个 portfolio CB 触发
- 0 次 CC 试图在 strike 低于 cost basis 提交
- 第 9-10 周 0 次需要人工介入

**LLM 质量（v1.1：从 "premium yield 对比" 改为 shadow A/B 多维评估）：**

外部 council 指出：用 paper PnL 来证明 LLM watchlist 比 baseline 强，样本太小且 Alpaca paper fill 失真。改为 **shadow A/B**：

- **Shadow A/B 设置：** 每周日 LLM curate 出 watchlist A；同时维护一个 frozen baseline watchlist B（Phase 2 static list）。两个 watchlist 都跑 gate + 模拟入场决策（不真的下单 B 的），记录后续 4 周 metrics
- **评估维度（不再单看 PnL）：**
  - **候选质量（candidate quality）：** A vs B 通过全部 gate 的候选数、平均 IV rank、平均 OI、平均 spread
  - **命中率（hit rate）：** 被推到 core 的 ticker，4 周后是否仍满足入场条件（earnings 漂移、IV 崩塌、被退市等导致失效的比例越低越好）
  - **后续风险事件率（risk event rate）：** A 推的 ticker 在持有期间触发 G19 single-name gap、rescue trigger、forced exit 的比例，应显著低于 B
  - **多样性 / 集中度：** A 的 sector / sub-industry 分布是否合理
- **Gate 阈值（v1.1）：**
  - A 在"命中率"上 ≥ B + 10pp（百分点），或
  - A 在"风险事件率"上 ≤ B − 5pp，或
  - 两者综合 score ≥ 70/100（按候选质量 30% + 命中率 40% + 风险事件率 30% 加权）
- 0 个 rescue 决策用户 review 时会推翻
- LLM 成本 ≤$30/周（v1.1：模型缩 3 个后实际 <$10/周）

**可观测性：**
- 日报邮件 ≥48/50 个交易日送达
- Workspace JSON 审计 trail 100% 交易日存在且可解析
- 每个 Phase 4 注入的压测事件 5 分钟内有推送通知

### 5.4 压测场景

| # | 场景 | 如何模拟 | 期望行为 |
|---|---|---|---|
| 1 | CSP 持有期间 earnings beat/miss | 自然发生（每 4-12 周/ticker 一次） | G1 应该挡住。若 earnings 日程后移进入 → 提前 2 个交易日强制 roll-out 或 close |
| 2 | VIX 突然飙升 | 等自然；若 Week 9 仍未发生，注入 VIX cache = 32 跑一个 cycle | 冻新仓 (G3)；旧仓位保留；regime LLM 确认 |
| 3 | 多日 drawdown | 自然 | CB1 触发 → 24h 冻 |
| 4 | Broker API 故障 | 注入 Wk 9：Alpaca client 返回 503 持续 30 分钟 | 进入只读模式；不提单；推送告警；恢复后自动；reconcile 重跑 |
| 5 | LLM 服务故障 | 注入：kill API key 一整 cycle，Wk 9 | Mechanical path 继续；rescue 跳过（记录）；cycle 完成 |
| 6 | 到期日 pin risk | 合成一个 CSP，距到期 1 天且 strike 在 mid 0.5% 内 | 早盘 cycle 检测到 pin risk；明确 roll-out 或接受 assignment；绝不模糊处理 |
| 7 | Assignment 通知漏 / 延迟 | 跳过周一早 reconcile；周二恢复 | 周二 reconcile 检测到漏掉的 assignment、更新 DB、算 basis、下个 cycle 开 CC；告警 |
| 8 | Cost basis 算错（分红 / 拆股） | 找一个即将分红的 ticker（真）；拆股则模拟 | Basis 按分红调整；CC strike floor 用调整后 basis。拆股 → 人工 review flag |
| 9 | 同日多个 assignment | 注入：3 个 deep-ITM CSP 同周五到期，3 个不同 ticker | 周一早 3 个一起处理；`positions` 表无 race；组合上限被执行 |
| **10** | **🆕 Ex-div 提前行权（CC）** | 找一个真实即将 ex-div 且有 deep ITM CC 候选的 ticker；或合成 ex-div 前 CC | EOD ex-div 预检触发；强制 close CC 或检测到 called-away 后用 `assignment_lifecycle.process_called_away` + ex-div flag 正确入账（不收 dividend） |
| **11** | **🆕 MLEG Roll partial fill** | 注入：roll 订单的 long leg 成交、short leg 未成交（设置极宽 limit price 让 short 不到位） | FSM 转 `PENDING_ROLL`；5 分钟超时；cancel 未成交 leg；如果 cancel 失败 → `MANUAL_REVIEW` + critical 推送 |
| **12** | **🆕 交易暂停 / LUDP** | 注入：API 返回 `halt` 状态 30 分钟 | 该 ticker 暂停所有新决策；现有仓位继续监控；解除后正常恢复 |
| **13** | **🆕 Chain 缺 Greeks** | 注入：Alpaca chain 响应缺 delta / gamma 字段 | `csp_selector` / `cc_selector` 拒绝选；G13 触发 stale-data；操作员告警 |
| **14** | **🆕 公司行动**（special dividend / spin-off / merger 调整） | 找一个真实即将 spin-off 的 ticker，或注入 `corporate_actions` 表 | `wheel_states` 进 `CORPORATE_ACTION` 状态；操作员手动 unfreeze 前不交易；OCC 调整后 basis 重算 |
| **15** | **🆕 Half-day cutoff** | 注入：market_clock 报告今天 13:00 ET 收盘 | EOD cycle 移到 13:15 ET 跑；intraday cycle 在 13:00 后停 |
| **16** | **🆕 Bid-ask spread 暴增到 20%** | 注入：chain mid 不变，spread 扩到 20% mid | G6 拒新仓；roll 决策走"挂 limit 等"路径，绝不 market order 跨 spread |

1-3 在 10 周里大概率自然发生；4-16 需要注入。每个有一个独立脚本在 `scripts/stress/` 下。

### 5.5 可观测性

**日报邮件（EOD 16:15 ET，EOD cycle 完成后）。** 复用 `options-copilot/skills/daily-report/scripts/report.py` 的多段 HTML 模板。Wheel 特有 section：
- **Wheel 状态板** —— 每 ticker 一行：state、DTE、P&L、在此状态天数
- **Cycle 进度** —— 每个 active cycle：已过天数、已收 premium、预期 vs 实际
- **Watchlist 健康** —— 今日通过全部 G1-G18 的候选数；若 0，原因
- **Cost basis 报告** —— 每个 share 仓位：basis vs current、cc_eligible flag
- **LLM 活动与成本** —— 今日 call 数、本周累计、与 baseline yield 对比

**推送 / Slack 通知（ntfy.sh 或 Slack webhook）。** 需当日人工注意的事件：
- Broker API 进入只读模式
- 任何 portfolio CB 触发
- 单 ticker 浮亏 >20%（rescue 触发）
- Reconcile drift >1 交易日
- LLM rescue 建议 "force close"（建议性，不自动执行）
- Daemon heartbeat >6h 在开市时间内陈旧
- 日报 **发送失败**

**Workspace JSON 审计 trail。** 与 `options-copilot/workspace/YYYY-MM-DD/` 同模式。每日：`cycle_input.json`、`entry_decisions.json`、`exit_decisions.json`、`llm_calls/*.json`、`reconcile.json`、`daily_report.{html,json}`、`metrics.json`。每个 ticker 子目录：当日 proposal/review。

**Dashboard。** v1 不做 web UI。日报 + `scripts/status.py`（CLI 按需打印 wheel 状态板）足够。

### 5.6 Paper → Live 资金阶梯

| Stage | 资金 | Ticker 数 | 时长 *(v1.1)* | 进下一阶段的 gate |
|---|---|---|---|---|
| **🆕 Live-μ** *(v1.1)* | **$5-10K** | 1（SPY） | **≥20 交易日或 1 完整 cycle，取长** | Live fill / slippage 真实 校准；与 paper 对比建立"divergence baseline"；用户批准 |
| **Live-α** | $50K | 2（SPY、QQQ） | **≥20 交易日或 1 完整 cycle，取长** | §5.3 所有 gate 在 live 数据上仍绿；与 paper / μ 比 0 个 surprise；用户批准 |
| **Live-β** | $150K | 4（加 AAPL、MSFT） | **≥20 交易日或 1 完整 cycle，取长** | Live P&L 减去真手续费仍正；reconcile 干净；slippage <20% vs paper。用户批准 |
| **Live-γ** | $300K | 6-8（完整 core） | **≥20 交易日或 1 完整 cycle，取长** | 0 个 CB 触发；LLM 行为与 paper 一致。用户批准 |
| **Live-1.0** | $500K | 完整 LLM-curated（8-12） | open | 稳态 |

**🆕 v1.1：Live-μ micro-test 的目的**（外部 council 强烈建议）

Alpaca paper fill 在期权上极度不真实（基本按 mid 瞬间成交），Phase 2 设定的 "<5% slippage" 在 paper 中没有验证价值。直接跳 paper → $50K 会在 Live-α 撞上严重 slippage 损耗。Live-μ 用 $5-10K 的"实验性"资金提前校准：
- 真实 fill price vs paper mid 的差距分布
- Real bid-ask spread 在 SPY/QQQ 上的实际表现
- Alpaca live API 与 paper API 的行为差异（限价单、cancel 行为、ack 延迟）
- 真实手续费、SEC 费、ORF 等小额扣费

**Live-α + Live-β 期间（≥40 交易日）做 paper-live 并行。** Paper 账户用 *同代码、同 watchlist、同 prompt* 继续跑。日报有 "paper vs live divergence" 对比。日 P&L 偏差 >1% 且无可解释原因 → 停下来调查。Live-β 之后 paper 降级为被动监控。

**为什么从 v1.0 "每档 2 周" 改为 "≥20 交易日或 1 完整 cycle，取长"（外部 council 提出）：** 一个完整 wheel cycle（CASH → CSP → assign → CC → called away → CASH）平均 4-8 周，2 周连一个 cycle 都跑不完。20 交易日（约 4 周）是观察一个 cycle 的最低门槛。

**批准 gate：** 用户每次资金升档前书面确认（一行 yes/no 回复每周 review 邮件）。默认 **stay**，不是 **advance**。沉默不等于批准。

### 5.7 每周 Review 清单（周五 EOD，约 20-30 分钟）

打开最新日报，按这个清单走：

**vs baseline 业绩：**
- WoW 累计 P&L 方向 + 量级
- 本周 cycle 胜率 vs 上周
- 年化 premium yield vs 8% floor
- 周内 max DD vs 5% 硬约束

**单 ticker 健康：**
- 任何 ticker 浮亏 >10% → 眼看一下 basis + cc_eligible
- 任何 cycle 运行 >60 天 → flag 为卡住
- 本周 0 交易的 ticker → 为什么？

**系统健康：**
- 本周 `job_runs` 里有 error 或缺行吗？
- 开市时段 heartbeat 缺口 >6h 吗？
- Reconcile drift 未解决？
- API 错误率

**LLM 质量：**
- 读本周 watchlist 提案。会推翻任何吗？
- 读所有 rescue 决策。会推翻吗？
- 成本 vs $30/周 预算

**压测场景：**
- §5.4 自然事件本周发生了吗？处理对了吗？
- Phase 4：所有注入场景都跑过了吗？

**决策树：**
- **全绿** → 进入下个 phase / stage
- **一个黄灯** → 继续但开 issue；下周再看
- **两个黄灯或一个红灯** → 冻新仓、让现有仓位收尾、修、同 phase 多跑一周再前进
- **Live phase + 任何红灯** → 冻新仓、不升档资金、立即推送 push 通知

---

## 6. 工程里程碑

对应 forward-test 的各 phase。工程工作发生在 Paper Phase 1 **之前**。

### M0 —— Shared 层 Bootstrap（Week -4 至 -3，2026-05-19 → 2026-06-01）
**交付物：**
- `wheels_copilot/shared/` 按 §2.3 模块表从 `options-copilot` vendor-copy 过来
- `scripts/sync_shared.sh` diff 工具
- `SHARED_PROVENANCE.md` 含 git SHA
- 🆕 **`shared/` 上游同步 SLA**（v1.1）：每周一次 diff、关键 bugfix 24h 内 sync、用 `contract_tests/` 锁住共享模块的接口
- `pyproject.toml`、`.env` 模板、`.gitignore`（已完成）
- `wheels_daemon.py` 框架（boot APScheduler，job 是 no-op）
- `db/schema.py` + `scripts/init_db.py` —— 新表创建（含 v1.1 新增 `wash_sale_events`、`corporate_actions`、`roll_decisions`）
- 🆕 **DB 抽象层**（v1.1）：SQLAlchemy core 或薄 DAO，paper/live 双 backend，connection string 决定。M0 实现 SQLite path，Postgres path 留 stub
- CI workflow（lint、unit test）

**退出：** daemon 启动、heartbeat、DB 初始化；测试通过。DB 抽象层在 SQLite 后端跑通所有 CRUD。

### M1 —— Wheel 状态机 + 单 ticker MVP（Week -3 至 -2，2026-06-02 → 2026-06-08）
**交付物：**
- `engines/wheel_state_machine.py` + `transitions.py`（手写 FSM，含 v1.1 新增的 `PENDING_ROLL` / `CORPORATE_ACTION` / `ASSIGNED_HELD` / `MANUAL_REVIEW`）
- `engines/csp_selector.py`（按 `config.yaml` 默认值 deterministic 选行权价/DTE）
- `engines/cc_selector.py`（强制 cost_basis floor；含 ex-div 预检逻辑）
- `engines/wheel_exit_plan.py`（50% TP、21 DTE 固定、delta 0.65 stop、delta 0.40 roll trigger）
- `engines/wheel_gates.py`（G1-G21 硬 gate；含 v1.1 G19/G20/G21）
- `engines/assignment_lifecycle.py`（含 ex-div 提前行权特例处理）
- `engines/risk_budget.py`
- 🆕 **`engines/cost_basis_repo.py`**（v1.1）：包含 wash sale 30 日窗口的损失追踪 + cost basis 调整逻辑
- 🆕 **OMS idempotency + per-ticker order lock**（v1.1）：在 `shared/engines/oms.py` 之上加一层薄 wrapper
- 🆕 **Broker activity stream consumer**（v1.1）：长连 Alpaca trade events
- `schemas/wheel_position.py`、`wheel_state.py`、`decisions.py`
- `skills/wheel-cycle/`（per-ticker dispatcher，暂不接 LLM）
- Daemon 的 pre-market / open-validate / morning / intraday / EOD cycle 接到 FSM
- `tests/unit/` 覆盖 FSM 转换、gate、selector、wash sale 计算
- `tests/integration/` 在 synthetic broker stub 上跑完整 cycle（含 ex-div、partial fill、wash sale 等 v1.1 场景）
- `scripts/dry_run_cycle.py`（跑完整 cycle 但不发单）

**退出：** 在 synthetic broker 上能跑通单 ticker SPY 的 CSP→assign→CC→called-away→cash；所有单元 + 集成测试通过；wash sale 边界 case 测试通过。

### M2 —— 多 ticker + Watchlist（Week -2 至 -1，2026-06-09 → 2026-06-15）
**交付物：**
- `db/watchlist_repo.py` + 从 `tickers.yaml` seed
- `engines/portfolio_risk.py`（行业集中度、per-ticker 敞口、净 delta）
- 所有 G8-G12 多仓位 gate 接通
- `engines/roll_decider.py`（deterministic roll/close/take-assignment）
- `engines/rescue_engine.py` skeleton（暂无 LLM；只接 tier T0-T4 标记）
- CB1-CB8 全部接通
- `scripts/reconcile_alpaca.py` 在真 paper 账户上
- Alpaca paper API 端到端集成（真 chain、真订单、真 fill）
- `skills/daily-report/` 加 wheel 特有 section
- `scripts/status.py` CLI

**退出：** Phase 1 进入条件全部满足。可以开始 paper。

### M3 —— Phase 1 运行（Paper Week 1-2）
不加新代码。运行 Phase 1。并行用 branch 推 M4。

### M4 —— LLM 集成层（Paper Week 1-3，并行 Phase 1-2）
**交付物：**
- `shared/adapters/openrouter.py` 验证可用
- `skills/market-outlook/`（Sonnet，每日 regime）
- `skills/code-screener/`（4 阶 pipeline）
- `adapters/transcripts.py`（API Ninjas）
- `adapters/web_fetch.py`（httpx + readability + 30d cache）
- `skills/ticker-evaluate/`（Opus fundamentals 深读）
- `skills/outlook-analysis/`（Sonnet 每周 outlook）
- `skills/watchlist-curate/`（5 模型 council，每周日）
- `skills/rescue-decide/`（5 模型 vote，事件触发）
- `schemas/llm_outputs.py` 涵盖 6 个 touchpoint
- LLM fallback path 测试（kill API key → bot 继续交易）
- `model_outputs` 表记录每次 call 及成本

**退出：** Phase 2 结束时 Phase 3 进入条件满足。

### M5 —— 压测脚本（Paper Week 4-5，并行 Phase 2-3）
**交付物（v1.1：从 6 个扩到 13 个场景）：**
- `scripts/stress/inject_vix_spike.py`
- `scripts/stress/inject_broker_outage.py`
- `scripts/stress/inject_llm_outage.py`
- `scripts/stress/inject_pin_risk.py`
- `scripts/stress/inject_missed_reconcile.py`
- `scripts/stress/inject_multi_assignment.py`
- 🆕 `scripts/stress/inject_ex_div_early_exercise.py`（v1.1）
- 🆕 `scripts/stress/inject_partial_fill_roll.py`（v1.1）
- 🆕 `scripts/stress/inject_halt_ludp.py`（v1.1）
- 🆕 `scripts/stress/inject_missing_greeks.py`（v1.1）
- 🆕 `scripts/stress/inject_corporate_action.py`（v1.1）
- 🆕 `scripts/stress/inject_half_day_cutoff.py`（v1.1）
- 🆕 `scripts/stress/inject_spread_blowout.py`（v1.1）
- 推送通知集成（ntfy.sh 或 Slack）

**退出：** Phase 4 进入条件满足。

### M6 —— Go-Live 准备（Paper Week 10）
**交付物：**
- Live Alpaca 账户开通（与 paper 分开）
- `config.yaml` "live" profile review
- Live API key 安全配置
- 操作 runbook `docs/RUNBOOK.md`
- 🆕 **Postgres 15+ 部署**（v1.1）：本地 / 云端，schema 迁移脚本，paper SQLite → Postgres 全量数据迁移演练 + 回滚演练
- 🆕 **Live-μ kickoff prep**（v1.1）：$5-10K micro 账户激活，仅 SPY，确认所有 CB / 推送链路在 live 下正常
- 在 live API 上 dry-run（不发单）1 个交易日

**退出：** §5.3 所有 go-live gate 全绿；用户签字；Postgres 迁移演练成功。

### M7-M11 —— Live 资金阶梯（v1.1：从 M7-M10 扩为 M7-M11，加 Live-μ）
- **🆕 M7：Live-μ（$5-10K，SPY only）** —— 校准 paper-to-live divergence
- M8：Live-α（$50K，SPY + QQQ）
- M9：Live-β（$150K，加 AAPL + MSFT）
- M10：Live-γ（$300K，6-8 个 core ticker）
- M11：Live-1.0（$500K，稳态完整 watchlist）

---

## 7. 待用户决定的开放问题

Council 都给了 provisional answer，v1.1 把外部 council 已经收敛的问题去掉、把新引入的问题补上。Kickoff 前未处理的部分，按上面 provisional 答案推进。

**已 resolved（v1.1 应用 council 后已经确定）：**

| # | 问题 | 决定 |
|---|---|---|
| ~~5~~ | LLM 模型组合 | ✅ **3 模型 council**：Opus 4.7 + GPT-5.4 + Gemini-3.1-Pro（v1.1 终态） |
| ~~7~~ | per-ticker 上限 | ✅ **6%**（$30K，v1.1 已 lock） |
| ~~8~~ | Drawdown CB 阈值 | ✅ **6%**（$30K，v1.1 已 lock） |

**仍待用户决定：**

| # | 问题 | Provisional 答案 | 为什么问 |
|---|---|---|---|
| 1 | **代码复用方式** | 现在 vendor-copy → 后续抽 pip package | 确认 vs monorepo 或 day-1 就抽 package |
| 2 | **账户类型** | taxable（含 v1.1 wash sale 追踪） | Alpaca 不支持 IRA。Wash sale 短期 capital gain 处理已纳入正文，但确认你接受这套税务复杂度 |
| 3 | **初始 Watchlist seed** | §3.3 列出的 15 个（7 core + 8 satellite） | 想加 / 减哪些？有"绝对不持有"列表？ |
| 4 | **推送通知通道** | 建议 ntfy.sh | 接受？还是用 Slack / SMS / 只邮件？ |
| 6 | **Live-α 起始资金** | $50K（$500K 中的 10%） | v1.1 已加 Live-μ ($5-10K) 在 α 之前。是否仍想用 $50K 作 α？ |
| 9 | **Earnings transcript 提供商** | API Ninjas（约 $30/月起步） | 这个成本可接受？或者跳过、只靠 10-Q？ |
| 10 | **Dashboard / Web UI** | v1 跳过（只 CLI + 邮件） | 想早点有个最小 web view？ |
| 11 | **Live 期间是否 paper 并行** | 是，Live-α + Live-β 前 4-8 周 | 有用还是浪费？ |
| 12 | **账户号** | wheels 用单独 Alpaca 账户 | 确认 —— 不与 options-copilot 共用账户 |
| **🆕 13** *(v1.1)* | **Postgres 部署位置** | 本地 / Docker / 云（Supabase / RDS / Railway） | M6 阶段决定即可，但 hosting 路径影响 SLA |
| **🆕 14** *(v1.1)* | **Live-μ micro-test 金额** | $5-10K | 确认 —— 想 $5K（更便宜）还是 $10K（更接近 $50K 行为）？ |
| **🆕 15** *(v1.1)* | **basis-below CC 例外路径是否启用** | 是，但仅 T3/T4 + 人工批准 + 季度损失预算 ≤1% equity | 接受？还是更激进（直接禁止）/ 更宽松（T2 即可）？ |
| **🆕 16** *(v1.1)* | **`shared/` 上游 sync SLA** | 每周一次 diff、bugfix 24h sync | 接受？还是更紧（每日）/ 更松（每月）？ |

---

## 8. Council 与过程说明

本 plan 经过 **两道 council** 才进入 v1.1：

**Council 1（起草 council，2026-05-18 上午）** —— 4 个 specialist subagent 并行起草后综合：

1. **System Architect**（Plan subagent）—— 负责 §2 + 模块表 + 状态机 + DB schema
2. **Quant / Risk Specialist**（general-purpose subagent）—— 负责 §3 + 参数 + 标的池 + circuit breaker
3. **AI / LLM Integration Specialist**（general-purpose subagent）—— 负责 §4 + 接入点 + prompt + 成本
4. **Forward Test Strategist**（general-purpose subagent）—— 负责 §5 + 10 周方案 + go-live gate + 上线阶梯

每个 council 成员都收到：
- `docs/RESEARCH.md` 作为必读
- 用户 4 条 lock 决策
- 限定的 scope + 字数预算（1500-2500 词）
- 输出 markdown 由我综合 → v1.0

**🆕 Council 2（外部 review council，2026-05-18 下午）** —— 通过 [`~/.claude/skills/council-review.md`](file:///Users/tianyuwang/.claude/skills/council-review.md) 触发：

1. **GPT-5.4**（`openai/gpt-5.4` via OpenRouter）—— 独立审阅 v1.0
2. **Gemini-3.1-Pro**（`google/gemini-3.1-pro-preview` via OpenRouter）—— 独立审阅 v1.0
3. **GPT-5.4 二次调用**做 3 方综合（Claude proposal + GPT-5.4 review + Gemini review）→ consensus

外部 review 给出了 11 项必改清单，v1.0 → v1.1 把每条都应用到正文里。原始 review + consensus 见 [附录 A](#附录-acouncil-review-2026-05-18-已应用)。

**这与 `options-copilot` 的 5 模型交易 council 不同：**
- **options-copilot council：** 5 个同角色 LLM 提议交易、互相 blind-score、投票 → 共识
- **wheels-copilot 起草 council：** 4 个不同角色 specialist 写互不重叠的章节，我做跨章节冲突 reconcile
- **wheels-copilot 外部 review council：** 2 个独立 LLM 做 adversarial review + 1 个综合 —— 像 PR review

3 种模式都符合 "council" 精神 —— 多元视角 + 结构化聚合。第一种适合 *同类决策的共识*；第二种适合 *不同维度的深度*；第三种适合 *Claude 自身盲点的发现*。Project planning 这种"先起草，后审视"的二段 council 模式是 v1.1 的核心方法论改进。

**综合阶段 reconcile 的跨章节冲突（Council 1）：**

| 冲突点 | Architect | Quant | LLM | Forward Test | 决议 |
|---|---|---|---|---|---|
| Watchlist 刷新日 | 周五 | — | 周日 18:00 | — | **周日 18:00**（LLM agent —— 数据更全、避开工作日） |
| Per-ticker 上限 | 10% | 8% | — | — | **8%**（Quant —— 风险数字归 Quant 管） |
| VIX gating | 单阈值 30 | 渐进（28/22） | — | — | **渐进**（Quant） |
| 日亏 CB | 2% | 1.5% | — | — | **1.5%**（Quant —— 更保守） |
| IV rank 阈值 | config 写 20-70 | 25-65 | screener 用 20-70 | — | **Screener 20-70、入场 gate 25-65**（screener 给更多候选；入场更严） |
| 日报发送时间 | 16:15 | — | — | 16:00 | **16:15**（EOD cycle 完成之后） |

---

**Plan v1.1 完。** 下一步：处理 §7 的开放问题，然后开始 M0 工程。

---

## 附录 A：Council Review（2026-05-18 已应用）

> **状态：** ✅ 已应用到正文（v1.0 → v1.1）。本附录保留作为审计记录 + future reference。
>
> **触发方式：** [`~/.claude/skills/council-review.md`](file:///Users/tianyuwang/.claude/skills/council-review.md)
>
> **审计 JSON：** `/tmp/council_review_audit_20260518_110724.json`
>
> 下面是 GPT-5.4 和 Gemini-3.1-Pro 对 v1.0 的原始 review，以及 GPT-5.4 综合的 consensus。**正文已经按 consensus 逐条修订**，请以正文 §0-§8 为准。

---

## GPT-5.4 Review Summary

1. **真钱隐藏风险**  
- §2.5/§5.4：assignment 只靠 morning reconcile 不够。美式期权可被**提前行权**（尤其 ex-div 前 deep ITM CC），且 Alpaca 事件/持仓更新可能延迟；需加 **broker activity stream + EOD/开盘前双重 reconcile**，否则会在已被 call away 后继续挂 CC/CSP。  
- §3.5：`K ≥ cost_basis` 过于理想化。真钱里长期套牢会导致**永远卖不出 CC**，机会成本巨大；应允许 **受控 basis-below call**，但仅限 T3/T4、需 realized loss budget。  
- §2.7/§3.6：未覆盖 **partial fill / cancel-replace / duplicate submit / stale order ack**。$500K 下一个幂等缺陷就能多卖一倍合约。OMS 上层必须有 **idempotency key + open-order inventory lock**。  
- §2.6：SQLite+WAL 跑真钱 daemon 有风险，遇到进程崩溃/文件锁/磁盘满恢复脆弱；至少 live 前换 **Postgres**。  

2. **过度/欠工程**  
- §4.1.2/§4.3：5 模型 council 用于周 watchlist，对单一 Wheel 偏**过度工程**；先 2-model + deterministic screener 足够。  
- §2.1 vendor-copy 15 文件可以，但到 live-1.0 仍 vendor-copy 偏**欠工程**：shared bugfix 漏同步是真风险。建议 kickoff 就定义 **upstream sync SLA + contract tests**。  
- §5.5 不做 dashboard 可接受，但 §5.7 仍靠人工看日报发现异常，对真钱偏**欠工程**；至少要有 **real-time exception panel**（open risk、stale reconcile、CB 状态）。  

3. **不切实际假设**  
- §0/§3.7：$500K Wheel 年化净 6–9%、max DD 6–10% 对含单股池子略乐观；若遇 2022/2020 类 regime shift，DD 可显著超过 10%。  
- §5.1/§5.3：10 周 paper 得出“LLM watchlist 有效”样本太短，且 paper fill/slippage 与 live options 差异很大。  
- §3.1：SPY/QQQ 放宽到 12% 尚可，但组合里 QQQ+AAPL+MSFT+GOOGL 相关性极高，§3.1 的 sector cap 不能替代 **factor/correlation cap**。  

4. **Forward test 盲点**  
- §5.4 缺少 **early assignment on CC before ex-div**、**halt/LUDP**、**option chain missing Greeks**、**corporate action（special dividend / spin-off / merger adjustment）**、**market half-day cutoff**。  
- §5.3 用 “paper premium yield vs baseline” 评估 LLM 有数据污染；应做 **shadow watchlist A/B**：同日同 gate，比候选质量，不比 fill 后 PnL。  
- §5.6 Live 阶梯太快；每档 2 周不足以覆盖一次完整 assign→CC→called away。建议每档至少 **1 个完整 cycle 或 20 交易日**。  

5. **参数修改建议**  
- §3.2：CSP delta 调成 **0.16–0.22，target 0.18**；CC delta **0.15–0.25，target 0.20**。  
- §3.2：DTE 调成 **35–50，target 42**；time-stop **21 DTE 固定**，不要随 IVR 漂移。  
- §3.2/§3.4：IV rank 入场改 **30–55**；>55 仅 ETF/Tier1，单股禁开。  
- §3.1：per-ticker 上限从 **8% 降到 6%**；SPY/QQQ 上限 **10%**。  
- §3.6：日亏 CB 从 **-1.5% 收紧到 -1.0%**；DD CB 从 **8% 收紧到 6%**；新增 **single-name gap CB：单票日跌>12% 禁该 ticker 新动作 5 天**。  
- §3.4：earnings blackout 从 **±7 扩到 entry/expiry 任一落在 ±10 日拒绝**。  
- §3.2：max_roll_count 从 2 降到 **1**，第二次直接 assignment/close/rescue。

---

## Gemini-3.1-Pro Review Summary

这是一份非常扎实的系统设计文档，架构清晰且对风险有敬畏之心。但作为 $500K 真钱生产系统，仍有几个关键的量化与工程盲点需要修正：

**1. 隐藏风险与边缘场景（真钱场景）**
*   **Wash Sale（洗售）税务灾难（§1, §3.5）：** 在 Taxable 账户跑高频 Wheel，亏损 Roll 或平仓极易触发 Wash Sale，导致 Cost Basis 计算极其复杂，甚至引发年底巨额税务拖累。**Action:** `cost_basis_repo.py` 必须引入 Wash Sale 追踪逻辑（30天规则），或者在 §3.3 标的池中强制轮换 Ticker 避免连续交易同一标的。
*   **分红提前行权（Early Assignment）与公司行动（§2.7, §5.4）：** 文档假设 Assignment 都在到期或深度 ITM 发生。实际中，除息日（Ex-dividend date）前夕的 Short Call 极易被提前行权。**Action:** FSM 需增加 `CORPORATE_ACTION` 冻结状态；§5.4 压测必须注入“除息日前 1 天的 ITM Short Call 被提前行权”场景。
*   **Roll 订单的 Partial Fill（§2.4, §2.7）：** Roll 是 MLEGs（多腿订单），流动性差时极易出现 Partial Fill。**Action:** FSM 缺少 `PENDING_ROLL` 状态。不能仅靠 OMS，FSM 必须知道当前仓位处于“部分平仓/部分开仓”的薛定谔状态，并暂停其他并发决策。

**2. 过度工程化与欠工程化**
*   **过度工程化（§4.1.2, §4.1.5）：** 5 模型 Council 用于每周 Watchlist 和 Rescue 过于冗余，增加了解析失败率和延迟。**Action:** 降级为 3 模型（如 Opus + GPT-4o + Sonnet），采用 2/3 多数决即可。
*   **欠工程化（§3.5）：** Cost Basis 的计算过于理想化。如果发生 Roll-down（支付 Debit），Cost Basis 会上升。**Action:** 明确公式：`Adjusted Basis = Strike + Roll Debits - All Credits`。代码中必须硬性防范“为了赚 Premium 而无限 Roll-down 导致 Basis 远高于现价”的死亡螺旋。

**3. $500K 真钱部署的不切实际假设**
*   **Paper 账户的 Fill 幻觉（§5.1）：** Alpaca Paper 账户的期权 Fill 极其不真实（通常瞬间在 Mid-price 成交）。Phase 2 设定的 `slippage < 5%` 在 Paper 中毫无意义。**Action:** 必须在 Phase 3 引入 $5K-$10K 的 Live Micro-test 专门校准 Slippage 和 Bid-Ask 行为，否则 Live-α 会遭遇严重的执行损耗。

**4. Forward Test 计划盲点**
*   **流动性枯竭压测（§5.4）：** 缺少对 Bid-Ask Spread 突然扩大的测试。**Action:** 注入场景：标的暴跌时，期权 Spread 扩大到 20%，触发 §3.2 的硬约束，系统应正确挂单等待或暂停 Roll，而不是以 Market Order 跨越 Spread 强平。

**5. 关键参数修改建议**
*   **Roll 触发条件（§3.2）：** `delta >= 0.50 AND <=21 DTE` **太迟了**。Delta 0.50 意味着已经 ITM，此时 Roll 的 Spread 极宽，极难获得 Net Credit。**建议：** 改为 `delta >= 0.40 OR (DTE <= 14 AND ATM)`，在尚未深度 ITM 时提前防守。
*   **IV Rank 上限（§3.2, §3.4）：** 限制 `IVR <= 65` 会砍掉 Wheel 策略最赚钱的肥尾机会（如大盘恐慌但公司基本面无虞时）。**建议：** 移除 65 的硬上限，改为 `IVR > 20`，依靠 §4.1.4 的 LLM Outlook 来过滤真正的破产风险（Idiosyncratic risk）。
*   **日亏 CB 阈值（§3.6）：** `-1.5% ($7.5K)` **太紧**。在 70% 仓位利用率下，大

---

## Three-Model Consensus

**Final consensus**

**1. 强一致观点**
- **Wheel 必须 FSM + deterministic-first**：LLM 只做 watchlist/基本面/前景/救援，不进 strike、DTE、仓位 sizing 的 critical path。  
- **Broker 为真相源，reconcile/审计必不可少**：需完整 workspace、job_runs、成本与决策留痕。  
- **先 paper 再 live，且资金阶梯上线**：不能直接 $500K 全开。  
- **早指派/公司行动/异常流动性是核心生产风险**：必须专项处理，不可只靠到期逻辑。  
- **vendor-copy 可接受，但必须有同步纪律**：shared 复用要配 provenance、sync 与 contract tests。

**2. 分歧与解决**
- **CC 是否允许低于 cost basis**：Claude 禁止；GPT/Gemini认为极端套牢会僵死。**折中：**默认仍 `K>=basis`；仅 **T3/T4 + 人工批准 + realized loss budget ≤账户净值1%/季** 时允许 basis-below call。  
- **LLM council 规模**：Claude 5 模型；两家 review 认为过重。**改为** watchlist/rescue 用 **3 模型**，新入 core 仍需 **3/3**。  
- **风险参数松紧**：GPT 主张更保守，Gemini认为部分阈值过紧/过迟。**折中：**收紧仓位与 DD，但放宽“日亏 CB”。  
- **SQLite vs Postgres**：Claude 用 SQLite；GPT 认为真钱不够稳。**结论：**paper 可 SQLite，**live 前强制切 Postgres**。  
- **10 周 paper 是否足够验证 LLM**：不足。**改为** LLM 只做 **shadow A/B watchlist** 评估，不用 paper PnL 证明“有效”。

**3. Kickoff 前必改清单**
- **§2.5**：仅 morning reconcile → **加 08:00/09:20/16:15 三次 reconcile + broker activity stream**。  
- **§2.7**：补 **idempotency_key、open-order inventory lock、duplicate submit 防护**。  
- **§2.4/§2.7**：FSM 新增 `PENDING_ROLL`,`CORPORATE_ACTION`。  
- **§2.6**：`wheels_copilot.db(SQLite)` → **paper=SQLite；live=Postgres 15+**。  
- **§3.1**：`per_ticker_max_pct 8%→6%`；`SPY/QQQ 12%→10%`。  
- **§3.2**：`put target 0.22→0.18`；`call target 0.25→0.20`；`DTE 28-45/35→35-50/42`；`max_roll_count 2→1`。  
- **§3.4**：earnings blackout `±7→±10`；新增 **single-name gap CB：单票日跌>12%，冻结5交易日**。  
- **§3.6**：`drawdown_cb 8%→6%`；`daily_loss_cb -1.5% 保持`。  
- **§4.1.2/4.1.5**：5 模型 council → **3 模型**。  
- **§5.3**：删 “LLM watchlist premium yield ≥5%” → **shadow A/B：候选质量、命中率、后续风险事件率**。  
- **§5.4**：新增压测：**ex-div 提前行权、partial fill roll、halt/LUDP、missing Greeks、special dividend/spin-off/merger、half-day cutoff、spread 扩至20%**。  
- **§5.6**：每档 live `2周` → **至少20交易日或1个完整 cycle（二者取长）**；并新增 **$5K–$10K live micro-test**。

---

*Council review 由 [`~/.claude/skills/council-review.md`](file:///Users/tianyuwang/.claude/skills/council-review.md) 触发执行于 2026-05-18 11:08:07。模型：`openai/gpt-5.4` + `google/gemini-3.1-pro-preview`，consensus 由 `openai/gpt-5.4` 综合。审计 JSON：`/tmp/council_review_audit_20260518_110724.json`。*
