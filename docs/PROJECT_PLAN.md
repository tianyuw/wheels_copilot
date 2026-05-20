# Wheels Copilot — 项目方案

**版本：** v1.3（2026-05-19 引入双层 watchlist + 用户激进 classic-wheel 偏好）
**日期：** 2026-05-19
**状态：** 已通过 3 道 council（specialist 起草 → 外部 review → 双层 + classic wheel 反馈），等用户最终 review → kickoff
**输入：** [`docs/RESEARCH.md`](RESEARCH.md)、用户已锁定的 4+5 条决策、3 次 council 迭代

**Changelog：**
- **v1.0 → v1.1**（2026-05-18）应用第 1 道外部 council-review consensus：FSM 加 `PENDING_ROLL` / `CORPORATE_ACTION`；3 次 daily reconcile + broker activity stream；live 切 Postgres；per-ticker 8%→6%、SPY/QQQ 12%→10%；put delta 0.22→0.18、call 0.25→0.20、DTE 35→42；max_roll_count 2→1；earnings ±7→±10；新增 single-name gap CB；drawdown CB 8%→6%；5-model council 缩为 3-model；LLM 评估改 shadow A/B；live 阶梯每档延长至 ≥20 交易日或 1 个完整 cycle；加 Live-μ micro-test；wash sale 追踪 + ex-div 提前行权 + MLEG partial fill 处理纳入正文。
- **v1.1 → v1.2**（2026-05-19，未单独 commit）3 specialist council 设计了**双层 watchlist**（Tier A Conviction + Tier B Premium）的初版（A 经典 wheel + 用户手工维护 / B tastytrade + LLM 3 模型 council）。
- **v1.2 → v1.3**（2026-05-19）应用用户对 v1.2 的 5 项关键修改 + 第 2 道外部 council-review 的安全网建议：
  - 资金分配：保留 30% cash 改为 **0% 硬保留**（A=$245K / B=$245K / 2% working float），council 警告了风险但 honor 用户选择
  - Tier A 转为 **真 classic wheel**：put delta -0.22→**-0.28**、DTE 42→**30**、**删除 profit take + time stop + roll**，只保留 emergency 安全网（delta>0.85 → MANUAL_REVIEW）
  - Tier A call delta +0.18→**+0.22**；IV rank 25-70→**20-75**；earnings ±10→**±5**；VIX freeze >27→**>30**（用户 override，更宽容）
  - Tier B 略激进化：put delta -0.16→**-0.20**、call delta +0.25→**+0.30**、DTE 35→**30**
  - Tier A watchlist 从"用户手工 YAML"改为 **LLM-curated by `conviction-curate` skill + 用户 natural-language preferences**（`config/conviction_preferences.md` git 编辑；月度 propose + per-symbol approve + 7 日 cooldown）
  - Council 新增的安全网（与用户偏好不冲突）：emergency MANUAL_REVIEW 触发（gap >12%、5日 RV > 2×60日、DOJ/FDA/SEC 事件、NBBO spread > 5% premium）；死轮逃生舱（cost-basis CC 年化 < 0.2% → 允许 sub-basis collar，人工批准）；Tier A LLM 候选 hard screener（市值 > $20B、OI > 5000、排除 biotech/区域银行/IPO<18m/M&A）
  - Forward test 从 10 周延长至 **16 周**（前 4 周 shadow-only）+ 2022H1 stress backtest
  - 原始 v1.3 council review + consensus 见 [附录 B](#附录-bv13-council-review-2026-05-19-已应用)

---

## 0. 执行摘要

`wheels_copilot` 是一个 Python daemon，在 $500K USD 的 Alpaca 账户上每日自动跑**单一策略——Wheel**，并在 *定性* 决策上有选择地调用 LLM（选股、基本面分析、前景分析、市场环境分析）。它是 `options-copilot` 的姊妹项目，复用其约 60% 的基础设施。

**本方案的 6 条核心信念（v1.3）：**

1. **Wheel 是状态机，不是策略。** 每个 ticker 是一台独立 FSM（`CASH → CSP_OPEN → ASSIGNED → CC_OPEN → ...`）。系统里每个决策的本质都是"这台状态机今天该做什么"。
2. **🆕 双层 policy regime。** Watchlist 分两层、走两套完全不同的机械规则：
   - **Tier A "Conviction Whitelist"**：5-8 个用户愿持有 2+ 年的公司。LLM 根据用户自然语言偏好 propose，用户 per-symbol approve。**真 classic wheel**：no profit take、no time stop、no roll、hold to expiry、愿意被 assigned 长持。
   - **Tier B "Premium Pool"**：10-15 个 LLM + screener 周度维护的标的。**Tastytrade-style**：低 delta、50%TP、21DTE time stop、激进 roll、避免 assignment、宁可止损也不接货。
3. **Validator-first，默认 mechanical。** 行权价、DTE、TP、rolls、sizing —— 全部 deterministic、code-enforced（two-tier 参数都在 config）。LLM 只做 *定性* 决策（Tier A curation、基本面深读、救援、market regime）。LLM 层断了交易也继续。
4. **Tier B Watchlist 每周日 LLM 3-model council curate；Tier A 月度 + 事件触发 propose，但用户 per-symbol approve 后才生效。**
5. **Forward test 是硬 gate，不是走过场。** **16 周** Alpaca paper（v1.3：前 4 周 shadow-only），覆盖 Tier A 至少 1 个完整 cycle。所有 go-live gate 全绿才上 live。资金分 5 步上：μ($5-10K) → α($50K) → β($150K) → γ($300K) → 1.0($500K)。
6. **🆕 v1.3 核心 trade-off：用户激进 + Council 安全网组合**
   - 用户激进选择保留：**0% cash reserve、Tier A put delta -0.28、DTE 30、no TP / no time stop、IV rank 20-75、earnings ±5 日**（council 多处建议更保守，但用户明确想先激进、问题再调）
   - Council 安全网新增（不冲突激进偏好）：emergency MANUAL_REVIEW（gap >12% / RV spike / 监管事件 / spread blowout）、dead-wheel escape（cost-basis CC 几乎无 yield 时允许人工批准 sub-basis collar）、Tier A LLM 候选 hard screener
   - 合理回报预期：**Tier A 7-10%、Tier B 5-7%、组合年化净 6-9%**，组合 Sharpe 0.9-1.2，max DD 7-11%。**不追求**在 bull market beat SPY —— 追求的是可生存性 + 稳定现金流。

**Council 成员（本 plan 是 3 道 council 迭代产出）：**

| 角色 | 负责 | 版本 |
|------|------|------|
| System Architect | 代码结构、模块、DB、daemon、config | v1.0 |
| Quant / Risk | 参数、标的池、风险预算、circuit breaker | v1.0 |
| AI/LLM Integration | LLM 接入点、prompt、成本、降级 | v1.0 |
| Forward Test Strategist | test 方案、go-live gate、资金阶梯 | v1.0 |
| **第 1 道 External council-review**（GPT-5.4 + Gemini-3.1-Pro） | v1.0 → v1.1 安全性修订 | v1.1 |
| **🆕 3 specialist subagents 第 2 道**（Architect / Quant / LLM）| v1.1 → v1.2 双层 watchlist 设计 | v1.2 |
| **🆕 第 2 道 External council-review**（GPT-5.4 + Gemini-3.1-Pro） | v1.2 + 用户 feedback → v1.3 安全网补充 | v1.3 |

**跨章节冲突的 reconcile（v1.3）：**
- **🆕 双层 watchlist 架构（v1.2-v1.3）：** Tier A (Conviction, 5-8 名，LLM propose + 用户 approve, classic wheel) + Tier B (Premium, 10-15 名，LLM 周度 curate, tastytrade-style)
- Watchlist 刷新：Tier B **周日 18:00 ET**；Tier A 月度 + 事件触发
- VIX gating：Tier A **>30** 冻；Tier B **>25** 冻（v1.3 用户选择 A 更宽容）
- 日亏 circuit breaker：**-1.5% equity（约 $7.5K）**（保留 v1.1）；新增 CB_A_DD (8%)、CB_B_DD (5%)、CB_B_TURNOVER (80%)
- Watchlist 权威源：Tier B 在 **DB**；Tier A 在 `config/conviction_preferences.md`（natural language preferences）+ DB（LLM proposed list + user approval status）
- LLM 模型分配：**3 模型 council**（v1.1 从 5 减为 3）用于 Tier B watchlist + rescue；单 Opus 用于 fundamentals + Tier A conviction-curate；单 Sonnet 用于 outlook + regime + outlook-quickcheck gate
- DB 存储：**paper 用 SQLite；live 切 Postgres 15+**（v1.1，外部 council 提出真钱场景下 SQLite 的崩溃/磁盘恢复脆弱）
- Reconcile 频率：**每日 3 次**（08:00 / 09:20 / 16:15 ET）+ broker activity stream（v1.1）
- max_roll_count：Tier A = **0**（v1.3 用户选择 no roll）；Tier B = **2**（v1.2 保留）
- **🆕 资金分配（v1.3 用户选择激进）：** 0% hard cash reserve + 2% working float / 49% Tier A / 49% Tier B（council 推荐 5% hard + 5% buffer，用户 override）

**时间表（从 2026-05-19 kickoff 起算，v1.3 forward test 延至 16 周）：**

| 窗口 | 周次 | 阶段 |
|------|------|------|
| 2026-05-19 → 2026-06-22 | -5 至 0 | 工程建设：M0 shared 层 + 双层 schema → M2 双层 ticker（达到 Phase 1 entry 标准） |
| 2026-06-23 → 2026-07-20 | 1-4 | **🆕 Paper Phase 1 (Shadow-only)**：Tier A 单 ticker 跑 + Tier B shadow（不真下单），先校准 ex-div / split / partial fill 等 edge case 处理 |
| 2026-07-21 → 2026-08-03 | 5-6 | Paper Phase 2：Tier A 5 个 + Tier B 5 个（小规模），全部真下单（paper），mechanical only |
| 2026-08-04 → 2026-09-07 | 7-11 | Paper Phase 3：完整 Tier A + Tier B watchlist + 全部 LLM 接入（conviction-curate / outlook-quickcheck / rescue 都启用） |
| 2026-09-08 → 2026-10-12 | 12-16 | Paper Phase 4：压测 + 边缘场景 + Tier A 至少 1 个完整 cycle（CSP→assign→CC→called away） |
| 2026-10-13 → 2026-11-09 | 17-20 | 🆕 **Live-μ micro-test**：$5-10K，SPY 1 个，≥20 交易日 / 1 完整 cycle |
| 2026-11-10 → 2026-12-07 | 21-24 | Live-α：$50K，2 个 ticker，≥20 交易日 / 1 完整 cycle |
| 2026-12-08 → 2027-01-04 | 25-28 | Live-β：$150K，4 个 ticker，≥20 交易日 / 1 完整 cycle |
| 2027-01-05 → 2027-02-01 | 29-32 | Live-γ：$300K，6-8 个 ticker，≥20 交易日 / 1 完整 cycle |
| 2027-02-02 起 | 33+ | Live-1.0：$500K，完整双层 watchlist |

**总计：kickoff 到 $500K 满仓 live 大约 9 个月**（v1.3 比 v1.1 又延长 2 个月：双层 schema 多 1 周工程 + Paper 从 10 → 16 周覆盖 Tier A 完整 cycle）。Council 一致认为 Tier A 真 classic wheel 设计 + 双层架构必须有足够多 cycle 样本才能下结论。

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
- **`PENDING_ROLL`**（v1.1 新增）—— MLEG roll 订单提交后到完全成交之间的过渡态。Partial fill 在 `options_chain` 流动性差时真实发生（Gemini 提出）；此状态下：(a) 该 ticker 上不允许其他并发决策，(b) 5 分钟内未完全成交触发 cancel-and-rebuild，(c) 完全成交后回到目标新状态（CSP_OPEN 或 CC_OPEN）。**Tier A 永不进此状态（max_roll=0）；只对 Tier B 适用。**
- **`CORPORATE_ACTION`**（v1.1 新增）—— 检测到底层股票发生 split、spin-off、special dividend、M&A 等公司行动时，**任何**当前状态都可转入此 freeze 态。期权合约会被 OCC 调整，自动操作此时会出错。EDGAR + corporate action API 监控触发；操作员手动 `unfreeze` 后转回上一状态或 CASH。**两个 tier 都适用。**
- **`ASSIGNED_HELD`** —— bag-holder 子状态。**Tier A 触发阈值 cost_basis × 0.75**（v1.3 用户更宽容）；**Tier B 触发阈值 cost_basis × 0.92**（v1.3 更早 trigger）。进入此状态后停止自动 CC，由 rescue 引擎接管。
- **🆕 `ASSIGNED_EXIT_PREMIUM`**（v1.3 新增，**仅 Tier B**）—— Tier B 被 assigned 后的特殊子状态。三种 mode：`twap_dump`（3-5 日 TWAP 卖股 + 接 capital loss）/ `cc_bleed`（短 DTE deep-OTM CC 加快 called-away）/ `hybrid`（默认 default：先 cc_bleed 14 天，未 called away 切 twap_dump）。**60 天硬上限**或 **DD ≤ -15%** 任一先达即强制 TWAP 卖股。Tier A 永不进此状态（A 接 assignment 就走经典 CC 路径）。
- **🆕 `MANUAL_REVIEW`**（v1.3 扩展触发条件）—— 触发条件：(a) `PENDING_ROLL` 5 分钟未完成；(b) 单日 underlying gap > 12%；(c) 5 日 realized vol > 2× 60 日中位数；(d) 监管事件（DOJ/FDA/SEC 重大裁决）；(e) NBBO spread > 5% premium；(f) Tier A 紧急 stop loss（delta > 0.85）。等待操作员明确处置。
- **`CYCLE_COMPLETE`** —— 瞬时状态，原子写入 `cycle_log` 后回到 `CASH`。

**🆕 v1.3 Tier 行为差异表：**

| 状态 / 转换 | Tier A (Conviction) | Tier B (Premium) |
|---|---|---|
| `CASH → CSP_OPEN` | put delta -0.28、DTE 30、IVR 20-75、earnings ±5 | put delta -0.20、DTE 30、IVR 30-65、earnings ±14 |
| `CSP_OPEN` 中的主动管理 | **无**（no TP、no time stop、no roll） | 50% TP / 14 DTE time stop / delta 0.30 roll |
| `CSP_OPEN → CASH` 路径 | 只通过到期 OTM | 50% TP close / time stop close / roll then close |
| `CSP_OPEN → ASSIGNED` | happy path | unwanted state → 立即进 `ASSIGNED_EXIT_PREMIUM` |
| `ASSIGNED → CC_OPEN` | call delta +0.22、DTE 30、`K ≥ cost_basis` 硬约束 | (跳过，进 `ASSIGNED_EXIT_PREMIUM`) |
| `CC_OPEN` 中的主动管理 | **无**（hold to expiry） | n/a |
| max_roll | **0** | 2 |
| 紧急 stop loss | delta > 0.85 → `MANUAL_REVIEW` | delta > 0.50 / 1.5× credit → close |
| `ASSIGNED_HELD` 触发 | cost_basis × 0.75 | cost_basis × 0.92 |
| 死轮逃生舱 | cost-basis CC 年化 < 0.2% → 允许人工批准 sub-basis collar | n/a（B 直接 `ASSIGNED_EXIT_PREMIUM` 走 twap_dump） |

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

-- 🆕 v1.3：双层 watchlist 支持
ALTER TABLE watchlist ADD COLUMN policy_profile TEXT NOT NULL DEFAULT 'premium';
-- 'conviction' | 'premium'
ALTER TABLE watchlist ADD COLUMN accept_assignment INTEGER NOT NULL DEFAULT 0;
-- conviction tier 默认 1，premium tier 默认 0；用于 selector 提前 bias K
ALTER TABLE watchlist ADD COLUMN curated_by TEXT NOT NULL DEFAULT 'llm';
-- 'manual' (人工 yaml) | 'llm' (LLM 提议) | 'llm_approved' (LLM 提议 + 用户批准)
ALTER TABLE watchlist ADD COLUMN policy_locked_at TEXT;
-- 用户最后一次手工或 approve 变更 policy_profile 的时间

-- 强制：conviction tier 不能直接 LLM 写入；必须经过用户 approve
CREATE TRIGGER trg_watchlist_conviction_approval BEFORE INSERT OR UPDATE ON watchlist
WHEN NEW.policy_profile = 'conviction' AND NEW.curated_by = 'llm'
BEGIN SELECT RAISE(ABORT, 'conviction tier requires curated_by=manual or llm_approved'); END;

-- 🆕 v1.3：Tier A LLM 提议（user approval 之前的中间态）
CREATE TABLE tier_a_proposals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_date           TEXT NOT NULL,
    user_preferences_hash   TEXT NOT NULL,
    proposal_json           TEXT NOT NULL,         -- conviction-curate skill 完整输出
    status                  TEXT NOT NULL DEFAULT 'PENDING',
    -- 'PENDING','APPROVED','REJECTED','PARTIAL'
    approved_at             TEXT,
    approved_by             TEXT,                  -- 'cli' | 'email' | 'manual_edit'
    applied_changes_json    TEXT,                  -- 实际 apply 到 watchlist 的子集
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 🆕 v1.3：wheel_states 增加 active policy snapshot
ALTER TABLE wheel_states ADD COLUMN active_policy_profile TEXT;
-- 在进入 CSP_OPEN 时从 watchlist snapshot；cycle 期间不再改
-- 用户中途改 policy_profile 只影响下一个 cycle，不影响在跑的仓位

-- 🆕 v1.3：emergency MANUAL_REVIEW 事件记录
CREATE TABLE manual_review_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL,
    triggered_at        TEXT NOT NULL,
    trigger_type        TEXT NOT NULL,
    -- 'pending_roll_timeout','gap_12pct','rv_spike','regulatory_event',
    -- 'spread_blowout','tier_a_emergency_delta','dead_wheel_low_yield'
    trigger_detail_json TEXT,
    suspended_actions   TEXT,                       -- comma-separated FSM transitions blocked
    resolved_at         TEXT,
    resolved_by         TEXT,                       -- 'auto' | 'operator'
    resolution_notes    TEXT
);

CREATE INDEX idx_tier_a_proposals_status ON tier_a_proposals(status, proposal_date);
CREATE INDEX idx_manual_review_ticker ON manual_review_events(ticker, triggered_at);
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

storage:                        # v1.1
  paper_backend: sqlite         # WAL 模式
  live_backend: postgres        # 切 live 前强制
  postgres_url_env: WHEELS_PG_URL

# 🆕 v1.3：资金分配（用户激进选择 0% cash）
allocation:
  hard_cash_reserve_pct: 0.00   # v1.3 用户 override，council 推荐 0.05
  working_float_pct: 0.02       # $10K，fees/settlement 必要
  tier_a_csp_max_pct: 0.49      # $245K
  tier_b_csp_max_pct: 0.49      # $245K
  # 注：council 推荐 cash 5% + buffer 5%；用户选择更激进，"问题再调"

schedule:                       # v1.1：3 次 reconcile + activity stream
  premarket_reconcile_et: "08:00"
  open_validate_reconcile_et: "09:20"
  morning_cycle_et: "09:45"
  intraday_interval_min: 15
  eod_cycle_et: "16:15"
  tier_b_watchlist_refresh_day: sunday
  tier_b_watchlist_refresh_time_et: "18:00"
  # 🆕 v1.3：Tier A 月度刷新
  tier_a_curate_day: "first_trading_day_of_month"
  tier_a_curate_time_et: "07:30"
  broker_activity_stream: true

risk:
  # 全局 CB（不分 tier）
  daily_loss_cb_pct: 0.015
  weekly_loss_cb_pct: 0.04
  drawdown_cb_pct: 0.06
  catastrophic_drawdown_pct: 0.12        # v1.1 已收紧

  # 🆕 v1.3：per-tier CB
  tier_a_dd_cb_pct: 0.08
  tier_b_dd_cb_pct: 0.05
  tier_b_weekly_turnover_cb_pct: 0.80

  # 全局组合约束
  per_ticker_max_pct_conviction: 0.06    # $30K
  per_ticker_max_pct_premium: 0.04       # $20K
  per_ticker_etf_max_pct: 0.10           # SPY/QQQ
  sector_max_pct_combined: 0.25
  sector_max_pct_tier_a_tech: 0.35       # A tech 例外
  sub_industry_max_concurrent: 2

# 🆕 v1.3：profile-driven 参数 (Tier A vs B 完全不同)
profiles:
  conviction:                            # Tier A —— 真 classic wheel
    put_delta: [0.22, 0.32]
    put_delta_target: 0.28               # 用户 override；council 推 0.24
    call_delta: [0.18, 0.28]
    call_delta_target: 0.22              # 用户 override；council 推 0.18-0.20
    dte: [21, 35]
    dte_target: 30
    iv_rank_range: [0.20, 0.75]          # 用户 override；council 推 [0.30, 0.65]
    earnings_blackout_days: 5            # 用户 override；council 推 10
    macro_blackout_days: 2
    profit_take_pct: null                # 🆕 v1.3：删除 TP（用户：hold to expiry）
    time_stop_dte: null                  # 🆕 v1.3：删除 time stop
    max_roll_count: 0                    # 🆕 v1.3：no roll
    emergency_stop_loss_delta: 0.85      # 仅 emergency 安全网
    emergency_stop_loss_credit_mult: 3.0
    cc_strike_floor: cost_basis          # K >= basis 硬约束
    assigned_held_trigger_pct: 0.75      # 股价 < basis × 0.75 → ASSIGNED_HELD
    min_credit_pct_strike_csp: 0.005
    min_credit_pct_strike_cc: 0.004
    vix_freeze_above: 30                 # 用户 override；council 推 27

  premium:                               # Tier B —— tastytrade-style
    put_delta: [0.16, 0.24]
    put_delta_target: 0.20               # 用户 override；council 推 0.16-0.18
    call_delta: [0.25, 0.35]
    call_delta_target: 0.30              # 用户 override；council 推 0.25
    dte: [21, 40]
    dte_target: 30                       # 用户：A、B 都 30
    iv_rank_range: [0.30, 0.65]
    earnings_blackout_days: 14
    macro_blackout_days: 2
    profit_take_pct: 0.40                # tastytrade TP
    time_stop_dte: 14
    max_roll_count: 2                    # 允许 roll，禁 pure roll-down
    roll_trigger_delta: 0.30
    roll_trigger_max_dte: 18
    stop_loss_delta: 0.50
    stop_loss_credit_mult: 1.5
    cc_strike_floor: market_realistic    # 允许 K < basis（仅在 ASSIGNED_EXIT_PREMIUM）
    assigned_held_trigger_pct: 0.92      # B 更早 trigger
    min_credit_pct_strike_csp: 0.007
    min_credit_pct_strike_cc: 0.006
    vix_freeze_above: 25                 # B 更紧
    # 🆕 v1.3：Tier B assigned 后处置策略
    exit_strategy:
      mode: hybrid                       # twap_dump | cc_bleed | hybrid
      twap_days: 5
      cc_dte_max: 14
      cc_delta_max: 0.30
      max_quarterly_realized_loss_pct: 0.005  # 0.5% equity / 季
      max_hold_days: 60
      max_hold_dd_pct: 0.15

# 🆕 v1.3：emergency MANUAL_REVIEW 触发器
emergency_triggers:
  single_day_gap_pct: 0.12               # 单日 |move| > 12%
  rv_spike_multiplier: 2.0               # 5 日 RV > 2× 60 日中位数
  regulatory_event: true                 # DOJ / FDA / SEC 重大裁决
  nbbo_spread_premium_pct: 0.05          # NBBO spread > 5% premium → 禁自动下单
  dead_wheel_annual_yield_threshold: 0.002  # cost-basis CC 年化 < 0.2% → 死轮逃生舱

gates:                                   # 全局硬 gate（per-tier 还有 profile overrides）
  min_open_interest: 500
  max_spread_pct_of_mid: 0.05
  min_avg_option_volume: 200
  max_daily_new_entries_global: 4        # A + B 合计
  max_daily_new_entries_per_tier: 2
  pending_roll_timeout_sec: 300
  ex_div_cc_close_extrinsic_lt_div: true
  # 🆕 v1.3：Tier A LLM 候选 hard screener
  tier_a_screener:
    min_market_cap_usd: 20_000_000_000   # $20B
    min_avg_option_volume_daily: 5000
    exclude_sectors: [biotech_small, regional_banks]
    exclude_recent_ipo_months: 18
    exclude_pending_mna: true
  # 🆕 v1.3：单 ticker gap freeze（CB9 配对）
  single_name_gap_freeze_pct_conviction: 0.15  # 用户更宽
  single_name_gap_freeze_pct_premium: 0.08     # B 更严
  single_name_gap_freeze_days_conviction: 5
  single_name_gap_freeze_days_premium: 10

llm:
  market_outlook:                        # 每日 regime read（不分 tier）
    enabled: true
    pattern: single
    model: "anthropic/claude-sonnet-4-7"
  tier_b_watchlist_curate:               # v1.3：原 watchlist-curate
    enabled: true
    pattern: council                     # 3 模型 propose + blind-score
    models:
      - "anthropic/claude-opus-4-7"
      - "openai/gpt-5.4"
      - "google/gemini-3.1-pro-preview"
    frequency: weekly
    exclude_tier_a: true                 # 自动排除 Tier A 候选
  # 🆕 v1.3：Tier A conviction LLM curation
  tier_a_conviction_curate:
    enabled: true
    pattern: single                      # 单 Opus 即可（用户最终 approve）
    model: "anthropic/claude-opus-4-7"
    frequency: monthly_plus_events       # 月度 + 事件触发
    user_preferences_file: "config/conviction_preferences.md"
    new_ticker_cooldown_days: 7          # 新加入 ticker 7 个交易日后才可首开 CSP
    require_per_symbol_approval: true
  conviction_health_check:               # 月度 + 事件
    enabled: true
    pattern: single
    model: "anthropic/claude-opus-4-7"
  fundamentals_deepdive:
    enabled: true
    pattern: single
    model: "anthropic/claude-opus-4-7"
    cache_quarters: 1
  outlook_analysis:                      # 周度（curate 内）
    enabled: true
    pattern: single
    model: "anthropic/claude-sonnet-4-7"
  # 🆕 v1.3：Tier B 入场前实时 outlook gate
  outlook_quickcheck:
    enabled: true
    pattern: single
    model: "anthropic/claude-sonnet-4-7"
    apply_to_tier: premium
    cache_per_day: true
  rescue_decide:
    enabled: true
    pattern: council_vote                # 3 模型，prompt 按 tier 分支
    models: [...]
    tie_default: "escalate_to_human"
  code_screener:
    enabled: true
  # 显式 NO-LLM 列表：strike/DTE/delta、order quantity、profit-take、stop-loss

reporting:
  email_to: "tianyuw@icloud.com"
  ses_region: us-east-1
  push_notify_url: "https://ntfy.sh/wheels-copilot-tianyu"
  include_cycle_log: true
  include_cost_basis_history: false
  # 🆕 v1.3：每周 review email 加 Tier A 升级建议、conviction proposal 等区段
  include_tier_a_proposals: true
  include_promotion_suggestions: true

dry_run: false                 # 全局 kill switch
```

**🗑️ 弃用：** v1.1 的 `tickers.yaml` 在 v1.3 不再使用。Tier A 用 `config/conviction_preferences.md`（用户写自然语言偏好）；Tier B 由 LLM 周度 curate 写入 DB。

**🆕 v1.3：`config/conviction_preferences.md`**（Tier A 用户偏好，自然语言）：

```markdown
# Conviction Whitelist Preferences

我愿意持有 2+ 年的公司类型：
- Mega-cap tech with AI infrastructure exposure（NVDA、MSFT、GOOGL、META）
- Established platform monopolies（AAPL、AMZN）
- High-quality consumer brands（KO、COST）

我不愿持有：
- Financials（不熟悉商业模式）
- Biotech / pharma（binary risk）
- Recent IPO / 高估值无盈利

价格偏好：
- 单 ticker 不超过 $40K reservation
- 偏好 $50-$300 股价（仓位粒度合理）

行业偏好：
- AI / cloud / 半导体重点关注
- 能源 / 防御性消费品作分散
```

LLM `conviction-curate` skill 月度读这个文件 + market data + fundamentals 提出 Tier A 名单建议（详见 §4）。用户 per-symbol approve 后才进 DB。

**Tier B 没有 yaml seed**——v1.3 由 LLM 在第一次 curate 时直接从 screener pool 中提议初始名单，用户在 paper Phase 2 起跑前 review approve。

---

## 3. 交易参数与风险管理

下面所有默认值都偏 **保守**，因为这是真实 $500K。每个参数都可以配置覆盖，但默认就是 day-1 跑的值。

### 3.1 资金分配 —— $500,000（v1.3 双层 + 0% cash）

**🆕 v1.3 关键变化：取消 hard cash reserve（用户 override）**

用户："我不希望保留任何的cash，我希望所有的资金都做wheel strategy"。Council 双方都强烈反对 0% cash，推荐至少 5% hard cash + 5% buffer。**我按用户选择 apply**（"问题再调"），但本节末附 council 担忧供 paper 阶段回看。

| 桶 | v1.0/v1.1 | **v1.3（用户 override）** | $ | 说明 |
|---|---|---|---|---|
| **硬保留 cash** | 20% | **0%** ❗ | $0 | 用户：全部资金做 wheel；council 推荐 5% |
| **工作 float** | (含在 buffer) | **2%** | $10K | 最小 in-flight settlement / fees / SEC fee（不是 hard reserve） |
| **Tier A CSP 抵押上限** | n/a | **49%** | $245K | 真 classic wheel 区 |
| **Tier B CSP 抵押上限** | n/a | **49%** | $245K | tastytrade-style premium 区 |
| **合计** | | **100%** | **$500K** | |

**Per-ticker 最大敞口（v1.3）：**
- **Tier A：6% 账户 = $30K**（沿用 v1.1）
- **Tier B：4% 账户 = $20K**（更紧，单票质量天花板低）
- **ETF（默认归 A）上限 $50K**

**目标并行仓位数：**
- Tier A：**5-8 个**（用户 + LLM 决定）
- Tier B：**8-10 个**（LLM 周度 curate）
- 组合：13-18 个并行（高于 v1.1 的 10，但 B 单票更小 → risk-equivalent 约 11-12 个 v1.1 仓位）

**🆕 v1.3 行业集中度（per-tier 独立计算）：**
- Tier A：tech 上限 35%（含 ETF），其他 sector 25%
- Tier B：tech 上限 25%（无 ETF），其他 sector 25%
- 组合 sector cap 仍是 25% / 35% Tier-1 作外圈约束
- 单一 sub-industry 最多 **2 个名字**

**双层满仓示例：**

| Tier | # | Ticker | Strike | Contracts | Reserved $ | Sector |
|------|---|--------|--------|-----------|-----------|--------|
| A | 1 | SPY | $470 | 1 | $47,000 | Index（ETF $50K 上限内） |
| A | 2 | QQQ | $440 | 1 | $44,000 | Index |
| A | 3 | AAPL | $200 | 1 | $20,000 | Tech |
| A | 4 | MSFT | $300 | 1 | $30,000 | Tech（$30K cap） |
| A | 5 | NVDA | $130 | 2 | $26,000 | Semi |
| A | 6 | GOOGL | $150 | 2 | $30,000 | Tech |
| | | | **Tier A 小计** | | **$197,000** (~40%) | |
| B | 7 | AMZN | $150 | 1 | $15,000 | Cons. Disc. |
| B | 8 | TSLA | $200 | 1 | $20,000 | Cons. Disc. |
| B | 9 | JPM | $200 | 1 | $20,000 | Financials |
| B | 10 | KO | $50 | 4 | $20,000 | Cons. Staples |
| B | 11 | XOM | $100 | 2 | $20,000 | Energy |
| B | 12 | LLY | $150 | 1 | $15,000 | Healthcare |
| B | 13 | V | $200 | 1 | $20,000 | Financials |
| B | 14 | INTC | $25 | 8 | $20,000 | Semi |
| | | | **Tier B 小计** | | **$150,000** (~30%) | |
| | | | **Total CSP 抵押** | | **~$347,000** (~69%) | |
| | | | **未部署 cash**（自然 buffer） | | **~$143K** (~29%) | |
| | | | **Working float** | | **$10K** (2%) | |

满仓后实际还有约 $143K 未部署 cash —— 这是 ticker 离散化 + 行业约束 + per-ticker cap 自然产生的流动性 buffer，**不是 hard reserve**。在市场提供更多机会时 LLM 可以扩大 watchlist 进一步部署。

**⚠️ Council 担忧（v1.3 必须 paper 阶段重点验证）：**
- **0% hard cash 在真钱场景的隐患：** assignment / exercise 与 option close 的 **settlement T+1 mismatch**；Alpaca 临时提高 house requirement、收 OCC pass-through fee、margin 异常 → "名义 cash 够、可用 buying power 不够"
- **真正暴露：** 当 8-10 个 CSP 同时被 assigned → 账户瞬间满仓 shares → 没有 cash 应对 broker 异常 / margin call / 紧急 rescue / 抓 dip 机会
- **Council 建议（待用户 paper 8 周后回看）：** 至少 5% ($25K) hard cash + 5% ($25K) buffer
- 这是 [§7 open decision #2](#7-待用户决定的开放问题) —— paper 后强制 review

### 3.2 默认交易参数（v1.3 双层分别）

🆕 v1.3：参数按 tier 完全分流。Tier A 真 classic wheel（no TP / no time stop / no roll / hold to expiry），Tier B 严格 tastytrade-style。

#### 3.2.A Tier A（Conviction Whitelist）—— 真 classic wheel

| 参数 | 默认 | 区间 | 说明 |
|---|---|---|---|
| Put delta (CSP 入场) | **-0.28** *(用户 v1.3)* | -0.22 ~ -0.32 | Council 推 -0.24；用户："稍微激进一些，问题再调"。-0.28 ≈ 72% OTM 概率（被 assigned 概率 ~28%，符合 classic wheel 接受 assignment 的设计） |
| Call delta (CC 入场) | **+0.22** *(用户 v1.3)* | +0.18 ~ +0.28 | Council 推 +0.18~+0.20；用户略激进 |
| DTE target | **30** *(用户 v1.3)* | 21-35 | 用户："Tier A DTE target 30 天左右"。年 cycle 数 ~12 |
| 提前关仓 (profit take) | **❌ 无** *(用户 v1.3)* | n/a | 用户："不应该有 profit take，尽量持有到到期日"。**牺牲 tastytrade 15-20% Sharpe 提升 换取真 classic wheel philosophy** |
| Time stop | **❌ 无** *(用户 v1.3)* | n/a | 用户："不应该有 time stop"。承受 0-30 DTE 全程 gamma |
| Stop loss | short delta ≥ **0.85** 或 close debit ≥ 3× credit | emergency only | v1.3 大幅放宽，仅 emergency 安全网。触发 → `MANUAL_REVIEW`（不自动平仓，给用户最终决定权） |
| Roll 触发 | **❌ 无 roll** *(用户 v1.3)* | n/a | 用户："就算跌破行权价，我也希望持有股票"。Roll 是为了避免 assignment；A 不避免 assignment |
| 最大 roll 次数 | **0** *(用户 v1.3)* | 硬上限 | 与上一条一致 |
| IV rank 入场 | **20 ≤ IVR ≤ 75** *(用户 v1.3)* | 硬约束 | Council 推 30-65；用户更宽（A 即使 IVR 极端也愿意接受） |
| Earnings 屏蔽 | ±**5** 日 *(用户 v1.3)* | 硬约束 | Council 推 ±10；用户更宽（A 是 conviction 持仓，earnings volatility 可接受） |
| VIX 全冻 | >**30** *(用户 v1.3)* | 硬约束 | Council 推 >27；用户更宽 |
| 🆕 Emergency MANUAL_REVIEW 触发（council v1.3 新增）| 单日 gap >12% / 5日 RV > 2×60日 / DOJ-FDA-SEC 事件 / NBBO spread > 5% premium | 必触发 | 不冲突激进偏好，安全网 |
| 🆕 死轮逃生舱（council v1.3 新增）| cost-basis CC 年化收益 < 0.2% → 允许 sub-basis CC + protective call collar（人工批准） | 人工 | 解决"愿意持有≠愿意永远套牢" |
| Bid-ask spread | ≤ 5% mid | 硬约束 | |
| Open interest | ≥ 500 | 硬约束 | |
| 最低 credit | ≥ 0.5% strike (CSP)、≥ 0.4% strike (CC) | 硬约束 | |

**Tier A 完整 lifecycle 流程：**

```
1. 开 CSP：put delta -0.28, DTE 30
2. 持有期间无任何主动干预（no TP, no time stop, no roll）
3. 到期 outcome：
   3a. OTM → expire worthless，premium 全收 → 回 CASH，开下一张
   3b. ITM → 接受 assignment，进 ASSIGNED 状态
4. ASSIGNED 后卖 CC：call delta +0.22, DTE 30, K ≥ cost_basis 硬约束
5. CC 持有期间无干预
6. 到期 outcome：
   6a. OTM → CC expire，premium 全收，继续持股，回 4 卖新 CC
   6b. ITM → 接受 called away → CYCLE_COMPLETE → CASH
7. 紧急安全网：
   7a. 短期 delta > 0.85 → MANUAL_REVIEW（不自动平）
   7b. 单日 gap > 12% / RV spike / 监管事件 / spread blowout → MANUAL_REVIEW + 暂停下单
   7c. cost-basis CC 几乎无 premium → 死轮逃生舱（人工批准 sub-basis collar）
   7d. 公司基本面恶化 → conviction-health-check skill 触发 alert
```

#### 3.2.B Tier B（Premium Pool）—— Tastytrade-style

| 参数 | 默认 | 区间 | 说明 |
|---|---|---|---|
| Put delta (CSP 入场) | **-0.20** *(用户 v1.3)* | -0.16 ~ -0.24 | Council 推 -0.16~-0.18；用户略激进 |
| Call delta (CC 入场) | **+0.30** *(用户 v1.3)* | +0.25 ~ +0.35 | Council 推 +0.25；用户更激进（B 希望快速被 called away） |
| DTE target | **30** *(用户 v1.3)* | 21-40 | 用户 A、B 都 30 |
| 提前关仓 (profit take) | **40% credit** | 30-50% | B 早撤；保留 tastytrade 哲学 |
| Time stop | **14 DTE** | 12-18 | B 避免末段 gamma |
| Stop loss | short delta ≥ 0.50 或 close debit ≥ 1.5× credit | | B 更早承认错误 |
| Roll 触发 | strike breached AND delta ≥ 0.30 AND ≤18 DTE | 三者同时 | B 抓 net credit 窗口更早 |
| 最大 roll 次数 | **2 次** *(禁纯 roll-down)* | 硬上限 | B 多一次"逃逸尝试" |
| IV rank 入场 | 30 ≤ IVR ≤ 65 | 硬约束 | B 两端都收紧 |
| Earnings 屏蔽 | ±14 日 | 硬约束 | B 不接受 event risk |
| VIX 全冻 | >25 | 硬约束 | B 在动荡中先停 |
| Bid-ask spread | ≤ 5% mid | 硬约束 | |
| Open interest | ≥ 500 | 硬约束 | |
| 最低 credit | ≥ 0.7% strike (CSP)、≥ 0.6% strike (CC) | 硬约束 | B 要更高 yield 补偿高 turnover |

**Tier B 完整 lifecycle 流程：**

```
1. 开 CSP：put delta -0.20, DTE 30
2. 主动管理：
   2a. 50% TP → close → 回 CASH
   2b. 14 DTE time stop → 评估 close / roll
   2c. delta ≥ 0.30 AND ≤18 DTE → roll out（最多 2 次，禁纯 down）
3. 如果被 assigned（应是稀有事件 → 设计上 <10%）：
   3a. 立即进 ASSIGNED_EXIT_PREMIUM 子状态
   3b. 评估 30+ DTE deep-OTM CC（delta < 0.15, K ≥ basis）：
      - 可得 credit ≥ 0.5% basis → 卖一张
      - 不可得 → 进 T_B1 escalation
   3c. 60 天硬上限或 DD ≤ -15%（任一先达）→ 强制 5-day TWAP 卖股
   3d. 任何 escalation 都不允许 basis-below CC（B 一律禁止 sub-basis）
```

### 3.3 标的池（v1.3 双层重组）

**🆕 v1.3：v1.1 的 "Tier 1/Tier 2 by 质量" 改组为 "Tier A/B by policy regime"**。注意：A/B 不是 LLM 质量评级（那个仍有 core/satellite），而是 *用户对该 ticker 的持有立场*。同一个 ticker 不可能同时在 A 和 B（互斥）。

#### Tier A 候选示例（5-8 个，**由 LLM 根据用户偏好提议 + 用户 per-symbol approve**）

用户在 `config/conviction_preferences.md` 写自然语言偏好，LLM 月度跑 `conviction-curate` skill 提议 Tier A 候选。下面是基于 v1.1 watchlist + 用户偏好（mega-cap tech with AI exposure 等）的 **可能初始建议**（实际由首次 conviction-curate 输出）：

| Ticker | 大约价 | 每张预留 | 理由（LLM 论证） |
|---|---|---|---|
| SPY | $470 | $47K | S&P 500 ETF，最深 option 市场，无 idiosyncratic risk；ETF $50K 上限 |
| QQQ | $440 | $44K | Nasdaq 100，与 SPY 互补 |
| AAPL | $200 | $20K | 顶级 mega-cap，长期 holdability ≥9/10 |
| MSFT | $300 | $30K | 现金流可预测，Azure + AI infrastructure 双 thesis |
| NVDA | $130 | $13K | AI 半导体核心；用户偏好"AI infrastructure" |
| GOOGL | $150 | $15K | 平台垄断 + AI exposure；股价低粒度好 |

**Tier A 硬性排除（永不进入，即使 LLM 提议也拒绝）：**
- Council 推荐 hard screener（v1.3 新增）：市值 < $20B、avg option volume < 5000/日、生物科技、区域银行、IPO < 18 月、M&A pending
- 用户偏好 exclusion（在 `conviction_preferences.md` 中声明）
- 已在 Tier B 中（互斥）

#### Tier B 候选示例（8-10 个，LLM 周度 curate）

LLM 每周日 18:00 ET 通过 `tier-b-watchlist-curate` skill（3-model council）从 screener pool 中提议。下面是可能的 v1.3 初始建议（剔除 Tier A 已有）：

| Ticker | 大约价 | 每张预留 | 说明 |
|---|---|---|---|
| AMZN | $150 | $15K | 巨型 consumer disc.，IV rank 中等 |
| TSLA | $200 | $20K | 高 IV，激进 premium farming 用 |
| META | $400 | n/a | 价格高于 $20K cap → LLM 可能选不到合适 strike |
| JPM | $200 | $20K | 金融锚 |
| KO | $50 | $20K | 防御性低 vol |
| XOM | $100 | $20K | 能源 |
| V | $200 | $20K | 支付，margin 稳 |
| LLY | $150 | $15K | 医药 |
| INTC | $25 | $20K | 半导体 |
| F | $11 | n/a | <$20 → 硬性排除 |

**Tier B 硬性排除（永不 wheel）：**
- Meme / 动量股（GME、AMC、TSLA 在极端 IV 时）
- 无收入生物科技 / 90 天内有 FDA catalyst
- 上市 <12 个月的次新股
- 股价 <$20
- 3 倍 leveraged ETF（TQQQ、SQQQ、SOXL...）
- 单商品 ETF（USO、UNG —— contango 慢慢吃你）
- v1 阶段市值 <$5B
- 60 天内有重大事件（并购投票、反垄断裁决等）
- 已在 Tier A（互斥）

### 3.4 入场前硬性 Gate（CSP 开仓）

代码强制，不接受 LLM debate。**任一**触发 → 拒绝：

| # | Gate | Tier A 阈值 | Tier B 阈值 |
|---|---|---|---|
| G1 | Earnings | ±**5** 日 *(用户)* | ±**14** 日 |
| G2 | 宏观事件 | FOMC / CPI / NFP / PCE ±2 日 | 同 |
| G3 | VIX | >**30** *(用户)* | >**25** |
| G4 | IV rank | **20-75** *(用户)* | 30-65 |
| G5 | OI | <500 在目标行权价 | 同 |
| G6 | 价差 | >5% mid | 同 |
| G7 | 期权 volume | 20 日均 <200 | <500 |
| G8 | per-ticker 唯一性 | 同一 ticker 已有 CSP/CC/shares | 同 |
| G9 | CSP cash 上限 | A 加上后超 $245K | B 加上后超 $245K |
| G10 | 行业集中度 | A tech >35%、其他 >25% | B tech >25%、其他 >25% |
| G11 | 子行业冲突 | 同一子行业已有未平仓位 | 同 |
| G12 | 单 ticker 敞口 | 预留 >$30K（ETF 例外 $50K） | 预留 >$20K |
| G13 | 数据陈旧 | quote / chain >5 分钟未更新 | 同 |
| G14 | Watchlist | Ticker 不在对应 tier 的 ACTIVE 列表 | 同 |
| G15 | 待 review 的 rescue | 此 ticker 有未解决的 rescue flag | 同 |
| G16 | 当日入场上限 | A ≤ 2/日 | B ≤ 2/日（组合 ≤4/日） |
| G17 | Drawdown brake | 账户在 CB 状态 | 同（含新 CB_A_DD / CB_B_DD） |
| G18 | Broker 健康 | Alpaca health monitor 降级 | 同 |
| G19 | Single-name gap freeze | >**15% / 5 日** *(用户)* | >**8% / 10 日** |
| G20 | Corporate action | 在 `CORPORATE_ACTION` 状态 → 拒绝 | 同 |
| G21 | Wash sale | 30 日内有亏损平仓 → 新仓评估后开（cost basis 调整） | 同 |
| **🆕 G22** | **Manual review 暂停**（v1.3） | 该 ticker 有未解决的 `manual_review_events` | 同 |
| **🆕 G23** | **NBBO spread blowout**（v1.3） | NBBO spread > 5% premium → 不自动下单 | 同 |
| **🆕 G24** | **Tier A new ticker cooldown**（v1.3） | 新 approve 进 A 的 ticker 必须 7 个交易日后才可首开 CSP | (n/a) |
| **🆕 G25** | **Tier A LLM hard screener**（v1.3） | LLM 提议候选必须：市值 > $20B、avg OI > 5000、排除 biotech/区域银行/IPO<18月/M&A pending | (n/a) |

**🆕 v1.3：Emergency MANUAL_REVIEW 触发条件**（不冲突激进偏好的安全网）

任一条件 → 该 ticker 进入 `MANUAL_REVIEW` 状态，自动操作全暂停，等人工处置：

- 单日 underlying gap > 12%（A 更宽 = 15%，B 更严 = 8%；由 G19 触发）
- 5 日 realized vol > 2× 60 日中位数
- DOJ / FDA / SEC 重大裁决新闻（Massive 标 high severity）
- NBBO spread > 5% premium（G23）
- Tier A 紧急 stop loss（short delta > 0.85）
- 死轮检测：cost-basis CC 年化收益 < 0.2%（触发逃生舱评估）

### 3.5 Assignment / Bag-Holder 管理（v1.3 双层）

被 assigned 时（两 tier 共通流程）：
1. **Cost basis 公式：** `Adjusted Basis = strike + Σ(roll debits) − Σ(all credits) − Σ(dividends)`
   - 包含：assignment 行权价 + 历次 roll 支付的 net debit − CSP 期 net credits − CC 期 net credits − 持有期分红
   - **死亡螺旋防护：** 代码硬性禁止"为赚 premium 反复 roll-down 导致 effective basis 升高"。Tier A `max_roll=0`，Tier B `max_roll=2 但禁纯 roll-down`
2. 预留 cash 变成 shares；重算 sector + ticker 敞口
3. 进入对应 tier 的 escalation 流程（见下）

**Wash Sale 追踪**（v1.1，taxable 账户必备）：`wash_sale_events` 表（§2.6）；G21 在新开 CSP 前查询 pending；cycle log 的 `realized_pnl` 区分会计 P&L 和经济 P&L

---

#### 3.5.A Tier A —— Classic Wheel Escalation

**硬约束：** 任一 Tier A CC 的 strike `K` 必须满足 `K ≥ cost_basis`。代码强制。

**🆕 v1.3：Tier A 由"愿意持有"驱动，escalation 阈值放宽（用户更宽容）**

| Tier | DD（vs basis） | 动作 |
|---|---|---|
| **T0 Normal** | 0% 到 -7% | 标准 CC：delta +0.22、DTE 30、K ≥ basis（v1.3 哲学：no TP / no time stop / no roll，hold to expiry） |
| **T1 Watch** | -7% 到 -12% | delta 降到 0.15-0.20；K = max(basis, current × 1.05)。日报标记 |
| **T2 Stress** | -12% 到 -22% | **暂停自动 CC。** 开 rescue ticket。LLM rescue 评估：继续持有？远期低 delta CC？加仓拉低 basis？ |
| **T3 Critical** | -22% 到 -35% | **此 ticker 全部自动暂停。** 强制 LLM rescue review。**可启用死轮逃生舱 + basis-below CC 例外路径** |
| **T4 Forced exit** | <-35% | 5 个工作日内人类必须 review。无输入则默认 3 日 TWAP 退出。Ticker 从 watchlist 移除 ≥ 90 天 |

**🆕 v1.3：死轮逃生舱**（council 推荐，处理"愿意持有"被永久套牢的边缘场景）

仅在 **同时满足以下所有条件**时，允许 CC strike < cost_basis（"basis-below CC"）：
1. 已至少 60 天未能在 K ≥ basis 卖出 CC（连续 4 周 `cc_no_viable_strike` log），或当前 K ≥ basis 的 CC 年化收益 < 0.2%
2. Ticker 在 T3 / T4 escalation
3. **操作员显式人工批准**（CLI / email）
4. 该笔锁定亏损 ≤ 季度 realized loss budget（默认账户净值 1%/季 = $5K/季）
5. **同时挂单买 protective call**（形成 collar，防股票反弹被低价 called away 锁定巨亏）—— Gemini 强烈建议
6. 在 daily report + 推送通知标红

不满足任一 → CC 不开，等价格恢复或强制退出。

**强制退出（无视 tier）：** underlying <$15/股、宣布破产、审计失败、持有 >365 天且未恢复到 -10% basis 内、市值跌破 $5B。

---

#### 3.5.B Tier B —— Fast Exit Escalation（v1.3 新设计）

**Tier B 被 assigned 是 *设计上的失败***。立即进入 `ASSIGNED_EXIT_PREMIUM` 子状态：

| Tier | DD（vs basis） | 动作 |
|---|---|---|
| **T_B_FAST_EXIT** | -3% 到 -8% | **立即评估**：30+ DTE deep-OTM CC（delta < 0.15、K ≥ basis）能否产生 ≥0.5% basis credit？能 → 卖一张；不能 → 进 T_B1 |
| **T_B1** | -8% 到 -15% | **暂停自动 CC。** 开 rescue ticket，2 选项：5 日 TWAP 卖股 / 远期 OTM CC（K ≥ basis）持 30-45 日 |
| **T_B2** | -15% 到 -25% | **5 日 TWAP 强制卖股**（不再 rescue review），ticker 自动从 B watchlist 移除 60 日 |
| **T_B3** | <-25% | 立即市价 close（gap risk 已实现）；ticker 移除 90 日；触发 CB3 检查 |

**🆕 v1.3：Tier B 持仓时间上限**

- **60 天硬上限** 或 **DD ≤ -15%** 任一先达 → 强制 5 日 TWAP 卖股（A 是 365 天）
- 这是 B 的核心 escape valve —— "不接受 lock-in long term loss"

**🆕 v1.3：Tier B 一律禁止 basis-below CC**

B 设计上不接受锁定亏损，宁可 TWAP 卖股。**不**走 A 的死轮逃生舱。这是 B 和 A 的根本差异 —— 否则 B 退化为 "A but smaller"。

### 3.6 组合层 Circuit Breaker（v1.3 加 per-tier CB）

**全局 CB（不分 tier）：**

| Breaker | 触发 | 动作 | 恢复 |
|---|---|---|---|
| **CB1 日亏** | 日 P&L < -1.5% equity (约 $7.5K) | 冻新仓 24h | 次日自动 |
| **CB2 周亏** | 5 日滚动 P&L < -4% (约 $20K) | 冻新仓；B 现有 TP 收紧到 30% | 手动解锁 |
| **CB3 Drawdown** | peak-to-trough DD ≥ **6%** (约 $30K) | 停新；CC 一有利润就平；保护现金 | 手动 + DD <4% 连续 5 日 |
| **CB4 灾难性 DD** | DD ≥ 12% (约 $60K) | 完全停；任何 option 都不开；只保 shares | 强制全系统 review |
| **CB5 Broker 健康** | 10 分钟内 API 错误 >10% 或 >5 个连续 5xx | 只读模式 | 30 分钟正常后自动 |
| **CB6 数据陈旧** | quote / IV / earnings >4h 未更新 | 跳过本 cycle | 数据新鲜后自动 |
| **CB7 仓位漂移** | Broker vs DB 不一致 >1 cycle | 暂停新仓 | reconcile 通过后自动 |
| **CB8 订单速率** | 单日新订单 >8 | 当日不再开仓 | 次日自动 |
| **CB9 单票 gap** | A: 单 ticker 日跌 >15% / B: >8% | 该 ticker 冻 A=5/B=10 个交易日（与 G19 联动） | 期满自动；或操作员手动早释 |
| **CB10 LLM 服务降级** | OpenRouter API 错误率 >50% 持续 30 分钟 | 进入 `llm_degraded` 模式：所有 LLM touchpoint 用 last-good cache | LLM 恢复后自动 |

**🆕 v1.3：Per-tier CB（双层独立监控）：**

| Breaker | 触发 | 动作 | 恢复 |
|---|---|---|---|
| **CB_A_DD** | Tier A 组件 peak-to-trough DD ≥ 8% | A 停新仓；A 现有 CC 一有利润就平；**B 继续** | 手动 + A DD <5% 连续 5 日 |
| **CB_B_DD** | Tier B 组件 DD ≥ 5% | B 全停 + 14 日内 B 全部新仓冻结；**A 不受影响** | 手动（强制 weekly review） |
| **CB_B_TURNOVER** | B 单周 turnover > 80%（已实现 P&L vs B AUM） | B 冻 7 日 | 自动 7 日后 |

**B 的 DD 阈值比 A 紧（5% vs 8%）** 是因为 B 单笔小、应该 self-stop 早；如果 B 整体 DD 都到 5% 说明 premium farming policy 本身在亏，需要先停下来审视。

**自动恢复** 用于基础设施问题（CB5/6/7、CB10）；**手动解锁** 用于资金问题（CB2/3/4、CB_A_DD、CB_B_DD）。资金类违规说明 *框架本身有问题*，不只是连接问题。

### 3.7 真实回报预期（$500K Base Case，v1.3 分 tier）

**🆕 v1.3：双层架构的回报路径完全不同，分别给出**

#### Tier A（占 $245K，约 49% 账户）—— Long stock + premium 模型

| 指标 | 基准 | 好年景 | 坏年景 |
|---|---|---|---|
| 年化总回报 | **7-10%** | 11-14% | -5% 到 +1% |
| Sharpe | 0.9-1.2 | 1.3-1.6 | 0.2-0.5 |
| Max DD | 8-13% | <6% | 15-22% |
| Assignment rate (CSP %) | **25-35%**（v1.3 delta 0.28 + no roll → 更高） | 18-25% | 40-55% |
| Premium 占总回报 | 约 50%（其余来自股票 capital gain / 分红） | 40% | 95% |

A 是 covered-stock 策略：股票 beta + premium cushion；DD 区间宽但 long-term path 稳。

#### Tier B（占 $245K，约 49% 账户）—— 纯 premium farming

| 指标 | 基准 | 好年景 | 坏年景 |
|---|---|---|---|
| 年化总回报 | **5-7%** | 9-12% | -8% 到 -2% |
| Sharpe | 0.6-0.9 | 1.0-1.3 | -0.3 到 0.1 |
| Max DD | 6-10% | <4% | 12-18% |
| Assignment rate (CSP %) | **<10%**（设计目标） | <6% | 15-20% |
| Premium 占总回报 | 95%+ | 95% | 100%+（capital loss） |

B 的 Sharpe 低于 A 不直观，但合理：B 高 turnover 带来 friction（slippage、wash sale、commission），单笔 edge 薄。B 的价值是 **与 A 低相关性** —— 牛市 cap upside 时 B 继续 farm premium；熊市 A 接货时 B 早就卖了。

#### 组合预期（A 49% + B 49% + 2% float）

| 指标 | v1.1 | **v1.3** | 解释 |
|---|---|---|---|
| 年化净回报 | 6-9% | **6-9%**（基准）/ 5-10%（极端区间扩） | 与 v1.1 几乎一样 |
| Sharpe | 0.8-1.2 | **0.9-1.2** | 略提（B 的低 correlation 帮助） |
| Max DD | 6-10% | **7-11%** | 上沿略提 1pp（0% cash 移除了软 buffer） |

**🆕 v1.3 诚实校准（用户激进选择的成本）：**
- **0% cash reserve**：组合在 8-10 个 CSP 同时被 assigned 时无 dry powder 应对 broker 异常 / 抓 dip，max DD 上沿从 10% 升到 11%
- **Tier A no TP/no time stop**：放弃 tastytrade 验证的 15-20% Sharpe 提升；A 的 Sharpe 上沿可能少 0.1-0.2
- **Tier A delta 0.28**：assignment rate 比 council 推荐的 0.24 高约 4-5pp；意味着更多时间是 long stock + CC 而非 CSP（这是 *feature* 不是 bug，符合 classic wheel 设计）
- **Tier A earnings ±5 日**：相比 council 推 ±10，会有少量 CSP 撞上 earnings vol，但用户表态可接受

**预期 vs 真实差距的最大不确定性：** Tier A 在熊市的真实 DD。v1.0 backtest 假设的 "可生存" 在 2022H1-style 单边下跌中可能恶化到 -20%（council Gemini 强烈建议 paper Phase 4 强制做 2022H1 stress backtest）。这是 §5 forward test 必须验证的核心问题。
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

### 4.1 LLM 接入点清单（v1.3：双层引入新 touchpoint）

**🆕 v1.3：8 个 touchpoint**（v1.1 是 6 个，新增 `conviction-curate` + `outlook-quickcheck`；原 `watchlist-curate` 重定位为 Tier B 专属）。

| # | Touchpoint | 频率 | 模型 | Tier 范围 |
|---|---|---|---|---|
| 4.1.1 | `market-outlook` | 每日 | Sonnet 单 | 全局 |
| 4.1.2 | `tier-b-watchlist-curate` | 每周日 | 3 模型 council | Tier B only |
| **🆕 4.1.3** | **`conviction-curate`** | 月度 + 事件 | Opus 单 | Tier A only |
| 4.1.4 | `fundamentals-deepdive` | 月度/事件 | Opus 单 | A + B |
| 4.1.5 | `outlook-analysis` | 每周 | Sonnet 单 | A + B |
| **🆕 4.1.6** | **`outlook-quickcheck`** | Tier B 入场前实时 | Sonnet 单 | Tier B only |
| 4.1.7 | `conviction-health-check` | 月度 + 事件 | Opus 单 | Tier A only |
| 4.1.8 | `rescue-decide` | 事件 | 3 模型 vote，prompt 按 tier 分支 | A + B |

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

#### 4.1.2 Tier B Watchlist curation —— `tier-b-watchlist-curate`（每周，3 模型 council）
- **🆕 v1.3：仅 Tier B**（原 v1.1 `watchlist-curate` 重定位为 Tier B only）
- **决策：** 在 screener shortlist（已排除 Tier A）上，给出 10-15 个 Tier B ticker 排名
- **频率：** 每周日 18:00 ET
- **输入：** 同 v1.1 watchlist-curate dossier；**评分维度调整**：去掉"愿意持有 2+ 年"，加入"premium_yield_30d_estimate / options_liquidity_score / catalyst_density_next_30d (少为佳) / avg_iv_rank_60d"
- **输出 schema 变化（v1.3）：** 每个 ticker 加 `tier_b_score(0-100)`, `flag_for_conviction_review(bool)` —— 后者用于 B → A 升级建议（≥4 周满足高分 + 3/3 一致 + 90 日胜率 ≥80%）
- **为什么用 LLM：** Tier B 是 "premium farming"，需要 LLM 评估近期 catalyst window 和 option liquidity 这种定性 + 多信号问题
- **模型：** **3 模型 propose-then-blind-score council**（v1.1 已收敛）—— `claude-opus-4-7` + `gpt-5.4` + `gemini-3.1-pro-preview`
- **🆕 v1.3 B → A 升级机制：** LLM **只能 flag**，不能自动升级。写到 weekly review email "建议考虑升级到 Conviction" 区。用户手工 `wheels-cli conviction promote XYZ --thesis-file thesis.md` + 必须输入 ≥200 字 thesis 才生效
- **成本：** ~$2-3/run × 每月 4-5 = **约 $8-12/月**（v1.3 候选池更小，比 v1.1 节省）

#### 🆕 4.1.3 Tier A Conviction curation —— `conviction-curate`（月度 + 事件，单 Opus）
- **决策：** 基于用户自然语言偏好（`config/conviction_preferences.md`），提议 Tier A 名单（add / remove / keep / flag）
- **频率：** 月度第 1 个交易日 07:30 ET + 事件触发（用户改 preferences、A 持仓有 earnings、conviction-health-check 标 urgent）
- **输入：** 用户 preferences 文件、Tier A 当前持仓 + 候选池（hard screener 过滤后：市值 >$20B、avg OI >5000、排除高风险类）、每个候选的 fundamentals-deepdive dossier + 90 日 outlook trend
- **输出 schema：**
  ```json
  {"effective_date": "ISO",
   "user_preferences_hash": "sha256...",
   "proposed_tier_a": [
     {"symbol": "NVDA",
      "action": "keep|add|remove|flag",
      "thesis": "...(paragraph)",
      "alignment_score": 0-100,
      "max_account_pct": float,
      "concerns": [...]}],
   "removed_from_prior": [...],
   "added_from_prior": [...]}
  ```
- **为什么用 LLM：** 用户用自然语言写"愿意持有"的偏好，需要 LLM 把这些定性 thesis 映射到具体 ticker 候选 + fundamentals 评估
- **模型：** 单 **Opus 4.7**（长上下文综合 + thesis 写作的强项）；用户保留最终决定权 → council 在此 overkill
- **🆕 v1.3 审批流程（不自动应用）：**
  1. LLM 输出 → `workspace/YYYY-MM-DD/conviction_proposal.json` + 写入 DB `tier_a_proposals` 表 PENDING
  2. 周末 review email 顶部：列出 add/remove/keep + LLM 论证
  3. 用户回邮件 / CLI：`wheels-cli conviction approve <date>` / `reject` / `partial --add NVDA --remove META`
  4. **per-symbol approve**（council 推荐）—— 不能一键全过
  5. 用户批准后才更新 watchlist DB；新入 ticker 7 个交易日 cooldown 后才可首开 CSP（G24）
  6. **永远不自动应用**；默认 stay，沉默不等于批准
- **成本：** ~$2-3/月度 run + ~$1-2 事件触发 = **约 $4-6/月**

#### 4.1.4 基本面深读 —— `fundamentals-deepdive`（每月每 ticker，单 Opus）
- **决策：** 单 ticker 基本面评分卡，作为 watchlist-curate council 的 *evidence* 使用，不单独决策。
- **频率：** 每月对 watchlist 上每个 ticker 做一次；earnings event 时刷新（事件触发）；screener 新候选首次进入时做一次。
- **输入：** 10-K + 最新 10-Q（EDGAR）、最新 earnings transcript、4 个季度 consensus、revision trend、资产负债率、3-5 个同业 peer 对标。
- **输出 schema：** `business_quality(1-10)`、`balance_sheet_strength(1-10)`、`earnings_quality(1-10)`、`valuation_vs_peers(cheap/fair/rich)`、`red_flags(str[])`、`competitive_moat`、`wheel_holdability(1-10)`、`summary`。
- **为什么用 LLM：** 读 10-K → 一页评分卡是 LLM 经典场景。
- **模型：** 单 **Opus 4.7** —— 长上下文文档综合的强项。不用 council；输出是被下游 council 加权的 evidence。
- **成本：** $0.40/ticker × 40 ticker-events/月 = $16/月（无缓存）；**$8/月（季度缓存）**。

#### 4.1.5 Outlook 分析 —— `outlook-analysis`（每周，单 Sonnet）
- **决策：** 每个 watchlist ticker 的前瞻 thesis：未来 1-3 月预期、催化日历、非对称风险标记。
- **频率：** 每周（作为 watchlist-curate 的输入）。加单日 >10% 大跳的事件触发。
- **输入：** 近期新闻聚合（Massive 14 日）、分析师评级变化（Finnhub）、即将到来的催化、UW 异常资金流、行业动量、技术面（RSI、距 SMA200、ATR）。
- **输出 schema：** `directional_lean(bullish/neutral/bearish)`、`catalyst_calendar[]`、`iv_compression_risk`、`tail_risk_flags`、`recommend_pause(bool, reason)`。
- **为什么用 LLM：** 把新闻 + 资金流 + 技术面综合成前瞻 thesis 是判断题。
- **模型：** 单 **Sonnet 4.7**。
- **成本：** $0.08/ticker × 20 watchlist tickers × 每周 1 次 × 4 周 = **约 $6-8/月**。

#### 🆕 4.1.6 Tier B 入场前 Outlook gate —— `outlook-quickcheck`（实时，单 Sonnet）
- **决策：** Tier B ticker 在 09:45 morning cycle 准备开 CSP 前，实时检查"近 14 天 outlook 是否较周日 curate 时显著恶化"
- **频率：** Tier B 每次入场前；同日同 ticker 命中 cache
- **输入：** ~150 token 简短 dossier（ticker + 当前 outlook score + 14 天主要新闻 + 重大价格变化）
- **输出 schema：** `{outlook_deteriorated: bool, severity: low|med|high, reason: str}`
- **Gate 行为：** `high` → 拒绝本次入场，flag 到日报；`med` → 目标 delta 收紧一档；`low` → 放行
- **不应用于 Tier A：** A 的长持 thesis 不应被 14 天新闻波动驱动入场决策
- **为什么用 LLM：** 周日 curate 后到实际入场可能有 5-7 天信息差，新闻可能改变 outlook
- **模型：** 单 **Sonnet 4.7**（latency-sensitive，需要快）
- **成本：** ~$0.005/次 × 约 80 次/月 = **约 $0.4/月**（cache hit 率高时更低）

#### 4.1.7 Tier A Conviction 健康检查 —— `conviction-health-check`（月度 + 事件，单 Opus）
- **决策：** Tier A 当前持仓 ticker 的健康状态评估（thesis 是否仍 intact、有无重大警报）
- **频率：** 月度第 1 个交易日 07:30（同 conviction-curate 触发） + 事件触发（earnings +1、单日 |move| > 10%、高严重新闻、多家 analyst downgrade 同日）
- **输入：** ticker 的最新 fundamentals + 90 日 outlook trend + earnings transcript + Massive 重大新闻
- **输出 schema：**
  ```json
  {"ticker": "AAPL",
   "review_date": "ISO",
   "thesis_intact": true|false|"weakening",
   "concerns": [{"severity": "info|warn|critical", "category": "...", "summary": "..."}],
   "user_action_required": false|"flag_for_review"|"urgent_review",
   "suggested_downgrade_to_tier_b": false,
   "long_term_thesis_summary": "..."}
  ```
- **能做：** fundamentals 健康检查、earnings 解读、新闻警报、行业前景变化
- **不能做：** 提议加 / 移除 ticker（用户专属权力）；改变 `max_account_pct`
- **User override 流程：** LLM 标 `urgent_review` → daily report 顶部红 banner + 推送通知 → 用户 CLI: `wheels-cli conviction review AAPL` 看完整 dossier → 保留 / 降级到 B（`demote AAPL`）/ 彻底 drop（`blacklist AAPL --days 90`）
- **模型：** 单 **Opus 4.7**
- **成本：** ~$3/月（8 ticker × 月度 + 偶尔事件触发）

#### 4.1.8 Bag-holder 救援 —— `rescue-decide`（事件触发，3 模型 vote，prompt 按 tier 分支）
- **触发：** Assigned ticker 跌破 basis -15%，或 ASSIGNED 状态 >60 天未成功卖出 CC，或 CC 已 roll 2 次。
- **频率：** 事件触发。稳态：0-3 次/月。
- **输入：** 完整仓位历史、当前 basis vs market、≥basis 的 CC 行权价候选、持有天数、最新基本面、行业 + 宏观背景、保护性 put / collar 合约链。
- **输出 schema：** 恰选一项：`{hold_and_wait, sell_cc_at_basis, sell_cc_below_basis_for_premium, buy_protective_put, convert_to_collar, take_loss_close, escalate_to_human}` + 理由 + 估算 P&L 影响。
- **为什么用 LLM：** Research §5 唯一明确点名 LLM 加价值的地方。高信息密度、多选项、单一规则 base rate 差。
- **🆕 v1.3：Prompt 按 tier 分支，输出空间不同**
  - **Tier A rescue**：评估 *thesis* 是否仍 intact。可选项：`hold_and_wait` / `sell_cc_below_basis_for_premium`（远期低 delta，配 collar）/ `convert_to_collar` / `take_loss_close`（仅 thesis 实质破坏才提）。**禁止**"快速无痛退出"
  - **Tier B rescue**：评估 *最快无痛退出*。可选项：`twap_exit_5d`（5 日 TWAP 平股）/ `sell_cc_below_basis_for_premium` 长慢 bleed / `take_loss_close`。**禁止**"加仓拉低 basis"
- **模型：** **3 模型 propose-and-vote**。同 watchlist 的 3 个。多数票胜（2/3）。平票 → `escalate_to_human`
- **成本：** ~$1-3/次。预期 **<$5/月**

#### 4.1.9 每日 "今天该不该交易" → 已合并到 4.1.1
Regime read 已经处理。单独的 gate 是冗余 + 增加延迟。

#### 4.1.10 Strike / DTE / delta 选择 —— **不用 LLM**
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

**🆕 v1.3 稳态月成本（双层 + 新 touchpoint）：**

| 接入点 | 频率 | $/run | $/月 |
|---|---|---|---|
| Regime read (Sonnet) | 22 交易日 | $0.02 | ~$0.50 |
| Tier B watchlist council (3×) | 4-5/月 | ~$2 | ~$8-10 |
| **🆕 Tier A conviction-curate (Opus)** | 月度 + 偶尔事件 | $2-3 | ~$4-6 |
| Fundamentals (Opus) | 约 40 ticker-events | $0.40 | ~$8（缓存） |
| Outlook (Sonnet) | 约 80 ticker-events | $0.08 | ~$5（v1.3 仅 B） |
| **🆕 Outlook-quickcheck gate (Sonnet)** | Tier B 入场前 | ~$0.005 | ~$0.40 |
| **🆕 Conviction health check (Opus)** | 月度 + 事件 | ~$0.40 | ~$3 |
| Rescue council (3×) | 0-3/月 | ~$2 | ~$3 |
| **总计** | | | **~$32-36/月** |

vs 预期月毛 premium $3-8K → 成本 <1% 收入。v1.3 比 v1.1（~$25-30）增加约 $5-6/月，主要是新增的 Tier A 工作流（conviction-curate + health-check）。仍远低于约束。

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

### 5.1 分阶段推进（v1.3：16 周 + Tier A 完整 cycle 覆盖）

🆕 v1.3：从 10 周延长到 16 周（council 一致认为原 10 周覆盖不了 Tier A 真 classic wheel 的完整 cycle）。Phase 1 改为 4 周 shadow-only；新增 2022H1 stress backtest。

#### Phase 1 —— Shadow-only（Week 1-4，2026-06-23 → 2026-07-20）
- **🆕 v1.3：4 周 shadow-only**（Tier A 单 ticker 真下单 + Tier B shadow，先校准 edge case）
- **范围：** Tier A 仅 SPY 真下单（1 张合约）；Tier B 影子模式（虚拟入场，不提交真订单），用于积累 outlook-quickcheck 数据 + 测试 conviction-curate workflow
- **进入条件：** M0-M2 完成（含双层 schema + conviction-curate skill）；手工 dry-run 跑通一次 Tier A 完整 CSP→assign→CC→called-away；OpenRouter 接入；`conviction_preferences.md` 已编写
- **退出条件：** Tier A SPY 至少完成 1 个完整 cycle；0 个 orphan 订单；0 个 reconcile drift；日报 ≥18/20 天到达；Tier B shadow 数据 > 100 条候选

#### Phase 2 —— Tier A 5 + Tier B 5，mechanical only（Week 5-6）
- **范围：** Tier A 5 个真下单；Tier B 5 个 + LLM curate 启用（outlook-quickcheck gate 在监控模式不阻断）
- **进入条件：** Phase 1 退出全绿；conviction-curate 首次提议被用户 approve；Tier B watchlist seeded
- **退出条件：** Tier A 至少 3 个完成至少 1 cycle；Tier B 5 个都至少开过 1 次 CSP；slippage <5% vs 理论 mid；0 个 portfolio CB 触发

#### Phase 3 —— 完整双层 + 全部 LLM 开启（Week 7-11，5 周）
- **范围：** 完整 Tier A 8 + Tier B 10 watchlist；所有 LLM touchpoint 全启用（含 outlook-quickcheck 实时 gate、rescue-decide 触发器、conviction-health-check 月度）
- **进入条件：** Phase 2 退出全绿；LLM fallback 路径验证（kill API key 不阻断交易）；shadow A/B 评估框架就位
- **退出条件：** Tier A 至少 4 个完成 1 完整 cycle + 1 个完成 2 完整 cycle；Tier B shadow A/B 达 §5.3 LLM 质量门槛；0 rescue 决策用户会推翻

#### Phase 4 —— 压测 + 边缘场景 + 2022H1 stress backtest（Week 12-16，5 周）
- **范围：** 不加新功能。注入 §5.4 全部 16 个压测场景 + 跑历史 stress backtest（2022 Jan-Jun 单边下跌）
- **🆕 v1.3：2022H1 stress backtest**——用代码回测 Tier A 真 classic wheel（no TP / no time stop / hold to expiry / accept assignment）在 -25% market 中的 max DD + 资金利用率退化路径。让用户对"死扛"有真实预期，再决定是否保留 v1.3 激进设置
- **进入条件：** Phase 3 退出全绿；近 14 天 0 个 critical bug
- **退出条件：** 全部 16 个压测场景都发生过（自然或注入）且被正确处理；2022H1 backtest 结果用户已 review；§5.3 全部 go-live gate 全绿

任一 phase 退出失败 → **重复该 phase 1 周**。总 buffer：2 周。Week 18 之后 → 重新规划。

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
- 累计 P&L 减去 (commission × 1.5) 在 **16 周中 >0**（v1.3）
- Cycle 胜率 ≥60%，**Tier A 至少 5 个完整 cycle，Tier B 至少 8 个**（v1.3）
- Max drawdown <**6%** 起始资金（$30K on $500K，v1.3 比 v1.1 5% 略放宽，因 0% cash）
- 年化 premium yield (paper) ≥**7%**（v1.3 略放宽，因 Tier A no TP 牺牲了 yield）
- **🆕 v1.3：2022H1 stress backtest 中 Tier A 模拟 max DD < 22%**（如果超过此值，强制把 Tier A delta 改回 -0.24）

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

**🆕 v1.3：M0/M1/M2 增加双层 schema + conviction workflow；M5 加 2022 backtest + 双层压测脚本；M6-M11 加 Postgres 迁移；Live 阶梯多 1 步（M7 Live-μ）。**

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

### M2 —— 双层 watchlist + Tier A conviction workflow（Week -2 至 0，2026-06-09 → 2026-06-22）
**交付物（🆕 v1.3）：**
- `db/watchlist_repo.py` —— 含 `policy_profile` 列、`tier_a_proposals` 表、`active_policy_profile` snapshot 逻辑
- `engines/policy_profile.py` —— 三层 config merge（profile defaults → ticker override → final）
- `engines/portfolio_risk.py`（行业集中度 per-tier、per-ticker 敞口、净 delta、Tier A/B 各自 AUM 跟踪）
- 所有 G8-G12 + G19-G25 双层 gate 接通
- `engines/csp_selector.py` / `cc_selector.py` / `wheel_exit_plan.py` 重构签名接受 `profile: PolicyProfile` 参数
- `engines/premium_exit.py` 实装 Tier B 三种 mode（twap_dump / cc_bleed / hybrid）
- `engines/roll_decider.py`（仅 Tier B；Tier A 无 roll）
- `engines/rescue_engine.py` skeleton + tier-aware T0-T4 / T_B_FAST_EXIT-T_B3
- CB1-CB10 + CB_A_DD + CB_B_DD + CB_B_TURNOVER 全部接通
- `scripts/seed_conviction_preferences.py` —— 生成 `config/conviction_preferences.md` 模板
- `scripts/reconcile_alpaca.py` 在真 paper 账户上
- Alpaca paper API 端到端集成（真 chain、真订单、真 fill）
- `skills/daily-report/` 加 wheel + 双层 section
- `wheels-cli`：`conviction {list,add,demote,promote,blacklist,review,approve,reject,partial}` 子命令
- `scripts/status.py` CLI

**退出：** Phase 1 进入条件全部满足。可以开始 paper（shadow-only）。

### M3 —— Phase 1 shadow 运行（Paper Week 1-4）
不加新代码。运行 Phase 1 shadow-only。并行用 branch 推 M4。

### M4 —— LLM 集成层（Paper Week 1-5，并行 Phase 1-2）
**交付物（🆕 v1.3：8 个 touchpoint）：**
- `shared/adapters/openrouter.py` 验证可用
- `skills/market-outlook/`（Sonnet，每日 regime）—— 全局
- `skills/code-screener/`（4 阶 pipeline）—— 双层共用，Tier A 额外加 hard screener
- `adapters/transcripts.py`（API Ninjas）
- `adapters/web_fetch.py`（httpx + readability + 30d cache）
- `skills/fundamentals-deepdive/`（Opus）—— A + B
- `skills/outlook-analysis/`（Sonnet 每周）—— A + B
- 🆕 `skills/outlook-quickcheck/`（Sonnet 实时 gate）—— Tier B only
- `skills/tier-b-watchlist-curate/`（3 模型 council，每周日）—— Tier B only
- 🆕 `skills/conviction-curate/`（Opus，月度 + 事件）—— Tier A，含 user approval workflow
- 🆕 `skills/conviction-health-check/`（Opus，月度 + 事件）—— Tier A
- `skills/rescue-decide/`（3 模型 vote，prompt 按 tier 分支）—— A + B
- `schemas/llm_outputs.py` 涵盖 8 个 touchpoint
- LLM fallback path 测试（kill API key → bot 继续交易）
- `model_outputs` + `tier_a_proposals` 表记录每次 call 及成本

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
- 🆕 `scripts/stress/inject_premium_assignment_cascade.py`（v1.3：Tier B 在 VIX spike 中批量 assigned）
- 🆕 `scripts/stress/backtest_2022_h1.py`（v1.3：用 2022 1-6 月历史数据回测 Tier A 真 classic wheel）
- 推送通知集成（ntfy.sh 或 Slack）

**退出：** Phase 4 进入条件满足，2022H1 backtest 结果用户已 review。

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

Kickoff 前未处理的部分，按 provisional 答案推进。

**已 resolved（v1.3 用户已锁定）：**

| # | 问题 | 决定 |
|---|---|---|
| ~~5~~ | LLM 模型组合 | ✅ **3 模型 council**：Opus 4.7 + GPT-5.4 + Gemini-3.1-Pro |
| ~~7~~ | per-ticker 上限 | ✅ A=6% ($30K)、B=4% ($20K)、ETF=10% ($50K) |
| ~~8~~ | Drawdown CB 阈值 | ✅ 全局 6% + per-tier CB_A_DD 8% / CB_B_DD 5% |
| ~~17~~ *(v1.3)* | **🆕 双层 vs 单层 watchlist** | ✅ **双层 policy regime**（Tier A Conviction 真 classic wheel + Tier B Premium tastytrade-style） |
| ~~18~~ *(v1.3)* | **🆕 Tier A 维护方式** | ✅ **LLM-curated by `conviction-curate` skill + 用户 natural-language preferences**（user per-symbol approve）|
| ~~19~~ *(v1.3)* | **🆕 资金 cash reserve** | ✅ **0% hard cash** + 2% working float（用户 override council 推 5%）|
| ~~20~~ *(v1.3)* | **🆕 Tier A 是否 hold to expiry** | ✅ **是**：no profit take、no time stop、no roll、accept assignment |

**仍待用户决定（v1.3 新增 + 沿用）：**

| # | 问题 | Provisional 答案 | 为什么问 |
|---|---|---|---|
| 1 | **代码复用方式** | 现在 vendor-copy → 后续抽 pip package | 确认 vs monorepo 或 day-1 就抽 package |
| 2 | **账户类型** | taxable（含 wash sale 追踪） | Alpaca 不支持 IRA。Wash sale 短期 capital gain 处理已纳入正文，但确认你接受这套税务复杂度 |
| 3 | **Tier A 初始候选 + `conviction_preferences.md` 起草** | 我可以根据你跟我说的偏好（mega-cap tech with AI、避开 financials/biotech 等）起草一份 | 你想我先起草让你 review？还是你自己写？ |
| 4 | **推送通知通道** | 建议 ntfy.sh | 接受？还是用 Slack / SMS / 只邮件？ |
| 6 | **Live-α 起始资金** | $50K（$500K 中的 10%） | 已加 Live-μ ($5-10K) 在 α 之前。是否仍想用 $50K 作 α？ |
| 9 | **Earnings transcript 提供商** | API Ninjas（约 $30/月起步） | 这个成本可接受？或者跳过、只靠 10-Q？ |
| 10 | **Dashboard / Web UI** | v1 跳过（只 CLI + 邮件） | 想早点有个最小 web view？ |
| 11 | **Live 期间是否 paper 并行** | 是，Live-α + Live-β 前 4-8 周 | 有用还是浪费？ |
| 12 | **账户号** | wheels 用单独 Alpaca 账户 | 确认 —— 不与 options-copilot 共用账户 |
| 13 *(v1.1)* | **Postgres 部署位置** | 本地 / Docker / 云（Supabase / RDS / Railway） | M6 阶段决定即可，但 hosting 路径影响 SLA |
| 14 *(v1.1)* | **Live-μ micro-test 金额** | $5-10K | 想 $5K（更便宜）还是 $10K（更接近 $50K 行为）？ |
| 15 *(v1.1)* | **basis-below CC 例外路径（Tier A 死轮逃生舱）** | 启用，T3/T4 + 60d 无 viable + 人工批准 + collar 同挂 + 季度 loss budget ≤1% | 接受？还是直接禁止（A 永不 sub-basis）/ 更宽松 |
| 16 *(v1.1)* | **`shared/` 上游 sync SLA** | 每周一次 diff、bugfix 24h sync | 接受？还是更紧（每日）/ 更松（每月）？ |
| **🆕 21** *(v1.3)* | **0% cash reserve 是否在 paper 8 周后强制 review** | 是，§3.1 council 强烈推荐至少 5% hard | 如果 paper 8 周 0 broker 事故/0 settlement 问题 → 保留 0%？还是规定无条件升到 5% |
| **🆕 22** *(v1.3)* | **Tier A LLM 自动 demote 到 B 的权限** | 默认 LLM 只能 `flag`，不能 auto demote | OK？或者允许 LLM 自动 demote（如果 thesis 显著恶化）减少用户负担？ |
| **🆕 23** *(v1.3)* | **B → A 升级的 thesis 字数要求** | 用户手工 promote 时必须输入 ≥200 字 thesis | 接受？还是更松（无要求）/ 更严（用 LLM check thesis 质量）？ |
| **🆕 24** *(v1.3)* | **conviction-curate 是否升级为 council** | 默认单 Opus；user approval 保底 | 如果对单模型偏见担忧，可升级为 3 模型（成本 +$8/月） |
| **🆕 25** *(v1.3)* | **2022H1 stress backtest 不合格阈值** | Tier A 模拟 max DD < 22% | 接受？或更严（如 <18%）/ 更宽（<25%）？ |

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

**🆕 Council 3（双层 specialist 设计 council，2026-05-19 上午）** —— 3 个 specialist subagent 并行设计 v1.2 双层 watchlist：

1. **System Architect**（Plan subagent）—— 设计 §2.6 schema 加 `policy_profile`、§2.4 FSM 加 `ASSIGNED_EXIT_PREMIUM`、§2.8 config `profiles:` 结构
2. **Quant / Risk Specialist** —— 设计 §3 双层参数表 + per-tier CB + Tier B fast-exit ladder
3. **AI / LLM Integration** —— 设计 §4 双层 LLM 工作流（A 用户主权 + B LLM curate）

输出综合为 v1.2 提案（未单独 commit），用户给出 5 项修改意见 → 形成 v1.3 提案。

**🆕 Council 4（外部 v1.3 review council，2026-05-19 下午）** —— 通过 `~/.claude/skills/council-review.md` 再次触发：

1. **GPT-5.4** + **Gemini-3.1-Pro** 独立审阅 v1.3 提案
2. **GPT-5.4** 综合 3 方观点出 consensus
3. 给出 9 项安全网建议 + 6 项与用户偏好的"温和分歧"（建议保守，用户保留激进选择）

我把 9 项不冲突安全网（emergency MANUAL_REVIEW、死轮逃生舱、Tier A LLM hard screener、16 周 forward test、2022H1 backtest 等）全部 apply；6 项与用户激进选择有分歧的（0% cash、Tier A delta、IVR 范围等）按用户选择 apply 但在正文标记 "用户 override，council 推 X"。原始 v1.3 review + consensus 见 [附录 B](#附录-bv13-council-review-2026-05-19-已应用)。

**这与 `options-copilot` 的 5 模型交易 council 不同：**
- **options-copilot council：** 5 个同角色 LLM 提议交易、互相 blind-score、投票 → 共识
- **wheels-copilot 起草 council（Council 1, 3）：** 不同角色 specialist 写互不重叠的章节，我做跨章节冲突 reconcile
- **wheels-copilot 外部 review council（Council 2, 4）：** 2 个独立 LLM 做 adversarial review + 1 个综合 —— 像 PR review

3 种模式都符合 "council" 精神 —— 多元视角 + 结构化聚合。第一种适合 *同类决策的共识*；第二种适合 *不同维度的深度*；第三种适合 *Claude / 用户盲点的发现*。Project planning 这种"先起草、用户反馈、再外部审视"的多段 council 模式是 v1.3 的核心方法论。

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

**Plan v1.3 完。** 下一步：处理 §7 仍待决定的问题（17 个，其中 5 个新增 v1.3 specific），然后开始 M0 工程。

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

---

## 附录 B：v1.3 Council Review（2026-05-19 已应用）

> **状态：** ✅ 已应用到正文（v1.1 → v1.3）。本附录是审计记录 + future reference。
>
> **触发方式：** [`~/.claude/skills/council-review.md`](file:///Users/tianyuwang/.claude/skills/council-review.md) 在 `/tmp/v1.3_proposal.md` 上跑
>
> **审计 JSON：** `/tmp/council_review_audit_20260519_211927.json`
>
> 下面是 v1.3 提案（v1.2 council 设计 + 用户 5 项 feedback）的 GPT-5.4 + Gemini-3.1-Pro review + consensus。**正文 §0-§8 已经按 consensus 应用了所有不冲突用户偏好的安全网**（emergency MANUAL_REVIEW、死轮逃生舱、Tier A LLM hard screener、16 周 forward test、2022H1 stress backtest 等）。**与用户偏好有分歧的 6 项**（0% cash、Tier A delta -0.28、IVR 20-75、earnings ±5、VIX >30 等）按用户选择 apply 并标记 "用户 override"，paper 8 周后强制 review。

---

以下按章节给 actionable feedback：

1. **§3.1 资金分配：0% cash 不适合 $500K 真钱**
- 隐藏风险：assignment/exercise 与期权平仓现金流存在 **settlement mismatch**；Alpaca 若临时提高 house requirement、收 OCC pass-through fee、数据/借贷费用异常，会导致账户“名义 cash 够、可用 buying power 不够”。
- 建议：**硬保留至少 5% true cash（$25K）**，工作 buffer 提到 **3%**；A/B 改成 **46%/46%/5%/3%**。若坚持激进，最低也别低于 **3% hard cash + 2% buffer**。
- 另：per-ticker cap $30K/$20K 在 $500K 下偏松，建议加 **sector cap 20%**、**single earnings-week gross exposure cap 15%**。

2. **§3.2 参数：Tier A 过激进，Tier B 欠约束**
- Tier A put delta **-0.28 → -0.24~-0.26**。30DTE 下 0.28 在单名股上 assignment 频率过高，Wheel 会退化成长期持股+CC。
- Tier A call delta **+0.22 → +0.18~+0.20**，否则优质股更容易被过早 called away，违背“愿意长期持有”。
- Tier A IV rank **20-75 太宽**；建议 **30-65**。IVR<25 premium 不够补 tail risk，>65 往往是 event/regime shift。
- Tier B put delta **-0.20 偏高**；建议回到 **-0.16~-0.18**，因为 B 不是 conviction bucket。
- Tier B call delta **+0.30 → +0.25**，减少低质量票在反弹时被过度 upside truncation。

3. **§3.3 Tier A lifecycle：no TP/no time stop 可以，但 emergency 规则不够**
- 仅靠 `delta>0.85 OR 3x credit` 太晚。真钱场景更怕 **overnight gap / halt / delisting / corporate action**。
- 建议新增强制 `MANUAL_REVIEW`：
  - **pre-earnings禁新开仓**：A 也别放到 ±5，改 **±10 calendar days**
  - **单日 gap >12%** 或 **5日 realized vol > 2x 60日中位数**
  - **credit rating / auditor / DOJ/FDA/SEC major event**
  - **option market wide NBBO spread > 5% premium** 时禁止自动下单
- `ASSIGNED_HELD` 触发从 0.75 提升到 **0.80~0.85**，0.75 才报警太迟。

4. **§3.5 LLM curation：最大盲点是“自动研究正确，主观偏好错误映射”**
- 必须加 **two-step approval**：`proposal -> explicit per-symbol approve`，禁止 batch yes 默认全过。
- 加 **cooldown**：新加入 Tier A 的 ticker **7 个交易日后** 才可首开 CSP。
- 加 **hard exclusion policy**：recent IPO<18m、M&A pending、dual-class governance red flags、avg option OI / volume 低流动性股票不得入 A。
- LLM 只能“提议”，**不得直接改 max_account_pct**；仓位上限必须是 deterministic rules engine。

5. **§5 Forward test：10周明显不够**
- 盲点：10周覆盖不了 **A bucket 的 assignment→CC→called away** 完整闭环，也看不到月度 curation 多轮效果。
- 建议：**paper forward 至少 16 周，最好 24 周**；其中前 8 周只做 signal+shadow orders，不真成交逻辑。
- 必测场景：**ex-div early assignment、halt、split/spin-off/special dividend、partial fill、stale quote、OCC exercise exception、broker API outage at expiry day**。

6. **Circuit breaker / 风控阈值具体建议**
- 账户级：**单日净值回撤 -2.5% soft stop，-4% hard stop；5日 -6% hard stop**
- 标的级：**单日 gap 10%**（A）/**8%**（B）即冻结新仓；5日累计跌幅 **15%/12%**
- VIX：A **>27 freeze new CSP**，B **>24**；VIX term structure inversion 连续 2 天也冻结
- 仓位：单 ticker **max 5% notional**（A/B 都降），单 sector **20%**，同时在途订单预留 **1.2x cash reservation**

7. **最关键的现实判断**
- §3.1+§3.2 当前组合会让 Tier A 在真实市场中大概率变成 **long stock inventory strategy**，不是 premium harvesting。若这是 feature，可接受；若不是，需把 A 调回 **-0.25 delta / 35-45 DTE / call 0.18**。

---

## Gemini-3.1-Pro Review Summary

这份 v1.3 提案在向“原教旨 Wheel”靠拢，但在 $500K 真钱生产环境中，存在致命的流动性与尾部风险。以下是具体的修改指令：

### 1. 资金分配与流动性风险 (§3.1)
**0% 现金是真钱环境的灾难（不切实际的假设）。**
*   **隐藏风险：** Alpaca 在期权行权（Assignment）时，资金结算存在 T+1 错配。如果多个 CSP 同时被 assign，账户瞬间满仓股票，此时若扣除 SEC fee、ADR 托管费或 margin 变动，账户会触发 Margin Call 甚至被强平（Liquidation），导致非受控亏损。
*   **Actionable 修改：** 绝对不能 0% Cash。强制设定 **5% ($25K) Hard Cash Reserve** + **5% ($25K) Buffer**。资金桶改为：**A 45% / B 45% / Cash 10%**。

### 2. 核心参数与“死轮”边缘场景 (§3.2, §3.3)
用户要求的“激进 Delta + 不止盈不止损 + 强制高于成本价卖 CC”会产生严重的 **“死轮（Stuck Wheel）”** 效应。
*   **隐藏风险：** 当底层资产暴跌 30%（如财报雷），你接盘后，若坚持 `K ≥ cost_basis` 卖 CC (§3.3 4a)，其 Premium 将趋近于 0（< 0.05）。系统将陷入长期持有死仓且无现金流的僵局，这违背了 Wheel 赚取 Premium 的初衷。
*   **Actionable 修改：**
    1.  **Put Delta 调降：** Tier A 的 -0.28 在 30 DTE 下 Gamma 风险极高，建议改为 **-0.24**。
    2.  **增加 Sub-basis CC 逃生舱 (§3.3 4a)：** 当 `cost_basis` 对应的 CC 收益率 < 0.2% 时，允许以低于成本价（如 Delta 0.15）卖出 CC，但**必须**同时挂单买入对应股票的 Protective Call（形成 Collar），防止股票反弹时被低价 Called away 锁定巨亏。
    3.  **重构 Circuit Breaker (§3.2)：** Delta ≥ 0.85 作为止损太迟钝（此时已深度 ITM）。应改为：**Underlying 跌破 Strike 15% 且 VIX > 35 时触发 Manual Review**。

### 3. LLM Curation 的过度工程与欠工程 (§3.5)
*   **过度工程：** 让 LLM 每月基于自然语言重新提议 Tier A 仓位，容易因 Prompt 漂移（Drift）导致风格漂移。
*   **欠工程（缺乏 Safeguard）：** LLM 可能会推荐流动性差或即将退市的标的。
*   **Actionable 修改：** 在 LLM 提议前，增加**硬编码的量化过滤器（Hard Screener）**：市值 > $20B、Option Volume > 5000/日、非生科/区域银行。LLM 只能在这个白名单池内做 NLP 偏好匹配。CLI 审批流程保留，这是极好的设计。

### 4. Forward Test 盲点 (§5 R6)
*   **盲点：** 10 周的 Forward test 根本跑不完 v1.3 的完整生命周期（尤其是深度套牢后的 CC 修复期）。
*   **Actionable 修改：**
    1.  Forward test 延长至 **12 周**。
    2.  **强制追加 Stress Backtest：** 截取 2022 年 1月-6月（单边下跌市），用代码回测 Tier A 逻辑，向用户展示“在不干预的情况下，最大 Drawdown 是多少，资金利用率会降到多低”，让用户对“死扛”有真实的预期，再决定是否保留 0.85 Delta 的安全网。

**总结：** 用户的意愿可以尊重，但作为系统工程师，你必须在底层加上硬性约束（5% Cash、硬编码过滤池、死轮逃生舱）。请将上述修改合入 v1.3。

---

## Three-Model Consensus

**Final consensus**

### 1) 强一致观点
- **§3.1 不能 0% cash**：三方都认为真钱 $500K 下必须留真实现金，应对 assignment/settlement mismatch、费用、broker house rule 变化。  
- **§3.2 Tier A 当前过激进**：`put delta -0.28 + DTE 30 + hold to expiry` 会把 A 变成 **long stock + CC inventory**，不再是纯 premium harvesting。  
- **§3.3 仅靠 `delta≥0.85 / 3x credit` 的 emergency rule 太晚**：需补充 gap、事件、流动性、财报前禁开等 manual review/circuit breaker。  
- **§3.5 LLM 只能提议，不能自动决策**：必须 deterministic hard filter + 显式用户审批。  
- **§5 10 周 forward test 不够**：至少延长，覆盖 A 的 assignment→CC 闭环。

### 2) 分歧与解决
- **现金比例**：GPT 主张 `5% hard cash + 3% buffer`，Gemini 主张 `5% + 5%`。  
  **折中建议**：先落地 **5% hard cash + 5% buffer**；实盘 8 周后若无流动性问题，再评估降到 8%。  
- **Tier A call/IV/earnings 阈值**：GPT 更保守；Gemini重点补“死轮逃生舱”。  
  **折中建议**：保留 classic wheel 主哲学，但增加 **sub-basis CC 仅限极端场景且需人工批准**，平时仍 `K ≥ cost_basis`。  
- **forward test 长度**：GPT 16–24 周，Gemini 12 周。  
  **折中建议**：**16 周**，其中前 **4 周 shadow-only**。

### 3) Kickoff 前必改清单
1. **§3.1 资金分配**：`0/49/49/2` → **`Cash 5%($25K) / Buffer 5%($25K) / Tier A 45%($225K) / Tier B 45%($225K)`**。  
2. **§3.1 新增风控**：增加 **sector cap 20%**、**single earnings-week gross exposure cap 15%**、**single ticker max 5% notional ($25K)**。  
3. **§3.2 Tier A put delta**：`-0.28（-0.22~-0.32）` → **`-0.24（-0.22~-0.26）`**。  
4. **§3.2 Tier A call delta**：`+0.22` → **`+0.18~+0.20`**。  
5. **§3.2 Tier A IV rank**：`20-75` → **`30-65`**；**earnings 屏蔽**：`±5日` → **`±10日`**；**VIX freeze**：`>30` → **`>27`**。  
6. **§3.3 emergency rules**：新增 **单日 gap >12% / 5日RV > 2×60日中位数 / DOJ-FDA-SEC重大事件 / NBBO spread > premium 5% ⇒ MANUAL_REVIEW & 禁自动下单**。  
7. **§3.3 死轮逃生舱**：新增 **若 cost-basis CC 年化收益 <0.2%，允许 sub-basis CC（delta≤0.15）+ protective call collar，且必须人工批准**。  
8. **§3.5 LLM curation**：新增 **硬筛选**：`市值>$20B、期权日成交量>5000、排除 biotech/区域银行/IPO<18月/M&A pending`；审批改为 **per-symbol approve**；新纳入 ticker **冷却 7 个交易日**。  
9. **§5 测试**：`10周` → **`16周（前4周 shadow-only）`**，并追加 **2022H1 stress backtest**。

---

*Council review 由 [`~/.claude/skills/council-review.md`](file:///Users/tianyuwang/.claude/skills/council-review.md) 触发执行于 2026-05-19 21:20:06。模型：`openai/gpt-5.4` + `google/gemini-3.1-pro-preview`，consensus 由 `openai/gpt-5.4` 综合。审计 JSON：`/tmp/council_review_audit_20260519_211927.json`。*
