# Wheels Copilot — 项目方案

**版本：** v1.0
**日期：** 2026-05-18
**状态：** 草稿，等用户 review → kickoff
**输入：** [`docs/RESEARCH.md`](RESEARCH.md)、用户已锁定的 4 条决策、4 人 specialist council

---

## 0. 执行摘要

`wheels_copilot` 是一个 Python daemon，在 $500K USD 的 Alpaca 账户上每日自动跑**单一策略——Wheel**，并在 *定性* 决策上有选择地调用 LLM（选股、基本面分析、前景分析、市场环境分析）。它是 `options-copilot` 的姊妹项目，复用其约 60% 的基础设施。

**本方案的 5 条核心信念：**

1. **Wheel 是状态机，不是策略。** 每个 ticker 是一台独立 FSM（`CASH → CSP_OPEN → ASSIGNED → CC_OPEN → ...`）。系统里每个决策的本质都是"这台状态机今天该做什么"。
2. **Validator-first，默认 mechanical。** 行权价、DTE、profit-take、roll、stop-loss、sizing —— 全部 deterministic、code-enforced。LLM 只做 *定性* 决策（watchlist 维护、基本面深读、救援决策）。LLM 层断了交易也继续。
3. **Watchlist 是 policy；每日交易是 policy 的执行。** Watchlist 每周日晚上 18:00 通过 5 模型 LLM council 重做一次；每日 cycle 机械地按这个 frozen watchlist 执行。
4. **Forward test 是硬 gate，不是走过场。** 10 周 Alpaca paper、4 个 phase。所有 go-live gate 全绿才上 live。资金分 4 步上：$50K → $150K → $300K → $500K。
5. **从第 1 天就按 $500K 真钱设计。** 保守的默认值（per-ticker 上限 8%、日亏 CB -1.5%、drawdown CB 8%）。合理回报预期：**年化净 6–9%**，Sharpe 0.8–1.2，max DD 大约 6–10%。**不追求**在 bull market beat SPY —— 追求的是可生存性 + 稳定现金流。

**Council 成员（本 plan 是 4 个 specialist subagent 输出的综合）：**

| 角色 | 负责 | 章节 |
|------|------|------|
| System Architect | 代码结构、模块、DB、daemon、config | §2 |
| Quant / Risk | 参数、标的池、风险预算、circuit breaker | §3 |
| AI/LLM Integration | LLM 接入点、prompt、成本、降级 | §4 |
| Forward Test Strategist | 10 周 test 方案、go-live gate、资金阶梯 | §5 |

**跨章节冲突的 reconcile：**
- Watchlist 刷新：**周日 18:00 ET**（LLM agent 的方案——数据更全、避开工作日波动）
- VIX gating：分级（≤22 两个 tier 都开 / 22–28 仅 Tier 1 / >28 全冻），而不是单一阈值
- 日亏 circuit breaker：**-1.5% equity（约 $7.5K）**——两个方案中更保守的那个
- Watchlist 权威源：**DB**，`tickers.yaml` 仅做一次性 seed
- LLM 模型分配：5 模型 council 用于 watchlist + rescue；单 Opus 用于 fundamentals；单 Sonnet 用于 outlook + regime

**时间表（从 2026-05-19 kickoff 起算）：**

| 窗口 | 周次 | 阶段 |
|------|------|------|
| 2026-05-19 → 2026-06-15 | -4 至 0 | 工程建设：M0 shared 层 → M2 多 ticker（达到 Phase 1 entry 标准） |
| 2026-06-16 → 2026-06-29 | 1-2 | Paper Phase 1：单 ticker (SPY)，纯 mechanical |
| 2026-06-30 → 2026-07-13 | 3-4 | Paper Phase 2：5 个 ticker，mechanical |
| 2026-07-14 → 2026-08-10 | 5-8 | Paper Phase 3：完整 watchlist + LLM 接入 |
| 2026-08-11 → 2026-08-24 | 9-10 | Paper Phase 4：压测 + 边缘场景 |
| 2026-08-25 → 2026-09-07 | 11-12 | Live-α：$50K，2 个 ticker |
| 2026-09-08 → 2026-09-21 | 13-14 | Live-β：$150K，4 个 ticker |
| 2026-09-22 → 2026-10-05 | 15-16 | Live-γ：$300K，6-8 个 ticker |
| 2026-10-06 起 | 17+ | Live-1.0：$500K，完整 watchlist |

**总计：kickoff 到 $500K 满仓 live 大约 5 个月。**

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
       │       CASH         │◄─────────────────────┐
       │  (无仓位)          │                       │
       └──────────┬─────────┘                       │
                  │ sell_csp (gate.csp_open_ok)     │ csp_expired_otm
                  ▼                                 │ 或 csp_closed_50%tp
       ┌────────────────────┐                       │
       │     CSP_OPEN       │───────────────────────┘
       │  (short put 在手)  │
       └──────────┬─────────┘
                  │ csp_assigned (broker 事件)
                  ▼
       ┌────────────────────┐  rescue_freeze     ┌──────────────────┐
       │      ASSIGNED      │───────────────────►│  ASSIGNED_HELD   │
       │ (100×N 股，        │                    │  (停止自动 CC，  │
       │  无 option)        │◄───────────────────│   人工 review)   │
       └──────────┬─────────┘   operator_resume  └──────────────────┘
                  │ sell_cc (gate.cc_strike_floor_ok)
                  ▼
       ┌────────────────────┐
       │      CC_OPEN       │──── cc_expired_otm ──┐
       │ (short call 在手， │                       │ (回到 ASSIGNED)
       │  100×N 股)         │                       ▼
       └──────────┬─────────┘              ┌────────────────────┐
                  │ cc_called_away         │      ASSIGNED      │
                  │ (broker 事件)          └────────────────────┘
                  ▼
       ┌────────────────────┐
       │  CYCLE_COMPLETE    │── 写入 cycle_log → CASH
       └────────────────────┘
```

`ASSIGNED_HELD` 是 bag-holder 子状态（股价 < cost_basis × 0.85）：停止自动 CC，由 rescue 引擎接管。`CYCLE_COMPLETE` 是瞬时状态——原子写入 `cycle_log` 后回到 `CASH`。

**Watchlist 持久化：** 详见下面的 DB schema。**DB 是权威源**。`tickers.yaml` 只用于一次性 bootstrap，之后所有变更都通过 `skills/watchlist-curate/` → `db/watchlist_repo.py` 走。

**Cost basis 与 cycle 历史：** 两张表。`cost_basis_history` 是 append-only 日志（初始 assignment、CC 收的 premium、分红）。`cycle_log` 是闭合 cycle 的账本（每个完整 CASH→CASH 周期一行）。

### 2.5 Daemon 调度

**单一 APScheduler `BlockingScheduler` daemon。** 三个 cycle，都按 wheel 的节奏量身设计：

**Morning cycle —— 09:45 ET（周一至周五）：**
1. 与 Alpaca 对账（仓位、订单、账户）。Broker = source of truth。
2. 处理隔夜 assignment 通知 → 状态切到 `ASSIGNED`，写入 cost basis。
3. Risk-off 检查（VIX、宏观日历、日亏 CB）。如果 risk-off，跳过第 5 步。
4. 遍历每一行 `wheel_state`，跑 gate：`CSP_OPEN`/`CC_OPEN` → 检查 50%TP / 21DTE / delta breach。`ASSIGNED` → 提出 CC。`CASH` → 如果 watchlist 有效且 gate 通过，提出 CSP。
5. 通过 OMS 提交订单。

09:45 比 `options-copilot` 的 14:30 早，是因为 wheel 不需要 intraday flow 数据 —— 我们要早点拿到 fill 以获得更好流动性。

**Intraday cycle —— 每 15 分钟，10:00-15:45 ET：**
1. 只对账订单（fill、partial、cancel）。
2. 跑机械退出规则：short option delta > 0.65 → 把 roll 决策入队等 EOD 处理。underlying 盘中跌穿 strike 超过 10% → 立即触发 rescue 评估。
3. 无 LLM call，无新仓位。

**EOD cycle —— 16:15 ET（周一至周五）：**
1. 最终对账。捕获 `account_snapshot`。检测 expiry。
2. 处理 expiry：short option OTM 失效 → premium 落袋、状态切换。ITM → assignment 已 posted，明早再 reconcile。
3. 跑 intraday 入队的 roll 决策。
4. 如果今天是周日 EOD → 触发 `skills/watchlist-curate/`（周日特殊，跑在 18:00 ET 而不是 16:15）。
5. 计算每日指标、生成日报。

**Heartbeat —— 每 30 分钟。** 与 `options-copilot` 完全一致。

### 2.6 DB Schema —— 仅列新增表

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
```

### 2.7 订单在 OMS 中的流转

**共享 OMS 直接复用，不改。** 它的状态机是 broker-generic（PENDING → SUBMITTED → FILLED/CANCELED/REJECTED）。Wheels 特有的 lifecycle 住在更上一层的 `wheel_state_machine.py` 里，它观察 OMS 状态变化并做出反应。

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

schedule:
  morning_cycle_et: "09:45"
  intraday_interval_min: 15
  eod_cycle_et: "16:15"
  watchlist_refresh_day: sunday
  watchlist_refresh_time_et: "18:00"

risk:
  per_ticker_max_pct: 0.08              # 详见 §3
  sector_max_pct: 0.25                  # Tier 1 可放宽到 0.35
  daily_loss_cb_pct: 0.015              # -1.5%
  weekly_loss_cb_pct: 0.04
  drawdown_cb_pct: 0.08
  catastrophic_drawdown_pct: 0.15
  per_position_stop_loss_delta: 0.65
  rescue_trigger_drawdown_pct: 0.15
  max_roll_count: 2

defaults:
  put_delta: [0.20, 0.30]
  put_delta_target: 0.22
  call_delta: [0.20, 0.35]
  call_delta_target: 0.25
  dte: [28, 45]
  dte_target: 35
  iv_rank_range: [0.25, 0.65]
  earnings_blackout_days: 7
  macro_blackout_days: 2
  profit_take_pct: 0.50
  time_stop_dte: 21

gates:
  vix_freeze_above: 28
  vix_tier1_only_above: 22
  min_open_interest: 500
  max_spread_pct_of_mid: 0.05
  min_avg_option_volume: 200
  min_credit_pct_of_strike_csp: 0.005
  min_credit_pct_of_strike_cc: 0.004
  max_daily_new_entries: 3

llm:
  watchlist_curate:
    enabled: true
    pattern: council                     # 5 模型 propose + blind-score
    models:
      - "anthropic/claude-opus-4-7"
      - "openai/gpt-5.4"
      - "x-ai/grok-4.20"
      - "google/gemini-3.1-pro-preview"
      - "deepseek/deepseek-v3.2"
    frequency: weekly
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
    pattern: council_vote                # 5 模型 propose + vote（无 blind-score）
    models: [...]                        # 同上 5 个
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

**单 ticker 最大敞口：8% 账户 = $40,000** 名义值（所有未平 CSP + 已 assigned shares 按 strike 计）。$40K 上限意味着即使某只单股 50% 跳空跌停，账户也只损失 4%。可生存。**例外：SPY/QQQ 上限 $60K (12%)**，因为它们无 idiosyncratic risk。

**目标并行仓位数：10**（research 给的 8-10 范围的上限）：
- 10 × $35K 平均 = $350K = 正好顶到 CSP cash 上限
- 10 个名字比 8 个分散得更好
- 少于 10 → 单仓敞口爬太高；多于 12 → earnings 冲突概率非线性升高

**行业集中度：**
- 任一 GICS sector 最多占已部署资金的 **25%**
- Tier 1 sector（指数 + 主导 tech）放宽到 **35%**
- 单一 sub-industry 最多 **2 个名字**（不能 AMD + NVDA 同时开）

**满仓示例（10 个仓位，$386K 部署）：**

| # | Ticker | Strike | Contracts | Reserved $ | Sector |
|---|---|---|---|---|---|
| 1 | SPY | $580 | 1 | $58,000 | Index |
| 2 | QQQ | $470 | 1 | $47,000 | Index |
| 3 | AAPL | $230 | 1 | $23,000 | Tech |
| 4 | MSFT | $420 | 1 | $42,000 | Tech |
| 5 | GOOGL | $180 | 2 | $36,000 | Tech |
| 6 | AMZN | $200 | 2 | $40,000 | Cons. Disc. |
| 7 | JPM | $230 | 1 | $23,000 | Financials |
| 8 | KO | $70 | 4 | $28,000 | Cons. Staples |
| 9 | XOM | $115 | 3 | $34,500 | Energy |
| 10 | UNH | $550 | 1 | $55,000 | Healthcare |
| | | | **合计** | **$386,500** | |

（引擎会把最贵那只缩到 ≤$350K 之内。Cash 闲置约 $150K = 30%，正好。）

### 3.2 默认交易参数

| 参数 | 默认 | 区间 | 理由 |
|---|---|---|---|
| Put delta (CSP 入场) | **-0.22** | -0.15 ~ -0.30 | Research 0.20-0.30 sweet spot 的保守端。约 78% OTM 到期概率 |
| Call delta (CC 入场) | **+0.25** | +0.20 ~ +0.35 | 比 CSP 略高 —— 被 assigned 后想让 called away 的概率合理 |
| DTE target（入场） | **35** | 28-45 | Research 30-45 区间中位。避开 <21 DTE gamma zone |
| 提前关仓 (profit take) | **50% credit** | 40-60% | tastytrade 200K trade 研究。50% 时关（除非 <7 DTE，让它过期） |
| Time stop | **21 DTE** | 18-25 | Research 默认值。IVR ≥50 延到 14 DTE，IVR ≤35 提前到 25 DTE |
| Stop loss | short delta ≥ 0.65 或 close 要付的 debit ≥ 2× credit | 同时 | 两个冗余触发；股票可能比单一规则更快穿仓 |
| Roll 触发 | strike breached AND short delta ≥ 0.50 AND ≤21 DTE | 三者必须同时 | 避免在 noise 上反应过度 |
| 最大 roll 次数 | **2 次** | 硬上限 | Research 默认。第三次决策：take assignment / close / rescue |
| IV rank 入场 | **25 ≤ IVR ≤ 65** | 硬约束 | <25 太便宜不能补偿 tail risk；>65 通常是 event 在被 price in |
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
| G1 | Earnings | underlying earnings ≤ ±7 日（相对入场或到期） |
| G2 | 宏观事件 | FOMC / CPI / NFP / PCE ±2 日内 |
| G3 | VIX | >28 全冻；22-28 仅 Tier 1；≤22 两层都开 |
| G4 | IV rank | <25 或 >65 |
| G5 | OI | <500 在目标行权价 |
| G6 | 价差 | >5% mid |
| G7 | 期权 volume | 20 日均 <200 |
| G8 | per-ticker 唯一性 | 同一 ticker 已经有 CSP/CC/shares |
| G9 | CSP cash 上限 | 加上这笔会超 $350K 总预留 |
| G10 | 行业集中度 | 加上后 sector >25%（Tier 1 >35%） |
| G11 | 子行业冲突 | 同一子行业已有未平仓位 |
| G12 | 单 ticker 敞口 | 预留需 >$40K（SPY/QQQ 例外 $60K） |
| G13 | 数据陈旧 | quote / chain >5 分钟未更新 |
| G14 | Watchlist | Ticker 不在 ACTIVE watchlist |
| G15 | 待 review 的 rescue | 此 ticker 有未解决的 rescue flag |
| G16 | 当日入场上限 | 当日已开 >3 个新 CSP |
| G17 | Drawdown brake | 账户在 CB 状态 |
| G18 | Broker 健康 | Alpaca health monitor 降级 |

### 3.5 Assignment / Bag-Holder 管理

被 assigned 时：
1. Cost basis = strike − 整个 wheel cycle 收到的净 premium（CSP credits − roll debit）。
2. 预留 cash 变成 shares；重算 sector + ticker 敞口。
3. 只有 escalation tier 允许时才提 CC（见下）。

**硬约束：** 任一 CC 的 strike `K` 必须满足 `K ≥ cost_basis`。代码强制。**全系统最重要的一行代码**。

**按 cost basis drawdown 的分级 escalation：**

| Tier | DD | 动作 |
|---|---|---|
| **T0 Normal** | 0% 到 -5% | 标准 CC：delta 0.25、DTE 30-45、K ≥ basis |
| **T1 Watch** | -5% 到 -10% | delta 降到 0.15-0.20；K = max(basis, current × 1.05)。日报里标记 |
| **T2 Stress** | -10% 到 -20% | **暂停自动 CC。** 开 rescue review ticket。LLM rescue 评估：继续持有？远期低 delta CC（<0.10）？加仓拉低 basis？ |
| **T3 Critical** | -20% 到 -30% | **此 ticker 全部自动操作暂停。** 强制 LLM rescue review。给人类 3 个选项 |
| **T4 Forced exit** | <-30% | 5 个工作日内人类必须 review。无输入则默认：3 日 TWAP 退出。Ticker 从 watchlist 移除至少 90 天 |

**强制退出（无视 tier）：** underlying <$15/股、宣布破产、审计失败、持有 >365 天且未恢复到 -10% basis 内、市值跌破 $5B。

### 3.6 组合层 Circuit Breaker

| Breaker | 触发 | 动作 | 恢复 |
|---|---|---|---|
| **CB1 日亏** | 日 P&L < -1.5% equity (约 $7.5K) | 冻新仓 24h | 次日自动 |
| **CB2 周亏** | 5 日滚动 P&L < -4% (约 $20K) | 冻新仓；现有 TP 收紧到 30% | 手动解锁 |
| **CB3 Drawdown** | peak-to-trough DD ≥ 8% (约 $40K) | 停新；CC 一有利润就平；保护现金 | 手动 + DD <5% 连续 5 日 |
| **CB4 灾难性 DD** | DD ≥ 15% (约 $75K) | 完全停；任何 option 都不开；只保 shares | 强制全系统 review |
| **CB5 Broker 健康** | 10 分钟内 API 错误 >10% 或 >5 个连续 5xx | 只读模式 | 30 分钟正常后自动 |
| **CB6 数据陈旧** | quote / IV / earnings >4h 未更新 | 跳过本 cycle | 数据新鲜后自动 |
| **CB7 仓位漂移** | Broker vs DB 不一致 >1 cycle | 暂停新仓 | reconcile 通过后自动 |
| **CB8 订单速率** | 单日新订单 >8 | 当日不再开仓 | 次日自动 |

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
- **模型：** **5 模型 propose-then-blind-score council** —— 直接复用 `options-copilot` 的模式。每个模型独立排序；提案匿名化后再被同 5 模型打分；进入 `core` 需 ≥3/5 同意。
- **成本：** $3-6/run × 每月 4-5 run = **约 $15-25/月**。

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
- **模型：** **5 模型 propose-and-vote**（无 blind-score —— 输出是 categorical，不连续）。多数票胜。平票 → `escalate_to_human`。
- **成本：** $2-4/次。预期 **<$10/月**。

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
| Watchlist curate | 5 模型 propose + blind-score | 最高 stakes；周度 policy；成本不对称偏向 consensus |
| Rescue decide | 5 模型 propose + vote（无 blind-score） | Categorical 输出；防止单模型在灾难路径上过度自信 |
| Fundamentals deepdive | 单 Opus | Evidence 不是 verdict；下游 council 加权 |
| Outlook analysis | 单 Sonnet | 调节信号；不直接执行 |
| Regime read | 单 Sonnet | 每日延迟敏感；规则已经盖了最坏情况 |

### 4.4 成本管理

**稳态月成本：**

| 接入点 | 频率 | $/run | $/月 |
|---|---|---|---|
| Regime read (Sonnet) | 22 交易日 | $0.02 | ~$0.50 |
| Watchlist council (5×) | 4-5/月 | $4 | ~$18 |
| Fundamentals (Opus) | 约 40 ticker-events | $0.40 | ~$16（缓存后 ~$8） |
| Outlook (Sonnet) | 约 80 ticker-events | $0.08 | ~$7 |
| Rescue council | 0-3/月 | $3 | ~$5 |
| **总计** | | | **~$40-50/月** |

vs 预期月毛 premium $3-8K → 成本 <2% 收入。不是约束。

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

**LLM 质量：**
- LLM-curated watchlist 比 frozen baseline 的 premium yield 高 ≥5%（Week 5-10 取 4 周 min sample）
- 0 个 rescue 决策用户 review 时会推翻
- LLM 成本 ≤$30/周

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

1-3 在 10 周里大概率自然发生；4-9 需要注入。每个有一个独立脚本在 `scripts/stress/` 下。

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

| Stage | 资金 | Ticker 数 | 时长 | 进下一阶段的 gate |
|---|---|---|---|---|
| **Live-α** | $50K | 2（SPY、QQQ） | 2 周 | §5.3 所有 gate 在 live 数据上仍绿；与 paper 比 0 个 surprise；用户批准 |
| **Live-β** | $150K | 4（加 AAPL、MSFT） | 2 周 | Live P&L 减去真手续费仍正；reconcile 干净；slippage <20% vs paper。用户批准 |
| **Live-γ** | $300K | 6-8（完整 core） | 2 周 | 0 个 CB 触发；LLM 行为与 paper 一致。用户批准 |
| **Live-1.0** | $500K | 完整 LLM-curated（8-12） | open | 稳态 |

**Live-α + Live-β 期间（前 4 周）做 paper-live 并行。** Paper 账户用 *同代码、同 watchlist、同 prompt* 继续跑。日报有 "paper vs live divergence" 对比。日 P&L 偏差 >1% 且无可解释原因 → 停下来调查。Live-β 之后 paper 降级为被动监控。

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
- `pyproject.toml`、`.env` 模板、`.gitignore`（已完成）
- `wheels_daemon.py` 框架（boot APScheduler，job 是 no-op）
- `db/schema.py` + `scripts/init_db.py` —— 新表创建
- CI workflow（lint、unit test）

**退出：** daemon 启动、heartbeat、DB 初始化；测试通过。

### M1 —— Wheel 状态机 + 单 ticker MVP（Week -3 至 -2，2026-06-02 → 2026-06-08）
**交付物：**
- `engines/wheel_state_machine.py` + `transitions.py`（手写 FSM）
- `engines/csp_selector.py`（按 `config.yaml` 默认值 deterministic 选行权价/DTE）
- `engines/cc_selector.py`（强制 cost_basis floor）
- `engines/wheel_exit_plan.py`（50% TP、21 DTE、delta 0.65 stop）
- `engines/wheel_gates.py`（G1-G18 硬 gate）
- `engines/assignment_lifecycle.py`
- `engines/risk_budget.py`
- `schemas/wheel_position.py`、`wheel_state.py`、`decisions.py`
- `skills/wheel-cycle/`（per-ticker dispatcher，暂不接 LLM）
- Daemon 的 morning/intraday/EOD cycle 接到 FSM
- `tests/unit/` 覆盖 FSM 转换、gate、selector
- `tests/integration/` 在 synthetic broker stub 上跑完整 cycle
- `scripts/dry_run_cycle.py`（跑完整 cycle 但不发单）

**退出：** 在 synthetic broker 上能跑通单 ticker SPY 的 CSP→assign→CC→called-away→cash；所有单元 + 集成测试通过。

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
**交付物：**
- `scripts/stress/inject_vix_spike.py`
- `scripts/stress/inject_broker_outage.py`
- `scripts/stress/inject_llm_outage.py`
- `scripts/stress/inject_pin_risk.py`
- `scripts/stress/inject_missed_reconcile.py`
- `scripts/stress/inject_multi_assignment.py`
- 推送通知集成（ntfy.sh 或 Slack）

**退出：** Phase 4 进入条件满足。

### M6 —— Go-Live 准备（Paper Week 10）
**交付物：**
- Live Alpaca 账户开通（与 paper 分开）
- `config.yaml` "live" profile review
- Live API key 安全配置
- 操作 runbook `docs/RUNBOOK.md`
- 在 live API 上 dry-run（不发单）1 个交易日

**退出：** §5.3 所有 go-live gate 全绿；用户签字。

### M7-M10 —— Live 资金阶梯
- M7：Live-α（$50K）
- M8：Live-β（$150K）
- M9：Live-γ（$300K）
- M10：Live-1.0（$500K，稳态）

---

## 7. 待用户决定的开放问题

Council 都给了 provisional answer，但在 kickoff 前值得你明确表态：

| # | 问题 | Provisional 答案 | 为什么问 |
|---|---|---|---|
| 1 | **代码复用方式** | 现在 vendor-copy → 后续抽 pip package | 确认 vs monorepo 或 day-1 就抽 package |
| 2 | **账户类型** | 假设 taxable | Alpaca 不支持 IRA，但需要确认你接受 taxable —— wheel 主要产生短期资本利得，按 ordinary income 计税 |
| 3 | **初始 Watchlist seed** | §3.3 列出的 15 个（7 core + 8 satellite） | 想加 / 减哪些？有"绝对不持有"列表？ |
| 4 | **推送通知通道** | 建议 ntfy.sh | 接受？还是用 Slack / SMS / 只邮件？ |
| 5 | **LLM 模型组合** | Opus 4.7 + Sonnet 4.7 + GPT-5.4 + Grok 4.20 + Gemini 3.1 + DeepSeek v3.2 | 接受这个 council 组合？对成本敏感？还是少几个？ |
| 6 | **Live-α 起始资金** | $50K（$500K 中的 10%） | 接受？还是更小（$25K）/ 更大（$100K）？ |
| 7 | **per-ticker 上限** | 8%（$40K） | 保守 —— 6% ($30K，更分散) 或 10% ($50K，更高效) 都可 |
| 8 | **Drawdown CB 阈值** | 8% peak-to-trough（$40K） | 激进操作员跑 10-12%，保守 5-6% |
| 9 | **Earnings transcript 提供商** | API Ninjas（约 $30/月起步） | 这个成本可接受？或者跳过、只靠 10-Q？ |
| 10 | **Dashboard / Web UI** | v1 跳过（只 CLI + 邮件） | 想早点有个最小 web view？ |
| 11 | **Live 期间是否 paper 并行** | 是，Live-α/β 前 4 周 | 有用还是浪费？ |
| 12 | **账户号** | wheels 用单独 Alpaca 账户 | 确认 —— 不与 options-copilot 共用账户 |

任何方向有想法都告诉我。Kickoff 前未处理的部分，按上面 provisional 答案推进。

---

## 8. Council 与过程说明

本 plan 由 4 个 specialist subagent 并行起草后综合而成：

1. **System Architect**（Plan subagent）—— 负责 §2 + 模块表 + 状态机 + DB schema
2. **Quant / Risk Specialist**（general-purpose subagent）—— 负责 §3 + 参数 + 标的池 + circuit breaker
3. **AI / LLM Integration Specialist**（general-purpose subagent）—— 负责 §4 + 接入点 + prompt + 成本
4. **Forward Test Strategist**（general-purpose subagent）—— 负责 §5 + 10 周方案 + go-live gate + 上线阶梯

每个 council 成员都收到：
- `docs/RESEARCH.md` 作为必读
- 用户 4 条 lock 决策
- 限定的 scope + 字数预算（1500-2500 词）
- 输出 markdown 由我综合

这与 `options-copilot` 的 5 模型交易 council 不同：
- **options-copilot council：** 5 个同角色 LLM 提议交易、互相 blind-score、投票 → 共识
- **wheels-copilot 规划 council：** 4 个不同角色 specialist 写互不重叠的章节，我做跨章节冲突 reconcile

两者都符合 "council" 精神 —— 多元视角 + 结构化聚合 —— 但交易版本优化 *同类决策的共识*，规划版本优化 *不同维度的深度*。Project planning 用 specialist decomposition 比 4 个 redundant 通用 proposal 出来的文档更有连贯性。

**综合阶段 reconcile 的跨章节冲突：**

| 冲突点 | Architect | Quant | LLM | Forward Test | 决议 |
|---|---|---|---|---|---|
| Watchlist 刷新日 | 周五 | — | 周日 18:00 | — | **周日 18:00**（LLM agent —— 数据更全、避开工作日） |
| Per-ticker 上限 | 10% | 8% | — | — | **8%**（Quant —— 风险数字归 Quant 管） |
| VIX gating | 单阈值 30 | 渐进（28/22） | — | — | **渐进**（Quant） |
| 日亏 CB | 2% | 1.5% | — | — | **1.5%**（Quant —— 更保守） |
| IV rank 阈值 | config 写 20-70 | 25-65 | screener 用 20-70 | — | **Screener 20-70、入场 gate 25-65**（screener 给更多候选；入场更严） |
| 日报发送时间 | 16:15 | — | — | 16:00 | **16:15**（EOD cycle 完成之后） |

---

**Plan v1.0 完。** 下一步：处理 §7 的开放问题，然后开始 M0 工程。
