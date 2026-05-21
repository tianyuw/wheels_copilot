# Markus Classic Wheel 自动交易系统 Proposal

## 1. 目标

本项目要实现的是一个专注于 **Markus Hodica 风格传统 Wheel Strategy** 的自动交易系统，而不是一个通用期权策略平台。

核心目标：

- 使用 Alpaca 的 **$500,000 paper account** 作为第一阶段执行环境，先在 paper 中验证完整 Markus wheel 生命周期。
- 自动筛选适合接盘并长期持有的价值股。
- 在这些股票上卖出短周期 weekly cash-secured put。
- Put 到期归零则继续下一轮；Put 被行权则接股票。
- 接股票后自动卖 covered call，行权价不低于调整后成本价。
- 股票被 called away 后关闭一轮 wheel cycle，资金回到 cash 状态。
- 全流程以程序化规则为主，LLM 只做低频定性辅助，不直接决定下单参数。

一句话定义：

> 这是一个 stock-quality-first 的 weekly wheel 自动化系统，不是一个 high-IV premium chasing bot。

初始资金假设：

- Broker: Alpaca
- Account type: paper trading
- Starting paper equity: $500,000
- Strategy mode: Markus classic weekly wheel
- Live trading: 不属于 MVP，必须等 paper forward test 通过后再讨论。

## 2. 输入依据

本 proposal 基于以下材料：

- `docs/Kinfo_Marcus_Hodica.md`
- 用户提供的 Markus 视频截图
- Markus Kinfo profile 44676 的交易数据分析
- `options-copilot` 项目的现有自动交易基础设施
- Kinfo 上多个盈利 wheel / short-premium trader 的行为对比
- Wheel 入场点和 technical support 调研：
  - Fidelity: [Moving averages](https://www.fidelity.com/viewpoints/active-investor/moving-averages)
  - Fidelity: [Support and resistance basics](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/support-and-resistance)
  - Fidelity: [Technical Analysis for Options transcript](https://www.fidelity.com/bin-public/060_www_fidelity_com/documents/learning-center/Transcript_Technical%20Analysis%20for%20Options_v2.pdf)
  - Schwab: [Three things to know about the wheel strategy](https://www.schwab.com/learn/story/three-things-to-know-about-wheel-strategy)
  - OIC: [Cash-secured put](https://www.optionseducation.org/strategies/all-strategies/cash-secured-put)
- TradingView 成熟脚本思路调研：
  - [Dynamic Support & Resistance UAlgo](https://www.tradingview.com/script/OFfF1AEi-Dynamic-Support-Resistance-UAlgo/): pivot clustering, touch count, active zone invalidation。
  - [Support and Resistance by ebecihalil](https://www.tradingview.com/script/x0pgNaRA-Support-and-Resistance/): ATR-width support/resistance zones。
  - [Automated Support / Resistance Lines](https://www.tradingview.com/script/wkLL1TFF-Automated-Support-Resistance-Lines/): multiple rejections and zone threshold。
  - [Trend Lines, Supports and Resistances](https://www.tradingview.com/script/47hbpISG-Trend-Lines-Supports-and-Resistances/): pivot-based levels and break alerts。
  - [Consolidation Box](https://www.tradingview.com/script/KbBM6Kc5/): close-based range floor/ceiling to reduce wick noise。
  - [Range Detector LuxAlgo](https://www.tradingview.com/script/QOuZIuvH-Range-Detector-LuxAlgo/): stationarity/range detection and range extremities。
  - [Range Finder & Profile UAlgo](https://www.tradingview.com/script/oZkAmhO2-Range-Finder-Profile-UAlgo/): pivot range, range validation, POC/VAH/VAL。
  - [Wheel Strategy Assistant v3.0](https://www.tradingview.com/script/IfgjcFN2-Wheel-Strategy-Assistant-v3-0/): wheel setup scoring, EMA trend filter, delta/ROC/earnings/OI checks。

生产系统不直接依赖 TradingView alert 作为唯一决策来源。TradingView 脚本用于验证和参数校准；核心筛选逻辑在本项目中本地实现，保证可回测、可审计、可复现。

需要忽略：

- 已删除的 `docs/PROJECT_PLAN.md` v1.3 双层 watchlist 方案。

该 v1.3 方案偏向双层策略架构和更广义 premium pool。当前项目方向应收窄为 Markus classic wheel MVP。

## 3. Markus 模式的实证特征

### 3.1 策略本质

Markus 的 Wheel 不是纯期权策略，而是股票库存管理策略：

1. 先选愿意长期持有的股票。
2. 在支撑位附近卖 cash-secured put。
3. 如果被 assigned，接股票。
4. 用 covered call 持续降低成本。
5. 被 call away 后重新开始。

系统设计必须承认：

- Assignment 是预期路径，不是异常。
- 股票质量比 premium 高低更重要。
- 主要风险来自股票长期下跌，而不是单笔 option 价格波动。

### 3.2 Markus Kinfo 实盘数据

从 `trades_comprehensive_44676.json` 分析：

| 子策略 | 笔数 | 特征 |
|---|---:|---|
| Cash-secured put | 168 | 核心入场方式 |
| Covered call | 49 | 接盘后的主要管理方式 |
| Stock trades | 173 | 多数与 assignment / stock inventory 有关 |
| Long calls / puts | 36 | 可能是 PMCC 或辅助手段，MVP 暂不实现 |
| Spreads | 7 | 小账户替代方案，MVP 暂不实现 |

关键统计：

- CSP median DTE 约 3 天。
- CC median DTE 约 4 天。
- CSP 约 94% 在 7 DTE 内开仓。
- CC 约 82% 在 7 DTE 内开仓。
- 大量仓位持有到到期或到期前 1-3 天才处理。

结论：

> MVP 应实现 weekly wheel，而不是默认 30-45 DTE / 21 DTE exit 的 tastytrade 风格。

## 4. 选股规则

### 4.1 Hard Reject 规则

候选股票触发以下任意条件，应直接拒绝：

- 无 weekly options。
- 期权到期日前有 earnings。
- 最近 5 个季度中，净利润为正少于 4 个季度。
- 最近 5 年中，净利润为正少于 4 年，2020 疫情异常年可豁免。
- P/E >= 50，除非人工批准。
- Market cap < 2B。
- 与当前组合任一持仓或 ETF 的相关性 > 0.65。
- Leveraged ETF。
- Biotech / FDA binary event 股票。
- Chinese ADR，例如 BABA、PDD、GOTU。
- 最近几周涨幅超过 100% 的 meme / crazy stock。
- Bid/ask spread 过宽。
- Option open interest / volume 不足。
- 账户现金不足以 cash-secured assignment。

### 4.2 Preferred 规则

这些规则用于排序和评分，不一定直接拒绝：

- Market cap >= 5B 优先。
- 支付 dividend 优先。
- 分红股票可允许轻微基本面偏差。
- 不分红股票必须满足更严格质量要求。
- 股票处于横盘震荡区间优先。
- 当前价格接近支撑区域优先。
- IV / premium 足够，但不能以 premium 覆盖基本面缺陷。
- 行业分散优先。

### 4.3 2B vs 5B 的处理

截图中写 `market cap of 2B or more`，Markdown 文件中写 `5B`。建议配置为：

```yaml
fundamental_filters:
  market_cap_min_hard: 2000000000
  market_cap_preferred: 5000000000
```

含义：

- `< 2B`：拒绝。
- `2B - 5B`：允许但扣分。
- `>= 5B`：正常通过。

## 5. 交易规则

### 5.1 CSP 入场规则

当 ticker 状态为 `CASH` 时，系统寻找 cash-secured put。

候选合约：

- Option type: put。
- Expiration: 最近 weekly expiration，通常 1-9 DTE。
- Expiration 必须早于 earnings date。
- Strike 必须接近支撑位。
- Markus 规则：strike price must be at support according to Lowest Low Indicator +/- one strike。

这里的 `support` 不能只写成一个主观判断，也不能只用 `20D lowest low`。系统应该采用 TradingView 成熟脚本中更常见的 support-zone 模型：

1. 先用趋势过滤器决定能不能交易：
   - 当前价格必须高于 `SMA200`。
   - `SMA200` 最近 20 个交易日斜率不能明显向下。
   - 如果价格跌破 `SMA200` 或刚刚跌破主要支撑，暂停卖 put，进入 watch/manual review。
2. 用 pivot cluster 生成主要支撑区：
   - 使用 confirmed pivot lows，而不是当前还未确认的低点。
   - 把价格接近的 pivot lows 聚合成同一个 support zone。
   - 每个 zone 记录 touch/rejection count、最近一次测试时间、是否被跌破。
   - 只有达到最小 touch/rejection count 的 zone 才能作为自动下单依据。
3. 用 ATR 定义支撑区宽度：
   - 支撑不是单一价格，而是 `center +/- ATR14 * zone_width_multiple`。
   - 高波动股票的支撑区天然更宽，避免把正常波动误判成破位。
4. 用 range/consolidation box 提供横盘下沿：
   - 参考 TradingView range detector / consolidation box 的思想，识别一段时间内价格是否在可接受范围内横盘。
   - 对 wheel 来说，优先选择在横盘箱体下沿附近、且没有跌破箱体的股票。
   - 箱体可以基于 close 而不是 wick 计算，减少盘中 spike 对支撑的污染。
5. 用辅助候选做 confirmation：
   - `SMA50` / `SMA200`。
   - `lowest_low_50d` / `lowest_low_100d`。
   - 20 日 Bollinger lower band。
   - 前期 range floor。
   - `20D lowest low` 只作为辅助候选或 Markus visual reference，不作为单独自动入场依据。
6. 对支撑区做 invalidation：
   - 如果最近 N 天有日线 close 明确跌破 support zone bottom，则该 zone 失效。
   - 如果当前价格正在创新低，不把新 low 立即当作支撑。
   - 必须等待重新站回支撑区、形成新 pivot，或进入 manual review。

建议的机械选择顺序：

1. 找到最近可交易 weekly expiration。
2. 确认 expiration 早于 earnings date。
3. 通过趋势过滤器。
4. 计算 TradingView-style support zones：
   - `pivot_cluster_zones`
   - `atr_support_zones`
   - `range_box_floor_zones`
   - `moving_average_supports`
   - `lowest_low_reference_levels`
5. 过滤掉弱支撑：
   - touch/rejection count 不足。
   - 最近刚被跌破。
   - 当前价格离支撑过远导致 premium 不足。
   - 当前价格正在下跌中创新低。
6. 选择最高分 support zone：
   - 优先级：pivot cluster + ATR zone + range floor 共振。
   - 其次：pivot cluster + SMA50/SMA200 共振。
   - 再次：range floor + lowest-low reference 共振。
7. 在 support zone 附近找 put strikes：
   - 自动下单默认要求 strike <= support zone bottom。
   - paper 阶段可以记录 strike 落在 support zone 内的 candidate，但默认不自动执行。
   - Markus 的 `+/- one strike` 保留为人工复核选项，不作为早期自动交易默认行为。
8. 对这些 strikes 做 liquidity / premium / delta / ROC 检查。
9. 选择满足条件且风险收益最好的 strike。

Delta 不使用固定区间，而是根据 support zone score 动态调整。原因是 delta 本质上承担一部分入场错误的风控：支撑越强，可以接受更接近现价、premium 更高的 put；支撑一般时，自动系统必须远一点卖，降低 assignment 频率和接飞刀概率。

建议默认 policy：

| Support setup | Support score | 自动交易 delta | 处理方式 |
|---|---:|---:|---|
| Strong support | `>= 85` | `0.15 - 0.30` | 可自动下单 |
| Normal support | `70 - 84` | `0.10 - 0.25` | 可自动下单 |
| Weak / aggressive | `< 70` 或 `0.25 - 0.35` | 不自动下单 | 只进入 manual review |

关键边界：

- 如果 support 太远导致 premium 不够，不追高，不交易。
- 如果当前价格刚跌破 support，不把下跌中的价格当成新入场机会，至少等待重新站回支撑或形成新 range。
- `SMA200` 更适合作为趋势门槛和长期动态支撑，不建议单独作为 CSP strike 选择依据。
- `20D lowest low` 只能作为辅助参考，不能作为自动下单的充分条件。
- Support 只解决“在哪里卖 put”，不能覆盖基本面、财报、相关性、仓位、流动性这些硬规则。

建议初始参数：

```yaml
trend_filter:
  require_price_above_sma200: true
  sma200_slope_lookback_days: 20
  require_sma200_slope_non_negative: true
  pause_if_price_breaks_sma200: true

support:
  primary_method: pivot_cluster_zone

  pivot_cluster:
    enabled: true
    pivot_left_bars: 4
    pivot_right_bars: 4
    lookback_days: 180
    min_touches_required: 2
    preferred_touches: 3
    zone_width_atr_multiple: 0.5
    zone_width_price_pct: 1.0
    reject_if_broken_within_days: 10
    break_requires_close_below_zone: true

  rejection_count:
    enabled: true
    min_rejections_required: 2
    wick_reclaim_required: true

  range_box:
    enabled: true
    lookback_days: 20
    min_box_bars: 10
    use_close_for_floor_ceiling: true
    max_box_height_atr_multiple: 4.0
    require_price_inside_box: true
    reject_if_close_below_floor: true

  moving_average_supports:
    enabled: true
    periods: [50, 200]
    zone_width_atr_multiple: 0.35

  lowest_low_reference:
    enabled: true
    lookbacks: [20, 50, 100]
    allow_20d_low_alone: false

  bollinger_lower_band:
    enabled: true
  bollinger_window_days: 20
  bollinger_stddev: 2

  scoring:
    min_score_to_trade: 70
    pivot_cluster_weight: 35
    range_floor_weight: 20
    ma_confluence_weight: 15
    rejection_count_weight: 15
    lowest_low_reference_weight: 5
    recency_weight: 10
    penalty_recent_break: -50
    penalty_new_low_within_days: -30

csp_selector:
  dte_min: 1
  dte_max: 9
  delta_policy:
    strong_support:
      min_support_score: 85
      target_delta_min: 0.15
      target_delta_max: 0.30
      auto_trade: true
    normal_support:
      min_support_score: 70
      max_support_score: 84
      target_delta_min: 0.10
      target_delta_max: 0.25
      auto_trade: true
    manual_review:
      target_delta_min: 0.25
      target_delta_max: 0.35
      auto_trade: false
  allowed_strike_offset_count: 1
  require_strike_at_or_below_support_zone_bottom: true
  allow_strike_inside_support_zone_only_in_paper: true
  allow_one_strike_above_support_only_manual_review: true
  min_strike_distance_atr_multiple: 1.0
  min_strike_distance_pct: 3.0
  min_bid: 0.20
  max_spread_pct_of_mid: 0.12
  min_open_interest: 100
  min_weekly_return_on_strike: 0.25  # percent
```

### 5.2 CSP 持仓管理

Markus 模式不应默认 50% profit take。

建议 MVP 行为：

- 默认持有到到期。
- 到期日前 1-3 天检查是否需要清理。
- 若剩余价值极低，可 buy-to-close 释放风险。
- 若 ITM，优先接受 assignment，除非触发 emergency rule。
- 不做 debit roll。
- 不做复杂 rolling，MVP 阶段先保持简单。

建议 exit rules：

```yaml
csp_management:
  default_hold_to_expiry: true
  allow_close_near_expiry: true
  close_if_value_remaining_pct_of_credit_below: 10
  close_window_days_before_expiry: 3
  no_debit_roll: true
  assignment_is_allowed: true
```

### 5.3 Assignment 规则

Put 被行权后，系统进入 `ASSIGNED` 状态。

必须计算 adjusted cost basis：

```text
adjusted_cost_basis
= assigned_strike
- put_premium_received
- prior_call_premium_received_in_cycle
- prior_put_premium_received_in_cycle
+ fees
```

系统要记录：

- assignment date
- shares received
- assigned strike
- collected put premium
- adjusted cost basis
- cycle id

### 5.4 Covered Call 规则

当 ticker 状态为 `ASSIGNED` 时，系统卖 covered call。

硬规则：

- 只有持有至少 100 股才允许卖 1 张 call。
- Call strike 必须 >= adjusted cost basis。
- 若 cost basis 上方没有足够 premium，不强行卖 call。
- 不允许裸 short call。

建议初始参数：

```yaml
cc_selector:
  dte_min: 1
  dte_max: 9
  min_strike_vs_cost_basis: 0.0
  prefer_strike_above_cost_basis_pct: 0.0
  target_delta_min: 0.10
  target_delta_max: 0.35
  min_bid: 0.10
  max_spread_pct_of_mid: 0.15
  min_open_interest: 50
```

如果 cost-basis call 收益太低：

- 不卖低于成本价的 call。
- 状态进入 `DEAD_WHEEL_REVIEW` 或 `WAITING_FOR_CC_PREMIUM`。
- 日报提示人工检查。

## 6. 状态机

每个 ticker 一个独立 wheel state machine。

```text
CASH
  -> CSP_ORDER_PENDING
  -> CSP_OPEN
  -> CASH                 # Put expires worthless or closed cheaply
  -> ASSIGNED             # Put assigned

ASSIGNED
  -> CC_ORDER_PENDING
  -> CC_OPEN
  -> ASSIGNED             # Call expires worthless or closed cheaply
  -> CALLED_AWAY
  -> CASH

Any state
  -> MANUAL_REVIEW
  -> CORPORATE_ACTION
  -> DEAD_WHEEL
```

状态说明：

| State | 含义 |
|---|---|
| `CASH` | 无股票、无 open option，可卖 CSP |
| `CSP_ORDER_PENDING` | CSP 订单已提交未成交 |
| `CSP_OPEN` | 有 open short put |
| `ASSIGNED` | 持有股票，等待卖 covered call |
| `CC_ORDER_PENDING` | CC 订单已提交未成交 |
| `CC_OPEN` | 有 open covered call |
| `CALLED_AWAY` | 股票被 call away，准备关闭 cycle |
| `DEAD_WHEEL` | 成本价上方 call 无收益，或股票深度 underwater |
| `MANUAL_REVIEW` | 需要人工确认 |
| `CORPORATE_ACTION` | 分拆、并购、特殊分红等 |

## 7. 系统架构

### 7.1 推荐目录结构

```text
wheels_copilot/
  config/
    markus_wheel.yaml
    watchlist.yaml
    blacklist.yaml

  adapters/
    alpaca_client.py          # 从 options-copilot 复用
    finnhub.py                # earnings / fundamentals
    yahoo.py                  # fallback fundamentals
    openrouter.py             # LLM low-frequency analysis

  db/
    base.py                   # 从 options-copilot 复用
    schema.py                 # wheel-specific schema
    wheel_state_repo.py
    cycle_repo.py
    decision_log_repo.py

  engines/
    wheel_state_machine.py
    markus_stock_screener.py
    support_zone_engine.py      # TradingView-style pivot cluster / ATR zone / range floor
    pivot_cluster.py
    range_box_detector.py
    support_zone_scoring.py
    csp_selector.py
    cc_selector.py
    assignment_lifecycle.py
    cost_basis.py
    risk_budget.py
    dead_wheel_detector.py
    portfolio_correlation.py
    order_planner.py
    reconcile.py

  skills/
    watchlist-curate/
    fundamental-review/
    dead-wheel-review/
    daily-report/

  scripts/
    init_db.py
    run_daily_cycle.py
    reconcile_broker.py
    dry_run.py
    seed_watchlist.py

  tests/
    unit/
    integration/
    synthetic_e2e/
```

### 7.2 从 options-copilot 复用的部分

应复用：

- Alpaca adapter：
  - stock price
  - stock bars
  - option chain
  - latest option quotes
  - account
  - order submission
- OMS：
  - order idempotency
  - order lifecycle
  - reconciliation
- Scheduler：
  - APScheduler daemon
  - heartbeat
  - job runs
- DB base：
  - SQLite WAL
  - job audit
  - account snapshots
- Reporting：
  - daily email report infrastructure
- Synthetic E2E pattern：
  - mock broker
  - replayable scenarios

不应直接复用：

- multi-strategy council
- generic strategy-propose
- current `wheel-decide` LLM strike picking
- tastytrade-style exit plan defaults
- generic position-adjust exit logic

Wheel 领域逻辑要单独写。

## 8. 数据库设计

### 8.1 `wheel_symbols`

记录 watchlist 和筛选结果。

字段：

- `ticker`
- `status`: active / paused / rejected / manual_review
- `approved_by_user`
- `fundamental_score`
- `has_weekly_options`
- `pays_dividend`
- `market_cap`
- `pe_ratio`
- `profit_quarters_passed`
- `profit_years_passed`
- `sector`
- `reject_reason`
- `last_reviewed_at`

### 8.2 `wheel_cycles`

每次完整 wheel 记录一行。

字段：

- `id`
- `ticker`
- `state`
- `started_at`
- `closed_at`
- `shares`
- `adjusted_cost_basis`
- `realized_option_premium`
- `realized_stock_pnl`
- `total_cycle_pnl`
- `status`

### 8.3 `wheel_legs`

记录每一笔 CSP / CC。

字段：

- `cycle_id`
- `leg_type`: csp / cc
- `contract_symbol`
- `strike`
- `expiration`
- `quantity`
- `entry_credit`
- `exit_debit`
- `entry_date`
- `exit_date`
- `status`
- `assignment_flag`
- `called_away_flag`

### 8.4 `stock_lots`

记录 assignment 后的股票库存。

字段：

- `cycle_id`
- `ticker`
- `shares`
- `entry_date`
- `entry_price`
- `adjusted_cost_basis`
- `exit_date`
- `exit_price`
- `status`

### 8.5 `decision_logs`

记录每个交易日为什么交易或不交易。

字段：

- `date`
- `ticker`
- `state`
- `decision`: sell_csp / sell_cc / hold / manual_review
- `reason_codes`
- `input_snapshot_json`
- `selected_contract_json`
- `rejected_candidates_json`

这张表非常重要。自动系统必须能回答：

> 今天为什么没有交易？

## 9. 每日流程

### 9.1 Pre-market / Morning Reconcile

时间：开盘前或开盘后 10-30 分钟。

步骤：

1. 同步 Alpaca positions / orders / activities。
2. 检查是否有 assignment / exercise / call away。
3. 更新 wheel state。
4. 更新 account cash / buying power。
5. 检查 corporate actions、earnings、dividend。

### 9.2 Candidate Scan

对 active watchlist：

1. 基本面 hard filters。
2. Weekly options 检查。
3. Earnings before expiration 检查。
4. Correlation / concentration 检查。
5. TradingView-style support zone 计算和评分。
6. Option chain liquidity 检查。

### 9.3 Decision Cycle

按状态执行：

- `CASH`：尝试卖 CSP。
- `CSP_OPEN`：持有、低价平仓或等待 assignment。
- `ASSIGNED`：尝试卖 CC。
- `CC_OPEN`：持有、低价平仓或等待 called away。
- `DEAD_WHEEL`：不自动下单，日报提示。

### 9.4 Order Submission

订单原则：

- 只用 limit order。
- Single-leg CSP / CC。
- 不使用 market order。
- 不使用 margin naked put。
- 不使用 debit roll。
- 每个订单必须有 idempotency key。

### 9.5 EOD Report

日报必须包含：

- 今日新开 CSP / CC。
- 今日关闭 / 到期 / assignment / called away。
- 每个 ticker 当前状态。
- 每个 cycle adjusted cost basis。
- 未交易原因。
- Dead wheel / manual review alerts。
- 账户现金、reserved assignment cash、最大潜在 assignment。

## 10. 风险控制

### 10.1 Assignment Stress

每天计算：

```text
required_cash_if_all_puts_assigned
= sum(short_put_strike * 100 * contracts)
```

硬规则：

- 任何时候都不能超过可用现金或配置比例。
- 不允许依赖 margin 承接 assignment。

建议：

```yaml
risk:
  max_assignment_cash_pct: 0.80
  min_cash_buffer_pct: 0.15
```

### 10.2 Position Sizing

Markus 本人账户很大，小账户复制风险高。

建议 MVP：

- 每个 ticker 最多一个 active cycle。
- 每个 ticker 初始最多 1 contract。
- paper 稳定后再开放多 contract。
- 单 ticker 最大 assignment notional 不超过账户 equity 的 10-15%。

```yaml
sizing:
  max_contracts_per_ticker_initial: 1
  max_single_ticker_assignment_pct: 0.15
  max_active_tickers: 5
```

### 10.3 Correlation / Concentration

规则：

- 新增 ticker 与现有 active tickers 的 60D correlation <= 0.65。
- 同行业最大 2 个 ticker。
- ETF 与成分股需要合并看风险。

### 10.4 Dead Wheel Detection

触发任一条件进入 `DEAD_WHEEL` 或 `MANUAL_REVIEW`：

- 当前价格低于 adjusted cost basis 超过 20-30%。
- cost basis 上方 7-14 DTE call 年化收益低于阈值。
- 基本面恶化。
- earnings / litigation / regulatory shock。
- 股票停牌、并购、分拆。

```yaml
dead_wheel:
  underwater_pct_warn: 0.15
  underwater_pct_manual_review: 0.30
  min_cc_annualized_yield_at_cost_basis: 0.02
```

### 10.5 DCA 规则

Markus 说股票跌 30% 后才考虑 DCA，且前提是基本面没有恶化。

MVP 建议：

- 不自动 DCA。
- 跌幅 >= 30% 只触发 manual review。
- DCA 必须人工批准。

## 11. LLM 的角色

LLM 不应该做：

- 直接决定 strike。
- 直接决定 expiration。
- 直接决定下单数量。
- 覆盖 hard reject。
- 在开盘时实时做交易决策。

LLM 可以做：

- 每周或每月 watchlist review。
- 总结基本面。
- 解释为什么某股票不适合 wheel。
- 检查新闻 / 监管 / 业务风险。
- Dead wheel rescue 分析。
- 生成日报解释。

建议 LLM 输出必须被 schema 限制：

```json
{
  "ticker": "UPS",
  "wheel_suitability": "pass",
  "quality_notes": "...",
  "risks": ["margin pressure", "sector weakness"],
  "human_review_required": false
}
```

## 12. 配置样例

```yaml
mode: paper
broker: alpaca

account:
  broker: alpaca
  account_type: paper
  starting_equity: 500000
  currency: USD
  live_trading_enabled: false

watchlist:
  tickers:
    - BBY
    - TSCO
    - IWM
    - MRK
    - XLE
    - UPS
    - HAL
    - ABNB
    - EOG
    - GDXJ

fundamental_filters:
  market_cap_min_hard: 2000000000
  market_cap_preferred: 5000000000
  pe_max: 50
  min_positive_quarters_out_of_5: 4
  min_positive_years_out_of_5: 4
  allow_2020_pandemic_exception: true
  prefer_dividend: true
  reject_biotech: true
  reject_chinese_adr: true
  reject_leveraged_etf: true
  reject_recent_100pct_movers: true

portfolio:
  max_active_tickers: 5
  max_correlation: 0.65
  max_same_sector_tickers: 2

trend_filter:
  require_price_above_sma200: true
  sma200_slope_lookback_days: 20
  require_sma200_slope_non_negative: true
  pause_if_price_breaks_sma200: true

support:
  primary_method: pivot_cluster_zone

  pivot_cluster:
    enabled: true
    pivot_left_bars: 4
    pivot_right_bars: 4
    lookback_days: 180
    min_touches_required: 2
    preferred_touches: 3
    zone_width_atr_multiple: 0.5
    zone_width_price_pct: 1.0
    reject_if_broken_within_days: 10
    break_requires_close_below_zone: true

  rejection_count:
    enabled: true
    min_rejections_required: 2
    wick_reclaim_required: true

  range_box:
    enabled: true
    lookback_days: 20
    min_box_bars: 10
    use_close_for_floor_ceiling: true
    max_box_height_atr_multiple: 4.0
    require_price_inside_box: true
    reject_if_close_below_floor: true

  moving_average_supports:
    enabled: true
    periods: [50, 200]
    zone_width_atr_multiple: 0.35

  lowest_low_reference:
    enabled: true
    lookbacks: [20, 50, 100]
    allow_20d_low_alone: false

  bollinger_lower_band:
    enabled: true
  bollinger_window_days: 20
  bollinger_stddev: 2

  scoring:
    min_score_to_trade: 70
    pivot_cluster_weight: 35
    range_floor_weight: 20
    ma_confluence_weight: 15
    rejection_count_weight: 15
    lowest_low_reference_weight: 5
    recency_weight: 10
    penalty_recent_break: -50
    penalty_new_low_within_days: -30

csp_selector:
  dte_min: 1
  dte_max: 9
  allowed_strike_offset_count: 1
  require_strike_at_or_below_support_zone_bottom: true
  allow_strike_inside_support_zone_only_in_paper: true
  allow_one_strike_above_support_only_manual_review: true
  min_strike_distance_atr_multiple: 1.0
  min_strike_distance_pct: 3.0
  delta_policy:
    strong_support:
      min_support_score: 85
      target_delta_min: 0.15
      target_delta_max: 0.30
      auto_trade: true
    normal_support:
      min_support_score: 70
      max_support_score: 84
      target_delta_min: 0.10
      target_delta_max: 0.25
      auto_trade: true
    manual_review:
      target_delta_min: 0.25
      target_delta_max: 0.35
      auto_trade: false
  min_bid: 0.20
  max_spread_pct_of_mid: 0.12
  min_open_interest: 100
  min_weekly_return_on_strike_pct: 0.25

cc_selector:
  dte_min: 1
  dte_max: 9
  min_strike_vs_cost_basis_pct: 0.0
  target_delta_min: 0.10
  target_delta_max: 0.35
  min_bid: 0.10
  max_spread_pct_of_mid: 0.15
  min_open_interest: 50

risk:
  max_assignment_cash_pct: 0.80
  min_cash_buffer_pct: 0.15
  max_single_ticker_assignment_pct: 0.15
  max_single_ticker_assignment_dollars: 75000
  no_margin_assignment: true

management:
  csp_hold_to_expiry: true
  cc_hold_to_expiry: true
  allow_close_near_expiry: true
  close_if_value_remaining_pct_of_credit_below: 10
  no_debit_roll: true
  dca_requires_manual_approval: true
```

## 13. Implementation Plan

### Phase 0: Proposal / Design Lock

Deliverables:

- 本 proposal。
- 明确 Markus MVP scope。
- 明确不做 v1.3 双层 strategy。
- 明确 watchlist 初始名单。

### Phase 1: Shared Infrastructure

从 `options-copilot` 复制或抽取：

- Alpaca adapter。
- OMS。
- DB base。
- Scheduler base。
- Daily report base。

Deliverables:

- `wheels_daemon.py`
- `config/markus_wheel.yaml`
- DB 初始化脚本。

### Phase 2: Wheel Domain Core

实现：

- `wheel_state_machine.py`
- `cost_basis.py`
- `assignment_lifecycle.py`
- `wheel_cycles` / `wheel_legs` / `stock_lots` schema。

Deliverables:

- 可手动 seed 一个 assigned stock 并生成 CC decision。
- 可从 mock fills 重建完整 wheel cycle。

### Phase 3: Screener + Selector

实现：

- Markus stock screener。
- Fundamental filters。
- Weekly options filter。
- Earnings filter。
- Correlation filter。
- Trend filter using SMA200。
- TradingView-style support engine:
  - Pivot cluster support zones。
  - ATR-width support zones。
  - Touch/rejection count。
  - Range/consolidation box floor。
  - SMA50/SMA200 confluence。
  - Lowest-low reference levels as auxiliary signals only。
  - Zone invalidation after confirmed breakdown。
- CSP selector。
- CC selector。

Deliverables:

- `dry_run.py --ticker TSCO`
- 输出 pass/fail reason。
- 输出 support zones、zone score、touch count、range floor、chosen support zone、rejection reason。
- 输出 selected CSP/CC candidate。

### Phase 4: Paper Execution

实现：

- 使用 Alpaca $500K paper account 进行 CSP 下单。
- 使用 Alpaca $500K paper account 进行 CC 下单。
- Order reconciliation。
- Assignment / called-away detection。

Deliverables:

- 1-3 个 ticker paper wheel。
- Account snapshot 显示 starting equity / cash / buying power / reserved assignment cash。
- 日报显示完整状态。

### Phase 5: Synthetic E2E

测试场景：

- CSP expires worthless。
- CSP assigned。
- CC expires worthless。
- CC called away。
- Dead wheel: stock -30%。
- Earnings before expiration: reject。
- Correlation > 0.65: reject。
- No weekly options: reject。
- Cost basis call has no premium: manual review。

### Phase 6: Forward Paper Test

建议至少 8-12 周 paper，使用 Alpaca $500K paper account，不接入 live。

Paper account 目标：

- 起始 paper equity: $500,000。
- 初始仅使用 1-3 个 ticker。
- 初始每个 ticker 最多 1 contract。
- Paper 稳定后逐步扩到最多 5 个 active tickers。
- 任何阶段都不允许使用 margin 承接 assignment。
- 每日记录 paper account equity、cash、buying power、reserved assignment cash。

Go-live gate：

- 至少 20 个 CSP/CC option legs。
- 至少 3 个完整 cycle 或 2 个 assignment lifecycle。
- 0 次 duplicate order。
- 0 次 naked call。
- 0 次 cash-secured violation。
- 所有 no-trade decision 有 reason log。
- Assignment / cost basis 计算可审计。
- Paper account 下 assignment lifecycle 至少完整验证 2 次。
- $500K paper account 的最大潜在 assignment cash 不超过配置上限。

### Phase 7: Small Live

只在以下条件满足后：

- 单 ticker。
- 单 contract。
- 人工批准 watchlist。
- live 前 20 个交易日保留 manual approval 或 shadow compare。

## 14. 成功指标

不要只看 win rate。Wheel 的 win rate 天然高，但可能隐藏大亏。

应跟踪：

- Monthly realized premium。
- Total cycle P&L。
- Assignment rate。
- Called-away rate。
- Average days in cycle。
- Average adjusted cost basis reduction。
- Underwater stock notional。
- Dead wheel count。
- Cash reserved for assignment。
- Max single ticker exposure。
- Strategy drawdown。
- Premium yield on reserved cash。
- Stock inventory unrealized P&L。

## 15. Non-goals for MVP

MVP 不做：

- PMCC。
- Put credit spread。
- 多模型 trading council。
- 多策略择优。
- High-IV meme premium pool。
- 自动 DCA。
- 自动 sub-cost-basis covered call。
- Live full-size deployment。
- 复杂 roll engine。

这些可以作为后续阶段，但不应该污染第一版。

## 16. 主要风险

### 16.1 数据风险

- Fundamentals 数据源不稳定。
- Earnings date 不准。
- Option quote 延迟。
- Alpaca paper assignment 行为可能和真实账户不同。

缓解：

- Broker reconcile 为准。
- 多数据源 cross-check earnings。
- 所有 order 只用 limit。
- Paper assignment 行为必须专项测试。

### 16.2 策略风险

- 高胜率掩盖 tail loss。
- 股票长期下跌导致 dead wheel。
- 同行业相关性在 crisis 中上升。
- 小账户被一个 assignment 锁死。

缓解：

- 严格 sizing。
- Assignment stress。
- Correlation gate。
- Dead wheel detector。
- Manual review。

### 16.3 自动化风险

- 重复下单。
- 状态机和 broker 状态不一致。
- 误卖 naked call。
- 错误 cost basis 导致低价 call 锁亏。

缓解：

- Idempotency key。
- 每日 reconcile。
- Broker positions 为最终 truth。
- CC 下单前强制检查 shares。
- Cost basis 单元测试和 replay。

## 17. 推荐结论

建议把 `wheels_copilot` 做成一个窄而深的系统：

```text
Markus Classic Weekly Wheel
= strict stock-quality screener
+ weekly CSP
+ assignment lifecycle
+ cost-basis covered call
+ dead-wheel risk control
```

第一版的核心不是追求最高收益，而是建立一个不会乱下单、不会裸露风险、能完整处理 assignment lifecycle 的系统。

如果这个 MVP 在 paper 中跑通 8-12 周，再考虑加入：

- PMCC。
- Put credit spread for small accounts。
- More aggressive premium pool。
- LLM-assisted rescue。
- Live deployment。

优先级应保持清晰：

1. 正确状态机。
2. 正确选股。
3. 正确 cost basis。
4. 正确风险控制。
5. 最后才是收益优化。
