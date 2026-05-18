# Wheels Copilot — Research Report

**作者：** Claude (基于 web research + options_copilot 架构分析)
**日期：** 2026-05-17
**目的：** 为 wheels_copilot 项目的 design / plan 阶段提供研究基础

> 这是一份 research 报告，**不是 implementation plan**。
> 报告完成后，下一步会基于本文档 + 进一步讨论，输出 wheels_copilot 的 architecture plan。

---

## 0. TL;DR — 给 5 分钟读者

1. **Wheel strategy 本质是一台"长期 covertly long delta + 持续卖 vol"的引擎**。它的成败 80% 取决于 *标的筛选* 和 *仓位管理*，而不是行权价/到期日的微调。自动化要把 80% 的精力放在前者。

2. **市场上现成的 wheel 自动化项目主要 4 个**：
   - **ThetaGang** (IBKR / Python) — 最成熟、社区最活跃，已演变成"组合管理框架"
   - **alpacahq/options-wheel** (Alpaca 官方 template) — 简洁、有 paper test 数据，但缺乏 rolling 和高级管理
   - **wheel-it** (Alpaca / Python) — 更强的 screener，但还在早期
   - **AllYouNeedIsWheel** (IBKR + Flask UI) — 半自动 dashboard，需要人工 approve

   它们都没有解决的核心问题：**"标的发现 + 何时该退出/不开仓"**。这是 wheels_copilot 真正的差异化机会。

3. **Wheel 不是稳赚的**。多个严肃 backtest 显示：在 SPY/QQQ 等指数上长期跑 wheel 跑不赢 buy & hold，且暴露 path-dependent risk（assigned 后 delta 暴露被动放大）。**任何严肃的自动化必须把"何时该不做"和"如何救援被深度套牢的仓位"作为一等公民**。

4. **options_copilot 已建立的能力可以直接复用 ~60%**：APScheduler daemon、SQLite OMS、Alpaca adapter、LLM orchestration、workspace audit、daily report。需要新增的核心是：**Wheel 专用 state machine、ticker watchlist 维护、assignment lifecycle、rolling 决策器、CSP/CC 协同的 risk budget**。

5. **推荐架构（要点）：**
   - **State machine 驱动**（CASH → CSP_OPEN → ASSIGNED → CC_OPEN → 回到 CASH）—— 每个 ticker 都是独立的 state
   - **Broker is source of truth**（positions/fills 以 broker 为准），DB 是决策审计层
   - **决策分两层**：deterministic rules（行权价、DTE、roll 阈值、stop loss）打底；LLM 仅在 *标的质量评估* 和 *异常救援* 层介入
   - **风险预算自顶向下**：account-level cash reservation → per-ticker max exposure → per-trade sizing
   - **建议先在 paper 上做 8-12 周的 forward test**，不要相信 in-sample backtest

---

## 1. Wheel Strategy 101（聚焦自动化相关要素）

### 1.1 经典 4 状态循环

```
┌──────────────┐   sell CSP    ┌───────────────┐
│   CASH (S0)  │ ─────────────▶│  CSP_OPEN(S1) │
└──────────────┘               └───────┬───────┘
       ▲                               │
       │ called away                   │ expire OTM
       │                               │ (back to S0, keep premium)
       │ sell CC                       │
       │                       ┌───────▼───────┐
       │                       │ ASSIGNED(S2)  │
       │                       └───────┬───────┘
       │   sell CC                     │ (now own 100×N shares
       │                               │  at strike − premium)
┌──────┴───────┐               ┌───────▼───────┐
│  CC_OPEN(S3) │ ◀─── sell CC ─│   ASSIGNED    │
└──────────────┘               └───────────────┘
```

**自动化关键洞察：**
- 这 4 个状态对自动化系统是 *本质的*——不应该被抽象掉。所有决策（开仓、roll、退出）都依附于状态。
- 状态转换的 **触发器** 完全可以观测：option expiry、assignment notification、fill。所以 state machine 应该是 *broker-event-driven*，而不是 schedule-driven。
- 一个账户里可以同时持有 N 个 ticker 的 wheel，每个 ticker 是独立的 state machine。这意味着 **每日 cycle 是 N 个独立状态机的扫描**。

### 1.2 核心参数（自动化要 expose 为 config）

| 参数 | 典型范围 | 含义 | Sensitivity |
|------|---------|------|-------------|
| **Put delta** | 0.15 – 0.35 | CSP 卖出时 short put 的 delta | 高 — 决定 win rate / 收益 trade-off |
| **Call delta** | 0.20 – 0.35 | CC 卖出时 short call 的 delta | 中 |
| **DTE** | 7 – 45 days | 到期日距离 | 高 — theta vs gamma 平衡 |
| **Profit take** | 50% of max | 提前 close 阈值 | tastytrade 200K+ trade 研究：50% TP 在 21 DTE 前关闭提升 15-20% 的 risk-adjusted return |
| **Time stop** | 21 DTE | 强制评估/平仓时间 | 高 — gamma risk 在最后 3 周加速 |
| **Stop loss** | 2× credit / 30% under strike | 何时认输 | 关键的"hidden risk control"，绝大多数 retail trader 没设 |
| **Roll trigger** | strike breached / delta > 0.50 | 何时滚动 | 影响 path dependency |
| **IV rank** | 30 – 60 入场 | 卖 vol 的"性价比" | 中高 — vol 太低不值得卖 |

### 1.3 一些常被低估的细节

1. **Strike floor on CC**：CC 的行权价必须 ≥ cost basis（assignment 价 − 已收 premium）。否则被 called away 会锁定亏损。这是初学者最常犯的错误，必须在 code 层强制。

2. **Assignment timing**：美股 equity option 是 American style，可以在到期前任何时间被 exercise（虽然实际上 >95% 的 assignment 发生在 expiry 当天 close 时）。所以：
   - 不能假设"到期前我都可以 roll"
   - 早期 assignment 在 deep ITM 时（特别是 dividend 前一天）真实发生

3. **Dividend risk**：股票分红前一日，deep ITM 的 short call 经常被早期 assigned（exerciser 想抢分红）。所以：
   - 在分红前需要检查 CC 状态
   - 也可能拿到 dividend，要在 P&L 里追踪

4. **Pin risk / settlement**：到期日股价非常接近 strike 时，是否被 assigned 不确定，可能导致周末/隔夜的"裸"股票或股票方向暴露。

5. **Wash sale rule (US taxable accounts)**：30 天内同一标的的损失，无法立即抵税——延迟到新仓位的 cost basis。Wheel 的高频换手 + 同标的重复交易 = wash sale 的高发地。**IRA 账户没有这个问题**。

---

## 2. Wheel Strategy 的成功条件与失败模式

### 2.1 真正的"成功经验"

社区/文献综合下来的高一致性建议：

1. **只在你 *愿意持有 2 年以上* 的股票上跑 wheel。** 这是所有教科书和论坛反复说的第一条。"被 assigned 不可怕，可怕的是 assigned 后你不想拿。"
2. **30-45 DTE，0.20-0.30 delta** 是大多数赢家的默认起点。这给出大约 70-80% 的 OTM expiry 概率和合理的 premium。
3. **50% profit + 21 DTE 双 trigger 提前关闭**（tastytrade 在 200K+ trades 上验证）：把多余的 gamma 风险还给市场，重新部署资本。
4. **分散到 8-10 个标的**：单一标的跑 wheel = 单一股票暴露，黑天鹅一来全完。
5. **避开高 IV rank（>70）的标的**：高 IV 通常意味着 *正在发生坏事*（财报、FDA、官司）—— 你以为是 free premium，实际上是 risk premium 的合理定价。
6. **Earnings filter 必须有**：除非你专门交易 earnings vol，否则在 earnings ±7 天不开 CSP。

### 2.2 大家是怎么爆仓的（自动化要防的真实失败）

整理自 r/thetagang、HN、博客和 backtest 文献：

| 失败模式 | 触发 | 自动化对策 |
|----------|------|-----------|
| **Bag holder spiral** | 在 hype 股（meme, biotech）卖 CSP，被 assigned 后股价继续跌 50%+，cost basis 远高于市场价，CC 永远卖不到 cost basis 之上 | (a) Ticker whitelist 限制在大盘+蓝筹；(b) IV rank > 70 拒绝开仓；(c) Assignment 后的 max drawdown 触发 *escalation*（卖更激进的 CC？买保护？强制止损？） |
| **Selling cheap vol** | IV rank 太低，premium 不值得 | IV rank < 30 拒绝开仓（或要求更高 delta） |
| **Earnings 翻车** | CSP 跨越 earnings，股价大跳空 | Earnings calendar 集成（Finnhub/Polygon），earnings ±7 天 freeze |
| **Macro 黑天鹅** | FOMC/CPI/战争事件 → 整组 wheel 同向亏损 | Macro calendar + VIX 阈值；vix > X 时暂停开新仓 |
| **过度集中** | 同一行业（如 7 个 mega-cap tech）一起爆仓 | Sector concentration limit |
| **CC 卖低于 cost basis** | 急着收 premium，strike 设到 cost basis 下方 | **硬约束**：reject any CC where strike < cost_basis |
| **Roll forever** | 一直 roll 永远不止损，最后变巨大 deep ITM 仓位 | **Roll 计数器**：max 2 次 roll，第三次强制 take assignment 或 close |
| **Leverage 放大** | 用 margin 卖 naked put，跌的时候被 forced liquidate | wheels_copilot 必须以 cash-secured 为默认；margin 模式需要单独 flag + 风控 |
| **Path dependency** | Assigned 后 effective delta 从 0.2 突变成 1.0，组合 delta 失控 | Portfolio-level delta tracking + 强制 rebalance 阈值 |

### 2.3 严肃 backtest 的"扫兴"结论

我找到的最严肃的 backtest 包括：

- **Spintwig 的 SPY 45-DTE wheel backtest**（多个变体）
- **Early Retirement Now part 12**（critical analysis）
- **QuantConnect 上的多个 wheel implementation**

综合结论：

1. **在 SPY 这类指数上长期跑 wheel，跑不赢 buy-and-hold**。一项对比 10 种 wheel 配置的研究发现：**没有一种配置 outperform 单纯持有 SPY 的 total return**，且 4/10 的配置长期是负的。
2. **绝大部分"alpha"来自 long stock leg 本身**，"short option" 部分的贡献很小甚至为负。换句话说：**wheel 的"income"是 long delta 风险的对价**，不是免费午餐。
3. **Path-dependent risk**：Early Retirement Now 的核心论点—— wheel 在亏损时被迫提高 delta 暴露（被 assigned 后变成 100 delta long），这违反"小 delta 收 income"的初心。
4. **指数 wheel 不存在结构性 edge**；个股 wheel 的 edge 来自 **stock selection + vol sell** 的组合，前者贡献更大。

**对 wheels_copilot 的启示：**

- 不要追求 "beat SPY" 作为目标（多数情况下做不到）。
- 真实目标应该是 **"在可控 drawdown 下生成稳定 cash flow，配合长期持有"**。
- 关键的差异化不是参数调优，而是 **(a) 标的选择质量** 和 **(b) 何时不交易**。

---

## 3. 市场扫描：现有的自动化 Wheel 项目

详细分析了 4 个主要的开源/商业项目，外加一些次要的。

### 3.1 ThetaGang（IBKR / Python）—— 行业事实标准

**仓库：** [github.com/brndnmtthws/thetagang](https://github.com/brndnmtthws/thetagang)
**Stack：** Python 3.10+，`ib_async`，IBC，SQLite，TOML config
**部署：** 一次性脚本 + cron + Docker

**架构特点：**
- **One-shot 调用模式**：不是 daemon，而是 `thetagang --config thetagang.toml` 每次跑一次。运维上靠 cron 触发。
- **TOML 配置驱动**：每个标的配置 `weight`（组合权重）、`delta`（行权价 target）、`primary_exchange`，可以做整个组合的 wheel + rebalance。
- **从 wheel 进化成完整组合工具**：现在支持 VIX call hedge、cash management（自动 sweep 到 SGOV）、regime-aware rebalancing。

**Wheel 实现的关键点：**
- 卖 CSP 直到被 assigned；
- Assigned 后自动卖 CC，**strike ≥ average cost** 强制；
- 持续 roll，除非 ITM puts 进入"等待 expire/assignment" 模式；
- Deep ITM call 优先 roll 而非 called away（避免税务影响）；
- **Premium 受限的 roll cap**：新仓位的 strike 最高 = 老 strike + premium received（防止"无脑 ratchet"）；
- High-water mark 选项防止 CC 被 roll 到更低 strike。

**有用的特性：**
- VIX hedge：可以配置一笔小比例的 VIX call 当 tail risk hedge（参考 VXTH index）；
- Cash management：账户里多余的现金自动买 SGOV 等短债 ETF；
- Dry-run 模式：`--dry-run` 给出建议但不下单——**这是任何 wheel 自动化的必备**。

**已知缺陷：**
- 对 implied vol > realized vol 这个 *假设* 完全裸露（README 自己承认）；
- 一个 IBKR 账户一个策略，不能跑多策略；
- 需要 IBKR 的 market data 订阅；
- TWS/Gateway 需要保活（IBC 帮你管，但还是一个挑战）。

**对 wheels_copilot 的启示：**
- **TOML/YAML config 驱动 + per-ticker 权重** 的设计很好抄。
- **"premium-capped roll" 是个聪明的设计**——防止机械 roll 把仓位越搞越糟。
- 它的 Cron-based one-shot 模式很简单，但 options_copilot 的 daemon 模式更灵活（可以做 intraday monitoring）；建议保留 daemon 设计。

### 3.2 alpacahq/options-wheel —— Alpaca 官方 template

**仓库：** [github.com/alpacahq/options-wheel](https://github.com/alpacahq/options-wheel)
**Stack：** Python，Alpaca-py SDK
**部署：** CLI（`run-strategy`）+ cron

**算法（直接抄自 README）：**
1. 检查当前 positions，识别 assignment；
2. 在 assigned stock 上卖 CC；
3. 按 buying power 过滤可交易的 underlying；
4. 给候选 puts 打分排序；
5. 执行 top-ranked trades。

**Strike/DTE 选择：** 通过 `config/params.py`，参数包括 `DELTA_MIN/MAX`、`OPEN_INTEREST_MIN`、`YIELD_MIN/MAX`、`SCORE_MIN`。

**Symbol 选择：** 静态 watchlist (`config/symbol_list.txt`)。

**已知缺陷：**
- **没有 rolling 自动化**——README 自己列在"Ideas for customization"。
- **每个 symbol 只交易 1 张合约**：简化但不可 scale。
- **没有 IV rank、earnings、technicals 过滤**。
- 只有 2 周的 paper test 数据（2025-05-14 至 2025-05-28：$100K → $100,951，+0.95% over 14 days）—— 完全不足以下任何结论。

**对 wheels_copilot 的启示：**
- Alpaca 在 options 上的 SDK 路径是 *公开验证可用的*——和 options_copilot 一致。
- 但官方 template 暴露了 *最小可用面积*——离生产差很远，特别是 rolling 和 management。
- **"One contract per symbol"** 是一个简化假设，wheels_copilot 应该从一开始就支持多张 + 不同 expiry 同时在线。

### 3.3 wheel-it —— Alpaca + 强 screener

**仓库：** [github.com/vahagn-madatyan/wheel-it](https://github.com/vahagn-madatyan/wheel-it)
**Stack：** Python + TypeScript（CLI/TUI），Alpaca + Finnhub
**部署：** CLI (`wheelit`) + cron + interactive TUI

**亮点：**
- **更强的 ticker screener**：4 阶段 pipeline（cheap-first）：
  1. Technicals (Alpaca)：价格区间、volume、RSI、SMA200、HV percentile
  2. Earnings (Finnhub)：剔除 upcoming earnings（7-21 天阈值随 preset 变）
  3. Fundamentals (Finnhub)：market cap、debt/equity、net margin、sales growth、sector
  4. Options chain (Alpaca)：OI、bid/ask spread
- **3 个 preset**（conservative / moderate / aggressive）作为 starting point
- **Sector exclusion**：conservative preset 排除 biotech/cannabis/O&G
- **Annualized return 用 time value（extrinsic）only**，不让 ITM 合约虚报 ranking——*很聪明*。

**已知缺陷：**
- 还没有 rolling 自动化；
- 没有 backtesting 框架；
- 文档少；
- One contract per symbol 限制依然在。

**对 wheels_copilot 的启示：**
- **4 阶段 screening pipeline 是很好的设计**——按"先便宜后贵"的 cost order 排查标的。
- **用 *time value only* 来打分**，避免 ITM 合约虚报 yield——这个细节值得抄。
- Earnings filter + sector exclusion 是必备项。

### 3.4 AllYouNeedIsWheel —— 半自动 IBKR dashboard

**仓库：** [github.com/xiao81/AllYouNeedIsWheel](https://github.com/xiao81/AllYouNeedIsWheel)
**Stack：** Python (Flask) + JS frontend + SQLite + IBKR (TWS API)
**部署：** Web app (localhost:8000)，需要 TWS/IB Gateway 在线

**特点：**
- **不是全自动**——是 recommendation dashboard，需要人 click execute；
- 有专门的 "Rollover" UI 处理接近 strike 的仓位；
- API 设计：`GET /api/options/<ticker>/<expiration>` 拉 chain，`POST /api/options/execute/<order_id>` 触发下单。

**对 wheels_copilot 的启示：**
- 它代表了 **"human-in-the-loop"** 模式——更保守、更易于建立信任，但失去了 daily auto 的优点。
- options_copilot 已经是 fully-auto + audit-trail 模式；wheels_copilot 应该延续，但保留 *kill-switch* 和 *dry-run* 模式作为后备。
- **专门的 rollover view 是好主意**——roll 是 wheel 的最复杂决策，值得专门的 UI/log surface。

### 3.5 商业产品：PeakBot, QuantConnect 模板等

- **PeakBot**：托管的 wheel bot 服务（订阅制），主要卖给"不想自己写代码"的人。功能上没有公开技术细节，但定位说明市场存在。
- **TradersPost / 各种 Discord bot**：处理 webhook → broker 的"通用自动化"，不专门做 wheel 逻辑。
- **MarketXLS / Excel-based**：依然有用户群，说明 *简单 + 透明 + 可审计* 的价值。
- **QuantConnect** 上有多个 wheel template，主要面向 backtesting，不是实盘部署。

**市场缺口：**

- 没有一个开源项目把 **"AI/LLM 介入标的发现 + 救援决策"** 做出来。
- 没有一个项目把 **"完整 audit trail + 每日报告 + workspace JSON"** 这套 production-grade observability 做好——这是 options_copilot 已经验证的方向。
- 没有项目认真做 **"assigned 后的救援逻辑"**——这恰恰是 wheel 最常失败的地方。

**这就是 wheels_copilot 的差异化机会。**

---

## 4. Broker / 数据基础设施对比

| 维度 | Alpaca | IBKR | Tradier |
|------|--------|------|---------|
| **API 质量** | REST + Python SDK, 现代 | TWS API 复杂，但 `ib_async` 缓解 | REST, 简洁 |
| **Options 支持** | 较新，覆盖 single + MLEG | 全面，含组合保证金 | 全面（option 是它的强项） |
| **Paper trading** | ✅ 一键切换 | ✅（paper account） | ✅（sandbox） |
| **数据成本** | 免费 + SIP 升级可选 | 需要订阅 market data add-on | $10/月含 chain + Greeks |
| **Commission** | $0 + $0/contract（部分）/ $0.65 | 分层级，options $0.65 | $0.35/contract |
| **Portfolio margin** | 没有 | ✅ 有 PM | 部分 |
| **Cash-settled index (SPX)** | ❌ | ✅ | ✅ |
| **Pre-market / 24h trading** | 有限 | ✅ | 标准时间 |
| **Auth 复杂度** | API key + secret | TWS/Gateway 必须保活 + IBC | API key |
| **已知缺陷** | options 数据偶尔 flaky；options_copilot 已遇到过 | TWS 断线 / MFA / market data 订阅；并发 session 限制 | 没有 IRA 账户，主要 taxable |

**评估（基于 options_copilot 已采用 Alpaca）：**

- **保持 Alpaca 作为默认 broker** —— options_copilot 的 alpaca adapter 已经成熟。
- **不要为了"capital efficiency"切到 IBKR + portfolio margin** —— 显著增加复杂度，对 wheel 这个本质要求 cash-secured 的策略收益有限。
- **Tradier 是未来可选**（如果发现 Alpaca 的 options 数据质量不够）——它的 $10/月套餐 + 全 Greeks 在 chain 里是个甜点。
- **不需要 SPX/index options**——wheel 必须用 equity option（要 share assignment）。

---

## 5. 自动化决策表面（Decision Surface）

把 wheel 的每个决策点拆开，看哪些应该 deterministic / mechanical，哪些 LLM 可能加价值。

| 决策 | 性质 | Mechanical | LLM 介入价值 |
|------|------|-----------|------------|
| **Watchlist 维护**（哪些 ticker 适合 wheel） | 周期性更新 | 基础 screener（market cap, IV rank, earnings calendar, liquidity） | ✅ 高 — 评估 "我愿意持有这个公司吗" 的定性判断 |
| **当日要不要开新仓** | 每日 | VIX 阈值、macro calendar 黑名单 | ✅ 中 — 解读当前市场环境，是否 risk-off |
| **行权价选择** | 入场时 | 按 target delta 精确选 | ❌ deterministic 即可 |
| **DTE 选择** | 入场时 | 按 config range 选最近合适的 expiry | ❌ |
| **仓位大小** | 入场时 | risk budget × confidence | 低 — confidence 可以来自 LLM evaluation |
| **是否提前 close**（hit 50% TP） | 每日扫描 | hard rule | ❌ |
| **是否到 21 DTE 强制评估** | 每日 | hard rule | ❌ |
| **stop loss 触发** | 每日 / intraday | hard rule（如 short put delta > 0.65） | ❌ |
| **要 roll 还是 take assignment** | 触发时 | 默认规则 + roll counter | ✅ 高 — 当前 thesis 是否还成立？标的基本面是否变化？ |
| **Assigned 后选择 CC 行权价/DTE** | 触发时 | 必须 ≥ cost basis + delta 0.20-0.30 | 中 — 是否要"激进"（短 DTE / 高 delta）还是"温和"（远 DTE / 低 delta） |
| **Bag-holder 救援** | 触发时（如 -20% 已 assigned） | escalation playbook | ✅ 高 — 是 hold/CC roll down/take loss/添加保护？ |
| **退出策略** | 月度 / 季度评估 | 单标的 max hold time | ✅ 中 |

**结论：**

- **绝大多数日常决策可以 deterministic**（行权价、DTE、profit take、time stop、stop loss）。这与 options_copilot 的"validator-first，LLM proposes，code enforces" 哲学一致。
- **LLM 的真正价值在 *标的层面的定性判断***（公司质量、当下市场环境、救援决策）—— 频率低、信息密集、deterministic rule 写不好。
- 不建议让 LLM 决定 strike/DTE 的精确数字——这是 options_copilot 反复验证过的：**LLM 在数值边界上不可靠**。

---

## 6. options_copilot 中可复用的能力

详尽审计了 options_copilot 的代码结构，明确哪些可以直接 reuse，哪些需要扩展，哪些需要新建。

### 6.1 直接复用（建议提到共享层）

| 模块 | 路径 | 用途 |
|------|------|------|
| Alpaca client | `adapters/alpaca_client.py` | 拉 chain、quotes、submit orders（支持 MLEG） |
| OpenRouter client | `adapters/openrouter.py` | 多模型 LLM 编排 + JSON parse |
| FRED / Finnhub / Unusual Whales / EDGAR adapters | `adapters/` | 宏观 / earnings / IV / insider / flow 数据 |
| DB schema + migration 模式 | `db/database.py` | SQLite + WAL + 版本化 schema |
| OMS state machine | `engines/oms.py` | 订单生命周期（PENDING → FILLED 等） |
| Alpaca health monitor | `engines/alpaca_health.py` | 降级模式 / 重试 |
| Market clock | `engines/market_clock.py` | 开盘、半日、节假日 |
| Technicals (TA-Lib) | `engines/technicals.py` | RSI/SMA/ATR/Bollinger |
| Vol features | `engines/vol_features.py` | IV rank、VRP、term、skew |
| Daily report (SES email) | `skills/daily-report/` | 收件箱版日报 |
| Workspace audit pattern | `workspace/YYYY-MM-DD/...` | LLM prompt / response / 决策落盘 |

**建议：** 提取出一个 `shared/` 或 `common/` 包，让 options_copilot 和 wheels_copilot 都依赖。短期可以 vendor 一份，长期合并。

### 6.2 需要扩展 / 改造的

| 模块 | 改造点 |
|------|--------|
| `engines/exit_plan.py` | 现在按 credit/debit strategy 分流；wheel 需要"per-ticker per-state"的 exit plan，而且 CSP 和 CC 的 exit plan 是 *联动的*（被 assigned 后 CSP exit plan 进入 CC entry plan） |
| `schemas/strategies.py` | 现在的 strategy_template 是 enum；wheel 需要更丰富的 lifecycle 状态（不只是"open / close"） |
| `engines/economics_gate.py` | CSP 和 CC 的 gate 已有；wheel 需要新增 "is this ticker still wheel-worthy" 这一更高层的 gate |
| `skills/ticker-discovery/` | 现在是"今日最佳交易标的"；wheel 需要 *持久化的 watchlist*，每周 / 每月演化 |
| `skills/position-adjust/` | 加入 roll 决策器 + bag-holder 救援逻辑 |

### 6.3 全新需要的

| 模块 | 用途 |
|------|------|
| **WheelStateMachine** | 每个 ticker 一个独立状态机（CASH / CSP_OPEN / ASSIGNED / CC_OPEN）+ 转换规则 |
| **WatchlistManager** | 维护"哪些 ticker 在跑 wheel"的列表，含 add/remove/freeze 操作 |
| **AssignmentLifecycle** | 处理 assigned 事件：更新 state、计算 cost basis、reserve 现金、触发 CC 选项 |
| **RollDecisionEngine** | 给定一个 CSP/CC 仓位 + 当前市场，决定 roll out / roll down / take assignment / close |
| **RescueEngine** | Bag-holder 场景的 escalation playbook（专门的 skill） |
| **PortfolioRiskTracker** | 整个组合的 net delta、净敞口、行业集中度（wheel 特别需要） |

---

## 7. 推荐的自动化架构（草图，留给 plan 阶段细化）

### 7.1 三层架构

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: STRATEGY ORCHESTRATION (skills/agents)            │
│  - watchlist-curate   - ticker-evaluate                     │
│  - daily-wheel-cycle  - roll-decide                         │
│  - bag-rescue         - daily-report                        │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│  Layer 2: WHEEL DOMAIN ENGINE                               │
│  - WheelStateMachine     (per-ticker FSM)                   │
│  - RollDecisionEngine                                       │
│  - WheelRiskBudget       (account → ticker → trade)         │
│  - AssignmentLifecycle                                      │
│  - Validators / Gates    (CSP delta, CC strike floor, etc.) │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│  Layer 1: SHARED INFRASTRUCTURE (reused from options_copilot)│
│  - alpaca_client   - openrouter   - db/oms                  │
│  - finnhub/fred/uw - workspace    - daily-report            │
└─────────────────────────────────────────────────────────────┘
```

### 7.2 每日 cycle 的伪代码

```python
def daily_wheel_cycle(today):
    # 0. PRE-FLIGHT
    reconcile_with_broker()              # broker = source of truth
    update_state_machines()              # 处理 overnight assignments / fills
    
    # 1. 全局风控检查
    if market_check.is_risk_off():       # VIX > 30, macro 事件等
        log("Risk-off day, no new entries"); return
    
    # 2. 扫描每个 ticker 的 state machine
    for ticker_state in active_wheels:
        match ticker_state.state:
            case CASH:
                if should_enter(ticker_state):
                    propose_csp(ticker_state)
            case CSP_OPEN:
                check_exit_or_roll(ticker_state)   # 50% TP, 21 DTE, stop, roll
            case ASSIGNED:
                propose_cc(ticker_state)
                if is_deep_underwater(ticker_state):
                    trigger_rescue_skill(ticker_state)
            case CC_OPEN:
                check_exit_or_roll(ticker_state)
    
    # 3. Watchlist 更新（频率 = 每周/每月，不是每日）
    if is_watchlist_refresh_day(today):
        run_watchlist_curate_skill()
    
    # 4. 报告
    generate_daily_report()
```

### 7.3 关键设计原则

1. **Broker is source of truth for positions**，DB 是"审计 + 决策上下文"。每个 cycle 开头先 reconcile，发现 broker 多/少的仓位都要 alert。

2. **State machine 是一等公民**。每个 ticker 一份记录，状态切换 *写日志、写 audit*。

3. **Watchlist != tradable today**。watchlist 是"我愿意在这个 ticker 上跑 wheel 的清单"，可以一周更新一次；每日决定 *从 watchlist 里挑哪些今天开仓*。

4. **每个仓位的 exit plan 在开仓时就 frozen**（继承 options_copilot 的 effective_exit_plan 模式）。运行时 mechanical 执行，不再让 LLM 介入数值层面。

5. **Risk budget 自顶向下：**
   - Account level：max cash reserved for CSP（如 60% of cash）
   - Ticker level：per-ticker max 5-10% of account
   - Trade level：每笔 max loss（基于股票从 strike 跌 30% 的假设）

6. **三个 "kill switches"：**
   - 单日 P&L < -X% → 暂停新仓
   - 单 ticker 浮亏 > Y% → 触发 rescue skill
   - 系统检测到 broker 断线 / 数据 stale → freeze 所有新仓决策

7. **Dry-run mode 必须从 day 1 就有**——交易任何一行 production code 之前，先用 dry-run 跑 4-8 周。

---

## 8. 风险控制与开盘前的"不要"清单

这是 wheels_copilot 的"事故清单"——预先编码的"什么情况下不能做什么"：

### 入场（开 CSP）的硬约束

- ❌ 标的不在 watchlist
- ❌ 标的有 earnings 在 ±7 天内
- ❌ FOMC/CPI/NFP 在 ±2 天内 + ticker 是 macro sensitive
- ❌ VIX > 30 且 ticker 不在 "safe core"（如 SPY/QQQ/AAPL/MSFT）
- ❌ Ticker IV rank > 70（异常事件信号）
- ❌ Ticker IV rank < 20（不值得卖 vol）
- ❌ 行权价不在 delta 0.15–0.35 区间
- ❌ DTE 不在 7–45 区间
- ❌ Bid-ask spread > 5% of mid
- ❌ Open interest < 100
- ❌ 此 ticker 已有未平 wheel 仓位（per-ticker 单一性）
- ❌ Account-level CSP cash reservation 已达上限
- ❌ Sector concentration 已达上限
- ❌ 单日已开仓 N 次（频次上限）

### CSP 管理的硬规则

- ✅ Hit 50% profit → close（除非 < 7 DTE）
- ✅ 21 DTE → 强制评估（roll out / close / take assignment）
- ✅ Short put |delta| > 0.65 → trigger roll-or-assign decision
- ✅ Underlying 跌破 strike × (1 - 5%) → 上报为 stress
- ❌ Roll count > 2 → 不允许第三次 roll（必须 close 或 take assignment）

### Assignment 后的硬规则

- ✅ 立即计算 effective cost basis（strike − total premium received）
- ✅ Reserve cash 已转为 shares（更新 portfolio risk）
- ✅ CC 行权价必须 ≥ cost basis（**绝不卖锁定亏损的 CC**）
- ✅ CC delta 0.20–0.35，DTE 30–45
- ❌ 如果当前股价 < cost basis × 0.85（浮亏 15%+）→ trigger rescue skill 评估
- ❌ 如果当前股价 < cost basis × 0.70（浮亏 30%+）→ 强制人工 review，暂停自动 CC

### CC 管理

- ✅ Hit 50% profit → close
- ✅ Hit 21 DTE → roll out or close
- ✅ Short call |delta| > 0.65 → roll out / let called away
- ❌ 不允许 roll CC 到 strike < cost basis 的合约

### Portfolio-level "circuit breakers"

- ⚠️ 单日 realized + unrealized P&L < -2% of equity → freeze new entries 24h
- ⚠️ 周累计亏损 > 5% → freeze new entries until manual reset
- ⚠️ 任一 ticker 浮亏 > 20% → trigger rescue evaluation
- ⚠️ Broker API 错误率 > 10% in last 10 min → 进入 read-only mode

---

## 9. 几个值得深思的设计问题

以下问题在 plan 阶段需要明确（**不需要现在回答**）：

### 9.1 单账户 vs 多账户

- options_copilot 已在一个 Alpaca paper account 上跑；wheels_copilot 是否复用同一账户？
- ThetaGang 强调 "one strategy per account"。如果共用账户，wheels 的 CSP cash reservation 会和 options_copilot 的 cash buying power 冲突。
- 建议：**用独立账户**（或至少 paper 阶段用独立 paper account）以隔离风险。

### 9.2 IRA vs taxable

- Wheel 的"短期资本利得 + wash sale"问题在 taxable account 会显著吃掉收益。
- 如果 wheels_copilot 长期是要在你 *自己的真钱* 上跑，强烈建议考虑 IRA（如果资格允许）。
- 不过 Alpaca 不支持 IRA（截至 2025）。这是一个未来 broker 选型的潜在压力。

### 9.3 是只跑大盘股 wheel，还是允许"defensive value" wheel？

- 最保守的 wheel：SPY/QQQ/IWM + 5-8 个 mega-cap（AAPL/MSFT/GOOGL/AMZN/NVDA/META...）。
- 更激进：单独行业（半导体、生物、能源）的中型股 wheel——premium 高，但 path-dependent risk 高。
- 建议：**先做 conservative core，验证 12 周后再考虑扩展**。

### 9.4 LLM 介入的频率和分工

- options_copilot 用 5 模型 council 在 *策略选择层面*——wheels_copilot 不需要这种结构，因为策略已经定死（就是 wheel）。
- 但 LLM 可以在 *标的层面定性评估*、*异常救援决策*、*市场环境解读* 介入。
- 建议：**LLM 只在 weekly/event-driven 的低频高价值决策上用**（如 watchlist 更新、rescue decision），日常 cycle 完全 deterministic。
- 这会显著降低 LLM 成本和系统复杂度。

### 9.5 多少历史数据才够做"forward test"？

- 文献的 backtest 不一致（部分 outperform SPY，部分 negative），核心原因是 *path dependency*——你怎么开始决定了你怎么结束。
- 我的建议：**不要相信 backtest，做 12-16 周的 paper forward test**，看：
  - 平均 monthly yield
  - Max ticker drawdown
  - Assignment rate
  - 平均 "wheel cycle time"（CASH → CASH 的周期）
  - 系统稳定性 / 错误率

### 9.6 怎么处理 long-term holding（"我反正愿意持有"）

- Wheel 哲学说"只在你愿意持有的股票上做"，但实际跑起来，wheel 会因为 CC 把好股票"called away"。
- 一个可选设计：**为 "long-term core position"**（如 200 shares NVDA）保留一部分，CC 只 cover excess shares。
- ThetaGang 的 `cap_factor` 和 `excess_only` 选项就是这个意思。
- wheels_copilot 应该考虑支持。

---

## 10. 项目里程碑建议（高层视角）

这是 plan 阶段的草稿，不是承诺：

### Milestone 0：基础设施抽象（1-2 周）
- 把 options_copilot 中可复用的部分抽到共享层
- 决定 monorepo / vendor / package 哪种形式

### Milestone 1：MVP——单 ticker，纯 mechanical（2-3 周）
- WheelStateMachine 单 ticker 端到端
- Alpaca paper 上从 CSP → assignment → CC → called away 完整跑通
- 单 ticker 例如 SPY，1 张合约
- Deterministic rules + 配置文件

### Milestone 2：多 ticker + watchlist（2 周）
- 支持 5-8 个 ticker 同时跑
- Watchlist 配置（先静态，后续可演化）
- Per-ticker risk budget

### Milestone 3：报告与可观测性（1 周）
- 复用 options_copilot 的 daily report
- 为 wheel 定制 sections：每个 ticker 的 state、cycle time、yield
- Workspace JSON audit trail

### Milestone 4：智能层（2-3 周）
- LLM-driven watchlist curation（每周）
- LLM-driven rescue decision（事件触发）
- Macro-aware "risk off" gating

### Milestone 5：Paper forward test（8-12 周）
- 不做新功能，只跑 + 观察 + 修小 bug
- 收集 metrics，validate 是否走得通

### Milestone 6：Live 准备
- 完整 dry-run audit
- Kill switch testing
- 切换到小金额 live account（如 $5-10K）

### Milestone 7：扩展（开放）
- 多账户 / IRA 支持
- 更激进的 ticker pool
- Hedging（VIX call 或 long put）
- Tax-aware position management

---

## 11. 参考资料

### 现有自动化项目
- [ThetaGang (IBKR)](https://github.com/brndnmtthws/thetagang)
- [alpacahq/options-wheel](https://github.com/alpacahq/options-wheel)
- [wheel-it (Alpaca)](https://github.com/vahagn-madatyan/wheel-it)
- [AllYouNeedIsWheel (IBKR + Flask UI)](https://github.com/xiao81/AllYouNeedIsWheel)
- [PeakBot (commercial)](https://peakbot.com/wheel-bot/)

### Strategy 教学与最佳实践
- [Alpaca 官方 wheel strategy 教程](https://alpaca.markets/learn/options-wheel-strategy)
- [Charles Schwab: Three Things to Know About Wheel Strategy](https://www.schwab.com/learn/story/three-things-to-know-about-wheel-strategy)
- [Option Alpha: How to Trade the Options Wheel Strategy](https://optionalpha.com/blog/wheel-strategy)
- [tastytrade: How to Sell Puts](https://tastytrade.com/learn/trading-products/options/sell-puts/)

### Backtest 和 Critical 分析
- [Early Retirement Now: Why the Wheel Strategy Doesn't Work](https://earlyretirementnow.com/2024/09/17/the-wheel-strategy-doesnt-work-options-series-part-12/) — **必读**，最严厉的批评
- [Spintwig: SPY Wheel 45-DTE Options Backtest](https://spintwig.com/spy-wheel-45-dte-options-backtest/)
- [QuantConnect: Automating the Wheel Strategy](https://www.quantconnect.com/research/17871/automating-the-wheel-strategy/)
- [SlashTraders: 3x SP500 ETF Returns With SPY Wheel](https://slashtraders.com/en/blog/sp500-spy-etf-wheel-strategy/)

### 标的选择
- [theoptionpremium: Best Stocks for Wheel Strategy](https://www.theoptionpremium.com/p/best-stocks-for-the-wheel-strategy)
- [QuantWheel: Best Stocks for Wheel Strategy 2026](https://quantwheel.com/learn/wheel-strategy/)
- [The Man Wire: 5 Hard Filters](https://themanwire.men/articles/finance/best-stocks-wheel-strategy-2026/)

### Rolling 技术
- [Wheel Strategy: How to Roll Options](https://wheelstrategy.substack.com/p/how-to-roll-options-wheel-strategy)
- [Option Wheel Logic: When to Roll a Cash Secured Put](https://www.optionwheellogic.com/blog/when-to-roll-a-cash-secured-put)

### Tax 和 Account 选择
- [Charles Schwab: Wash-Sale Rule](https://www.schwab.com/learn/story/primer-on-wash-sales)
- [Options Cafe: Complete Wheel Guide](https://options.cafe/blog/wheel-options-strategy-complete-guide/)

### Broker / API
- [Tradier API docs](https://docs.tradier.com/)
- [Alpaca Trading API docs](https://docs.alpaca.markets/)
- [IBKR API automation guide](https://www.interactivebrokers.com/campus/ibkr-quant-news/automating-financial-strategies-with-python-bots/)

### 量化数据点参考
- tastytrade 200K+ trade 研究：50% TP + 21 DTE 提升 risk-adjusted return 15-20%（多个 article 引用）
- spintwig 多变体 backtest：10 配置中 0 个超越 buy & hold（在 SPY 上）
- Early Retirement Now: 2000-2013 SPY 56.8% drawdown，13 年才回本——wheel 在长期 bear market 中的极端 case

---

## 附录 A：术语速查

| 术语 | 含义 |
|------|------|
| **CSP** | Cash-Secured Put—— 卖出 put 同时账户保留行权资金 |
| **CC** | Covered Call —— 持有 100 股同时卖出 call |
| **DTE** | Days To Expiration —— 距离到期日的天数 |
| **Delta** | option 价格对底层股价变化的敏感度，put 是负值，绝对值大致 = 被 assigned 的概率 |
| **Theta** | option 时间衰减率（每天损耗多少 premium） |
| **IV rank** | 当前 IV 在过去 1 年范围里的百分位 |
| **VRP** | Vol Risk Premium = IV − Realized Vol，正值代表 vol 卖家有 edge |
| **Roll** | 同时关闭旧合约 + 开新合约（更远 expiry 或 更低 strike） |
| **Assignment** | 你卖的 short put 被对手方 exercise，你必须按 strike 买入 100 股 |
| **Called away** | 你卖的 short call 被对手方 exercise，你必须按 strike 卖出 100 股 |
| **Bag holder** | Assigned 后股价继续跌，cost basis 远高于市场价，CC 在 cost basis 上方卖不出去的尴尬状态 |
| **Cost basis** | 持有 share 的 effective 单价 = assignment strike − received premium |
| **Pin risk** | 到期日股价接近 strike，是否 assigned 不确定的风险 |

## 附录 B：options_copilot 关键复用模块清单

详细分析见报告第 6 节。归纳：

**复用：** alpaca_client, openrouter, db schema, oms, market_clock, technicals, vol_features, daily-report, workspace pattern, finnhub/fred/uw/edgar/massive adapters

**扩展：** exit_plan, schemas/strategies, economics_gate, ticker-discovery, position-adjust

**新建：** WheelStateMachine, WatchlistManager, AssignmentLifecycle, RollDecisionEngine, RescueEngine, PortfolioRiskTracker

---

**报告完。** 下一步建议是：你看完后，告诉我哪些设计决策需要先 align（如 broker 选型、账户类型、watchlist 哲学、保守 vs 激进），然后我们就可以进入 plan / architecture 阶段。
