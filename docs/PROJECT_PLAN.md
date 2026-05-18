# Wheels Copilot — Project Plan

**Version：** v1.0
**Date：** 2026-05-18
**Status：** Draft for user review → kickoff
**Inputs：** [`docs/RESEARCH.md`](RESEARCH.md), user's 4 locked decisions, 4-member specialist council

---

## 0. Executive Summary

`wheels_copilot` is a Python daemon that runs a single options strategy — the **Wheel** — on a $500K USD Alpaca account, daily, automatically, with selective LLM enrichment for the qualitative parts of the decision (stock selection, fundamentals, outlook, market regime). It is the sibling project of `options-copilot` and reuses ~60% of its infrastructure.

**The plan's core convictions:**

1. **The wheel is a state machine, not a strategy.** Each ticker is an independent FSM (`CASH → CSP_OPEN → ASSIGNED → CC_OPEN → ...`). Every decision in the system is "what should this state-machine instance do today?"
2. **Validator-first, mechanical-by-default.** Strikes, DTEs, profit-takes, rolls, stop-losses, sizing — all deterministic, code-enforced. LLMs are for *qualitative* decisions only (watchlist curation, fundamentals deep-dive, rescue calls). The system keeps trading if the LLM layer is down.
3. **Watchlist is policy; trades are execution of policy.** Watchlist refreshes weekly (Sunday evening) via a 5-model LLM council. Daily cycle executes mechanically against that frozen watchlist.
4. **Forward test is a hard gate, not a formality.** 10 weeks of Alpaca paper across 4 phases. Live trading begins only after every go-live gate is green. Capital ramps in 4 stages ($50K → $150K → $300K → $500K).
5. **Build for $500K real money from day one.** Conservative defaults (per-ticker max 8%, daily-loss CB at -1.5%, drawdown CB at 8%). Reasonable return target: **6–9% annualized net**, Sharpe 0.8–1.2, max DD ~6–10%. Not aiming to beat SPY in bull markets — aiming for survivability + steady cash flow.

**Council members (this plan is the synthesis of 4 specialist subagent outputs):**

| Role | Owns | Section |
|------|------|---------|
| System Architect | Code structure, modules, DB, daemon, config | §2 |
| Quant / Risk | Parameters, ticker pool, risk budget, circuit breakers | §3 |
| AI/LLM Integration | Where LLM enters, prompts, cost, fallback | §4 |
| Forward Test Strategist | 10-week test plan, go-live gates, rollout staircase | §5 |

**Cross-cutting decisions resolved during synthesis:**
- Watchlist refresh: **Sunday 18:00 ET** (LLM agent's call — more data, less weekday churn).
- VIX gating: gradient (≤22 both tiers / 22–28 tier-1 only / >28 freeze), not single threshold.
- Daily-loss circuit breaker: **-1.5% of equity (~$7.5K)** — the more conservative of two proposals.
- Watchlist authority: DB is source of truth; `tickers.yaml` is *seed only* (one-time bootstrap).
- LLM model assignment: 5-model council for watchlist + rescue; single Opus for fundamentals; single Sonnet for outlook + regime.

**Timeline (from 2026-05-19 kickoff):**

| Window | Weeks | Phase |
|--------|-------|-------|
| 2026-05-19 → 2026-06-15 | -4 to 0 | Engineering: M0 shared layer → M2 multi-ticker (Phase-1-ready) |
| 2026-06-16 → 2026-06-29 | 1-2 | Paper Phase 1: Single ticker (SPY), mechanical only |
| 2026-06-30 → 2026-07-13 | 3-4 | Paper Phase 2: 5 tickers, mechanical |
| 2026-07-14 → 2026-08-10 | 5-8 | Paper Phase 3: Full watchlist + LLM enabled |
| 2026-08-11 → 2026-08-24 | 9-10 | Paper Phase 4: Stress + edge cases |
| 2026-08-25 → 2026-09-07 | 11-12 | Live-α: $50K, 2 tickers |
| 2026-09-08 → 2026-09-21 | 13-14 | Live-β: $150K, 4 tickers |
| 2026-09-22 → 2026-10-05 | 15-16 | Live-γ: $300K, 6-8 tickers |
| 2026-10-06 onward | 17+ | Live-1.0: $500K, full watchlist |

**Expected total: ~5 months from kickoff to full-capital live.**

---

## 1. Locked Decisions

From the user, baked into every subsequent section:

1. **Account:** $500,000 USD Alpaca account. Paper for forward test → live after gates pass.
2. **Strategy:** Wheels only. No other options strategies in this codebase.
3. **LLM usage:** Stock selection (watchlist), fundamentals analysis, outlook/prospect analysis, market regime analysis. Plus a code-based screener feeding the LLM.
4. **Broker:** Alpaca (consistent with `options-copilot`).
5. **Forward test:** 8–12 weeks paper, gated to 10 weeks + 2-week slack.
6. **Reference project:** `/Users/tianyuwang/Projects/options-copilot/` — vendor-copy ~15 files from it under `shared/`, with a SHA-tracked sync script.

Other framing decisions made during planning (open to revision, see §7):
- Account type: assumed **taxable** for now. IRA migration deferred (Alpaca doesn't currently support IRA).
- Reuse strategy: **vendor-copy now → pip-installable `copilot-core` package later** (no monorepo).
- Daemon model: APScheduler `BlockingScheduler`, mirroring `options-copilot`.

---

## 2. Architecture

### 2.1 Code Reuse Strategy — Vendor-copy now, package-extract later

**Decision: vendor-copy a curated subset of `options-copilot` into `wheels_copilot/shared/`**, with a clear deprecation path to a pip-installable `copilot-core` package at month 3-4.

Rationale:
- **Monorepo is wrong now.** `options-copilot` is 60K+ LOC of production-tested code with its own DB, .venv, daemon plist, workspace artifacts. Merging it means rewriting paths in dozens of skills, breaking the running paper account.
- **A pip package is right but premature.** Until we know which files actually need to be shared (some adapters will fork, some won't), extracting `copilot-core` creates a phantom API boundary we'll thrash for weeks.
- **Vendor-copy ships in 1 week.** Copy ~15 files into `shared/`, add a `SHARED_PROVENANCE.md` listing the git SHA each file came from, write `scripts/sync_shared.sh` that does a 3-way diff to surface upstream drift.

**Path forward:** once both repos run for a quarter, extract `shared/` into a sibling repo `copilot-core` and install via `pip install -e ../copilot-core` from both projects.

### 2.2 Directory Structure

```
wheels_copilot/
├── README.md
├── pyproject.toml
├── config.yaml                          # account-level config (risk, LLM toggles, schedule)
├── tickers.yaml                         # seed only — initial watchlist bootstrap
├── .env                                 # ALPACA_*, OPENROUTER_*, FINNHUB_*, FRED_*
├── com.wheels-copilot.daemon.plist      # launchd unit
├── wheels_daemon.py                     # APScheduler entrypoint
├── wheels_copilot.db                    # SQLite, WAL mode
│
├── shared/                              # VENDORED from options-copilot
│   ├── SHARED_PROVENANCE.md             # git SHA per file
│   ├── adapters/                        # alpaca_client, openrouter, finnhub, fred, edgar
│   ├── db/base.py                       # connection, job_runs, heartbeat, hash
│   ├── engines/                         # alpaca_health, market_clock, technicals, vol_features, oms
│   └── skills_common/                   # daily-report base, workspace primitives
│
├── db/
│   ├── schema.py                        # wheels-specific tables + migrations
│   ├── wheel_state_repo.py              # CRUD wheel_states
│   ├── watchlist_repo.py
│   ├── cost_basis_repo.py
│   └── cycle_log_repo.py
│
├── engines/                             # WHEELS DOMAIN — all NEW
│   ├── wheel_state_machine.py           # the FSM
│   ├── transitions.py                   # transition table + guards
│   ├── csp_selector.py                  # strike/DTE — deterministic
│   ├── cc_selector.py                   # ditto, with cost_basis floor
│   ├── roll_decider.py                  # roll vs close vs take-assignment
│   ├── assignment_lifecycle.py          # handle assigned events
│   ├── rescue_engine.py                 # bag-holder escalation
│   ├── risk_budget.py                   # account → ticker → trade allocator
│   ├── portfolio_risk.py                # net delta, sector concentration
│   ├── wheel_exit_plan.py               # per-state lifecycle exits
│   ├── wheel_gates.py                   # hard pre-trade constraints
│   └── earnings_calendar.py             # Finnhub wrapper with cache
│
├── skills/                              # LLM-driven, low-frequency
│   ├── watchlist-curate/                # weekly, Sunday 18:00 ET
│   ├── ticker-evaluate/                 # per-add fundamentals deep-dive
│   ├── market-outlook/                  # daily regime read
│   ├── rescue-decide/                   # event-driven bag-holder rescue
│   ├── code-screener/                   # 4-stage cheap-first screener
│   ├── wheel-cycle/                     # per-ticker decision dispatcher (mostly deterministic)
│   └── daily-report/                    # wheel-specific email report
│
├── schemas/
│   ├── wheel_position.py                # WheelPosition, WheelLeg, CostBasis
│   ├── wheel_state.py                   # WheelStateEnum, WheelTransition
│   ├── watchlist.py                     # WatchlistEntry
│   ├── decisions.py                     # CSPProposal, CCProposal, RollDecision, RescuePlan
│   └── llm_outputs.py                   # Pydantic schemas for each LLM touchpoint
│
├── scripts/
│   ├── init_db.py
│   ├── seed_watchlist.py
│   ├── reconcile_alpaca.py
│   ├── force_close.py
│   ├── replay_state.py                  # rebuild wheel_states from fills (audit)
│   ├── dry_run_cycle.py
│   ├── sync_shared.sh                   # diff shared/ vs options-copilot upstream
│   └── stress/                          # forward-test stress injection scripts
│
├── workspace/                           # YYYY-MM-DD/<ticker>/ — LLM I/O, decisions
│
├── tests/
│   ├── unit/                            # state_machine transitions, gates, selectors
│   ├── integration/                     # full cycle on synthetic broker
│   └── fixtures/                        # canned chains, account snapshots, fills
│
├── docs/
│   ├── RESEARCH.md                      # this exists
│   ├── PROJECT_PLAN.md                  # this file
│   ├── RUNBOOK.md                       # operator daily/weekly playbook (to write)
│   └── decisions/                       # ADRs for non-trivial decisions
│
└── logs/                                # daemon.log, fills.log, decisions.log
```

### 2.3 Module Ownership Boundaries

| Module | Disposition | Source (options-copilot) | Destination (wheels_copilot) | Notes |
|---|---|---|---|---|
| Alpaca client | REUSE | `adapters/alpaca_client.py` | `shared/adapters/alpaca_client.py` | Already supports CSP/CC/MLEG |
| OpenRouter | REUSE | `adapters/openrouter.py` | `shared/adapters/openrouter.py` | 5-model orchestration ready |
| Finnhub | REUSE | `adapters/finnhub.py` | `shared/adapters/finnhub.py` | Earnings + fundamentals |
| FRED | REUSE | `adapters/fred.py` | `shared/adapters/fred.py` | Macro calendar |
| EDGAR | REUSE | `adapters/edgar.py` | `shared/adapters/edgar.py` | 10-K / 10-Q for fundamentals |
| Unusual Whales | SKIP | `adapters/unusual_whales.py` | — | Flow data is for directional; wheel doesn't need (yet) |
| DB base | REUSE | `db/database.py` (split) | `shared/db/base.py` | Connection, heartbeat, hash |
| OMS | REUSE | `engines/oms.py` | `shared/engines/oms.py` | Broker-generic |
| Market clock | REUSE | `engines/market_clock.py` | `shared/engines/market_clock.py` | Half-day / holiday |
| Alpaca health | REUSE | `engines/alpaca_health.py` | `shared/engines/alpaca_health.py` | Degradation modes |
| Technicals | REUSE | `engines/technicals.py` | `shared/engines/technicals.py` | TA-Lib wrapper |
| Vol features | REUSE | `engines/vol_features.py` | `shared/engines/vol_features.py` | IV rank, VRP, skew |
| Daily-report base | REUSE | `skills/daily-report/scripts/report.py` | `shared/skills_common/report_base.py` | SES email + template |
| Workspace audit | REUSE | (inline pattern) | `shared/skills_common/workspace.py` | Refactor inline → helpers |
| Exit-plan engine | EXTEND | `engines/exit_plan.py` | `engines/wheel_exit_plan.py` | Per-state lifecycle; CSP-exit feeds CC-entry |
| Economics gate | EXTEND | `engines/economics_gate.py` | `engines/wheel_gates.py` | FSM transition guards |
| Strategy schemas | EXTEND | `schemas/strategies.py` | `schemas/decisions.py` | CSPProposal/CCProposal/RollDecision as distinct types |
| Portfolio gates | EXTEND | `engines/portfolio_gates.py` | `engines/portfolio_risk.py` | Net stock delta tracking |
| Ticker discovery | REPLACE | `skills/ticker-discovery/` | `skills/code-screener/` + `skills/watchlist-curate/` | Different problem: screen *eligible pool*, not *today's best trade* |
| Position adjust | REPLACE | `skills/position-adjust/` | `engines/roll_decider.py` + `engines/rescue_engine.py` | Wheel exits are deterministic |
| WheelStateMachine | NEW | — | `engines/wheel_state_machine.py` | Core domain |
| AssignmentLifecycle | NEW | — | `engines/assignment_lifecycle.py` | Detect → mutate → compute cost basis |
| Watchlist | NEW | — | `db/watchlist_repo.py` + `skills/watchlist-curate/` | Persistent, weekly LLM-curated |
| Risk budget | NEW | — | `engines/risk_budget.py` | Account → ticker → trade cascade |
| Cycle log | NEW | — | `db/cycle_log_repo.py` | Closed-cycle analytics |

### 2.4 Core Domain Primitives

**WheelPosition (Pydantic sketch):**

```python
class WheelLeg(BaseModel):
    role: Literal["short_put", "short_call", "long_stock"]
    occ_symbol: str | None
    strike: Decimal | None
    expiration: date | None
    contracts: int                # 1 contract = 100 shares
    entry_credit: Decimal | None
    alpaca_order_id: str
    alpaca_position_id: str | None

class WheelPosition(BaseModel):
    id: int
    ticker: str
    state: WheelState
    state_entered_at: datetime
    current_leg: WheelLeg | None
    cost_basis: Decimal | None    # set when state=ASSIGNED
    shares_owned: int
    roll_count: int               # in current option-episode
    cycle_id: int
    realized_pnl_cycle: Decimal
    last_reconciled_at: datetime
```

**State machine: hand-rolled FSM with enum + transition table.** No library.

```
       ┌────────────────────┐
       │       CASH         │◄─────────────────────┐
       │  (no position)     │                      │
       └──────────┬─────────┘                      │
                  │ sell_csp (gate.csp_open_ok)    │ csp_expired_otm
                  ▼                                │ OR csp_closed_50%tp
       ┌────────────────────┐                      │
       │     CSP_OPEN       │──────────────────────┘
       │  (short put live)  │
       └──────────┬─────────┘
                  │ csp_assigned (broker event)
                  ▼
       ┌────────────────────┐  rescue_freeze     ┌──────────────────┐
       │      ASSIGNED      │───────────────────►│  ASSIGNED_HELD   │
       │  (100×N shares,    │                    │  (no auto-CC,    │
       │   no live option)  │◄───────────────────│   manual review) │
       └──────────┬─────────┘   operator_resume  └──────────────────┘
                  │ sell_cc (gate.cc_strike_floor_ok)
                  ▼
       ┌────────────────────┐
       │      CC_OPEN       │──── cc_expired_otm ──┐
       │  (short call live, │                       │ (back to ASSIGNED)
       │   100×N shares)    │                       ▼
       └──────────┬─────────┘              ┌────────────────────┐
                  │ cc_called_away         │      ASSIGNED      │
                  │ (broker event)         └────────────────────┘
                  ▼
       ┌────────────────────┐
       │  CYCLE_COMPLETE    │── write cycle_log row, then → CASH
       └────────────────────┘
```

`ASSIGNED_HELD` is the bag-holder sub-state (price < cost_basis × 0.85): auto-CC writing is paused; rescue engine takes over. `CYCLE_COMPLETE` is transient — used to atomically write `cycle_log` and return to `CASH`.

**Watchlist persistence:** see DB schema below. Authoritative source = DB. The `tickers.yaml` file is *bootstrap only* — once seeded, all mutations go through `skills/watchlist-curate/` → `db/watchlist_repo.py`.

**Cost basis & cycle history:** two tables. `cost_basis_history` is append-only journal (initial assignment, additional credits via CC, dividends). `cycle_log` is closed-cycle ledger (one row per completed CASH→CASH).

### 2.5 Daemon Schedule

**Single APScheduler `BlockingScheduler` daemon.** Three cycles, each wheel-shaped:

**Morning cycle — 09:45 ET (Mon-Fri):**
1. Reconcile with Alpaca (positions, orders, account). Broker = source of truth.
2. Process overnight assignment notifications → state transitions to `ASSIGNED`, cost basis written.
3. Risk-off check (VIX, macro calendar, daily P&L CB). If risk-off, skip step 5.
4. For each `wheel_state` row, evaluate gates: `CSP_OPEN`/`CC_OPEN` → check 50%TP / 21DTE / delta breach. `ASSIGNED` → propose CC. `CASH` → if-watchlist-active and gates pass, propose CSP.
5. Submit orders via OMS.

09:45 is earlier than `options-copilot`'s 14:30 because wheel doesn't need intraday flow data — we want early fills for better liquidity.

**Intraday cycle — every 15 min, 10:00-15:45 ET:**
1. Reconcile orders only (fills, partial fills, cancels).
2. Apply mechanical exit rules: short option delta > 0.65 → enqueue roll decision for EOD. Underlying gap-down > 10% intraday → trigger rescue evaluation NOW.
3. No LLM calls. No new entries intraday.

**EOD cycle — 16:15 ET (Mon-Fri):**
1. Final reconcile. Capture `account_snapshot`. Detect expiries.
2. Process expiries: short option expired OTM → premium realized, state transition back. ITM → assignment posted, reconciled next morning.
3. Run queued roll decisions from intraday.
4. If today is Sunday EOD → invoke `skills/watchlist-curate/`. (Sunday is special — runs at 18:00 ET, not 16:15.)
5. Compute daily metrics, generate daily report.

**Heartbeat — every 30 min.** Identical to `options-copilot`.

### 2.6 DB Schema — New Tables

Reused as-is from `options-copilot` (no schema change): `account_snapshots`, `positions`, `orders`, `fills`, `signals`, `model_outputs`, `job_runs`, `daemon_status`, `daily_metrics`.

**New wheels tables:**

```sql
-- One row per ticker actively being wheeled. FSM lives here.
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
    last_curation_json      TEXT,                            -- LLM evaluation payload
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

### 2.7 Order Flow Through OMS

**Reuse the shared OMS as-is.** Its state machine is broker-generic (PENDING → SUBMITTED → FILLED/CANCELED/REJECTED). Wheels-specific lifecycle lives one layer up in `wheel_state_machine.py`, which observes OMS state changes and reacts.

**Flow A — CSP submit → fill → expire OTM → back to cash:**
1. Morning cycle: `csp_selector.propose(ticker)` returns leg + limit. `wheel_gates.csp_open_ok()` validates.
2. `oms.create_order(...)`. `positions` row written with `effective_exit_plan` frozen (TP=50%, time-stop=21DTE, stop-loss=delta 0.65). State: CASH → CSP_OPEN.
3. `oms.submit()` → Alpaca. Status PENDING → SUBMITTED.
4. Intraday: `oms.reconcile_orders()` pulls fills. SUBMITTED → FILLED. `cost_basis_history` records `csp_credit`.
5. At expiry (OTM): EOD detects expiration. `positions.status='CLOSED'`, `close_reason='expired_otm'`. State: CSP_OPEN → CASH.

**Flow B — CSP → fill → assignment → cash-to-shares → CC → called away:**
1-4. Same as Flow A through fill.
5. At expiry (ITM) or earlier: Alpaca posts assignment. Next morning's reconcile detects new shares + missing short put.
6. `assignment_lifecycle.process(ticker)`:
   - Insert `positions` row, `strategy_template='long_stock'`, qty = 100 × contracts.
   - Close CSP row: `status='ASSIGNED'`, `close_reason='assigned'`.
   - Append `cost_basis_history`: `assignment` event (+strike), then negative entries for accumulated csp_credits. Resulting basis = strike − total_premium.
   - State: CSP_OPEN → ASSIGNED. `cost_basis`, `shares_owned`, `current_position_id` updated atomically.
   - If price < cost_basis × 0.85 → state = ASSIGNED_HELD, emit rescue alert.
7. Morning cycle: `cc_selector.propose(ticker)` enforces **`strike ≥ cost_basis`** as a hard floor in code, not LLM. Picks where strike ≥ min AND delta ∈ [0.20, 0.35] AND DTE ∈ [30, 45]. If no viable strike → returns None, log `cc_no_viable_strike`. State stays ASSIGNED.
8. OMS submits CC. State: ASSIGNED → CC_OPEN.
9. Days later: called away (deep ITM or expiry). Reconcile sees shares + call gone, cash returned.
10. `assignment_lifecycle.process_called_away(ticker)`:
    - Close long_stock position. Capital gain = (strike − cost_basis) × shares.
    - Close short_call position.
    - State: CC_OPEN → CYCLE_COMPLETE → CASH.
    - Finalize `cycle_log` row: outcome='called_away', totals computed, `annualized_yield` calculated.

The key property: **OMS doesn't know about wheels.** It moves orders through PENDING→FILLED. The wheels engine subscribes to OMS state changes (via polling `reconcile_orders` results) and mutates `wheel_states` accordingly. This preserves the option to plug in a third strategy later without coupling.

### 2.8 Config Schema — YAML, two files

**`config.yaml`** (account-level, hand-edited):

```yaml
account:
  broker: alpaca
  mode: paper                   # paper | live
  account_id: WHEELS-PAPER-1    # used in idempotency keys
  total_capital_usd: 500000
  cash_reservation_pct: 0.70    # max % of cash committed to open CSP collateral

schedule:
  morning_cycle_et: "09:45"
  intraday_interval_min: 15
  eod_cycle_et: "16:15"
  watchlist_refresh_day: sunday
  watchlist_refresh_time_et: "18:00"

risk:
  per_ticker_max_pct: 0.08              # see §3
  sector_max_pct: 0.25                  # 0.35 for Tier 1
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
    pattern: council                     # 5-model propose + blind-score
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
  market_outlook:                        # daily regime read
    enabled: true
    pattern: single
    model: "anthropic/claude-sonnet-4-7"
  rescue_decide:
    enabled: true
    pattern: council_vote                # 5-model propose + vote (no blind-score)
    models: [...]                        # same 5
  code_screener:
    enabled: true                        # pure code, no LLM call
  # Explicit NO-LLM list: strike/DTE/delta picks, order quantity, profit-take, stop-loss.

reporting:
  email_to: "tianyuw@icloud.com"
  ses_region: us-east-1
  push_notify_url: "https://ntfy.sh/wheels-copilot-tianyu"   # for critical events
  include_cycle_log: true
  include_cost_basis_history: false

dry_run: false                 # global kill switch
```

**`tickers.yaml`** (seed only, ingested once):

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

Once seeded, the **watchlist DB table is the authority**. YAML is bootstrap / archival only.

---

## 3. Trading Parameters & Risk Management

All defaults below are **conservative** because this is real $500K. Every parameter is config-tunable, but defaults are what we ship on day 1.

### 3.1 Capital Allocation Breakdown — $500,000

| Bucket | % | $ | Notes |
|---|---|---|---|
| **Hard cash reserve (untouchable)** | 20% | $100,000 | SGOV / cash. Never reservable for CSP. Buffers margin calls, broker glitches, rescue capital. |
| **Active CSP cash reservation (max)** | 70% | $350,000 | Hard cap on total reserved cash backing open short puts. |
| **Working buffer** | 10% | $50,000 | Settling cash, premium float, in-flight orders, fees. |
| **Total** | 100% | $500,000 | |

**Per-ticker max exposure:** **8% of account = $40,000** notional (across all open CSPs + assigned shares marked to strike). At $40K cap, a 50% gap-down on one ticker = 4% account hit. Survivable. Exception: SPY/QQQ get a **$60K (12%)** cap because no idiosyncratic risk.

**Target concurrent positions: 10** (upper end of research's 8–10 range):
- 10 × $35K avg = $350K = exactly the cap
- 10 names = better idiosyncratic diversification than 8
- Below 10 → per-position exposure creeps up; above 12 → earnings-conflict probability escalates

**Sector concentration:**
- Max **25%** of deployed capital per GICS sector
- Tier 1 sectors (mega-cap index + dominant tech) may go to **35%**
- Max **2 names per sub-industry** (no AMD + NVDA at once)

**Example fully-deployed snapshot:**

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
| | | | **Total** | **$386,500** | |

(Engine scales back the most expensive name's contracts to land ≤$350K. Cash idle ~$150K = 30%.)

### 3.2 Default Trading Parameters

| Parameter | Default | Range | Rationale |
|---|---|---|---|
| Put delta (CSP entry) | **-0.22** | -0.15 to -0.30 | Conservative end of research's 0.20-0.30 sweet spot. ~78% OTM probability. |
| Call delta (CC entry) | **+0.25** | +0.20 to +0.35 | Slightly higher than CSP — once assigned, want realistic chance of being called away. |
| DTE target (entry) | **35** | 28-45 | Center of research's 30-45 range. Avoids <21 DTE gamma zone. |
| Profit take | **50% of credit** | 40-60% | tastytrade's 200K-trade study. Close at 50% captured (unless <7 DTE, let it expire). |
| Time stop | **21 DTE** | 18-25 | Research default. Extend to 14 DTE for IVR ≥50, tighten to 25 for IVR ≤35. |
| Stop loss | Short delta ≥ 0.65 OR debit-to-close ≥ 2× credit | n/a | Two redundant triggers; stocks can blow through strikes faster than either alone fires. |
| Roll trigger | Strike breached AND short delta ≥ 0.50 AND ≤21 DTE | All three must hold | Avoids reactive rolling on noise. |
| Max rolls | **2** per position | hard cap | Research default. Third decision: take assignment / close / rescue. |
| IV rank | **25 ≤ IVR ≤ 65** | hard | <25 too cheap to compensate tail risk; >65 signals event being priced in. |
| Bid-ask spread | ≤ 5% of mid | hard | Slippage |
| Open interest | ≥ 500 at target strike | hard | Tighter than research's 100 — at $500K we may need to roll size. |
| Min credit | ≥ 0.5% strike (CSP), ≥ 0.4% strike (CC) | hard | Floor on "worth the contract slot." |

### 3.3 Ticker Pool

**Tier 1 — Safe Core (7 names, ~60% of deployed):**

| Ticker | ~Price | Reserve / Contract | Why |
|---|---|---|---|
| SPY | $580 | $58K | S&P 500 ETF, deepest options market |
| QQQ | $470 | $47K | Nasdaq 100, complements SPY |
| IWM | $230 | $23K | Russell 2000, small-cap diversification |
| AAPL | $230 | $23K | Highest-quality mega-cap |
| MSFT | $420 | $42K | Predictable cash flows |
| GOOGL | $180 | $18K | Lower share price → granular sizing |
| KO | $70 | $7K | Defensive, low-vol, useful in VIX-elevated regimes |

**Tier 2 — Quality Growth (8 names, ~40%):**

| Ticker | ~Price | Reserve / Contract | Notes |
|---|---|---|---|
| AMZN | $200 | $20K | Mega-cap consumer disc. |
| META | $580 | $58K | Single-contract sizing |
| NVDA | $140 | $14K | Volatile but quality, one contract MAX |
| AMD | $160 | $16K | Semi diversification — never both at once |
| JPM | $230 | $23K | Financials anchor |
| V | $290 | $29K | Payments stability |
| XOM | $115 | $11.5K | Energy diversifier |
| UNH | $550 | $55K | Healthcare anchor |

**Hard exclusions (never wheel):**
- Meme / momentum (GME, AMC, TSLA in extreme-IV regimes)
- Pre-revenue biotech / pending-FDA-catalyst within 90 days
- Recent IPO (<12 months public)
- Below $20 share price
- 3x leveraged ETFs (TQQQ, SQQQ, SOXL...)
- Single-commodity ETFs (USO, UNG — contango eats you)
- Market cap < $20B in v1
- Within 60 days of major catalyst (M&A vote, antitrust ruling, etc.)

### 3.4 Hard Pre-Trade Gates (CSP entry)

Code-enforced, not LLM-debatable. **Any** of these → reject:

| # | Gate | Threshold |
|---|---|---|
| G1 | Earnings | Underlying earnings ≤ ±7 calendar days from entry or expiry |
| G2 | Macro | FOMC / CPI / NFP / PCE within ±2 days |
| G3 | VIX | >28 freeze; 22-28 Tier 1 only; ≤22 both tiers |
| G4 | IV rank | <25 or >65 |
| G5 | Liquidity (OI) | <500 at target strike |
| G6 | Liquidity (spread) | >5% of mid |
| G7 | Liquidity (volume) | 20-day avg option volume <200 |
| G8 | Per-ticker singularity | Existing CSP/CC/shares on same ticker |
| G9 | CSP cash reservation cap | Would exceed $350K total reserved |
| G10 | Sector concentration | Would push sector >25% (35% for Tier 1) |
| G11 | Sub-industry conflict | Same sub-industry already open |
| G12 | Per-ticker exposure | Reserve required >$40K ($60K for SPY/QQQ) |
| G13 | Stale data | Quote / chain >5 min stale |
| G14 | Watchlist | Ticker not ACTIVE in watchlist |
| G15 | Open rescue review | Ticker has unresolved rescue flag |
| G16 | Daily entry cap | >3 new CSPs today already |
| G17 | Drawdown brake | Account in circuit-breaker state |
| G18 | Broker health | Alpaca health monitor degraded |

### 3.5 Assignment / Bag-Holder Management

On assignment:
1. Cost basis = strike − total net premium received over the wheel cycle (CSP credits − roll debits).
2. Reserved cash → shares; sector + ticker exposure recomputed.
3. CC proposed *only* if escalation tier permits (below).

**Hard rule:** Any CC strike `K` must satisfy `K ≥ cost_basis`. Code-enforced. Single most important line in the system.

**Tiered escalation by drawdown from cost basis:**

| Tier | DD | Action |
|---|---|---|
| **T0 Normal** | 0% to -5% | Standard CC: delta 0.25, DTE 30-45, K ≥ basis |
| **T1 Watch** | -5% to -10% | Lower delta to 0.15-0.20, K = max(basis, current × 1.05). Daily log flag. |
| **T2 Stress** | -10% to -20% | **Pause auto-CC.** Open rescue review ticket. LLM rescue evaluates: hold? Deep-OTM far-DTE CC (delta <0.10)? Add to lower basis? |
| **T3 Critical** | -20% to -30% | **All automation suspended on ticker.** Forced LLM rescue review. Three options to human. |
| **T4 Forced exit** | <-30% | Human review within 5 days. Default if no input: TWAP exit over 3 days. Ticker removed from watchlist 90 days minimum. |

**Forced exit (regardless of tier):** underlying <$15/share, bankruptcy announcement, audit failure, position held >365d without recovery to within -10%, market cap drops below $5B.

### 3.6 Portfolio-Level Circuit Breakers

| Breaker | Trigger | Action | Resume |
|---|---|---|---|
| **CB1 Daily P&L** | day P&L < -1.5% equity (~$7.5K) | Freeze new entries 24h | Auto next day |
| **CB2 Weekly P&L** | 5d rolling P&L < -4% (~$20K) | Freeze new; tighten TP to 30% on existing | Manual unlock |
| **CB3 Drawdown** | peak-to-trough DD ≥ 8% (~$40K) | Halt new; close CC at first profit; protect cash | Manual + DD <5% sustained 5d |
| **CB4 Catastrophic DD** | DD ≥ 15% (~$75K) | Full halt; no new options; preserve shares only | Mandatory full system review |
| **CB5 Broker health** | API error >10% in 10min OR >5 consecutive 5xx | Read-only mode | Auto after 30min clean |
| **CB6 Data staleness** | Quote / IV / earnings >4h stale | Skip cycle | Auto on fresh data |
| **CB7 Position drift** | Broker vs DB mismatch >1 cycle | Suspend new entries | Auto on reconcile |
| **CB8 Order velocity** | >8 new orders in single day | Block further entries that day | Auto next day |

**Auto-resume** for infrastructure problems (CB5/6/7); **manual unlock** for capital problems (CB2/3/4). Capital breaches mean something is wrong with the framework, not the connection.

### 3.7 Realistic Return Expectations ($500K Base Case)

Honest, not aspirational:

| Metric | Base | Stretch | Stress |
|---|---|---|---|
| Monthly premium captured | 0.6-0.9% of equity ($3-4.5K) | 1.0-1.3% ($5-6.5K) | 0.2-0.4% ($1-2K) |
| Gross annualized premium | 8-11% | 12-15% | 3-5% |
| **Annualized total return (net)** | **6-9%** | 10-13% | -3% to 0% |
| Max drawdown | 6-10% | <5% | 12-18% |
| Assignment rate (% of CSPs) | 15-25% | 10-15% | 30-40% |
| Cycles per ticker per year | 6-10 | 10-14 | 3-5 |
| Win rate on CSPs (closed at profit) | 75-82% | 82-88% | 65-72% |

**Honesty calibrations:**
- 6-9% net is **below** S&P long-term ~10%. By design — trading return for path predictability + lower DD.
- In strong bull (SPY +20%), we underperform dramatically (CC caps capped). Structural cost of wheel.
- In sharp bear (SPY -25%), we likely lose but **less** than buy-and-hold thanks to premium cushion + diversification.
- "0.6-0.9% / month premium captured" is **after** TPs, roll debits, losing CSPs going to assignment. Gross theta is much larger; net is what matters.
- **Sharpe target: 0.8-1.2** vs SPY ~0.5-0.7 — this is the actual "edge" we're aiming for.

---

## 4. LLM & AI Integration

**Core principle:** the deterministic layer (state machine, gates, sizing, order builder) is *complete* on its own. The LLM layer is enrichment, never a critical-path dependency. Comment out every LLM call and the bot still wheels (using last week's watchlist + "normal" regime assumption).

### 4.1 LLM Touchpoints Inventory

Six touchpoints. Four user-mandated, two are evaluations.

#### 4.1.1 Regime read — `market-outlook` (daily, single-model)
- **Decision:** Is today risk-on / normal / caution / risk-off / crisis? Output controls global `new_entries_allowed` gate + `size_multiplier`.
- **Frequency:** Daily, 08:30 ET pre-market.
- **Input:** VIX + 5/20d change, SPY/QQQ returns, term structure, DXY, 10Y yield + 2s10s, FRED macro releases today, Massive macro news 24h, regime tags from `vol_features`.
- **Output schema:**
  ```json
  {"regime": "normal|caution|risk_off|crisis",
   "confidence": 0.0-1.0,
   "new_entries_allowed": bool,
   "size_multiplier": 0.0|0.5|1.0,
   "reasoning": "...", "key_drivers": ["..."], "expires_at": "ISO"}
  ```
- **Why LLM:** No rule distinguishes "VIX 22 on idiosyncratic noise" from "VIX 22 on credit spreads widening." Cross-signal synthesis.
- **Model:** Single **Sonnet 4.7**.
- **Cost:** ~$0.02/day → **~$0.50/month**.

#### 4.1.2 Watchlist curation — `watchlist-curate` (weekly, 5-model council)
- **Decision:** Given screener shortlist, produce ranked watchlist of 12-20 tickers with `core/satellite/probation/drop` tiers.
- **Frequency:** **Weekly, Sunday 18:00 ET.** Stable by design (research §5/§9.4 — "watchlist is the policy, not the trade").
- **Input:** Per-candidate dossier — 60-day price action, IV rank/percentile/term, fundamentals snapshot (Finnhub), next earnings + last 4 surprises, dividend schedule, options liquidity, recent UW flow, 10-K/10-Q summary, Massive news 14d, current portfolio, **previous watchlist + last week's reasoning** (continuity).
- **Output schema:**
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
- **Why LLM:** Watchlist composition IS the question "would I as a long-term investor own this at the strike?" — pure qualitative business-quality. Research §2.1: only wheel stocks you'd hold 2+ years.
- **Model:** **5-model council, propose-then-blind-score** — reuse `options-copilot`'s exact pattern. Each model independently ranks; proposals anonymized and re-scored by same 5; final `core` tier requires ≥3/5 agreement.
- **Cost:** ~$3-6/run × 4 runs/mo = **~$15-25/month**.

#### 4.1.3 Fundamentals deep-dive — `fundamentals-deepdive` (monthly per ticker, single Opus)
- **Decision:** Per-ticker fundamentals rubric, consumed by watchlist-curate council as evidence.
- **Frequency:** Monthly per watchlist ticker; on every earnings event (event-triggered); on every new screener-shortlist entry.
- **Input:** 10-K + latest 10-Q (EDGAR), latest earnings transcript, consensus analyst estimates (4-quarter), revisions trend, balance sheet ratios, sector peer comparison (3-5 peers).
- **Output schema:** `business_quality(1-10)`, `balance_sheet_strength(1-10)`, `earnings_quality(1-10)`, `valuation_vs_peers(cheap/fair/rich)`, `red_flags(str[])`, `competitive_moat`, `wheel_holdability(1-10)`, `summary`.
- **Why LLM:** Reading 10-K → one-page rubric is canonical LLM task.
- **Model:** Single **Opus 4.7** — long-context document synthesis specialty. No council; output is evidence weighed by downstream council.
- **Cost:** ~$0.40/ticker × 40 ticker-events/month = ~$16/month uncached; **~$8/month with quarterly cache.**

#### 4.1.4 Outlook analysis — `outlook-analysis` (weekly, single Sonnet)
- **Decision:** Forward-looking thesis per watchlist ticker: 1-3 month outlook, catalyst calendar, asymmetric-risk flags.
- **Frequency:** Weekly (input to watchlist-curate). Plus event-triggered on >10% single-session move.
- **Input:** Recent news cluster (Massive 14d), analyst rating changes (Finnhub), upcoming catalysts, unusual flow (UW), sector momentum, technicals (RSI, dist from SMA200, ATR).
- **Output schema:** `directional_lean(bullish/neutral/bearish)`, `catalyst_calendar[]`, `iv_compression_risk`, `tail_risk_flags`, `recommend_pause(bool, reason)`.
- **Why LLM:** Synthesizing news + flow + technicals into forward thesis is judgment.
- **Model:** Single **Sonnet 4.7**.
- **Cost:** ~$0.08/ticker × 20 watchlist tickers/week × 4 weeks = **~$6-8/month**.

#### 4.1.5 Bag-holder rescue — `rescue-decide` (event-triggered, 5-model vote)
- **Trigger:** Assigned ticker drops >15% below basis, OR ASSIGNED state >60 days without successful CC monetization, OR CC roll-count hit 2.
- **Frequency:** Event-triggered. Steady state: 0-3/month.
- **Input:** Full position history, current basis vs market, available CC strikes at ≥basis, time held, latest fundamentals, sector + macro context, chains for protective puts / collar candidates.
- **Output schema:** Pick exactly one of `{hold_and_wait, sell_cc_at_basis, sell_cc_below_basis_for_premium, buy_protective_put, convert_to_collar, take_loss_close, escalate_to_human}` + reasoning + estimated P&L impact.
- **Why LLM:** The one place research §5 explicitly endorses LLMs. High-context, multi-option, poor base rates for any single rule.
- **Model:** **5-model propose-and-vote** (no blind-score — outputs are categorical, not continuous). Majority wins. Tie → `escalate_to_human`.
- **Cost:** ~$2-4/invocation. Expected **<$10/month**.

#### 4.1.6 Daily "should we trade today" → folded into 4.1.1
Already handled by regime read. Separate gate would be redundant + add latency.

#### 4.1.7 Strike / DTE / delta picking — **NO**
Research §5 + `options-copilot`'s validation history: LLMs unreliable on numerical boundaries. These ALL live in `config.yaml` + deterministic gate engine. LLM never proposes a number it doesn't read from structured input.

### 4.2 Screener + LLM Combination Workflow

Cheap-first cascade (mirrors `wheel-it`'s 4-stage pipeline + `options-copilot`'s data-enrichment):

```
Step 1 — CODE SCREENER  (cost: ~$0)
   Universe: S&P 500 + NASDAQ 100 + curated mid-caps (~700 names)
   Filters:
     1a. Liquidity: avg volume >2M, market cap >$5B
     1b. Options: front-month OI >500 on 30-DTE 0.25Δ strikes
     1c. IV rank: 20 ≤ IVR ≤ 70
     1d. Earnings clear: no earnings within 14 days
     1e. Sector exclusion: bio, cannabis, OTC, recent IPO (<2y)
     1f. Price: $20 ≤ last_close ≤ $400
   → ~30-60 candidates

Step 2 — DATA AGGREGATION (cost: API calls only)
   Per candidate: Alpaca + Finnhub + EDGAR + Massive + UW + FRED
   → dossiers (~50K tokens each)

Step 3 — LLM EVALUATION
   3a: fundamentals-deepdive (Opus, single) — only on the ~15 not cached this quarter
   3b: outlook-analysis (Sonnet, single) — all ~30-60 candidates
   3c: watchlist council propose + blind-score on combined dossiers

Step 4 — SYNTHESIS & PERSISTENCE
   - Council majority → tier
   - Compare vs existing watchlist:
       core → drop      = probation 1 week (no force-close, no new entries)
       drop → promote   = satellite first, never straight to core
       new → core       = require unanimous 5/5
   - Write workspace/YYYY-WW/ audit + DB watchlist update
```

**Cadence: weekly, Sunday 18:00 ET.** Daily is wasteful; bi-weekly is too slow for earnings season.

### 4.3 Prompt Design Principles

**Always include in every LLM prompt (standard context block):**
1. Account state — cash, equity, deployed % in CSPs, % in shares.
2. Open positions — every CSP/CC, strike, DTE, P&L, days held.
3. Current watchlist — full tier list + last reasoning blob.
4. Recent performance — trailing 30/90d realized P&L, assignment count, avg cycle time, biggest DD.
5. System config snapshot — delta range, DTE range, profit-take %, hard rules (so LLM knows what code enforces).
6. Date, market regime, VIX, recent macro releases.
7. **Explicit "you may not propose" list** — stated up front in system prompt.

**Never ask the LLM for:**
- Strikes, DTEs, deltas, roll-trigger thresholds, profit-take %, stop-loss % — anything that's a number consumed by the order builder.
- Order quantity (comes from `WheelRiskBudget`).
- "Should we trade ticker X today" — deterministic gate's job.
- Roll decisions when rules give a clean answer.

**Structured output enforcement:**
- Pydantic schema per touchpoint in `schemas/llm_outputs.py`.
- Prompt ends with `Return ONLY valid JSON matching this schema: {schema_as_text}`.
- `openrouter.parse_json_from_llm` (5 fallback strategies from `options-copilot`) front line.
- On parse fail: 1 retry at temp=0.0 with "Your previous response was invalid JSON" appended. Second fail → mark touchpoint degraded, fall back.
- Hallucinated tickers post-parse validated against screener-eligible set. Unknown dropped + logged.

**Council pattern matrix:**

| Touchpoint | Pattern | Rationale |
|---|---|---|
| Watchlist curate | 5-model propose + blind-score | Highest stakes; weekly policy; cost asymmetry favors consensus |
| Rescue decide | 5-model propose + vote (no blind-score) | Categorical output; consensus prevents single-model overconfidence in disaster paths |
| Fundamentals deepdive | Single Opus | Evidence, not verdict; weighed by council downstream |
| Outlook analysis | Single Sonnet | Modulating signal; doesn't execute |
| Regime read | Single Sonnet | Daily latency-sensitive; rules already gate worst cases |

### 4.4 Cost Management

**Steady-state monthly LLM cost:**

| Touchpoint | Freq | $/run | $/mo |
|---|---|---|---|
| Regime read (Sonnet) | 22 trading days | $0.02 | ~$0.50 |
| Watchlist council (5×) | 4-5/mo | $4 | ~$18 |
| Fundamentals (Opus) | ~40 ticker-events | $0.40 | ~$16 (cached: ~$8) |
| Outlook (Sonnet) | ~80 ticker-events | $0.08 | ~$7 |
| Rescue council | 0-3/mo | $3 | ~$5 |
| **Total** | | | **~$40-50/month** |

vs expected $3-8K/month gross premium → cost is <2% of revenue. Not a binding constraint.

**Caching strategy** (huge multiplier):
- Fundamentals output per (ticker, latest_10Q_filing_date) — quarterly. $1.60/ticker/year vs $19.20 uncached.
- EDGAR 10-K/10-Q excerpts at adapter layer for 90 days.
- Outlook daily cache (same prompt within a day = cache hit).
- **Anthropic prompt caching** (`cache_control: ephemeral`) on long-context fundamentals — static blocks cached, ~75% input token savings on council scoring phase.

### 4.5 Failure Modes & Guardrails

| Failure | Detection | Fallback |
|---|---|---|
| Hallucinated ticker | Post-parse validator vs screener-eligible | Drop unknown; if >25% invalid, mark run degraded, revert to last good watchlist |
| Malformed JSON | `parse_json_from_llm` → None | Retry at temp=0 + reformat; 2nd fail → mark touchpoint degraded |
| LLM confidently wrong watchlist | Council requires ≥3/5 for `core`, unanimous 5/5 for new entries | Dissent auto-demotes to `satellite` |
| LLM slow / timeout | 180s timeout + 2 retries in `openrouter.py` | Daily flow: regime defaults to last good for 4h, else `risk_off`. Weekly: keep prior watchlist, retry next morning. |
| Full LLM outage | OpenRouter health monitor | `llm_degraded` mode: all touchpoints use last-good cached. **Trading continues deterministically** against existing watchlist. |
| "Drop all positions" panic output | Change-rate-limit validator: max 25% of watchlist may change tier per weekly run | Excess changes queued for next week |
| 5-way disagreement on rescue | No majority → forced `escalate_to_human` (critical email) | No auto action; bag-holder freeze remains |

### 4.6 Data Inputs — Adapter Ecosystem

**Reuse from options-copilot:**
- Alpaca (chain, prices, technicals)
- Finnhub (fundamentals, earnings, estimates)
- FRED (macro)
- Unusual Whales (flow, IV percentile)
- EDGAR (10-K / 10-Q)
- Massive (news)
- yfinance (fallback)

**New adapters needed:**

1. **Earnings transcripts** — `adapters/transcripts.py`. **API Ninjas** ($10-50/mo) to start; upgrade to Seeking Alpha API ($199/mo) if coverage hurts watchlist quality. Needed for M4.
2. **Web fetch** — `adapters/web_fetch.py`. Generic httpx + readability for URLs surfaced by news/filings. Cap 5 fetches per LLM run, 30-day cache. Needed for M4.
3. **Analyst revisions trend** — DEFER. Finnhub has estimates but thin revisions. Tipranks/Visible Alpha ($200+/mo) overkill.
4. **Insider transactions** — already in EDGAR adapter (Form 4).
5. **Sector ETF momentum** — derive from Alpaca bars for SPDR sector ETFs. No new adapter.

**Total incremental data cost: ~$10-50/month.** Combined AI layer: ~$60-100/month, <2% of expected gross premium.

---

## 5. Forward Test & Go-Live Plan

10 weeks paper on Alpaca + 6 weeks live capital staircase.

### 5.1 Phased Rollout

#### Phase 1 — Single Ticker, Pure Mechanical (Weeks 1-2, 2026-06-16 → 2026-06-29)
- **Scope:** 1 ticker (SPY), 1 contract max, no LLM. Hard-coded watchlist. All entry/exit deterministic.
- **Entry:** M0-M2 complete; one manual end-to-end CSP→assign→CC→called-away dry run; Alpaca paper creds in `.env`; heartbeat updating; daily report email lands.
- **Exit:** at least 1 full cycle completed without manual intervention; 0 orphaned orders (PENDING/SUBMITTED >10min); 0 reconciliation discrepancies; daily report ≥8/10 days.

#### Phase 2 — 5 Tickers, Still Deterministic (Weeks 3-4, 2026-06-30 → 2026-07-13)
- **Scope:** SPY, QQQ, AAPL, MSFT, GLD. Static watchlist. LLM still off. Per-ticker risk budget enforced. ≤1 contract per ticker.
- **Entry:** Phase 1 gates green; risk budget config validated; sector-concentration unit-tested.
- **Exit:** all 5 entered ≥1 CSP; ≥2 of 5 completed full cycle; 0 portfolio CBs tripped; reconciliation passes 10 consecutive days; fill slippage <5% vs theoretical mid.

#### Phase 3 — Full Watchlist + LLM (Weeks 5-8, 2026-07-14 → 2026-08-10)
- **Scope:** 8-12 tickers, LLM watchlist curation Sun 18:00, LLM rescue on triggers, regime read once per cycle.
- **Entry:** Phase 2 gates green; LLM prompts checked into repo with hash; OpenRouter live; `model_outputs` logging every call with cost; LLM fallback path verified.
- **Exit:** ≥4 full cycles completed across watchlist; LLM-curated watchlist outperforms frozen Phase-2 baseline on premium yield over 4 weeks; 0 rescue decisions user would override on review; LLM cost ≤$30/week.

#### Phase 4 — Stress + Edge Cases (Weeks 9-10, 2026-08-11 → 2026-08-24)
- **Scope:** No new features. Inject §5.4 stress scenarios + review naturally-occurring events from Phases 1-3.
- **Entry:** Phase 3 gates green; 0 critical bugs in last 14 days.
- **Exit:** all 9 stress scenarios occurred (natural or injected) and were handled correctly; all §5.3 go-live gates green.

If any phase fails its exit, **repeat for one extra week**. Total slack: 2 weeks. Beyond week 12 → re-plan.

### 5.2 Daily Metrics Tracked

**Account (every cycle, retained forever):**
- equity, cash, buying_power, margin_used (from `account_snapshots`)
- `cash_reserved_for_csp` — sum of (strike × 100 × contracts) for open CSPs
- day_pnl, cumulative_pnl, cumulative_pnl_pct

**Per open position (daily snapshot):**
- delta, theta, gamma, iv (from chain)
- dte, days_held, unrealized_pnl, pct_to_profit_target
- For shares: cost_basis, current_price, pct_vs_basis, cc_eligible

**Per cycle (one row in `cycle_log`):**
- ticker, started/ended_at, cycle_time_days
- total_premium_collected, realized_pnl, unrealized_pnl_at_end
- n_csps, n_assignments, n_ccs, n_rolls
- outcome: csp_closed | called_away | still_holding | force_closed
- annualized_yield, win

**Daily aggregate (`wheel_daily_metrics`):**
- assignment_rate (rolling 30d)
- avg_cycle_time_days (rolling 30d)
- avg_premium_yield_per_csp_pct
- win_rate_cycles
- max_drawdown_pct (cumulative from any peak)
- max_single_ticker_drawdown_pct

**System health:**
- missed_runs_count (30d)
- api_error_rate (rolling 1h)
- avg_cycle_latency_seconds
- reconciliation_drift_count (must be 0 daily)

**LLM:**
- llm_calls_today, llm_cost_usd_today, llm_cost_usd_week
- llm_p95_latency_ms, llm_failure_rate
- llm_watchlist_premium_yield_vs_baseline (weekly)

**Retention:** all snapshots forever in SQLite. `model_outputs.prompt_text` retained 90 days then truncated to hash.

### 5.3 Go-Live Decision Gates

All thresholds measured against **last 4 weeks of paper data** (Weeks 7-10). All must be GREEN before live.

**System reliability:**
- 0 critical bugs (data loss, wrong-direction order, ghost position) in last 28 days
- <2% missed daemon runs (≥98% of expected morning + EOD cycles completed `ok`)
- 0 reconciliation drift events unresolved >1 trading day
- Heartbeat: `daemon_status.last_heartbeat` never older than 6 hours during market-open windows

**Performance:**
- Cumulative P&L net of (commissions × 1.5) >0 over full 10-week test
- Cycle win rate ≥60% across ≥8 completed cycles
- Max drawdown <5% of starting equity ($25K on $500K)
- Annualized premium yield (paper) ≥8% — below this, strategy doesn't pay for operational risk

**Risk:**
- 0 breached portfolio circuit breakers in Weeks 7-10
- 0 CC orders attempted at strike below cost basis
- 0 manual interventions required in Weeks 9-10

**LLM quality:**
- LLM-curated watchlist produces ≥5% higher premium yield than frozen baseline (over Weeks 5-10, 4-week min sample)
- 0 rescue decisions user would have overridden on weekly review
- LLM cost ≤$30/week

**Observability:**
- Daily report email delivered ≥48/50 trading days
- Workspace JSON audit trail present and parseable 100% of trading days
- Push notification arrived within 5 min for every injected stress event in Phase 4

### 5.4 Stress Test Scenarios

| # | Scenario | How to simulate | Expected behavior |
|---|---|---|---|
| 1 | Earnings beat/miss with CSP open | Natural (occurs every 4-12 wk per ticker) | G1 should prevent. If shifted earnings → force roll-out or close 2 days before. |
| 2 | Sudden VIX spike | Wait for natural; if none by Wk 9, inject by editing VIX cache to 32 for one cycle | Freeze new entries (G3); existing held; regime LLM confirms |
| 3 | Multi-day drawdown | Natural | CB1 fires → 24h freeze |
| 4 | Broker API outage | Inject Wk 9: Alpaca client returns 503 for 30min | Read-only mode; no orders; push alert; resume on recovery; reconcile rerun |
| 5 | LLM outage | Inject: kill API key for one full cycle Wk 9 | Mechanical path continues; rescue skipped (logged); cycle completes |
| 6 | Pin risk at expiration | Synthetic CSP one day before expiry within 0.5% of strike | Detect pin risk in morning; roll-out OR accept assignment explicitly; never ambiguous |
| 7 | Missed/delayed assignment notification | Skip morning reconcile one Monday; resume Tuesday | Tuesday reconcile detects, updates DB, computes basis, next cycle opens CC; alert |
| 8 | Cost basis miscalc (dividend / split) | Ticker with upcoming dividend (real); simulate split if needed | Basis adjusts; CC strike floor uses adjusted basis. Split → manual review flag |
| 9 | Multiple assignments same day | Inject: 3 deep-ITM CSPs expire same Friday on 3 tickers | All 3 processed Monday morning; no race in `positions`; portfolio limits enforced |

1-3 should occur naturally in 10 weeks; 4-9 require injection. Each gets a script under `scripts/stress/`.

### 5.5 Observability

**Daily email report (EOD, 16:15 ET after EOD cycle).** Reuse multipart HTML from `options-copilot/skills/daily-report/scripts/report.py`. Wheel-specific sections:
- **Wheel state board** — one row per ticker: state, DTE, P&L, days in state
- **Cycle progress** — per active cycle: days elapsed, premium so far, expected vs actual
- **Watchlist health** — candidates passing all G1-G18 today; if 0, why
- **Cost basis report** — every share position, basis vs current, cc_eligible flag
- **LLM activity & cost** — today's calls, weekly running total, baseline yield comparison

**Push / Slack notifications (ntfy.sh or Slack webhook).** Same-day human attention required:
- Broker API in read-only mode
- Any portfolio circuit breaker tripped
- Single ticker >20% unrealized loss (rescue trigger)
- Reconciliation drift >1 trading day
- LLM rescue recommends "force close" (advisory)
- Daemon heartbeat >6h stale during market hours
- Daily report **failed** to send

**Workspace JSON audit trail.** Same pattern as `options-copilot/workspace/YYYY-MM-DD/`. Per day: `cycle_input.json`, `entry_decisions.json`, `exit_decisions.json`, `llm_calls/*.json`, `reconcile.json`, `daily_report.{html,json}`, `metrics.json`. Per ticker subdir: proposal/review for the day.

**Dashboard.** Web UI skipped for v1. Daily email + `scripts/status.py` (CLI prints wheel state board on-demand) is enough.

### 5.6 Paper → Live Rollout Staircase

| Stage | Capital | Tickers | Duration | Gate to next |
|---|---|---|---|---|
| **Live-α** | $50K | 2 (SPY, QQQ) | 2 wks | All §5.3 gates green on live; 0 surprises vs paper; user approval |
| **Live-β** | $150K | 4 (add AAPL, MSFT) | 2 wks | Live P&L positive net real fees; reconciliation clean; slippage <20% vs paper. User approval |
| **Live-γ** | $300K | 6-8 (full core) | 2 wks | No CBs tripped; LLM behavior matches paper. User approval |
| **Live-1.0** | $500K | full LLM-curated (8-12) | open | Steady state |

**Side-by-side paper + live for Live-α + Live-β (4 weeks).** Paper keeps running with *same code*, *same watchlist*, *same prompts*. Daily report has "paper vs live divergence" diff. >1% daily P&L divergence without an explainable cause → halt and investigate. After Live-β, paper downgraded to passive monitor.

**Approval gate:** user signs off in writing (one-line yes/no in response to weekly review email) before each bump. Default = **stay**, not **advance**. Silence ≠ approval.

### 5.7 Weekly Review Checklist (Friday EOD, ~20-30 min)

**Performance vs baseline:**
- WoW cumulative P&L direction + magnitude
- Cycle win rate this week vs prior
- Premium yield annualized vs 8% floor
- Max DD intra-week vs 5% hard limit

**Per-ticker health:**
- Any ticker >10% unrealized loss → eyeball basis + cc_eligible
- Any cycle >60 days → flag as stuck
- Any ticker with 0 trades this week → why?

**System health:**
- `job_runs` for the week: any errors or missing rows?
- Heartbeat gaps >6h during market hours?
- Reconciliation drift unresolved?
- API error rate

**LLM quality:**
- Read this week's watchlist proposal. Override any?
- Read any rescue calls. Override?
- Cost vs $30/week budget

**Stress scenarios:**
- Any §5.4 natural events occurred? Handled correctly?
- Phase 4: all injected scenarios run?

**Decision tree:**
- **All green** → advance to next phase / next stage
- **One yellow** → continue but file issue; revisit next Friday
- **Two yellow OR one red** → halt new entries, let positions wind down, fix, repeat same phase one extra week
- **Live phase + any red** → halt, do not advance capital, push notify

---

## 6. Engineering Milestones

Maps to forward-test phases. Engineering work happens BEFORE Phase 1 of paper testing.

### M0 — Shared Layer Bootstrap (Week -4 to -3, 2026-05-19 → 2026-06-01)
**Deliverables:**
- `wheels_copilot/shared/` vendor-copied from `options-copilot` per §2.3 module table
- `scripts/sync_shared.sh` diff tool
- `SHARED_PROVENANCE.md` with git SHAs
- `pyproject.toml`, `.env` template, `.gitignore` (already done)
- `wheels_daemon.py` skeleton (boots APScheduler, jobs no-op)
- `db/schema.py` + `scripts/init_db.py` — new tables created
- CI workflow (lint, unit tests)

**Exit:** daemon boots, heartbeats, DB initializes; tests pass.

### M1 — Wheel State Machine + Single-Ticker MVP (Week -3 to -2, 2026-06-02 → 2026-06-08)
**Deliverables:**
- `engines/wheel_state_machine.py` + `transitions.py` (hand-rolled FSM)
- `engines/csp_selector.py` (deterministic strike/DTE per `config.yaml` defaults)
- `engines/cc_selector.py` (with cost_basis floor enforced)
- `engines/wheel_exit_plan.py` (50% TP, 21 DTE, delta 0.65 stop)
- `engines/wheel_gates.py` (G1-G18 hard gates)
- `engines/assignment_lifecycle.py`
- `engines/risk_budget.py`
- `schemas/wheel_position.py`, `wheel_state.py`, `decisions.py`
- `skills/wheel-cycle/` (per-ticker dispatcher, no LLM yet)
- Daemon's morning/intraday/EOD cycles wired to FSM
- `tests/unit/` for FSM transitions, gates, selectors
- `tests/integration/` full-cycle on synthetic broker stub
- `scripts/dry_run_cycle.py` (full cycle, no orders submitted)

**Exit:** can drive single-ticker SPY end-to-end CSP→assign→CC→called-away→cash on synthetic broker; all unit + integration tests pass.

### M2 — Multi-Ticker + Watchlist (Week -2 to -1, 2026-06-09 → 2026-06-15)
**Deliverables:**
- `db/watchlist_repo.py` + seed from `tickers.yaml`
- `engines/portfolio_risk.py` (sector concentration, per-ticker exposure, net delta)
- All G8-G12 multi-position gates wired
- `engines/roll_decider.py` (deterministic roll/close/take-assignment)
- `engines/rescue_engine.py` skeleton (no LLM yet; tier escalation flags T0-T4 only)
- Circuit breakers CB1-CB8 wired
- `scripts/reconcile_alpaca.py` against real paper account
- Alpaca paper API integration end-to-end (real chains, real orders, real fills)
- `skills/daily-report/` wheel-specific sections
- `scripts/status.py` CLI

**Exit:** Phase 1 entry criteria all met. Ready to start paper.

### M3 — Phase 1 Operations (Weeks 1-2 of paper)
No new code. Operate Phase 1. Build M4 in parallel branches.

### M4 — LLM Integration Layer (Weeks 1-3 in parallel with Phase 1-2)
**Deliverables:**
- `shared/adapters/openrouter.py` confirmed working
- `skills/market-outlook/` (Sonnet, daily regime)
- `skills/code-screener/` (4-stage pipeline)
- `adapters/transcripts.py` (API Ninjas)
- `adapters/web_fetch.py` (httpx + readability + 30d cache)
- `skills/ticker-evaluate/` (Opus fundamentals deep-dive)
- `skills/outlook-analysis/` (Sonnet weekly outlook)
- `skills/watchlist-curate/` (5-model council, weekly Sunday)
- `skills/rescue-decide/` (5-model vote, event-triggered)
- `schemas/llm_outputs.py` for all 6 touchpoints
- LLM fallback path tested (kill API key → bot keeps trading)
- `model_outputs` table logging every call with cost

**Exit:** Phase 3 entry criteria met by end of Phase 2.

### M5 — Stress Test Scripts (Week 4-5 in parallel with Phase 2-3)
**Deliverables:**
- `scripts/stress/inject_vix_spike.py`
- `scripts/stress/inject_broker_outage.py`
- `scripts/stress/inject_llm_outage.py`
- `scripts/stress/inject_pin_risk.py`
- `scripts/stress/inject_missed_reconcile.py`
- `scripts/stress/inject_multi_assignment.py`
- Push notification integration (ntfy.sh or Slack)

**Exit:** Phase 4 entry criteria met.

### M6 — Go-Live Prep (Week 10 of paper)
**Deliverables:**
- Live account setup on Alpaca (separate account, not paper)
- `config.yaml` "live" profile reviewed
- Live API keys secured
- Operational runbook `docs/RUNBOOK.md`
- Dry-run on live API (no order submission) for 1 trading day

**Exit:** all §5.3 go-live gates green; user signs off.

### M7-M10 — Live Capital Staircase
- M7: Live-α ($50K)
- M8: Live-β ($150K)
- M9: Live-γ ($300K)
- M10: Live-1.0 ($500K, steady state)

---

## 7. Open Decisions for User

Things the council made provisional calls on, but worth your explicit input before kickoff:

| # | Question | Provisional answer | Why ask |
|---|---|---|---|
| 1 | **Code reuse mechanism** | Vendor-copy now → pip package later | Confirm vs monorepo or pip-package-from-day-1 |
| 2 | **Account type** | Taxable assumed | IRA isn't possible on Alpaca but worth confirming taxable is OK — wheel generates short-term gains taxed as ordinary income |
| 3 | **Watchlist seed** | 15 tickers (7 core + 8 satellite) listed in §3.3 | Want to add/remove any? Personal "would never own" list? |
| 4 | **Push notification channel** | ntfy.sh suggested | OK or prefer Slack / SMS / email-only? |
| 5 | **LLM model menu** | Opus 4.7 + Sonnet 4.7 + GPT-5.4 + Grok 4.20 + Gemini 3.1 + DeepSeek v3.2 | OK with this council mix? Cost-sensitive? Prefer fewer? |
| 6 | **Live-α kickoff capital** | $50K from $500K | Comfortable? Want to start smaller ($25K) or larger ($100K)? |
| 7 | **Per-ticker max** | 8% ($40K) | Conservative — could be 6% ($30K, more diversification) or 10% ($50K, more efficiency) |
| 8 | **Drawdown CB threshold** | 8% peak-to-trough ($40K) | Aggressive operators run 10-12%, conservative 5-6% |
| 9 | **Earnings transcript provider** | API Ninjas (~$30/mo to start) | Acceptable cost? Or skip and rely on 10-Q only? |
| 10 | **Dashboard / web UI** | Skip for v1 (CLI + email only) | Want a minimal web view earlier? |
| 11 | **Side-by-side paper during live** | Yes, first 4 weeks of Live-α/β | Useful or wasteful? |
| 12 | **Account number** | Separate Alpaca account for wheels | Confirm — do not share with options-copilot's account |

If you have direction on any, drop a comment and we'll iterate. Anything you don't address before kickoff, we'll proceed with the provisional answer above.

---

## 8. Council & Process Note

This plan was synthesized from 4 specialist subagent outputs run in parallel:

1. **System Architect** (Plan subagent) — owns §2 + module table + state machine + DB schema
2. **Quant / Risk Specialist** (general-purpose subagent) — owns §3 + parameters + ticker pool + circuit breakers
3. **AI / LLM Integration Specialist** (general-purpose subagent) — owns §4 + touchpoints + prompts + cost
4. **Forward Test Strategist** (general-purpose subagent) — owns §5 + 10-week plan + go-live gates + rollout

Each council member received:
- `docs/RESEARCH.md` as required reading
- The 4 user-locked decisions
- Scope-specific focus + word budget (1500-2500 words)
- Output as markdown for me to synthesize

This differs from `options-copilot`'s 5-model trading council:
- **options-copilot council:** 5 same-role LLMs propose trades, blind-score each other, vote → consensus emerges
- **wheels-copilot planning council:** 4 different-role specialists author non-overlapping sections, I resolve cross-section conflicts

Both patterns are "council" in spirit — diverse perspectives, structured aggregation — but the trading version optimizes for consensus on similar choices, while the planning version optimizes for depth across different concerns. For project planning, specialist decomposition produces a more coherent document than 4 redundant general-purpose proposals.

**Cross-section conflicts resolved during synthesis:**

| Conflict | Architect | Quant | LLM | Forward Test | Resolution |
|---|---|---|---|---|---|
| Watchlist refresh day | Friday | — | Sunday 18:00 | — | **Sunday 18:00** (LLM agent — more data, less weekday churn) |
| Per-ticker max | 10% | 8% | — | — | **8%** (Quant — owns risk numbers) |
| VIX gating | single threshold 30 | gradient (28/22) | — | — | **Gradient** (Quant) |
| Daily loss CB | 2% | 1.5% | — | — | **1.5%** (Quant — more conservative) |
| IV rank cutoff | 20-70 in config | 25-65 | 20-70 in screener | — | **Screener 20-70, entry gate 25-65** (gives screener more candidates; entry stricter) |
| Daily report time | 16:15 | — | — | 16:00 | **16:15** (after EOD cycle completes) |

---

**End of Plan v1.0.** Next: address open decisions in §7, then begin M0 engineering.
