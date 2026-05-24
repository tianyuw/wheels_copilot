from __future__ import annotations

import copy
import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .csp_selector import evaluate_csp_candidates
from .execution_price import (
    BacktestExecutionModel,
    build_backtest_execution_model,
    entry_fill_diagnostics,
    entry_fill_price,
)
from .gates import (
    evaluate_covered_call_earnings_gate,
    evaluate_covered_call_ex_dividend_gate,
    evaluate_earnings_gate,
    evaluate_fundamentals,
)
from .historical_data import HistoricalDataStore, detect_price_space_breaks
from .historical_fundamentals import (
    DEFAULT_FUNDAMENTALS_CACHE_DIR,
    HistoricalFundamentalStore,
    build_historical_fundamental_store,
    provenance_payload,
)
from .indicators import sma_values
from .models import (
    BrokerAccountSnapshot,
    BrokerPosition,
    CspCandidate,
    FundamentalSnapshot,
    GateResult,
    OptionQuote,
    PortfolioSnapshot,
    PriceBar,
)
from .portfolio_risk import evaluate_portfolio_risk
from .price_space_breaks import (
    DEFAULT_PRICE_SPACE_BREAK_CACHE_DIR,
    PRICE_SPACE_BREAK_ALLOW_REAL_GAP,
    PRICE_SPACE_BREAK_RESET_LOOKBACK,
    PriceSpaceBreakClassification,
    PriceSpaceBreakClassifier,
    SplitEventProvider,
    build_price_space_break_classifier,
    normalize_classifier_mode,
)
from .support import analyze_support
from .trading_calendar import nyse_trading_days_after


BACKTEST_VERSION = "phase_two_csp_cc_execution_v2"
DEFAULT_LOOKBACK_CALENDAR_DAYS = 430
DEFAULT_SLIPPAGE_PCT = 0.05
DEFAULT_OPTION_FEE_PER_CONTRACT = 0.10
DEFAULT_RISK_FREE_RATE = 0.04
FUNDAMENTAL_PROFILES = {
    "technical_only",
    "fundamentals_warn",
    "fundamentals_moderate",
    "fundamentals_strict_financials",
    "fundamentals_strict_all",
}
CC_RISK_PROFILES = {"strict", "warn_unknown_dates"}
EXIT_MODELS = {
    "hold_to_expiration",
    "close_at_50pct_profit_or_expiry",
    "manage_at_dte_or_expiry",
    "close_at_50pct_profit_or_manage_dte_or_expiry",
}
STRICT_FINANCIAL_REASON_PREFIXES = (
    "recent_move_",
    "pe_ratio_non_positive",
    "pe_ratio_at_or_above_",
    "positive_quarters_",
    "positive_years_",
)
MODERATE_FUNDAMENTAL_HARD_REASONS = {
    "leveraged_etf",
    "biotech_or_binary_event_industry",
    "chinese_adr",
    "pe_ratio_non_positive",
}
MODERATE_FUNDAMENTAL_REASON_PREFIXES = (
    "market_cap_below_",
    "positive_quarters_",
    "positive_years_",
    "recent_move_",
)


@dataclass
class ShortPutPosition:
    trade_id: str
    ticker: str
    symbol: str
    expiration: date
    strike: float
    contracts: int
    entry_date: date
    entry_price: float
    gross_credit: float
    fees: float
    support_score: float | None
    delta: float | None
    weekly_return_on_strike_pct: float
    assignment_cash_required: float
    settlement_missing_since: date | None = None


@dataclass
class ShortCallPosition:
    trade_id: str
    ticker: str
    symbol: str
    expiration: date
    strike: float
    contracts: int
    entry_date: date
    entry_price: float
    gross_credit: float
    fees: float
    delta: float | None
    adjusted_cost_basis: float | None
    settlement_missing_since: date | None = None


@dataclass
class StockPosition:
    ticker: str
    shares: int = 0
    cost_basis_total: float = 0.0
    premium_credit_total: float = 0.0

    @property
    def average_cost(self) -> float | None:
        if self.shares <= 0:
            return None
        return self.cost_basis_total / self.shares

    @property
    def adjusted_average_cost(self) -> float | None:
        if self.shares <= 0:
            return None
        return (self.cost_basis_total - self.premium_credit_total) / self.shares


@dataclass
class BacktestState:
    starting_equity: float
    cash: float
    equity: float
    open_short_puts: list[ShortPutPosition]
    open_short_calls: list[ShortCallPosition]
    stocks: dict[str, StockPosition]
    realized_option_pnl: float = 0.0
    realized_stock_pnl: float = 0.0
    total_fees: float = 0.0

    @property
    def reserved_assignment_cash(self) -> float:
        return sum(position.assignment_cash_required for position in self.open_short_puts)


@dataclass
class DailyCspCandidate:
    ticker: str
    candidate: CspCandidate
    support_summary: dict[str, Any] | None
    fundamental_context: dict[str, Any] | None
    candidate_earnings_gate: GateResult | None
    post_earnings_cooldown_gate: GateResult | None
    quality_score: float
    rank_within_day: int | None = None


def run_backtest(
    *,
    config: dict[str, Any],
    data: HistoricalDataStore,
    universe: Iterable[str],
    start: date,
    end: date,
    schedule: str = "daily",
    lookback_calendar_days: int = DEFAULT_LOOKBACK_CALENDAR_DAYS,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    option_fee_per_contract: float = DEFAULT_OPTION_FEE_PER_CONTRACT,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    max_orders_per_day: int | None = None,
    split_ratio_low: float = 0.75,
    split_ratio_high: float = 1.25,
    fundamental_profile: str = "technical_only",
    historical_fundamentals: HistoricalFundamentalStore | None = None,
    fundamentals_cache_dir: Path = DEFAULT_FUNDAMENTALS_CACHE_DIR,
    fundamentals_env_file: Path | None = None,
    fundamentals_timeout_seconds: float = 30.0,
    cc_risk_profile: str | None = None,
    post_earnings_cooldown_days: int | None = None,
    price_space_break_classifier: str | None = None,
    price_space_break_split_provider: SplitEventProvider | None = None,
    price_space_break_cache_dir: Path = DEFAULT_PRICE_SPACE_BREAK_CACHE_DIR,
    price_space_break_env_file: Path | None = None,
    price_space_break_timeout_seconds: float = 30.0,
    price_space_split_reset_min_support_bars: int | None = None,
) -> dict[str, Any]:
    if schedule != "daily":
        raise ValueError("phase one backtest only supports daily schedule")
    if end < start:
        raise ValueError("end date must be on or after start date")
    fundamental_profile = _normalize_fundamental_profile(fundamental_profile)
    cc_risk_profile = _normalize_cc_risk_profile(cc_risk_profile, config)
    if post_earnings_cooldown_days is None:
        post_earnings_cooldown_days = int(
            config.get("backtest", {}).get("post_earnings_cooldown_days", 0) or 0
        )
    post_earnings_cooldown_days = max(0, int(post_earnings_cooldown_days))
    needs_historical_fundamentals = (
        fundamental_profile != "technical_only" or post_earnings_cooldown_days > 0
    )
    if not needs_historical_fundamentals:
        historical_fundamentals = None
    price_space_break_classifier = normalize_classifier_mode(
        price_space_break_classifier
        or config.get("backtest", {}).get("price_space_break_classifier")
        or "off"
    )
    if price_space_split_reset_min_support_bars is None:
        price_space_split_reset_min_support_bars = int(
            config.get("backtest", {}).get(
                "price_space_split_reset_min_support_bars",
                30,
            )
        )
    price_space_split_reset_min_support_bars = max(
        1,
        int(price_space_split_reset_min_support_bars),
    )

    tickers = sorted({str(ticker).strip().upper() for ticker in universe if str(ticker).strip()})
    if not tickers:
        raise ValueError("backtest universe is empty")

    starting_equity = float(config.get("account", {}).get("starting_equity", 500000.0))
    state = BacktestState(
        starting_equity=starting_equity,
        cash=starting_equity,
        equity=starting_equity,
        open_short_puts=[],
        open_short_calls=[],
        stocks={ticker: StockPosition(ticker=ticker) for ticker in tickers},
    )

    history_start = start - timedelta(days=lookback_calendar_days)
    market_regime_symbols = _market_regime_symbols(config)
    stock_bars = data.load_stock_bars(tickers, history_start, end)
    market_regime_bars, market_regime_data_issues = _load_market_regime_bars(
        data=data,
        symbols=market_regime_symbols,
        history_start=history_start,
        end=end,
    )
    if needs_historical_fundamentals and historical_fundamentals is None:
        historical_fundamentals = build_historical_fundamental_store(
            config=config,
            cache_dir=fundamentals_cache_dir,
            env_file=fundamentals_env_file,
            timeout_seconds=fundamentals_timeout_seconds,
        )
    if historical_fundamentals is not None and needs_historical_fundamentals:
        historical_fundamentals.preload(tickers, start, end)
    break_classifier = build_price_space_break_classifier(
        mode=price_space_break_classifier,
        env_file=price_space_break_env_file,
        cache_dir=price_space_break_cache_dir,
        timeout_seconds=price_space_break_timeout_seconds,
        split_provider=price_space_break_split_provider,
    )
    if break_classifier is not None:
        break_classifier.preload(tickers, history_start, end)

    bars_by_day = _bars_by_day(stock_bars)
    pre_start_split_issues = _detect_pre_start_split_issues(
        stock_bars,
        start=start,
        ratio_low=split_ratio_low,
        ratio_high=split_ratio_high,
    )
    blocked_tickers: set[str] = set()
    price_space_reset_dates: dict[str, date] = {}
    data_issues: list[dict[str, Any]] = []
    data_issues.extend(market_regime_data_issues)
    events: list[dict[str, Any]] = []
    for ticker, issues in pre_start_split_issues.items():
        for issue in issues:
            classification = _classify_price_space_break(
                break_classifier,
                ticker=ticker,
                issue=issue,
            )
            data_issue = (
                {
                    "date": issue["date"],
                    "ticker": ticker,
                    "type": "price_space_break",
                    "details": issue,
                }
            )
            if classification is not None:
                data_issue["classification"] = classification.to_payload()
            data_issues.append(data_issue)
            if _price_space_break_should_reset_lookback(classification):
                blocked_tickers.discard(ticker)
                _record_price_space_reset(price_space_reset_dates, ticker, classification)
                events.append(
                    {
                        "date": issue.get("date") or classification.date.isoformat(),
                        "ticker": ticker,
                        "type": "PRICE_SPACE_BREAK_RESET",
                        "reason": "pre_start_confirmed_split_reset_lookback",
                        "reset_date": classification.date.isoformat(),
                        "details": issue,
                        "classification": classification.to_payload(),
                    }
                )
                _record_open_option_price_space_reset_issues(
                    state=state,
                    data_issues=data_issues,
                    events=events,
                    day=start,
                    ticker=ticker,
                    classification=classification,
                )
            elif _price_space_break_should_block(classification):
                blocked_tickers.add(ticker)
                events.append(
                    {
                        "date": start.isoformat(),
                        "ticker": ticker,
                        "type": "PRICE_SPACE_BREAK_BLOCK",
                        "reason": "pre_start_price_space_break_in_lookback_window",
                        "details": issue,
                        "classification": (
                            classification.to_payload() if classification else None
                        ),
                    }
                )
            else:
                events.append(
                    {
                        "date": start.isoformat(),
                        "ticker": ticker,
                        "type": "PRICE_SPACE_BREAK_ALLOWED",
                        "reason": "pre_start_real_gap_move_unblocked",
                        "details": issue,
                        "classification": classification.to_payload(),
                    }
                )

    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    candidate_ledger: list[dict[str, Any]] = []
    rejected_reason_counts: Counter[str] = Counter()
    support_stats = _new_support_stats()
    market_regime_stats = _new_market_regime_stats()
    fundamental_stats = _new_fundamental_stats(fundamental_profile)
    latest_close: dict[str, float] = {
        ticker: bar.close
        for ticker, bars in stock_bars.items()
        if (bar := _last_bar_before(bars, start)) is not None
    }
    previous_bar_by_ticker: dict[str, PriceBar] = {
        ticker: bar
        for ticker, bars in stock_bars.items()
        if (bar := _last_bar_before(bars, start)) is not None
    }

    run_days = data.trading_days(start, end)
    csp_cfg = config.get("csp_selector", {})
    dte_min = int(csp_cfg.get("dte_min", 1))
    dte_max = int(csp_cfg.get("dte_max", 9))
    cc_cfg = config.get("cc_selector", {})
    cc_dte_min = int(cc_cfg.get("dte_min", 1))
    cc_dte_max = int(cc_cfg.get("dte_max", 9))
    default_quantity = max(1, int(config.get("trade_planner", {}).get("default_contract_quantity", 1)))
    if max_orders_per_day is None:
        max_orders_per_day = int(config.get("execution", {}).get("max_orders_per_run", 3))
    execution_model = build_backtest_execution_model(config, slippage_pct=slippage_pct)
    cc_backtest_config = _covered_call_backtest_config(config, cc_risk_profile)
    csp_exit_model, cc_exit_model = _management_exit_models(config)
    profit_take_pct = _management_profit_take_pct(config)
    profit_take_price_field = _management_profit_take_price_field(config)
    manage_at_dte = _management_manage_at_dte(config)

    for day in run_days:
        todays_bars = bars_by_day.get(day, {})
        for ticker, bar in todays_bars.items():
            issue = _same_day_price_space_break(
                previous_bar_by_ticker.get(ticker),
                bar,
                ratio_low=split_ratio_low,
                ratio_high=split_ratio_high,
            )
            if issue is not None:
                classification = _classify_price_space_break(
                    break_classifier,
                    ticker=ticker,
                    issue=issue,
                )
                data_issue = {
                    "date": day.isoformat(),
                    "ticker": ticker,
                    "type": "price_space_break",
                    "details": issue,
                }
                if classification is not None:
                    data_issue["classification"] = classification.to_payload()
                data_issues.append(data_issue)
                if _price_space_break_should_reset_lookback(classification):
                    blocked_tickers.discard(ticker)
                    _record_price_space_reset(
                        price_space_reset_dates,
                        ticker,
                        classification,
                    )
                    events.append(
                        {
                            "date": day.isoformat(),
                            "ticker": ticker,
                            "type": "PRICE_SPACE_BREAK_RESET",
                            "reason": "same_day_confirmed_split_reset_lookback",
                            "reset_date": classification.date.isoformat(),
                            "details": issue,
                            "classification": classification.to_payload(),
                        }
                    )
                    _record_open_option_price_space_reset_issues(
                        state=state,
                        data_issues=data_issues,
                        events=events,
                        day=day,
                        ticker=ticker,
                        classification=classification,
                    )
                elif _price_space_break_should_block(classification):
                    blocked_tickers.add(ticker)
                    events.append(
                        {
                            "date": day.isoformat(),
                            "ticker": ticker,
                            "type": "PRICE_SPACE_BREAK_BLOCK",
                            "reason": "same_day_price_space_break",
                            "details": issue,
                            "classification": (
                                classification.to_payload() if classification else None
                            ),
                        }
                    )
                else:
                    events.append(
                        {
                            "date": day.isoformat(),
                            "ticker": ticker,
                            "type": "PRICE_SPACE_BREAK_ALLOWED",
                            "reason": "same_day_real_gap_move_unblocked",
                            "details": issue,
                            "classification": classification.to_payload(),
                        }
                    )

        orders_opened_today = 0
        if todays_bars:
            _manage_open_short_puts_for_day(
                state=state,
                data=data,
                day=day,
                latest_close=latest_close,
                trades=trades,
                events=events,
                execution_model=execution_model,
                option_fee_per_contract=option_fee_per_contract,
                risk_free_rate=risk_free_rate,
                exit_model=csp_exit_model,
                profit_take_pct=profit_take_pct,
                profit_take_price_field=profit_take_price_field,
                manage_at_dte=manage_at_dte,
            )
            _manage_open_short_calls_for_day(
                state=state,
                data=data,
                day=day,
                latest_close=latest_close,
                trades=trades,
                events=events,
                execution_model=execution_model,
                option_fee_per_contract=option_fee_per_contract,
                risk_free_rate=risk_free_rate,
                exit_model=cc_exit_model,
                profit_take_pct=profit_take_pct,
                profit_take_price_field=profit_take_price_field,
                manage_at_dte=manage_at_dte,
            )
            market_regime_gate = _evaluate_market_regime_gate(
                config,
                market_regime_bars,
                day,
            )
            _record_market_regime_stats(market_regime_stats, market_regime_gate)
            csp_day_config, csp_regime_override = _conditional_csp_config_for_day(
                config,
                market_regime_bars,
                day,
            )
            csp_day_cfg = csp_day_config.get("csp_selector", {})
            csp_day_dte_min = int(csp_day_cfg.get("dte_min", dte_min))
            csp_day_dte_max = int(csp_day_cfg.get("dte_max", dte_max))
            prefetch_options = getattr(data, "prefetch_option_day_rows", None)
            if callable(prefetch_options):
                active_symbols = {
                    ticker
                    for ticker in tickers
                    if ticker not in blocked_tickers and ticker in todays_bars
                }
                prefetch_options(
                    day,
                    active_symbols,
                    option_type="put",
                )
                prefetch_options(
                    day,
                    active_symbols,
                    option_type="call",
                )
            orders_opened_today += _open_covered_calls_for_day(
                state=state,
                data=data,
                historical_fundamentals=historical_fundamentals,
                fundamental_profile=fundamental_profile,
                fundamental_stats=fundamental_stats,
                tickers=tickers,
                blocked_tickers=blocked_tickers,
                price_space_reset_dates=price_space_reset_dates,
                price_space_reset_min_support_bars=(
                    price_space_split_reset_min_support_bars
                ),
                todays_bars=todays_bars,
                stock_bars=stock_bars,
                day=day,
                config=cc_backtest_config,
                cc_risk_profile=cc_risk_profile,
                dte_min=cc_dte_min,
                dte_max=cc_dte_max,
                slippage_pct=slippage_pct,
                risk_free_rate=risk_free_rate,
                option_fee_per_contract=option_fee_per_contract,
                max_orders=max_orders_per_day,
                execution_model=execution_model,
                trades=trades,
                events=events,
                rejected_reason_counts=rejected_reason_counts,
                latest_close=latest_close,
            )
            daily_csp_candidates: list[DailyCspCandidate] = []
            for ticker in tickers:
                if ticker in blocked_tickers:
                    _record_candidate_ledger(
                        candidate_ledger,
                        config=config,
                        day=day,
                        ticker=ticker,
                        decision="REJECT",
                        reason="price_space_break_blocked",
                    )
                    continue
                if ticker not in todays_bars:
                    _reject(
                        events,
                        rejected_reason_counts,
                        day,
                        ticker,
                        "missing_stock_bar_on_scan_day",
                    )
                    _record_candidate_ledger(
                        candidate_ledger,
                        config=config,
                        day=day,
                        ticker=ticker,
                        decision="REJECT",
                        reason="missing_stock_bar_on_scan_day",
                    )
                    continue
                if _has_open_short_put(state, ticker):
                    _record_candidate_ledger(
                        candidate_ledger,
                        config=config,
                        day=day,
                        ticker=ticker,
                        decision="SKIP",
                        reason="existing_short_put_open",
                    )
                    continue
                if state.stocks.get(ticker, StockPosition(ticker)).shares >= 100:
                    _record_candidate_ledger(
                        candidate_ledger,
                        config=config,
                        day=day,
                        ticker=ticker,
                        decision="SKIP",
                        reason="long_stock_position_open",
                    )
                    continue
                if market_regime_gate.status == "REJECT":
                    reason = _gate_primary_reason("market_regime_gate", market_regime_gate)
                    diagnostics = {"market_regime_gate": asdict(market_regime_gate)}
                    _reject(
                        events,
                        rejected_reason_counts,
                        day,
                        ticker,
                        reason,
                        diagnostics=diagnostics,
                    )
                    _record_candidate_ledger(
                        candidate_ledger,
                        config=config,
                        day=day,
                        ticker=ticker,
                        decision="REJECT",
                        reason=reason,
                        diagnostics=diagnostics,
                    )
                    continue

                fundamental_context = _fundamental_context(
                    historical_fundamentals=historical_fundamentals,
                    profile=fundamental_profile,
                    ticker=ticker,
                    day=day,
                    bars=_bars_after_price_space_reset(
                        stock_bars.get(ticker, []),
                        price_space_reset_dates.get(ticker),
                    ),
                    config=config,
                    require_snapshot=post_earnings_cooldown_days > 0,
                )
                if fundamental_context is not None:
                    if fundamental_profile != "technical_only":
                        _record_fundamental_context(
                            fundamental_stats,
                            events,
                            day,
                            ticker,
                            fundamental_context,
                            phase="csp",
                        )
                    fundamental_gate = fundamental_context["fundamental_gate"]
                    if _should_block_fundamental_gate(
                        fundamental_profile,
                        fundamental_gate,
                        fundamental_context["snapshot"],
                    ):
                        reason = _gate_primary_reason(
                            "fundamental_gate",
                            fundamental_gate,
                        )
                        diagnostics = _fundamental_context_payload(fundamental_context)
                        _reject(
                            events,
                            rejected_reason_counts,
                            day,
                            ticker,
                            reason,
                            diagnostics=diagnostics,
                        )
                        _record_candidate_ledger(
                            candidate_ledger,
                            config=config,
                            day=day,
                            ticker=ticker,
                            decision="REJECT",
                            reason=reason,
                            diagnostics=diagnostics,
                        )
                        continue

                candidate, support_summary, rejection_summary = _select_candidate_for_day(
                    data=data,
                    ticker=ticker,
                    day=day,
                    bars=_bars_after_price_space_reset(
                        stock_bars.get(ticker, []),
                        price_space_reset_dates.get(ticker),
                    ),
                    config=csp_day_config,
                    dte_min=csp_day_dte_min,
                    dte_max=csp_day_dte_max,
                    slippage_pct=slippage_pct,
                    risk_free_rate=risk_free_rate,
                    execution_model=execution_model,
                    price_space_reset_date=price_space_reset_dates.get(ticker),
                    price_space_reset_min_support_bars=(
                        price_space_split_reset_min_support_bars
                    ),
                    support_stats=support_stats,
                    csp_regime_override=csp_regime_override,
                )
                if candidate is None:
                    reason = _primary_rejection_reason(rejection_summary)
                    diagnostics = {"csp_rejection_summary": rejection_summary}
                    _reject(
                        events,
                        rejected_reason_counts,
                        day,
                        ticker,
                        reason,
                        support=support_summary,
                        diagnostics=diagnostics,
                    )
                    _record_candidate_ledger(
                        candidate_ledger,
                        config=config,
                        day=day,
                        ticker=ticker,
                        decision="REJECT",
                        reason=reason,
                        support=support_summary,
                        diagnostics=diagnostics,
                    )
                    continue
                if not candidate.auto_trade:
                    diagnostics = _candidate_diagnostics(candidate)
                    _reject(
                        events,
                        rejected_reason_counts,
                        day,
                        ticker,
                        "candidate_watch_only",
                        support=support_summary,
                        diagnostics=diagnostics,
                    )
                    _record_candidate_ledger(
                        candidate_ledger,
                        config=config,
                        day=day,
                        ticker=ticker,
                        decision="WATCH",
                        reason="candidate_watch_only",
                        support=support_summary,
                        candidate=candidate,
                        diagnostics=diagnostics,
                    )
                    continue

                candidate_earnings_gate: GateResult | None = None
                post_earnings_cooldown_gate: GateResult | None = None
                if fundamental_context is not None:
                    candidate_earnings_gate, _allowed = evaluate_earnings_gate(
                        fundamental_context["snapshot"],
                        [candidate.option],
                        as_of=day,
                    )
                    _record_gate_stats(
                        fundamental_stats,
                        "csp_earnings_gate",
                        candidate_earnings_gate,
                    )
                    if _should_block_csp_earnings_gate(
                        fundamental_profile,
                        candidate_earnings_gate,
                    ):
                        reason = _gate_primary_reason(
                            "csp_earnings_gate",
                            candidate_earnings_gate,
                        )
                        diagnostics = {
                            **_fundamental_context_payload(fundamental_context),
                            "earnings_gate": asdict(candidate_earnings_gate),
                            "candidate": _candidate_diagnostics(candidate),
                        }
                        _reject(
                            events,
                            rejected_reason_counts,
                            day,
                            ticker,
                            reason,
                            support=support_summary,
                            diagnostics=diagnostics,
                        )
                        _record_candidate_ledger(
                            candidate_ledger,
                            config=config,
                            day=day,
                            ticker=ticker,
                            decision="REJECT",
                            reason=reason,
                            support=support_summary,
                            candidate=candidate,
                            diagnostics=diagnostics,
                        )
                        continue
                    post_earnings_cooldown_gate = _evaluate_post_earnings_cooldown_gate(
                        fundamental_context["snapshot"],
                        as_of=day,
                        cooldown_days=post_earnings_cooldown_days,
                    )
                    if post_earnings_cooldown_gate is not None:
                        _record_gate_stats(
                            fundamental_stats,
                            "csp_post_earnings_cooldown_gate",
                            post_earnings_cooldown_gate,
                        )
                        if post_earnings_cooldown_gate.status == "REJECT":
                            reason = _gate_primary_reason(
                                "csp_post_earnings_cooldown_gate",
                                post_earnings_cooldown_gate,
                            )
                            diagnostics = {
                                **_fundamental_context_payload(fundamental_context),
                                "post_earnings_cooldown_gate": asdict(
                                    post_earnings_cooldown_gate
                                ),
                                "candidate": _candidate_diagnostics(candidate),
                            }
                            _reject(
                                events,
                                rejected_reason_counts,
                                day,
                                ticker,
                                reason,
                                support=support_summary,
                                diagnostics=diagnostics,
                            )
                            _record_candidate_ledger(
                                candidate_ledger,
                                config=config,
                                day=day,
                                ticker=ticker,
                                decision="REJECT",
                                reason=reason,
                                support=support_summary,
                                candidate=candidate,
                                diagnostics=diagnostics,
                            )
                            continue

                daily_csp_candidates.append(
                    DailyCspCandidate(
                        ticker=ticker,
                        candidate=candidate,
                        support_summary=support_summary,
                        fundamental_context=fundamental_context,
                        candidate_earnings_gate=candidate_earnings_gate,
                        post_earnings_cooldown_gate=post_earnings_cooldown_gate,
                        quality_score=_candidate_quality_score(
                            candidate,
                            support_summary,
                        ),
                    )
                )

            orders_opened_today += _execute_ranked_csp_candidates_for_day(
                state=state,
                candidates=daily_csp_candidates,
                config=config,
                csp_execution_config=csp_day_config,
                day=day,
                latest_close=latest_close,
                data=data,
                default_quantity=default_quantity,
                max_new_orders=max(0, max_orders_per_day - orders_opened_today),
                option_fee_per_contract=option_fee_per_contract,
                execution_model=execution_model,
                trades=trades,
                events=events,
                rejected_reason_counts=rejected_reason_counts,
                candidate_ledger=candidate_ledger,
            )

        for ticker, bar in todays_bars.items():
            latest_close[ticker] = bar.close

        _resolve_expiring_short_puts(
            state=state,
            day=day,
            todays_close={ticker: bar.close for ticker, bar in todays_bars.items()},
            trades=trades,
            events=events,
        )
        _resolve_expiring_short_calls(
            state=state,
            day=day,
            todays_close={ticker: bar.close for ticker, bar in todays_bars.items()},
            trades=trades,
            events=events,
        )
        state.equity = _mark_state_equity(
            state,
            latest_close,
            data,
            day,
            data_issues=data_issues,
        )
        equity_curve.append(_daily_equity_row(state, day, latest_close))
        for ticker, bar in todays_bars.items():
            previous_bar_by_ticker[ticker] = bar

    state.equity = _mark_state_equity(state, latest_close, data, end)
    summary = _summarize_backtest(
        config=config,
        state=state,
        equity_curve=equity_curve,
        trades=trades,
        events=events,
        rejected_reason_counts=rejected_reason_counts,
        data_issues=data_issues,
        support_stats=support_stats,
        market_regime_stats=market_regime_stats,
        candidate_ledger=candidate_ledger,
    )
    fundamental_diagnostics = _finalize_fundamental_stats(
        fundamental_stats,
        historical_fundamentals,
    )
    summary["fundamental_profile"] = fundamental_profile
    summary["fundamental_would_reject_count"] = fundamental_diagnostics[
        "would_reject_count"
    ]
    summary["fundamental_warn_count"] = fundamental_diagnostics["warn_count"]
    summary["cc_risk_profile"] = cc_risk_profile
    summary["post_earnings_cooldown_days"] = post_earnings_cooldown_days
    summary["csp_exit_model"] = csp_exit_model
    summary["cc_exit_model"] = cc_exit_model
    summary["profit_take_pct_of_credit"] = profit_take_pct
    summary["profit_take_price_field"] = profit_take_price_field
    summary["manage_at_dte"] = manage_at_dte
    summary["execution_model"] = execution_model.model
    summary["execution_fill_policy"] = execution_model.fill_policy
    summary["execution_reference_price_source"] = execution_model.reference_price_source
    summary["execution_calibration_status"] = execution_model.calibration_status
    cache_stats_fn = getattr(data, "cache_stats_snapshot", None)
    data_cache_stats = cache_stats_fn() if callable(cache_stats_fn) else None
    return {
        "version": BACKTEST_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "phase_two_csp_then_cc",
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "universe": tickers,
        "assumptions": [
            "Phase 2 opens cash-secured puts, assigns ITM puts, and sells covered calls against uncovered 100-share lots on the next eligible trading day.",
            "Support/trend signals use only bars strictly before the scan date.",
            "Short put entry uses the configured backtest execution model; default v2 uses a synthetic bid/ask spread around the slippage-adjusted option day-aggregate reference price and fills at modeled mid.",
            "Covered call entry uses the same configured backtest execution model as short puts.",
            "Covered call risk gates use the configured backtest CC risk profile; warn_unknown_dates only downgrades unknown historical earnings/ex-dividend dates to WARN and still blocks known events inside the contract window.",
            "Day-aggregate option volume above zero is treated as necessary but not sufficient fillability evidence.",
            "Delta filters inferred from day-aggregate option prices are approximate in Phase 1.",
            "No early assignment or rolling is modeled in Phase 1.",
            "When configured, open short options can be closed early by a daily option mark profit target and/or manage-at-DTE rule before expiration settlement.",
            "Candidate ledger records the CSP screener audit path for diagnostics; by default it records rejections and ranked/opened candidates, while skip rows are configurable.",
            "CSP candidate generation scans the full daily universe before execution selection, so candidate supply is separated from max_orders_per_day capacity.",
            "Short puts that remain open to expiration settle by underlying close: close < strike assigns, otherwise expires worthless.",
            "Covered calls that remain open to expiration settle by underlying close: close > strike is called away, otherwise expires worthless.",
            "Corporate action or price-space breaks are guarded by large historical close-to-close and same-day open-to-previous-close ratio detection.",
            "When enabled, the price-space break classifier unblocks real_gap_move classifications and resets technical lookbacks after confirmed splits; unknown breaks remain conservatively blocked.",
            "Confirmed split reset uses the split execution date as the first bar in the new price space; exact OCC option contract adjustment through a split is not modeled and is surfaced as a data issue when encountered.",
        ],
        "run_parameters": {
            "schedule": schedule,
            "lookback_calendar_days": lookback_calendar_days,
            "slippage_pct": slippage_pct,
            "option_fee_per_contract": option_fee_per_contract,
            "risk_free_rate": risk_free_rate,
            "max_orders_per_day": max_orders_per_day,
            "split_ratio_low": split_ratio_low,
            "split_ratio_high": split_ratio_high,
            "price_space_break_classifier": price_space_break_classifier,
            "price_space_break_cache_dir": (
                str(price_space_break_cache_dir)
                if price_space_break_classifier != "off"
                else None
            ),
            "price_space_break_diagnostics": (
                break_classifier.diagnostics() if break_classifier is not None else None
            ),
            "price_space_real_gap_ratio_bounds": (
                {
                    "low": break_classifier.real_gap_ratio_low,
                    "high": break_classifier.real_gap_ratio_high,
                }
                if break_classifier is not None
                else None
            ),
            "price_space_split_reset_min_support_bars": (
                price_space_split_reset_min_support_bars
            ),
            "price_space_reset_dates": {
                ticker: reset_date.isoformat()
                for ticker, reset_date in sorted(price_space_reset_dates.items())
            },
            "contract_quantity": default_quantity,
            "position_sizing": _position_sizing_config(config, default_quantity),
            "fundamental_profile": fundamental_profile,
            "cc_risk_profile": cc_risk_profile,
            "post_earnings_cooldown_days": post_earnings_cooldown_days,
            "management": {
                "csp_exit_model": csp_exit_model,
                "cc_exit_model": cc_exit_model,
                "profit_take_pct_of_credit": profit_take_pct,
                "profit_take_price_field": profit_take_price_field,
                "manage_at_dte": manage_at_dte,
            },
            "market_regime": _market_regime_config(config),
            "fundamentals_cache_dir": (
                str(fundamentals_cache_dir)
                if needs_historical_fundamentals
                else None
            ),
            "backtest_execution": execution_model.metadata(),
            "data_cache": data_cache_stats,
        },
        "config_snapshot": config,
        "fundamental_diagnostics": fundamental_diagnostics,
        "summary": summary,
        "data_issues": data_issues,
        "trades": trades,
        "events": events,
        "candidate_ledger": candidate_ledger,
        "equity_curve": equity_curve,
        "open_positions": {
            "short_puts": [_serialize_short_put(position) for position in state.open_short_puts],
            "short_calls": [
                _serialize_short_call(position) for position in state.open_short_calls
            ],
            "stocks": [
                _serialize_stock(position)
                for position in sorted(state.stocks.values(), key=lambda pos: pos.ticker)
                if position.shares
            ],
        },
    }


def write_backtest_outputs(result: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / "backtest_results.json",
        "summary": output_dir / "summary.json",
        "trades": output_dir / "trades.csv",
        "events": output_dir / "events.csv",
        "candidate_ledger": output_dir / "candidate_ledger.csv",
        "equity_curve": output_dir / "equity_curve.csv",
        "report": output_dir / "report.md",
        "config": output_dir / "run_config_snapshot.json",
    }
    paths["json"].write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8",
    )
    paths["summary"].write_text(
        json.dumps(result["summary"], indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8",
    )
    paths["config"].write_text(
        json.dumps(
            {
                "version": result["version"],
                "date_range": result["date_range"],
                "universe": result["universe"],
                "run_parameters": result["run_parameters"],
                "config_snapshot": result["config_snapshot"],
                "assumptions": result["assumptions"],
            },
            indent=2,
            ensure_ascii=False,
            default=_json_default,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_csv(paths["trades"], result["trades"])
    _write_csv(paths["events"], result["events"])
    _write_csv(paths["candidate_ledger"], result.get("candidate_ledger", []))
    _write_csv(paths["equity_curve"], result["equity_curve"])
    paths["report"].write_text(_render_report(result), encoding="utf-8")
    return paths


def _select_candidate_for_day(
    *,
    data: HistoricalDataStore,
    ticker: str,
    day: date,
    bars: list[PriceBar],
    config: dict[str, Any],
    dte_min: int,
    dte_max: int,
    slippage_pct: float,
    risk_free_rate: float,
    execution_model: BacktestExecutionModel,
    price_space_reset_date: date | None = None,
    price_space_reset_min_support_bars: int = 30,
    support_stats: dict[str, Any] | None = None,
    csp_regime_override: dict[str, Any] | None = None,
) -> tuple[CspCandidate | None, dict[str, Any] | None, dict[str, int]]:
    support_bars = [bar for bar in bars if bar.date < day]
    min_required = (
        int(price_space_reset_min_support_bars)
        if price_space_reset_date is not None
        else 30
    )
    if len(support_bars) < min_required:
        summary = (
            {
                "price_space_reset_date": price_space_reset_date.isoformat(),
                "post_reset_support_bar_count": len(support_bars),
                "post_reset_min_support_bars": min_required,
            }
            if price_space_reset_date is not None
            else None
        )
        reason = (
            "post_split_reset_insufficient_support_history"
            if price_space_reset_date is not None
            else "insufficient_support_history"
        )
        return None, summary, {reason: 1}
    try:
        support = analyze_support(support_bars, config)
    except ValueError as exc:
        return None, None, {f"support_error:{exc}": 1}

    support_summary = {
        "tradable": support.tradable,
        "current_price": support.current_price,
        "atr14": support.atr14,
        "trend_passed": support.trend.passed,
        "preconditions_passed": support.preconditions_passed,
        "precondition_metrics": support.precondition_metrics,
        "context_metrics": support.context_metrics,
        "trend_reasons": support.trend.reasons,
        "selected_zone": (
            _support_zone_payload(support.selected_zone)
            if support.selected_zone
            else None
        ),
        "reasons": support.reasons,
    }
    if price_space_reset_date is not None:
        support_summary["price_space_reset_date"] = price_space_reset_date.isoformat()
        support_summary["post_reset_support_bar_count"] = len(support_bars)
    if csp_regime_override is not None:
        support_summary["csp_regime_override"] = csp_regime_override
    if not support.preconditions_passed:
        return None, support_summary, {"support_precondition_failed": 1}
    options = data.option_chain(
        ticker,
        day,
        dte_min=dte_min,
        dte_max=dte_max,
        option_type="put",
        price_field="open",
        slippage_pct=slippage_pct,
        risk_free_rate=risk_free_rate,
        stock_price=support.current_price,
        execution_model=execution_model,
    )
    if not options:
        _record_support_stats(
            support_stats,
            support=support,
            candidate=None,
            options_count=0,
        )
        return None, support_summary, {"no_fillable_put_options": 1}

    result = evaluate_csp_candidates(options, support, config, risk_free_rate)
    if result.candidate is not None:
        support_summary["selected_zone"] = _support_zone_payload(
            result.candidate.support_zone
        )
        support_summary["selection_policy"] = result.candidate.diagnostics.get(
            "support_selection_policy"
        )
        support_summary["support_zone_rank"] = result.candidate.diagnostics.get(
            "support_zone_rank"
        )
        support_summary["support_zones_considered"] = result.candidate.diagnostics.get(
            "support_zones_considered"
        )
    _record_support_stats(
        support_stats,
        support=support,
        candidate=result.candidate,
        options_count=len(options),
    )
    if result.candidate is None:
        return None, support_summary, result.rejection_summary or {"no_csp_candidate": 1}
    return result.candidate, support_summary, result.rejection_summary


def _support_zone_payload(zone) -> dict[str, Any]:
    return {
        "method": zone.method,
        "center": zone.center,
        "bottom": zone.bottom,
        "top": zone.top,
        "score": zone.score,
        "touches": zone.touches,
        "rejections": zone.rejections,
        "last_touch_date": (
            zone.last_touch_date.isoformat() if zone.last_touch_date else None
        ),
    }


def _new_support_stats() -> dict[str, Any]:
    return {
        "attempts": 0,
        "with_zones": 0,
        "trend_passed": 0,
        "tradable": 0,
        "candidate_found": 0,
        "auto_trade_candidate": 0,
        "options_count": 0,
        "zone_count": 0,
        "selected_score_sum": 0.0,
        "spot_to_support_bottom_pct_sum": 0.0,
        "spot_to_support_top_pct_sum": 0.0,
        "spot_to_support_bottom_atr_sum": 0.0,
        "spot_to_support_bottom_atr_count": 0,
        "selected_method_counts": Counter(),
        "selection_policy_counts": Counter(),
    }


def _record_support_stats(
    stats: dict[str, Any] | None,
    *,
    support,
    candidate: CspCandidate | None,
    options_count: int,
) -> None:
    if stats is None:
        return
    stats["attempts"] += 1
    stats["options_count"] += options_count
    if support.zones:
        stats["with_zones"] += 1
        stats["zone_count"] += len(support.zones)
    if support.trend.passed:
        stats["trend_passed"] += 1

    zone = candidate.support_zone if candidate is not None else support.selected_zone
    if zone is not None and support.trend.passed and zone.score >= support.min_score_to_trade:
        stats["tradable"] += 1
    if candidate is not None:
        stats["candidate_found"] += 1
        if candidate.auto_trade:
            stats["auto_trade_candidate"] += 1
        policy = candidate.diagnostics.get("support_selection_policy")
        if policy:
            stats["selection_policy_counts"][str(policy)] += 1

    if zone is None or support.current_price <= 0:
        return
    stats["selected_method_counts"][zone.method] += 1
    stats["selected_score_sum"] += float(zone.score)
    stats["spot_to_support_bottom_pct_sum"] += (
        (support.current_price - zone.bottom) / support.current_price * 100.0
    )
    stats["spot_to_support_top_pct_sum"] += (
        (support.current_price - zone.top) / support.current_price * 100.0
    )
    if support.atr14 and support.atr14 > 0:
        stats["spot_to_support_bottom_atr_sum"] += (
            (support.current_price - zone.bottom) / support.atr14
        )
        stats["spot_to_support_bottom_atr_count"] += 1


def _finalize_support_stats(stats: dict[str, Any]) -> dict[str, Any]:
    attempts = int(stats.get("attempts") or 0)
    with_zones = int(stats.get("with_zones") or 0)
    candidate_found = int(stats.get("candidate_found") or 0)
    auto_trade = int(stats.get("auto_trade_candidate") or 0)
    selected_count = sum(stats["selected_method_counts"].values())
    atr_count = int(stats.get("spot_to_support_bottom_atr_count") or 0)

    def pct(count: int) -> float | None:
        return round(count / attempts * 100.0, 4) if attempts else None

    return {
        "attempts": attempts,
        "with_zones": with_zones,
        "support_coverage_pct": pct(with_zones),
        "trend_passed_pct": pct(int(stats.get("trend_passed") or 0)),
        "tradable_pct": pct(int(stats.get("tradable") or 0)),
        "candidate_found": candidate_found,
        "candidate_found_pct": pct(candidate_found),
        "auto_trade_candidate": auto_trade,
        "auto_trade_candidate_pct": pct(auto_trade),
        "average_options_count": (
            round(float(stats.get("options_count") or 0) / attempts, 4)
            if attempts
            else None
        ),
        "average_zone_count": (
            round(float(stats.get("zone_count") or 0) / attempts, 4)
            if attempts
            else None
        ),
        "average_selected_score": (
            round(float(stats.get("selected_score_sum") or 0.0) / selected_count, 4)
            if selected_count
            else None
        ),
        "average_spot_to_support_bottom_pct": (
            round(
                float(stats.get("spot_to_support_bottom_pct_sum") or 0.0)
                / selected_count,
                4,
            )
            if selected_count
            else None
        ),
        "average_spot_to_support_top_pct": (
            round(
                float(stats.get("spot_to_support_top_pct_sum") or 0.0)
                / selected_count,
                4,
            )
            if selected_count
            else None
        ),
        "average_spot_to_support_bottom_atr": (
            round(
                float(stats.get("spot_to_support_bottom_atr_sum") or 0.0)
                / atr_count,
                4,
            )
            if atr_count
            else None
        ),
        "selected_method_counts": dict(stats["selected_method_counts"].most_common()),
        "selection_policy_counts": dict(stats["selection_policy_counts"].most_common()),
    }


def _open_short_put(
    *,
    state: BacktestState,
    candidate: CspCandidate,
    day: date,
    ticker: str,
    contracts: int,
    option_fee_per_contract: float,
    execution_model: BacktestExecutionModel,
    support_summary: dict[str, Any] | None,
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
    diagnostics: dict[str, Any],
) -> bool:
    option = candidate.option
    entry_price = entry_fill_price(option, execution_model, side="sell")
    if entry_price is None or entry_price <= 0:
        return False
    execution_diagnostics = entry_fill_diagnostics(
        option, execution_model, side="sell"
    )
    fees = option_fee_per_contract * contracts
    gross_credit = entry_price * 100.0 * contracts
    trade_id = f"{day.isoformat()}-{ticker}-{len(trades) + 1}"
    position = ShortPutPosition(
        trade_id=trade_id,
        ticker=ticker,
        symbol=option.symbol,
        expiration=option.expiration,
        strike=option.strike,
        contracts=contracts,
        entry_date=day,
        entry_price=entry_price,
        gross_credit=gross_credit,
        fees=fees,
        support_score=(
            support_summary.get("selected_zone", {}).get("score")
            if support_summary and support_summary.get("selected_zone")
            else None
        ),
        delta=candidate.delta,
        weekly_return_on_strike_pct=candidate.weekly_return_on_strike_pct,
        assignment_cash_required=option.strike * 100.0 * contracts,
    )
    state.cash += gross_credit - fees
    state.total_fees += fees
    state.open_short_puts.append(position)
    trades.append(
        {
            "trade_id": trade_id,
            "ticker": ticker,
            "strategy": "cash_secured_put",
            "status": "OPEN",
            "entry_date": day.isoformat(),
            "expiration": option.expiration.isoformat(),
            "symbol": option.symbol,
            "strike": option.strike,
            "contracts": contracts,
            "entry_price": entry_price,
            "entry_market_bid": execution_diagnostics["market_bid"],
            "entry_market_ask": execution_diagnostics["market_ask"],
            "entry_market_mid": execution_diagnostics["market_mid"],
            "entry_spread_pct_of_mid": execution_diagnostics["spread_pct_of_mid"],
            "entry_fill_discount_pct_of_mid": execution_diagnostics[
                "fill_discount_pct_of_mid"
            ],
            "execution_model": execution_diagnostics["execution_model"],
            "execution_fill_policy": execution_diagnostics["fill_policy"],
            "execution_reference_price_source": execution_diagnostics[
                "reference_price_source"
            ],
            "execution_calibration_status": execution_diagnostics[
                "calibration_status"
            ],
            "gross_credit": gross_credit,
            "fees": fees,
            "net_credit": gross_credit - fees,
            "delta": candidate.delta,
            "support_score": position.support_score,
            "weekly_return_on_strike_pct": candidate.weekly_return_on_strike_pct,
            "assignment_cash_required": position.assignment_cash_required,
        }
    )
    events.append(
        {
            "date": day.isoformat(),
            "ticker": ticker,
            "type": "OPEN_SHORT_PUT",
            "trade_id": trade_id,
            "symbol": option.symbol,
            "strike": option.strike,
            "expiration": option.expiration.isoformat(),
            "contracts": contracts,
            "entry_price": entry_price,
            "execution": execution_diagnostics,
            "gross_credit": gross_credit,
            "fees": fees,
            "support": support_summary,
            "diagnostics": diagnostics,
        }
    )
    return True


def _open_covered_calls_for_day(
    *,
    state: BacktestState,
    data: HistoricalDataStore,
    historical_fundamentals: HistoricalFundamentalStore | None,
    fundamental_profile: str,
    fundamental_stats: dict[str, Any],
    tickers: list[str],
    blocked_tickers: set[str],
    price_space_reset_dates: dict[str, date],
    price_space_reset_min_support_bars: int,
    todays_bars: dict[str, PriceBar],
    stock_bars: dict[str, list[PriceBar]],
    day: date,
    config: dict[str, Any],
    cc_risk_profile: str,
    dte_min: int,
    dte_max: int,
    slippage_pct: float,
    risk_free_rate: float,
    option_fee_per_contract: float,
    max_orders: int,
    execution_model: BacktestExecutionModel,
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
    rejected_reason_counts: Counter[str],
    latest_close: dict[str, float],
) -> int:
    opened = 0
    for ticker in tickers:
        if opened >= max_orders:
            break
        if ticker in blocked_tickers or ticker not in todays_bars:
            continue
        stock = state.stocks.get(ticker)
        if stock is None or _uncovered_shares(state, ticker) < 100:
            continue
        reset_date = price_space_reset_dates.get(ticker)
        if reset_date is not None:
            post_reset_count = _post_reset_support_bar_count(
                stock_bars.get(ticker, []),
                reset_date,
                day,
            )
            min_required = int(price_space_reset_min_support_bars)
            if post_reset_count < min_required:
                _reject(
                    events,
                    rejected_reason_counts,
                    day,
                    ticker,
                    "cc_post_split_reset_insufficient_support_history",
                    diagnostics={
                        "phase": "covered_call",
                        "price_space_reset_date": reset_date.isoformat(),
                        "post_reset_support_bar_count": post_reset_count,
                        "post_reset_min_support_bars": min_required,
                        "why_no_cc_after_assignment": (
                            "cc_post_split_reset_insufficient_support_history"
                        ),
                    },
                )
                continue
        option, rejection_summary, selection_diagnostics = _select_covered_call_for_day(
            data=data,
            ticker=ticker,
            day=day,
            stock=stock,
            config=config,
            dte_min=dte_min,
            dte_max=dte_max,
            slippage_pct=slippage_pct,
            risk_free_rate=risk_free_rate,
            stock_price=latest_close.get(ticker),
            execution_model=execution_model,
        )
        if option is None:
            reason = _primary_rejection_reason(rejection_summary)
            _reject(
                events,
                rejected_reason_counts,
                day,
                ticker,
                reason,
                diagnostics={
                    "phase": "covered_call",
                    "cc_risk_profile": cc_risk_profile,
                    "cc_rejection_summary": rejection_summary,
                    "cc_selection_diagnostics": selection_diagnostics,
                    "why_no_cc_after_assignment": reason,
                },
            )
            continue
        fundamental_context = _fundamental_context(
            historical_fundamentals=historical_fundamentals,
            profile=fundamental_profile,
            ticker=ticker,
            day=day,
            bars=_bars_after_price_space_reset(
                stock_bars.get(ticker, []),
                price_space_reset_dates.get(ticker),
            ),
            config=config,
        )
        cc_earnings_gate: GateResult | None = None
        cc_ex_dividend_gate: GateResult | None = None
        if fundamental_context is not None:
            _record_fundamental_context(
                fundamental_stats,
                events,
                day,
                ticker,
                fundamental_context,
                phase="covered_call",
            )
            snapshot = fundamental_context["snapshot"]
            cc_earnings_gate = evaluate_covered_call_earnings_gate(
                snapshot,
                option,
                as_of=day,
                config=config,
            )
            cc_ex_dividend_gate = evaluate_covered_call_ex_dividend_gate(
                snapshot,
                option,
                as_of=day,
                config=config,
            )
            _record_gate_stats(
                fundamental_stats,
                "cc_earnings_gate",
                cc_earnings_gate,
            )
            _record_gate_stats(
                fundamental_stats,
                "cc_ex_dividend_gate",
                cc_ex_dividend_gate,
            )
            blocking_gate = _covered_call_blocking_gate(
                fundamental_profile,
                cc_earnings_gate,
                cc_ex_dividend_gate,
            )
            if blocking_gate is not None:
                _reject(
                    events,
                    rejected_reason_counts,
                    day,
                    ticker,
                    _gate_primary_reason("covered_call_risk_gate", blocking_gate),
                    diagnostics={
                        **_fundamental_context_payload(fundamental_context),
                        "phase": "covered_call",
                        "cc_risk_profile": cc_risk_profile,
                        "cc_selection_diagnostics": selection_diagnostics,
                        "cc_earnings_gate": asdict(cc_earnings_gate),
                        "cc_ex_dividend_gate": asdict(cc_ex_dividend_gate),
                        "why_no_cc_after_assignment": _gate_primary_reason(
                            "covered_call_risk_gate",
                            blocking_gate,
                        ),
                    },
                )
                continue
        contracts = min(
            int(_uncovered_shares(state, ticker) // 100),
            max(1, int(config.get("cc_selector", {}).get("default_contract_quantity", 1))),
        )
        if contracts <= 0:
            continue
        if _open_short_call(
            state=state,
            option=option,
            day=day,
            ticker=ticker,
            contracts=contracts,
            option_fee_per_contract=option_fee_per_contract,
            execution_model=execution_model,
            trades=trades,
            events=events,
            adjusted_cost_basis=stock.adjusted_average_cost,
            diagnostics={
                **(
                    _fundamental_context_payload(fundamental_context)
                    if fundamental_context is not None
                    else {}
                ),
                **(
                    {"cc_earnings_gate": asdict(cc_earnings_gate)}
                    if cc_earnings_gate is not None
                    else {}
                ),
                **(
                    {"cc_ex_dividend_gate": asdict(cc_ex_dividend_gate)}
                    if cc_ex_dividend_gate is not None
                    else {}
                ),
                "phase": "covered_call",
                "cc_risk_profile": cc_risk_profile,
                "cc_selection_diagnostics": selection_diagnostics,
            },
        ):
            opened += 1
    return opened


def _select_covered_call_for_day(
    *,
    data: HistoricalDataStore,
    ticker: str,
    day: date,
    stock: StockPosition,
    config: dict[str, Any],
    dte_min: int,
    dte_max: int,
    slippage_pct: float,
    risk_free_rate: float,
    stock_price: float | None,
    execution_model: BacktestExecutionModel,
) -> tuple[OptionQuote | None, dict[str, int], dict[str, Any]]:
    options = data.option_chain(
        ticker,
        day,
        dte_min=dte_min,
        dte_max=dte_max,
        option_type="call",
        price_field="open",
        slippage_pct=slippage_pct,
        risk_free_rate=risk_free_rate,
        stock_price=stock_price,
        execution_model=execution_model,
    )
    selection_diagnostics = _covered_call_selection_diagnostics(
        options,
        stock=stock,
        config=config,
    )
    if not options:
        return None, {"no_fillable_call_options": 1}, selection_diagnostics

    cc_cfg = config.get("cc_selector", {})
    min_strike = _min_covered_call_strike(stock, config)
    min_bid = float(cc_cfg.get("min_bid", 0.10))
    max_spread = float(cc_cfg.get("max_spread_pct_of_mid", 0.15))
    min_oi = int(cc_cfg.get("min_open_interest", 0))
    min_delta = float(cc_cfg.get("target_delta_min", 0.10))
    max_delta = float(cc_cfg.get("target_delta_max", 0.35))
    eligible: list[OptionQuote] = []
    rejected: Counter[str] = Counter()
    for option in options:
        reasons: list[str] = []
        if min_strike is None:
            reasons.append("adjusted_cost_basis_missing")
        elif option.strike < min_strike:
            reasons.append("strike_below_adjusted_cost_basis")
        if option.bid < min_bid:
            reasons.append("bid_below_min")
        spread = option.spread_pct_of_mid
        if spread is None:
            reasons.append("missing_spread")
        elif spread > max_spread:
            reasons.append("spread_too_wide")
        if option.open_interest is not None and option.open_interest < min_oi:
            reasons.append("open_interest_below_min")
        if option.delta is None:
            reasons.append("missing_delta")
        elif option.delta < min_delta or option.delta > max_delta:
            reasons.append("delta_outside_target")
        if reasons:
            rejected.update(reasons)
        else:
            eligible.append(option)
    if not eligible:
        return (
            None,
            dict(rejected) or {"no_eligible_call_contract": 1},
            selection_diagnostics,
        )
    midpoint = (min_delta + max_delta) / 2.0
    eligible.sort(
        key=lambda option: (
            option.dte,
            abs(float(option.delta or 0.0) - midpoint),
            -option.bid,
            option.strike,
        )
    )
    selected = eligible[0]
    selection_diagnostics["selected_call"] = _covered_call_option_diagnostics(
        selected,
        min_strike=min_strike,
    )
    return selected, dict(rejected), selection_diagnostics


def _open_short_call(
    *,
    state: BacktestState,
    option: OptionQuote,
    day: date,
    ticker: str,
    contracts: int,
    option_fee_per_contract: float,
    execution_model: BacktestExecutionModel,
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
    adjusted_cost_basis: float | None,
    diagnostics: dict[str, Any] | None = None,
) -> bool:
    entry_price = entry_fill_price(option, execution_model, side="sell")
    if entry_price is None or entry_price <= 0:
        return False
    execution_diagnostics = entry_fill_diagnostics(
        option, execution_model, side="sell"
    )
    fees = option_fee_per_contract * contracts
    gross_credit = entry_price * 100.0 * contracts
    trade_id = f"{day.isoformat()}-{ticker}-{len(trades) + 1}"
    position = ShortCallPosition(
        trade_id=trade_id,
        ticker=ticker,
        symbol=option.symbol,
        expiration=option.expiration,
        strike=option.strike,
        contracts=contracts,
        entry_date=day,
        entry_price=entry_price,
        gross_credit=gross_credit,
        fees=fees,
        delta=option.delta,
        adjusted_cost_basis=adjusted_cost_basis,
    )
    state.cash += gross_credit - fees
    state.total_fees += fees
    state.open_short_calls.append(position)
    trades.append(
        {
            "trade_id": trade_id,
            "ticker": ticker,
            "strategy": "covered_call",
            "status": "OPEN",
            "entry_date": day.isoformat(),
            "expiration": option.expiration.isoformat(),
            "symbol": option.symbol,
            "strike": option.strike,
            "contracts": contracts,
            "entry_price": entry_price,
            "entry_market_bid": execution_diagnostics["market_bid"],
            "entry_market_ask": execution_diagnostics["market_ask"],
            "entry_market_mid": execution_diagnostics["market_mid"],
            "entry_spread_pct_of_mid": execution_diagnostics["spread_pct_of_mid"],
            "entry_fill_discount_pct_of_mid": execution_diagnostics[
                "fill_discount_pct_of_mid"
            ],
            "execution_model": execution_diagnostics["execution_model"],
            "execution_fill_policy": execution_diagnostics["fill_policy"],
            "execution_reference_price_source": execution_diagnostics[
                "reference_price_source"
            ],
            "execution_calibration_status": execution_diagnostics[
                "calibration_status"
            ],
            "gross_credit": gross_credit,
            "fees": fees,
            "net_credit": gross_credit - fees,
            "delta": option.delta,
            "adjusted_cost_basis": adjusted_cost_basis,
        }
    )
    events.append(
        {
            "date": day.isoformat(),
            "ticker": ticker,
            "type": "OPEN_SHORT_CALL",
            "trade_id": trade_id,
            "symbol": option.symbol,
            "strike": option.strike,
            "expiration": option.expiration.isoformat(),
            "contracts": contracts,
            "entry_price": entry_price,
            "execution": execution_diagnostics,
            "gross_credit": gross_credit,
            "fees": fees,
            "adjusted_cost_basis": adjusted_cost_basis,
            "diagnostics": diagnostics or {},
        }
    )
    return True


def _manage_open_short_puts_for_day(
    *,
    state: BacktestState,
    data: HistoricalDataStore,
    day: date,
    latest_close: dict[str, float],
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
    execution_model: BacktestExecutionModel,
    option_fee_per_contract: float,
    risk_free_rate: float,
    exit_model: str,
    profit_take_pct: float,
    profit_take_price_field: str,
    manage_at_dte: int,
) -> None:
    if exit_model == "hold_to_expiration":
        return
    still_open: list[ShortPutPosition] = []
    for position in state.open_short_puts:
        if position.expiration <= day:
            still_open.append(position)
            continue
        close_reason = _option_management_close_reason(
            position=position,
            day=day,
            exit_model=exit_model,
            profit_take_pct=profit_take_pct,
            profit_take_price_field=profit_take_price_field,
            manage_at_dte=manage_at_dte,
            data=data,
            latest_close=latest_close,
            risk_free_rate=risk_free_rate,
        )
        if close_reason is None:
            still_open.append(position)
            continue
        mark = close_reason["mark"]
        close_price = entry_fill_price(mark, execution_model, side="buy")
        if close_price is None or close_price < 0:
            still_open.append(position)
            continue
        close_debit = close_price * 100.0 * position.contracts
        close_fees = option_fee_per_contract * position.contracts
        realized_pnl = position.gross_credit - position.fees - close_debit - close_fees
        state.cash -= close_debit + close_fees
        state.total_fees += close_fees
        state.realized_option_pnl += realized_pnl
        status = (
            "CLOSED_PROFIT_TARGET"
            if close_reason["reason"] == "profit_target"
            else "CLOSED_MANAGE_DTE"
        )
        _close_trade(
            trades,
            position.trade_id,
            status=status,
            close_date=day,
            underlying_close=latest_close.get(position.ticker, 0.0),
            realized_option_pnl=realized_pnl,
            close_price=close_price,
            close_fees=close_fees,
            close_reason=close_reason["reason"],
        )
        events.append(
            {
                "date": day.isoformat(),
                "ticker": position.ticker,
                "type": status,
                "trade_id": position.trade_id,
                "symbol": position.symbol,
                "close_price": close_price,
                "close_debit": close_debit,
                "fees": close_fees,
                "realized_option_pnl": round(realized_pnl, 2),
                "reason": close_reason["reason"],
                "dte": close_reason["dte"],
                "profit_take_target_price": close_reason["target_price"],
                "price_field": profit_take_price_field,
            }
        )
    state.open_short_puts = still_open


def _manage_open_short_calls_for_day(
    *,
    state: BacktestState,
    data: HistoricalDataStore,
    day: date,
    latest_close: dict[str, float],
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
    execution_model: BacktestExecutionModel,
    option_fee_per_contract: float,
    risk_free_rate: float,
    exit_model: str,
    profit_take_pct: float,
    profit_take_price_field: str,
    manage_at_dte: int,
) -> None:
    if exit_model == "hold_to_expiration":
        return
    still_open: list[ShortCallPosition] = []
    for position in state.open_short_calls:
        if position.expiration <= day:
            still_open.append(position)
            continue
        close_reason = _option_management_close_reason(
            position=position,
            day=day,
            exit_model=exit_model,
            profit_take_pct=profit_take_pct,
            profit_take_price_field=profit_take_price_field,
            manage_at_dte=manage_at_dte,
            data=data,
            latest_close=latest_close,
            risk_free_rate=risk_free_rate,
        )
        if close_reason is None:
            still_open.append(position)
            continue
        mark = close_reason["mark"]
        close_price = entry_fill_price(mark, execution_model, side="buy")
        if close_price is None or close_price < 0:
            still_open.append(position)
            continue
        close_debit = close_price * 100.0 * position.contracts
        close_fees = option_fee_per_contract * position.contracts
        realized_pnl = position.gross_credit - position.fees - close_debit - close_fees
        state.cash -= close_debit + close_fees
        state.total_fees += close_fees
        state.realized_option_pnl += realized_pnl
        stock = state.stocks.setdefault(
            position.ticker,
            StockPosition(ticker=position.ticker),
        )
        stock.premium_credit_total += realized_pnl
        status = (
            "CALL_CLOSED_PROFIT_TARGET"
            if close_reason["reason"] == "profit_target"
            else "CALL_CLOSED_MANAGE_DTE"
        )
        _close_trade(
            trades,
            position.trade_id,
            status=status,
            close_date=day,
            underlying_close=latest_close.get(position.ticker, 0.0),
            realized_option_pnl=realized_pnl,
            close_price=close_price,
            close_fees=close_fees,
            close_reason=close_reason["reason"],
        )
        events.append(
            {
                "date": day.isoformat(),
                "ticker": position.ticker,
                "type": status,
                "trade_id": position.trade_id,
                "symbol": position.symbol,
                "close_price": close_price,
                "close_debit": close_debit,
                "fees": close_fees,
                "realized_option_pnl": round(realized_pnl, 2),
                "reason": close_reason["reason"],
                "dte": close_reason["dte"],
                "profit_take_target_price": close_reason["target_price"],
                "price_field": profit_take_price_field,
            }
        )
    state.open_short_calls = still_open


def _option_management_close_reason(
    *,
    position: ShortPutPosition | ShortCallPosition,
    day: date,
    exit_model: str,
    profit_take_pct: float,
    profit_take_price_field: str,
    manage_at_dte: int,
    data: HistoricalDataStore,
    latest_close: dict[str, float],
    risk_free_rate: float,
) -> dict[str, Any] | None:
    dte = (position.expiration - day).days
    initial_dte = (position.expiration - position.entry_date).days
    mark = data.option_mark(
        position.symbol,
        day,
        price_field=profit_take_price_field,
        stock_price=latest_close.get(position.ticker),
        risk_free_rate=risk_free_rate,
    )
    target_price = position.entry_price * (1.0 - profit_take_pct)
    allows_profit_target = exit_model in {
        "close_at_50pct_profit_or_expiry",
        "close_at_50pct_profit_or_manage_dte_or_expiry",
    }
    if (
        allows_profit_target
        and mark is not None
        and mark.mid <= target_price
    ):
        return {
            "reason": "profit_target",
            "mark": mark,
            "dte": dte,
            "target_price": round(target_price, 6),
        }
    allows_manage_dte = exit_model in {
        "manage_at_dte_or_expiry",
        "close_at_50pct_profit_or_manage_dte_or_expiry",
    }
    if (
        allows_manage_dte
        and manage_at_dte > 0
        and initial_dte > manage_at_dte
        and dte <= manage_at_dte
        and mark is not None
    ):
        return {
            "reason": "manage_dte",
            "mark": mark,
            "dte": dte,
            "target_price": round(target_price, 6),
        }
    return None


def _resolve_expiring_short_puts(
    *,
    state: BacktestState,
    day: date,
    todays_close: dict[str, float],
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> None:
    still_open: list[ShortPutPosition] = []
    for position in state.open_short_puts:
        if position.expiration > day:
            still_open.append(position)
            continue
        underlying_close = todays_close.get(position.ticker)
        if underlying_close is None:
            still_open.append(position)
            if position.settlement_missing_since is None:
                position.settlement_missing_since = day
                events.append(
                    {
                        "date": day.isoformat(),
                        "ticker": position.ticker,
                        "type": "EXPIRATION_MISSING_UNDERLYING_CLOSE",
                        "trade_id": position.trade_id,
                        "expiration": position.expiration.isoformat(),
                    }
                )
            continue
        if position.settlement_missing_since is not None:
            events.append(
                {
                    "date": day.isoformat(),
                    "ticker": position.ticker,
                    "type": "RESOLVED_AFTER_MISSING_EXPIRATION_CLOSE",
                    "trade_id": position.trade_id,
                    "expiration": position.expiration.isoformat(),
                    "missing_since": position.settlement_missing_since.isoformat(),
                    "settlement_close_date": day.isoformat(),
                }
            )
        if underlying_close < position.strike:
            stock = state.stocks.setdefault(
                position.ticker, StockPosition(ticker=position.ticker)
            )
            assigned_shares = 100 * position.contracts
            assignment_cost = position.strike * assigned_shares
            net_credit = position.gross_credit - position.fees
            stock.shares += assigned_shares
            stock.cost_basis_total += assignment_cost
            stock.premium_credit_total += net_credit
            state.cash -= assignment_cost
            state.realized_option_pnl += net_credit
            _close_trade(
                trades,
                position.trade_id,
                status="ASSIGNED",
                close_date=day,
                underlying_close=underlying_close,
                realized_option_pnl=net_credit,
            )
            events.append(
                {
                    "date": day.isoformat(),
                    "ticker": position.ticker,
                    "type": "ASSIGNED",
                    "trade_id": position.trade_id,
                    "symbol": position.symbol,
                    "strike": position.strike,
                    "underlying_close": underlying_close,
                    "shares_acquired": assigned_shares,
                    "assignment_cost": assignment_cost,
                }
            )
            continue

        state.realized_option_pnl += position.gross_credit - position.fees
        _close_trade(
            trades,
            position.trade_id,
            status="EXPIRED_WORTHLESS",
            close_date=day,
            underlying_close=underlying_close,
            realized_option_pnl=position.gross_credit - position.fees,
        )
        events.append(
            {
                "date": day.isoformat(),
                "ticker": position.ticker,
                "type": "EXPIRED_WORTHLESS",
                "trade_id": position.trade_id,
                "symbol": position.symbol,
                "strike": position.strike,
                "underlying_close": underlying_close,
            }
        )
    state.open_short_puts = still_open


def _resolve_expiring_short_calls(
    *,
    state: BacktestState,
    day: date,
    todays_close: dict[str, float],
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> None:
    still_open: list[ShortCallPosition] = []
    for position in state.open_short_calls:
        if position.expiration > day:
            still_open.append(position)
            continue
        underlying_close = todays_close.get(position.ticker)
        if underlying_close is None:
            still_open.append(position)
            if position.settlement_missing_since is None:
                position.settlement_missing_since = day
                events.append(
                    {
                        "date": day.isoformat(),
                        "ticker": position.ticker,
                        "type": "CALL_EXPIRATION_MISSING_UNDERLYING_CLOSE",
                        "trade_id": position.trade_id,
                        "expiration": position.expiration.isoformat(),
                    }
                )
            continue
        if position.settlement_missing_since is not None:
            events.append(
                {
                    "date": day.isoformat(),
                    "ticker": position.ticker,
                    "type": "CALL_RESOLVED_AFTER_MISSING_EXPIRATION_CLOSE",
                    "trade_id": position.trade_id,
                    "expiration": position.expiration.isoformat(),
                    "missing_since": position.settlement_missing_since.isoformat(),
                    "settlement_close_date": day.isoformat(),
                }
            )
        net_credit = position.gross_credit - position.fees
        state.realized_option_pnl += net_credit
        stock = state.stocks.setdefault(
            position.ticker, StockPosition(ticker=position.ticker)
        )
        if underlying_close > position.strike:
            called_shares = 100 * position.contracts
            removed_cost, _removed_premium = _remove_stock_shares(stock, called_shares)
            sale_proceeds = position.strike * called_shares
            state.cash += sale_proceeds
            realized_stock_pnl = sale_proceeds - removed_cost
            state.realized_stock_pnl += realized_stock_pnl
            _close_trade(
                trades,
                position.trade_id,
                status="CALLED_AWAY",
                close_date=day,
                underlying_close=underlying_close,
                realized_option_pnl=net_credit,
                realized_stock_pnl=realized_stock_pnl,
            )
            events.append(
                {
                    "date": day.isoformat(),
                    "ticker": position.ticker,
                    "type": "CALLED_AWAY",
                    "trade_id": position.trade_id,
                    "symbol": position.symbol,
                    "strike": position.strike,
                    "underlying_close": underlying_close,
                    "shares_sold": called_shares,
                    "sale_proceeds": sale_proceeds,
                    "realized_stock_pnl": realized_stock_pnl,
                }
            )
            continue

        stock.premium_credit_total += net_credit
        _close_trade(
            trades,
            position.trade_id,
            status="EXPIRED_WORTHLESS",
            close_date=day,
            underlying_close=underlying_close,
            realized_option_pnl=net_credit,
        )
        events.append(
            {
                "date": day.isoformat(),
                "ticker": position.ticker,
                "type": "CALL_EXPIRED_WORTHLESS",
                "trade_id": position.trade_id,
                "symbol": position.symbol,
                "strike": position.strike,
                "underlying_close": underlying_close,
            }
        )
    state.open_short_calls = still_open


def _mark_state_equity(
    state: BacktestState,
    latest_close: dict[str, float],
    data: HistoricalDataStore,
    day: date,
    *,
    data_issues: list[dict[str, Any]] | None = None,
) -> float:
    stock_value = sum(
        position.shares * latest_close.get(ticker, 0.0)
        for ticker, position in state.stocks.items()
        if position.shares > 0
    )
    option_liability = 0.0
    for position in state.open_short_puts:
        close = latest_close.get(position.ticker)
        mark = data.option_mark(
            position.symbol,
            day,
            price_field="close",
            stock_price=close,
        )
        if mark is not None:
            option_liability += mark.mid * 100.0 * position.contracts
        elif close is not None:
            if data_issues is not None and position.expiration >= day:
                data_issues.append(
                    {
                        "date": day.isoformat(),
                        "ticker": position.ticker,
                        "type": "missing_option_mark",
                        "details": {
                            "trade_id": position.trade_id,
                            "symbol": position.symbol,
                            "expiration": position.expiration.isoformat(),
                            "fallback": "intrinsic_value",
                        },
                    }
                )
            option_liability += max(0.0, position.strike - close) * 100.0 * position.contracts
    for position in state.open_short_calls:
        close = latest_close.get(position.ticker)
        mark = data.option_mark(
            position.symbol,
            day,
            price_field="close",
            stock_price=close,
        )
        if mark is not None:
            option_liability += mark.mid * 100.0 * position.contracts
        elif close is not None:
            if data_issues is not None and position.expiration >= day:
                data_issues.append(
                    {
                        "date": day.isoformat(),
                        "ticker": position.ticker,
                        "type": "missing_option_mark",
                        "details": {
                            "trade_id": position.trade_id,
                            "symbol": position.symbol,
                            "expiration": position.expiration.isoformat(),
                            "fallback": "intrinsic_value",
                        },
                    }
                )
            option_liability += max(0.0, close - position.strike) * 100.0 * position.contracts
    return state.cash + stock_value - option_liability


def _daily_equity_row(
    state: BacktestState,
    day: date,
    latest_close: dict[str, float],
) -> dict[str, Any]:
    exposure = _stock_exposure_metrics(state, latest_close)
    reserved_assignment_cash = state.reserved_assignment_cash
    capital_deployed = reserved_assignment_cash + float(exposure["stock_value"])
    capital_utilization_pct = (
        round(capital_deployed / state.equity * 100.0, 4)
        if state.equity > 0
        else None
    )
    reserved_assignment_cash_pct = (
        round(reserved_assignment_cash / state.equity * 100.0, 4)
        if state.equity > 0
        else None
    )
    assigned_stock_utilization_pct = (
        round(float(exposure["stock_value"]) / state.equity * 100.0, 4)
        if state.equity > 0
        else None
    )
    cash_pct = round(state.cash / state.equity * 100.0, 4) if state.equity > 0 else None
    return {
        "date": day.isoformat(),
        "cash": round(state.cash, 2),
        "equity": round(state.equity, 2),
        "stock_value": exposure["stock_value"],
        "assigned_shares": exposure["assigned_shares"],
        "covered_assigned_shares": exposure["covered_assigned_shares"],
        "uncovered_assigned_shares": exposure["uncovered_assigned_shares"],
        "stock_unrealized_pnl": exposure["stock_unrealized_pnl"],
        "stock_unrealized_pnl_after_premiums": exposure[
            "stock_unrealized_pnl_after_premiums"
        ],
        "reserved_assignment_cash": round(reserved_assignment_cash, 2),
        "capital_deployed": round(capital_deployed, 2),
        "open_short_puts": len(state.open_short_puts),
        "open_short_calls": len(state.open_short_calls),
        "long_stock_positions": sum(1 for stock in state.stocks.values() if stock.shares > 0),
        "capital_utilization_pct": capital_utilization_pct,
        "reserved_assignment_cash_pct": reserved_assignment_cash_pct,
        "assigned_stock_utilization_pct": assigned_stock_utilization_pct,
        "cash_pct": cash_pct,
        "cash_idle": capital_deployed <= 0,
    }


def _stock_exposure_metrics(
    state: BacktestState,
    latest_close: dict[str, float],
) -> dict[str, Any]:
    stock_value = 0.0
    stock_unrealized_pnl = 0.0
    stock_unrealized_pnl_after_premiums = 0.0
    assigned_shares = 0
    covered_assigned_shares = 0
    uncovered_assigned_shares = 0
    for ticker, position in state.stocks.items():
        if position.shares <= 0:
            continue
        close = latest_close.get(ticker, 0.0)
        value = position.shares * close
        stock_value += value
        stock_unrealized_pnl += value - position.cost_basis_total
        stock_unrealized_pnl_after_premiums += (
            value - (position.cost_basis_total - position.premium_credit_total)
        )
        assigned_shares += position.shares
        covered = min(
            position.shares,
            sum(
                call.contracts * 100
                for call in state.open_short_calls
                if call.ticker == ticker
            ),
        )
        covered_assigned_shares += covered
        uncovered_assigned_shares += max(0, position.shares - covered)
    return {
        "stock_value": round(stock_value, 2),
        "assigned_shares": assigned_shares,
        "covered_assigned_shares": covered_assigned_shares,
        "uncovered_assigned_shares": uncovered_assigned_shares,
        "stock_unrealized_pnl": round(stock_unrealized_pnl, 2),
        "stock_unrealized_pnl_after_premiums": round(
            stock_unrealized_pnl_after_premiums,
            2,
        ),
    }


def _portfolio_snapshot(state: BacktestState, equity: float) -> PortfolioSnapshot:
    positions: list[BrokerPosition] = []
    for stock in state.stocks.values():
        if stock.shares:
            positions.append(
                BrokerPosition(
                    symbol=stock.ticker,
                    qty=stock.shares,
                    asset_class="us_equity",
                    side="long",
                    cost_basis=stock.cost_basis_total,
                )
            )
    for put in state.open_short_puts:
        positions.append(
            BrokerPosition(
                symbol=put.symbol,
                qty=-put.contracts,
                asset_class="option",
                side="short",
                underlying_symbol=put.ticker,
                option_type="put",
                expiration=put.expiration,
                strike=put.strike,
            )
        )
    for call in state.open_short_calls:
        positions.append(
            BrokerPosition(
                symbol=call.symbol,
                qty=-call.contracts,
                asset_class="option",
                side="short",
                underlying_symbol=call.ticker,
                option_type="call",
                expiration=call.expiration,
                strike=call.strike,
            )
        )
    return PortfolioSnapshot(
        account=BrokerAccountSnapshot(
            status="ACTIVE",
            equity=equity,
            cash=state.cash,
            buying_power=state.cash,
        ),
        positions=positions,
        source="backtest_synthetic",
    )


def _load_market_regime_bars(
    *,
    data: HistoricalDataStore,
    symbols: set[str],
    history_start: date,
    end: date,
) -> tuple[dict[str, list[PriceBar]], list[dict[str, Any]]]:
    bars_by_symbol: dict[str, list[PriceBar]] = {}
    issues: list[dict[str, Any]] = []
    for symbol in sorted(symbols):
        try:
            loaded = data.load_stock_bars([symbol], history_start, end)
        except Exception as exc:
            bars_by_symbol[symbol] = []
            issues.append(
                {
                    "date": history_start.isoformat(),
                    "ticker": symbol,
                    "type": "market_regime_indicator_unavailable",
                    "details": {
                        "symbol": symbol,
                        "history_start": history_start.isoformat(),
                        "end": end.isoformat(),
                        "error": str(exc),
                    },
                }
            )
            continue
        bars_by_symbol[symbol] = loaded.get(symbol, [])
    return bars_by_symbol, issues


def _market_regime_config(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config.get("market_regime") or {})
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "market_symbol": str(cfg.get("market_symbol") or "SPY").upper(),
        "require_market_price_above_sma200": bool(
            cfg.get("require_market_price_above_sma200", False)
        ),
        "require_market_sma200_slope_non_negative": bool(
            cfg.get("require_market_sma200_slope_non_negative", False)
        ),
        "market_sma200_slope_lookback_days": int(
            cfg.get("market_sma200_slope_lookback_days", 20)
        ),
        "reject_unknown_market_trend": bool(
            cfg.get("reject_unknown_market_trend", True)
        ),
        "vix_enabled": bool(cfg.get("vix_enabled", False)),
        "vix_symbol": str(cfg.get("vix_symbol") or "I:VIX").upper(),
        "min_vix": float(cfg.get("min_vix", 0.0)),
        "max_vix": float(cfg.get("max_vix", 999.0)),
        "reject_unknown_vix": bool(cfg.get("reject_unknown_vix", False)),
    }


def _market_regime_symbols(config: dict[str, Any]) -> set[str]:
    cfg = _market_regime_config(config)
    conditional_cfg = _conditional_csp_overrides_config(config)
    if not cfg["enabled"] and not conditional_cfg["enabled"]:
        return set()
    symbols = {cfg["market_symbol"]}
    if cfg["vix_enabled"]:
        symbols.add(cfg["vix_symbol"])
    return {symbol for symbol in symbols if symbol}


def _evaluate_market_regime_gate(
    config: dict[str, Any],
    bars_by_symbol: dict[str, list[PriceBar]],
    day: date,
) -> GateResult:
    cfg = _market_regime_config(config)
    if not cfg["enabled"]:
        return GateResult(status="PASS", reasons=["market_regime_disabled"], warnings=[])
    reasons: list[str] = []
    warnings: list[str] = []
    market_payload = _market_trend_payload(
        bars_by_symbol.get(cfg["market_symbol"], []),
        day=day,
        slope_lookback=int(cfg["market_sma200_slope_lookback_days"]),
    )
    if cfg["require_market_price_above_sma200"]:
        if market_payload["sma200"] is None:
            reason = f"market_sma200_unknown:{cfg['market_symbol']}"
            if cfg["reject_unknown_market_trend"]:
                reasons.append(reason)
            else:
                warnings.append(reason)
        elif market_payload["close"] <= market_payload["sma200"]:
            reasons.append(f"market_price_below_sma200:{cfg['market_symbol']}")
    if cfg["require_market_sma200_slope_non_negative"]:
        if market_payload["sma200_slope"] is None:
            reason = f"market_sma200_slope_unknown:{cfg['market_symbol']}"
            if cfg["reject_unknown_market_trend"]:
                reasons.append(reason)
            else:
                warnings.append(reason)
        elif market_payload["sma200_slope"] < 0:
            reasons.append(f"market_sma200_slope_negative:{cfg['market_symbol']}")

    vix_payload = None
    if cfg["vix_enabled"]:
        vix_payload = _latest_bar_payload(
            bars_by_symbol.get(cfg["vix_symbol"], []),
            day=day,
        )
        vix_close = vix_payload.get("close") if vix_payload else None
        if vix_close is None:
            reason = f"vix_unknown:{cfg['vix_symbol']}"
            if cfg["reject_unknown_vix"]:
                reasons.append(reason)
            else:
                warnings.append(reason)
        elif float(vix_close) < cfg["min_vix"]:
            reasons.append(f"vix_below_min:{float(vix_close):.2f}<{cfg['min_vix']:.2f}")
        elif float(vix_close) > cfg["max_vix"]:
            reasons.append(f"vix_above_max:{float(vix_close):.2f}>{cfg['max_vix']:.2f}")

    status = "REJECT" if reasons else ("WARN" if warnings else "PASS")
    if market_payload.get("as_of") is not None:
        warnings.append(
            "market_trend:"
            f"{cfg['market_symbol']}@{market_payload['as_of']}"
            f":close={market_payload['close']}"
            f":sma200={market_payload['sma200']}"
            f":slope={market_payload['sma200_slope']}"
        )
    if vix_payload is not None:
        warnings.append(
            "vix:"
            f"{cfg['vix_symbol']}@{vix_payload['as_of']}"
            f":close={vix_payload['close']}"
        )
    return GateResult(
        status=status,
        reasons=reasons or ["market_regime_passed"],
        warnings=warnings,
    )


def _market_trend_payload(
    bars: list[PriceBar],
    *,
    day: date,
    slope_lookback: int,
) -> dict[str, Any]:
    prior = [bar for bar in bars if bar.date < day]
    if not prior:
        return {"close": None, "as_of": None, "sma200": None, "sma200_slope": None}
    closes = [bar.close for bar in prior]
    sma200_series = sma_values(closes, 200)
    sma200 = sma200_series[-1]
    old_sma = (
        sma200_series[-1 - slope_lookback]
        if len(sma200_series) > slope_lookback
        else None
    )
    slope = None if sma200 is None or old_sma is None else sma200 - old_sma
    return {
        "close": prior[-1].close,
        "as_of": prior[-1].date.isoformat(),
        "sma200": round(sma200, 6) if sma200 is not None else None,
        "sma200_slope": round(slope, 6) if slope is not None else None,
    }


def _latest_bar_payload(
    bars: list[PriceBar],
    *,
    day: date,
) -> dict[str, Any] | None:
    prior = [bar for bar in bars if bar.date < day]
    if not prior:
        return None
    bar = prior[-1]
    return {
        "as_of": bar.date.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _conditional_csp_overrides_config(config: dict[str, Any]) -> dict[str, Any]:
    market_cfg = dict(config.get("market_regime") or {})
    cfg = dict(market_cfg.get("conditional_csp_overrides") or {})
    below_cfg = dict(cfg.get("when_market_price_below_sma200") or {})
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "market_symbol": str(
            cfg.get("market_symbol") or market_cfg.get("market_symbol") or "SPY"
        ).upper(),
        "slope_lookback_days": int(
            cfg.get(
                "market_sma200_slope_lookback_days",
                market_cfg.get("market_sma200_slope_lookback_days", 20),
            )
        ),
        "when_market_price_below_sma200": {
            "patch": dict(below_cfg.get("patch") or {}),
        },
    }


def _conditional_csp_config_for_day(
    config: dict[str, Any],
    bars_by_symbol: dict[str, list[PriceBar]],
    day: date,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    cfg = _conditional_csp_overrides_config(config)
    if not cfg["enabled"]:
        return config, None
    market_symbol = cfg["market_symbol"]
    market_payload = _market_trend_payload(
        bars_by_symbol.get(market_symbol, []),
        day=day,
        slope_lookback=int(cfg["slope_lookback_days"]),
    )
    diagnostics: dict[str, Any] = {
        "enabled": True,
        "market_symbol": market_symbol,
        "condition": "market_price_below_sma200",
        "market_trend": market_payload,
        "applied": False,
        "patch": {},
    }
    close = market_payload.get("close")
    sma200 = market_payload.get("sma200")
    if close is None or sma200 is None:
        diagnostics["reason"] = "market_trend_unknown"
        return config, diagnostics
    if float(close) > float(sma200):
        diagnostics["reason"] = "market_price_above_sma200"
        return config, diagnostics
    patch = cfg["when_market_price_below_sma200"]["patch"]
    diagnostics["applied"] = bool(patch)
    diagnostics["patch"] = patch
    diagnostics["reason"] = "market_price_below_sma200"
    if not patch:
        return config, diagnostics
    return _apply_dotted_overrides(config, patch), diagnostics


def _apply_dotted_overrides(
    config: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    patched = copy.deepcopy(config)
    for path, value in patch.items():
        target: Any = patched
        parts = str(path).split(".")
        if not parts:
            continue
        for part in parts[:-1]:
            if not isinstance(target, dict) or part not in target:
                raise ValueError(f"Unknown conditional CSP override path: {path}")
            target = target[part]
        leaf = parts[-1]
        if not isinstance(target, dict) or leaf not in target:
            raise ValueError(f"Unknown conditional CSP override path: {path}")
        target[leaf] = value
    return patched


def _new_market_regime_stats() -> dict[str, Any]:
    return {
        "days": 0,
        "status_counts": Counter(),
        "reason_counts": Counter(),
    }


def _record_market_regime_stats(
    stats: dict[str, Any],
    gate: GateResult,
) -> None:
    stats["days"] += 1
    stats["status_counts"][gate.status] += 1
    for reason in gate.reasons:
        stats["reason_counts"][reason] += 1


def _finalize_market_regime_stats(stats: dict[str, Any]) -> dict[str, Any]:
    days = int(stats.get("days") or 0)
    reject_days = int(stats["status_counts"].get("REJECT", 0))
    return {
        "days": days,
        "reject_days": reject_days,
        "reject_day_pct": round(reject_days / days * 100.0, 4) if days else 0.0,
        "status_counts": dict(sorted(stats["status_counts"].items())),
        "reason_counts": dict(stats["reason_counts"].most_common()),
    }


def _management_exit_models(config: dict[str, Any]) -> tuple[str, str]:
    cfg = dict(config.get("management") or {})
    csp_exit_model = str(cfg.get("csp_exit_model") or "hold_to_expiration")
    cc_exit_model = str(cfg.get("cc_exit_model") or "hold_to_expiration")
    for model in (csp_exit_model, cc_exit_model):
        if model not in EXIT_MODELS:
            allowed = ", ".join(sorted(EXIT_MODELS))
            raise ValueError(f"unsupported management exit model {model!r}; expected one of {allowed}")
    return csp_exit_model, cc_exit_model


def _management_profit_take_pct(config: dict[str, Any]) -> float:
    value = float(
        (config.get("management") or {}).get("profit_take_pct_of_credit", 0.50)
    )
    return min(max(value, 0.0), 1.0)


def _management_profit_take_price_field(config: dict[str, Any]) -> str:
    field = str((config.get("management") or {}).get("profit_take_price_field") or "low")
    if field not in {"open", "high", "low", "close"}:
        raise ValueError(f"unsupported management.profit_take_price_field {field!r}")
    return field


def _management_manage_at_dte(config: dict[str, Any]) -> int:
    return max(0, int((config.get("management") or {}).get("manage_at_dte", 21)))


def _normalize_fundamental_profile(profile: str) -> str:
    normalized = str(profile or "technical_only").strip().lower()
    if normalized not in FUNDAMENTAL_PROFILES:
        allowed = ", ".join(sorted(FUNDAMENTAL_PROFILES))
        raise ValueError(f"unsupported fundamental_profile {profile!r}; expected one of {allowed}")
    return normalized


def _normalize_cc_risk_profile(
    profile: str | None,
    config: dict[str, Any],
) -> str:
    if profile is None:
        profile = (config.get("backtest") or {}).get("cc_risk_profile", "strict")
    normalized = str(profile or "strict").strip().lower()
    if normalized not in CC_RISK_PROFILES:
        allowed = ", ".join(sorted(CC_RISK_PROFILES))
        raise ValueError(f"unsupported cc_risk_profile {profile!r}; expected one of {allowed}")
    return normalized


def _covered_call_backtest_config(
    config: dict[str, Any],
    cc_risk_profile: str,
) -> dict[str, Any]:
    if cc_risk_profile == "strict":
        return config
    gate_config = copy.deepcopy(config)
    cc_risk = dict(gate_config.get("cc_risk") or {})
    if cc_risk_profile == "warn_unknown_dates":
        cc_risk["block_unknown_stock_earnings_date"] = False
        cc_risk["block_unknown_ex_dividend_date_for_dividend_payers"] = False
    gate_config["cc_risk"] = cc_risk
    return gate_config


def _new_fundamental_stats(profile: str) -> dict[str, Any]:
    return {
        "profile": profile,
        "snapshot_count": 0,
        "diagnostic_event_limit": 5000,
        "diagnostic_events_emitted": 0,
        "diagnostic_events_suppressed": 0,
        "gate_status_counts": Counter(),
        "fundamental_gate_status_counts": Counter(),
        "field_quality_counts": defaultdict(Counter),
        "would_reject_reason_counts": Counter(),
        "warn_reason_counts": Counter(),
    }


def _fundamental_context(
    *,
    historical_fundamentals: HistoricalFundamentalStore | None,
    profile: str,
    ticker: str,
    day: date,
    bars: list[PriceBar],
    config: dict[str, Any],
    require_snapshot: bool = False,
) -> dict[str, Any] | None:
    if (profile == "technical_only" and not require_snapshot) or historical_fundamentals is None:
        return None
    try:
        snapshot = historical_fundamentals.snapshot(ticker, day, bars)
        gate = evaluate_fundamentals(snapshot, config)
    except Exception as exc:
        snapshot = FundamentalSnapshot(ticker=ticker.upper())
        gate = GateResult(
            status="WARN",
            reasons=["historical_fundamentals_unavailable"],
            warnings=[str(exc)],
        )
    return {"snapshot": snapshot, "fundamental_gate": gate}


def _record_fundamental_context(
    stats: dict[str, Any],
    events: list[dict[str, Any]],
    day: date,
    ticker: str,
    context: dict[str, Any],
    *,
    phase: str,
) -> None:
    snapshot: FundamentalSnapshot = context["snapshot"]
    gate: GateResult = context["fundamental_gate"]
    stats["snapshot_count"] += 1
    _record_gate_stats(stats, "fundamental_gate", gate)
    stats["fundamental_gate_status_counts"][gate.status] += 1
    for field, provenance in snapshot.provenance.items():
        stats["field_quality_counts"][field][provenance.quality] += 1
    if gate.status != "PASS":
        if stats["diagnostic_events_emitted"] >= stats["diagnostic_event_limit"]:
            stats["diagnostic_events_suppressed"] += 1
            return
        stats["diagnostic_events_emitted"] += 1
        events.append(
            {
                "date": day.isoformat(),
                "ticker": ticker,
                "type": "FUNDAMENTAL_DIAGNOSTIC",
                "phase": phase,
                "would_reject": gate.status == "REJECT",
                "fundamental_gate": asdict(gate),
                "fundamental_snapshot": _snapshot_payload(snapshot),
            }
        )


def _record_gate_stats(
    stats: dict[str, Any],
    gate_name: str,
    gate: GateResult,
) -> None:
    stats["gate_status_counts"][f"{gate_name}:{gate.status}"] += 1
    if gate.status == "REJECT":
        for reason in gate.reasons or ["reject"]:
            stats["would_reject_reason_counts"][f"{gate_name}:{reason}"] += 1
    elif gate.status == "WARN" or gate.warnings:
        for reason in gate.reasons or ["warn"]:
            stats["warn_reason_counts"][f"{gate_name}:{reason}"] += 1


def _should_block_fundamental_gate(
    profile: str,
    gate: GateResult,
    snapshot: FundamentalSnapshot,
) -> bool:
    if gate.status != "REJECT":
        return False
    if profile == "fundamentals_warn":
        return False
    if profile == "fundamentals_strict_all":
        return True
    if profile == "fundamentals_strict_financials":
        return any(
            _is_strict_financial_reason(reason)
            and _reason_has_strict_pit_provenance(reason, snapshot)
            for reason in gate.reasons
        )
    if profile == "fundamentals_moderate":
        return any(_is_moderate_fundamental_reason(reason) for reason in gate.reasons)
    return False


def _should_block_csp_earnings_gate(profile: str, gate: GateResult) -> bool:
    return profile == "fundamentals_strict_all" and gate.status == "REJECT"


def _evaluate_post_earnings_cooldown_gate(
    snapshot: FundamentalSnapshot,
    *,
    as_of: date,
    cooldown_days: int,
) -> GateResult | None:
    if cooldown_days <= 0:
        return None
    previous = snapshot.previous_earnings_date
    if previous is None:
        return GateResult(
            status="WARN",
            reasons=["previous_earnings_date_unknown"],
            warnings=["cannot_evaluate_post_earnings_cooldown"],
        )
    if previous > as_of:
        return GateResult(
            status="WARN",
            reasons=["previous_earnings_date_after_scan_date"],
            warnings=[f"previous_earnings_date:{previous.isoformat()}"],
        )
    trading_days = nyse_trading_days_after(previous, as_of)
    if trading_days <= cooldown_days:
        return GateResult(
            status="REJECT",
            reasons=[f"post_earnings_cooldown_{trading_days}_lte_{cooldown_days}"],
            warnings=[
                f"previous_earnings_date:{previous.isoformat()}",
                f"trading_days_since_previous_earnings:{trading_days}",
            ],
        )
    return GateResult(
        status="PASS",
        reasons=[f"post_earnings_cooldown_clear_{trading_days}_gt_{cooldown_days}"],
        warnings=[f"previous_earnings_date:{previous.isoformat()}"],
    )


def _covered_call_blocking_gate(
    profile: str,
    earnings_gate: GateResult,
    ex_dividend_gate: GateResult,
) -> GateResult | None:
    if profile == "fundamentals_strict_all":
        if earnings_gate.status == "REJECT":
            return earnings_gate
        if ex_dividend_gate.status == "REJECT":
            return ex_dividend_gate
    if profile in {"fundamentals_strict_financials", "fundamentals_moderate"} and (
        ex_dividend_gate.status == "REJECT"
    ):
        return ex_dividend_gate
    return None


def _is_strict_financial_reason(reason: str) -> bool:
    return any(reason.startswith(prefix) for prefix in STRICT_FINANCIAL_REASON_PREFIXES)


def _is_moderate_fundamental_reason(reason: str) -> bool:
    if reason in MODERATE_FUNDAMENTAL_HARD_REASONS:
        return True
    return any(reason.startswith(prefix) for prefix in MODERATE_FUNDAMENTAL_REASON_PREFIXES)


def _reason_has_strict_pit_provenance(
    reason: str,
    snapshot: FundamentalSnapshot,
) -> bool:
    fields = _fields_for_fundamental_reason(reason)
    if not fields:
        return False
    return any(
        snapshot.provenance.get(field) is not None
        and snapshot.provenance[field].quality == "strict_pit"
        for field in fields
    )


def _fields_for_fundamental_reason(reason: str) -> list[str]:
    if reason.startswith("recent_move_"):
        return ["recent_move_pct"]
    if reason.startswith("pe_ratio_"):
        return ["pe_ratio"]
    if reason.startswith("positive_quarters_"):
        return ["quarterly_net_income"]
    if reason.startswith("positive_years_"):
        return ["annual_net_income"]
    return []


def _gate_primary_reason(prefix: str, gate: GateResult) -> str:
    reason = gate.reasons[0] if gate.reasons else gate.status.lower()
    return f"{prefix}:{reason}"


def _fundamental_context_payload(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "fundamental_snapshot": _snapshot_payload(context["snapshot"]),
        "fundamental_gate": asdict(context["fundamental_gate"]),
    }


def _snapshot_payload(snapshot: FundamentalSnapshot) -> dict[str, Any]:
    return {
        "ticker": snapshot.ticker,
        "quote_type": snapshot.quote_type,
        "short_name": snapshot.short_name,
        "long_name": snapshot.long_name,
        "sector": snapshot.sector,
        "industry": snapshot.industry,
        "country": snapshot.country,
        "market_cap": snapshot.market_cap,
        "pe_ratio": snapshot.pe_ratio,
        "dividend_yield": snapshot.dividend_yield,
        "annual_dividend_rate": snapshot.annual_dividend_rate,
        "ex_dividend_date": snapshot.ex_dividend_date,
        "quarterly_net_income": snapshot.quarterly_net_income,
        "annual_net_income": snapshot.annual_net_income,
        "next_earnings_date": snapshot.next_earnings_date,
        "previous_earnings_date": snapshot.previous_earnings_date,
        "recent_move_pct": snapshot.recent_move_pct,
        "provenance": provenance_payload(snapshot),
    }


def _finalize_fundamental_stats(
    stats: dict[str, Any],
    historical_fundamentals: HistoricalFundamentalStore | None,
) -> dict[str, Any]:
    return {
        "profile": stats["profile"],
        "snapshot_count": stats["snapshot_count"],
        "diagnostic_event_limit": stats["diagnostic_event_limit"],
        "diagnostic_events_emitted": stats["diagnostic_events_emitted"],
        "diagnostic_events_suppressed": stats["diagnostic_events_suppressed"],
        "would_reject_count": sum(stats["would_reject_reason_counts"].values()),
        "warn_count": sum(stats["warn_reason_counts"].values()),
        "gate_status_counts": dict(sorted(stats["gate_status_counts"].items())),
        "fundamental_gate_status_counts": dict(
            sorted(stats["fundamental_gate_status_counts"].items())
        ),
        "would_reject_reason_counts": dict(
            stats["would_reject_reason_counts"].most_common()
        ),
        "warn_reason_counts": dict(stats["warn_reason_counts"].most_common()),
        "field_quality_counts": {
            field: dict(sorted(counter.items()))
            for field, counter in sorted(stats["field_quality_counts"].items())
        },
        "store": (
            historical_fundamentals.diagnostics()
            if historical_fundamentals is not None
            else None
        ),
    }


def _detect_pre_start_split_issues(
    stock_bars: dict[str, list[PriceBar]],
    *,
    start: date,
    ratio_low: float,
    ratio_high: float,
) -> dict[str, list[dict[str, Any]]]:
    issues: dict[str, list[dict[str, Any]]] = {}
    for ticker, bars in stock_bars.items():
        breaks = detect_price_space_breaks(
            [bar for bar in bars if bar.date < start],
            ratio_low=ratio_low,
            ratio_high=ratio_high,
        )
        if breaks:
            issues[ticker] = breaks
    return issues


def _same_day_price_space_break(
    previous: PriceBar | None,
    current: PriceBar,
    *,
    ratio_low: float,
    ratio_high: float,
) -> dict[str, Any] | None:
    if previous is None or previous.close <= 0:
        return None
    open_ratio = current.open / previous.close if current.open > 0 else None
    if open_ratio is None or ratio_low <= open_ratio <= ratio_high:
        return None
    return {
        "date": current.date.isoformat(),
        "previous_close": previous.close,
        "open": current.open,
        "close": current.close,
        "ratio": open_ratio,
        "ratio_basis": "open_to_previous_close",
    }


def _classify_price_space_break(
    classifier: PriceSpaceBreakClassifier | None,
    *,
    ticker: str,
    issue: dict[str, Any],
) -> PriceSpaceBreakClassification | None:
    if classifier is None:
        return None
    return classifier.classify(ticker=ticker, issue=issue)


def _price_space_break_should_block(
    classification: PriceSpaceBreakClassification | None,
) -> bool:
    if classification is None:
        return True
    return classification.action not in {
        PRICE_SPACE_BREAK_ALLOW_REAL_GAP,
        PRICE_SPACE_BREAK_RESET_LOOKBACK,
    }


def _price_space_break_should_reset_lookback(
    classification: PriceSpaceBreakClassification | None,
) -> bool:
    return (
        classification is not None
        and classification.action == PRICE_SPACE_BREAK_RESET_LOOKBACK
    )


def _record_price_space_reset(
    reset_dates: dict[str, date],
    ticker: str,
    classification: PriceSpaceBreakClassification,
) -> None:
    current = reset_dates.get(ticker)
    if current is None or classification.date > current:
        reset_dates[ticker] = classification.date


def _record_open_option_price_space_reset_issues(
    *,
    state: BacktestState,
    data_issues: list[dict[str, Any]],
    events: list[dict[str, Any]],
    day: date,
    ticker: str,
    classification: PriceSpaceBreakClassification,
) -> None:
    positions: list[ShortPutPosition | ShortCallPosition] = [
        position
        for position in [*state.open_short_puts, *state.open_short_calls]
        if position.ticker == ticker
        and position.entry_date < classification.date <= position.expiration
    ]
    for position in positions:
        details = {
            "trade_id": position.trade_id,
            "symbol": position.symbol,
            "expiration": position.expiration.isoformat(),
            "reset_date": classification.date.isoformat(),
            "note": "option contract adjustment through split is not modeled",
        }
        data_issues.append(
            {
                "date": day.isoformat(),
                "ticker": ticker,
                "type": "open_option_through_price_space_reset",
                "details": details,
                "classification": classification.to_payload(),
            }
        )
        events.append(
            {
                "date": day.isoformat(),
                "ticker": ticker,
                "type": "OPEN_OPTION_THROUGH_PRICE_SPACE_RESET_UNMODELED",
                "trade_id": position.trade_id,
                "details": details,
                "classification": classification.to_payload(),
            }
        )


def _bars_after_price_space_reset(
    bars: list[PriceBar],
    reset_date: date | None,
) -> list[PriceBar]:
    if reset_date is None:
        return bars
    # Massive split execution-date bars are the first bars in the new price
    # space, so the reset date is inclusive. Same-day CSP/CC entries are
    # prevented because cooldown counts only bars strictly before the scan day.
    return [bar for bar in bars if bar.date >= reset_date]


def _post_reset_support_bar_count(
    bars: list[PriceBar],
    reset_date: date,
    day: date,
) -> int:
    return sum(1 for bar in bars if reset_date <= bar.date < day)


def _last_bar_before(bars: list[PriceBar], day: date) -> PriceBar | None:
    previous: PriceBar | None = None
    for bar in sorted(bars, key=lambda item: item.date):
        if bar.date >= day:
            break
        previous = bar
    return previous


def _bars_by_day(stock_bars: dict[str, list[PriceBar]]) -> dict[date, dict[str, PriceBar]]:
    by_day: dict[date, dict[str, PriceBar]] = defaultdict(dict)
    for ticker, bars in stock_bars.items():
        for bar in bars:
            by_day[bar.date][ticker] = bar
    return dict(by_day)


def _scaled_candidate(candidate: CspCandidate, quantity: int) -> CspCandidate:
    if quantity == 1:
        return candidate
    return replace(
        candidate,
        assignment_cash_required=candidate.option.strike * 100.0 * quantity,
    )


def _position_sizing_config(config: dict[str, Any], default_quantity: int) -> dict[str, Any]:
    sizing = dict(config.get("backtest_position_sizing") or {})
    mode = str(sizing.get("mode") or "fixed").strip().lower()
    return {
        "mode": mode,
        "default_contract_quantity": default_quantity,
        "target_equity_pct": sizing.get("target_equity_pct"),
        "target_dollars": sizing.get("target_dollars"),
        "min_contracts": sizing.get("min_contracts", 1),
        "max_contracts": sizing.get("max_contracts"),
    }


def _csp_contract_quantity(
    config: dict[str, Any],
    candidate: CspCandidate,
    *,
    equity: float,
    default_quantity: int,
) -> tuple[int, dict[str, Any]]:
    sizing = _position_sizing_config(config, default_quantity)
    mode = str(sizing["mode"])
    contract_notional = candidate.option.strike * 100.0
    min_contracts = max(0, int(sizing.get("min_contracts") or 0))
    max_contracts = _optional_positive_int(sizing.get("max_contracts"))
    target_dollars: float | None = None
    if mode == "fixed":
        quantity = default_quantity
    elif mode == "equity_pct":
        target_pct = float(sizing.get("target_equity_pct") or 0.0)
        target_dollars = max(0.0, equity * target_pct)
        quantity = int(target_dollars // contract_notional) if contract_notional > 0 else 0
    elif mode == "fixed_dollars":
        target_dollars = max(0.0, float(sizing.get("target_dollars") or 0.0))
        quantity = int(target_dollars // contract_notional) if contract_notional > 0 else 0
    else:
        raise ValueError(f"unsupported backtest_position_sizing.mode {mode!r}")
    if quantity < min_contracts:
        quantity = min_contracts
    if max_contracts is not None:
        quantity = min(quantity, max_contracts)
    quantity = max(0, quantity)
    return quantity, {
        **sizing,
        "equity": round(equity, 2),
        "contract_notional": round(contract_notional, 2),
        "target_dollars": round(target_dollars, 2) if target_dollars is not None else None,
        "contracts": quantity,
        "assignment_cash_required": round(contract_notional * quantity, 2),
    }


def _rank_daily_csp_candidates(
    candidates: list[DailyCspCandidate],
) -> list[DailyCspCandidate]:
    ranked = sorted(
        candidates,
        key=lambda item: (
            -item.quality_score,
            -item.candidate.weekly_return_on_strike_pct,
            item.candidate.assignment_cash_required,
            item.ticker,
            item.candidate.option.expiration,
            item.candidate.option.strike,
        ),
    )
    for index, item in enumerate(ranked, start=1):
        item.rank_within_day = index
    return ranked


def _execute_ranked_csp_candidates_for_day(
    *,
    state: BacktestState,
    candidates: list[DailyCspCandidate],
    config: dict[str, Any],
    csp_execution_config: dict[str, Any] | None = None,
    day: date,
    latest_close: dict[str, float],
    data: HistoricalDataStore,
    default_quantity: int,
    max_new_orders: int,
    option_fee_per_contract: float,
    execution_model: BacktestExecutionModel,
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
    rejected_reason_counts: Counter[str],
    candidate_ledger: list[dict[str, Any]],
) -> int:
    execution_config = csp_execution_config or config
    opened_count = 0
    for item in _rank_daily_csp_candidates(candidates):
        ticker = item.ticker
        candidate = item.candidate
        if opened_count >= max_new_orders:
            _record_candidate_ledger(
                candidate_ledger,
                config=execution_config,
                day=day,
                ticker=ticker,
                decision="CANDIDATE",
                reason="not_selected_daily_order_limit",
                support=item.support_summary,
                candidate=candidate,
                diagnostics=_candidate_diagnostics(candidate),
                selected_for_backtest=False,
                selection_stage="capacity",
                rank_within_day=item.rank_within_day,
                quality_score=item.quality_score,
                cash_required=candidate.assignment_cash_required,
                cash_available_at_decision=round(
                    state.cash - state.reserved_assignment_cash,
                    2,
                ),
            )
            continue

        marked_equity = _mark_state_equity(state, latest_close, data, day)
        quantity, position_sizing = _csp_contract_quantity(
            execution_config,
            candidate,
            equity=marked_equity,
            default_quantity=default_quantity,
        )
        if quantity < 1:
            diagnostics = {
                "position_sizing": position_sizing,
                "candidate": _candidate_diagnostics(candidate),
            }
            _reject(
                events,
                rejected_reason_counts,
                day,
                ticker,
                "position_size_below_one_contract",
                support=item.support_summary,
                diagnostics=diagnostics,
            )
            _record_candidate_ledger(
                candidate_ledger,
                config=execution_config,
                day=day,
                ticker=ticker,
                decision="REJECT",
                reason="position_size_below_one_contract",
                support=item.support_summary,
                candidate=candidate,
                diagnostics=diagnostics,
                selected_for_backtest=False,
                selection_stage="sizing",
                rank_within_day=item.rank_within_day,
                quality_score=item.quality_score,
                cash_required=position_sizing.get("assignment_cash_required"),
                cash_available_at_decision=round(
                    state.cash - state.reserved_assignment_cash,
                    2,
                ),
            )
            continue

        scaled_candidate = _scaled_candidate(candidate, quantity)
        available_cash_for_assignment = state.cash - state.reserved_assignment_cash
        if available_cash_for_assignment < scaled_candidate.assignment_cash_required:
            diagnostics = {
                "cash": round(state.cash, 2),
                "reserved_assignment_cash": round(state.reserved_assignment_cash, 2),
                "available_cash_for_assignment": round(
                    available_cash_for_assignment,
                    2,
                ),
                "new_assignment_cash_required": round(
                    scaled_candidate.assignment_cash_required,
                    2,
                ),
                "position_sizing": position_sizing,
            }
            _reject(
                events,
                rejected_reason_counts,
                day,
                ticker,
                "insufficient_cash_secured_capacity",
                support=item.support_summary,
                diagnostics=diagnostics,
            )
            _record_candidate_ledger(
                candidate_ledger,
                config=execution_config,
                day=day,
                ticker=ticker,
                decision="REJECT",
                reason="insufficient_cash_secured_capacity",
                support=item.support_summary,
                candidate=scaled_candidate,
                diagnostics=diagnostics,
                selected_for_backtest=False,
                selection_stage="capital",
                rank_within_day=item.rank_within_day,
                quality_score=item.quality_score,
                cash_required=scaled_candidate.assignment_cash_required,
                cash_available_at_decision=round(available_cash_for_assignment, 2),
            )
            continue

        portfolio_gate, portfolio_diagnostics = evaluate_portfolio_risk(
            ticker,
            scaled_candidate,
            _portfolio_snapshot(state, marked_equity),
            execution_config,
            required=True,
        )
        if portfolio_gate is None or portfolio_gate.status == "REJECT":
            reason = (
                portfolio_gate.reasons[0]
                if portfolio_gate and portfolio_gate.reasons
                else "portfolio_risk_reject"
            )
            diagnostics = {
                "portfolio_gate": asdict(portfolio_gate) if portfolio_gate else None,
                "portfolio_risk": portfolio_diagnostics,
            }
            _reject(
                events,
                rejected_reason_counts,
                day,
                ticker,
                reason,
                support=item.support_summary,
                diagnostics=diagnostics,
            )
            _record_candidate_ledger(
                candidate_ledger,
                config=execution_config,
                day=day,
                ticker=ticker,
                decision="REJECT",
                reason=reason,
                support=item.support_summary,
                candidate=scaled_candidate,
                diagnostics=diagnostics,
                selected_for_backtest=False,
                selection_stage="portfolio",
                rank_within_day=item.rank_within_day,
                quality_score=item.quality_score,
                cash_required=scaled_candidate.assignment_cash_required,
                cash_available_at_decision=round(available_cash_for_assignment, 2),
            )
            continue

        opening_diagnostics = {
            **(
                _fundamental_context_payload(item.fundamental_context)
                if item.fundamental_context is not None
                else {}
            ),
            **(
                {"earnings_gate": asdict(item.candidate_earnings_gate)}
                if item.candidate_earnings_gate is not None
                else {}
            ),
            **(
                {
                    "post_earnings_cooldown_gate": asdict(
                        item.post_earnings_cooldown_gate
                    )
                }
                if item.post_earnings_cooldown_gate is not None
                else {}
            ),
            "candidate_quality_score": item.quality_score,
            "rank_within_day": item.rank_within_day,
            "position_sizing": position_sizing,
            "portfolio_gate": asdict(portfolio_gate),
            "portfolio_risk": portfolio_diagnostics,
        }
        opened = _open_short_put(
            state=state,
            candidate=scaled_candidate,
            day=day,
            ticker=ticker,
            contracts=quantity,
            option_fee_per_contract=option_fee_per_contract,
            execution_model=execution_model,
            support_summary=item.support_summary,
            trades=trades,
            events=events,
            diagnostics=opening_diagnostics,
        )
        if opened:
            opened_count += 1
            _record_candidate_ledger(
                candidate_ledger,
                config=execution_config,
                day=day,
                ticker=ticker,
                decision="OPENED",
                reason="opened_short_put",
                support=item.support_summary,
                candidate=scaled_candidate,
                diagnostics=opening_diagnostics,
                selected_for_backtest=True,
                selection_stage="executed",
                rank_within_day=item.rank_within_day,
                quality_score=item.quality_score,
                cash_required=scaled_candidate.assignment_cash_required,
                cash_available_at_decision=round(available_cash_for_assignment, 2),
            )
        else:
            _reject(
                events,
                rejected_reason_counts,
                day,
                ticker,
                "no_executable_entry_fill",
                support=item.support_summary,
                diagnostics=opening_diagnostics,
            )
            _record_candidate_ledger(
                candidate_ledger,
                config=execution_config,
                day=day,
                ticker=ticker,
                decision="REJECT",
                reason="no_executable_entry_fill",
                support=item.support_summary,
                candidate=scaled_candidate,
                diagnostics=opening_diagnostics,
                selected_for_backtest=False,
                selection_stage="execution",
                rank_within_day=item.rank_within_day,
                quality_score=item.quality_score,
                cash_required=scaled_candidate.assignment_cash_required,
                cash_available_at_decision=round(available_cash_for_assignment, 2),
            )
    return opened_count


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _has_open_short_put(state: BacktestState, ticker: str) -> bool:
    return any(position.ticker == ticker for position in state.open_short_puts)


def _has_open_short_call(state: BacktestState, ticker: str) -> bool:
    return any(position.ticker == ticker for position in state.open_short_calls)


def _uncovered_shares(state: BacktestState, ticker: str) -> int:
    stock = state.stocks.get(ticker)
    if stock is None:
        return 0
    covered = sum(
        position.contracts * 100
        for position in state.open_short_calls
        if position.ticker == ticker
    )
    return max(0, stock.shares - covered)


def _min_covered_call_strike(
    stock: StockPosition,
    config: dict[str, Any],
) -> float | None:
    basis = stock.adjusted_average_cost
    if basis is None:
        return None
    pct = float(config.get("cc_selector", {}).get("min_strike_vs_cost_basis_pct", 0.0))
    unadjusted = stock.average_cost or 0.0
    floor_pct = float(
        config.get("cc_selector", {}).get(
            "min_strike_floor_pct_of_unadjusted_cost",
            100.0,
        )
    )
    floor = max(0.01, unadjusted * floor_pct / 100.0)
    return max(floor, basis * (1.0 + pct / 100.0))


def _covered_call_selection_diagnostics(
    options: list[OptionQuote],
    *,
    stock: StockPosition,
    config: dict[str, Any],
) -> dict[str, Any]:
    cc_cfg = config.get("cc_selector", {})
    min_strike = _min_covered_call_strike(stock, config)
    min_delta = float(cc_cfg.get("target_delta_min", 0.10))
    max_delta = float(cc_cfg.get("target_delta_max", 0.35))
    midpoint = (min_delta + max_delta) / 2.0
    best_available = _best_available_call(options, midpoint=midpoint)
    closest_above = (
        min(
            (option for option in options if min_strike is not None and option.strike >= min_strike),
            key=lambda option: (option.strike, option.dte, -option.bid),
            default=None,
        )
        if min_strike is not None
        else None
    )
    closest_to_min = (
        min(
            options,
            key=lambda option: (
                abs(option.strike - min_strike),
                option.dte,
                -option.bid,
            ),
            default=None,
        )
        if min_strike is not None
        else None
    )
    return {
        "assigned_shares": stock.shares,
        "average_cost": (
            round(stock.average_cost, 4) if stock.average_cost is not None else None
        ),
        "adjusted_cost_basis": (
            round(stock.adjusted_average_cost, 4)
            if stock.adjusted_average_cost is not None
            else None
        ),
        "min_covered_call_strike": round(min_strike, 4) if min_strike is not None else None,
        "min_strike_vs_cost_basis_pct": float(
            cc_cfg.get("min_strike_vs_cost_basis_pct", 0.0)
        ),
        "min_strike_floor_pct_of_unadjusted_cost": float(
            cc_cfg.get("min_strike_floor_pct_of_unadjusted_cost", 100.0)
        ),
        "call_contract_count": len(options),
        "lowest_call_strike": min((option.strike for option in options), default=None),
        "highest_call_strike": max((option.strike for option in options), default=None),
        "best_available_call_strike": (
            best_available.strike if best_available is not None else None
        ),
        "best_available_call": _covered_call_option_diagnostics(
            best_available,
            min_strike=min_strike,
        ),
        "strike_to_adjusted_cost_basis_gap": (
            round(min_strike - best_available.strike, 4)
            if min_strike is not None and best_available is not None
            else None
        ),
        "closest_call_above_cost_basis": _covered_call_option_diagnostics(
            closest_above,
            min_strike=min_strike,
        ),
        "closest_call_delta": closest_above.delta if closest_above is not None else None,
        "closest_call_bid": closest_above.bid if closest_above is not None else None,
        "closest_call_mid": (
            round(closest_above.mid, 4) if closest_above is not None else None
        ),
        "closest_call_to_min_strike": _covered_call_option_diagnostics(
            closest_to_min,
            min_strike=min_strike,
        ),
    }


def _best_available_call(
    options: list[OptionQuote],
    *,
    midpoint: float,
) -> OptionQuote | None:
    if not options:
        return None
    return sorted(
        options,
        key=lambda option: (
            option.dte,
            abs(float(option.delta) - midpoint) if option.delta is not None else 999.0,
            -option.bid,
            option.strike,
        ),
    )[0]


def _covered_call_option_diagnostics(
    option: OptionQuote | None,
    *,
    min_strike: float | None,
) -> dict[str, Any] | None:
    if option is None:
        return None
    return {
        "symbol": option.symbol,
        "expiration": option.expiration.isoformat(),
        "dte": option.dte,
        "strike": option.strike,
        "strike_gap_to_min": (
            round(option.strike - min_strike, 4) if min_strike is not None else None
        ),
        "bid": option.bid,
        "ask": option.ask,
        "mid": round(option.mid, 4),
        "spread_pct_of_mid": (
            round(option.spread_pct_of_mid, 6)
            if option.spread_pct_of_mid is not None
            else None
        ),
        "delta": option.delta,
        "open_interest": option.open_interest,
        "volume": option.volume,
    }


def _remove_stock_shares(stock: StockPosition, shares: int) -> tuple[float, float]:
    if shares <= 0 or stock.shares <= 0:
        return 0.0, 0.0
    removed = min(shares, stock.shares)
    fraction = removed / stock.shares
    removed_cost = stock.cost_basis_total * fraction
    removed_premium = stock.premium_credit_total * fraction
    stock.shares -= removed
    stock.cost_basis_total -= removed_cost
    stock.premium_credit_total -= removed_premium
    if stock.shares == 0:
        stock.cost_basis_total = 0.0
        stock.premium_credit_total = 0.0
    return removed_cost, removed_premium


def _reject(
    events: list[dict[str, Any]],
    counts: Counter[str],
    day: date,
    ticker: str,
    reason: str,
    *,
    support: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> None:
    counts[reason] += 1
    events.append(
        {
            "date": day.isoformat(),
            "ticker": ticker,
            "type": "REJECT",
            "reason": reason,
            "support": support,
            "diagnostics": diagnostics,
        }
    )


def _candidate_ledger_config(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(config.get("candidate_ledger") or {})
    return {
        "schema_version": "candidate_ledger.v2",
        "enabled": bool(cfg.get("enabled", True)),
        "include_rejections": bool(cfg.get("include_rejections", True)),
        "include_skips": bool(cfg.get("include_skips", False)),
    }


def _candidate_quality_filter_config(config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict((config.get("candidate_ledger") or {}).get("quality_filter") or {})
    max_spread = (
        cfg["max_spread_pct_of_mid"]
        if "max_spread_pct_of_mid" in cfg
        else 0.30
    )
    return {
        "require_auto_trade": bool(cfg.get("require_auto_trade", True)),
        "delta_abs_min": float(cfg.get("delta_abs_min", 0.05)),
        "delta_abs_max": float(cfg.get("delta_abs_max", 0.30)),
        "min_weekly_return_on_strike_pct": float(
            cfg.get("min_weekly_return_on_strike_pct", 0.10)
        ),
        "max_spread_pct_of_mid": (
            None
            if max_spread is None
            else float(max_spread)
        ),
    }


def _candidate_quality_score(
    candidate: CspCandidate | None,
    support: dict[str, Any] | None,
) -> float:
    if candidate is None:
        return 0.0
    zone = (support or {}).get("selected_zone") or {}
    support_score = float(zone.get("score") or 0.0)
    premium_score = min(max(candidate.weekly_return_on_strike_pct, 0.0), 2.0) * 5.0
    option = candidate.option
    spread = option.spread_pct_of_mid
    spread_penalty = (spread * 50.0) if spread is not None else 10.0
    delta_penalty = abs(abs(candidate.delta) - 0.20) * 25.0
    liquidity_score = 0.0
    if option.open_interest is not None:
        liquidity_score += min(max(option.open_interest, 0) / 100.0, 5.0)
    if option.volume is not None:
        liquidity_score += min(max(option.volume, 0) / 100.0, 5.0)
    return round(
        max(
            0.0,
            support_score + premium_score + liquidity_score - spread_penalty - delta_penalty,
        ),
        6,
    )


def _record_candidate_ledger(
    rows: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    day: date,
    ticker: str,
    decision: str,
    reason: str,
    support: dict[str, Any] | None = None,
    candidate: CspCandidate | None = None,
    diagnostics: dict[str, Any] | None = None,
    selected_for_backtest: bool | None = None,
    selection_stage: str | None = None,
    rank_within_day: int | None = None,
    quality_score: float | None = None,
    cash_required: float | None = None,
    cash_available_at_decision: float | None = None,
) -> None:
    ledger_cfg = _candidate_ledger_config(config)
    if not ledger_cfg["enabled"]:
        return
    normalized_decision = decision.upper()
    if normalized_decision == "REJECT" and not ledger_cfg["include_rejections"]:
        return
    if normalized_decision == "SKIP" and not ledger_cfg["include_skips"]:
        return
    support = support or {}
    zone = support.get("selected_zone") or {}
    option = candidate.option if candidate is not None else None
    rows.append(
        {
            "schema_version": ledger_cfg["schema_version"],
            "date": day.isoformat(),
            "ticker": ticker,
            "phase": "csp",
            "decision": normalized_decision,
            "reason": reason,
            "candidate_present": candidate is not None,
            "auto_trade": candidate.auto_trade if candidate is not None else None,
            "selected_for_backtest": selected_for_backtest,
            "selection_stage": selection_stage,
            "rank_within_day": rank_within_day,
            "quality_score": (
                quality_score
                if quality_score is not None
                else _candidate_quality_score(candidate, support)
            ),
            "current_price": support.get("current_price"),
            "atr14": support.get("atr14"),
            "support_tradable": support.get("tradable"),
            "trend_passed": support.get("trend_passed"),
            "support_preconditions_passed": support.get("preconditions_passed"),
            "support_precondition_metrics": support.get("precondition_metrics"),
            "support_context_metrics": support.get("context_metrics"),
            "support_method": zone.get("method"),
            "support_score": zone.get("score"),
            "support_bottom": zone.get("bottom"),
            "support_top": zone.get("top"),
            "support_zone_rank": support.get("support_zone_rank"),
            "support_zones_considered": support.get("support_zones_considered"),
            "csp_regime_override": support.get("csp_regime_override"),
            "option_symbol": option.symbol if option is not None else None,
            "expiration": option.expiration.isoformat() if option is not None else None,
            "dte": option.dte if option is not None else None,
            "strike": option.strike if option is not None else None,
            "bid": option.bid if option is not None else None,
            "ask": option.ask if option is not None else None,
            "mid": round(option.mid, 6) if option is not None else None,
            "spread_pct_of_mid": (
                round(option.spread_pct_of_mid, 6)
                if option is not None and option.spread_pct_of_mid is not None
                else None
            ),
            "delta": candidate.delta if candidate is not None else None,
            "delta_bucket": candidate.delta_bucket if candidate is not None else None,
            "weekly_return_on_strike_pct": (
                candidate.weekly_return_on_strike_pct
                if candidate is not None
                else None
            ),
            "assignment_cash_required": (
                candidate.assignment_cash_required if candidate is not None else None
            ),
            "cash_required": cash_required,
            "cash_available_at_decision": cash_available_at_decision,
            "diagnostics": diagnostics,
        }
    )


def _primary_rejection_reason(summary: dict[str, int]) -> str:
    if not summary:
        return "no_csp_candidate"
    return sorted(summary.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _candidate_diagnostics(candidate: CspCandidate) -> dict[str, Any]:
    return {
        "symbol": candidate.option.symbol,
        "expiration": candidate.option.expiration.isoformat(),
        "strike": candidate.option.strike,
        "delta": candidate.delta,
        "weekly_return_on_strike_pct": candidate.weekly_return_on_strike_pct,
        "assignment_cash_required": candidate.assignment_cash_required,
        "reasons": candidate.reasons,
        "diagnostics": candidate.diagnostics,
    }


def _close_trade(
    trades: list[dict[str, Any]],
    trade_id: str,
    *,
    status: str,
    close_date: date,
    underlying_close: float,
    realized_option_pnl: float,
    realized_stock_pnl: float | None = None,
    close_price: float | None = None,
    close_fees: float | None = None,
    close_reason: str | None = None,
) -> None:
    for trade in trades:
        if trade["trade_id"] != trade_id:
            continue
        trade["status"] = status
        trade["close_date"] = close_date.isoformat()
        trade["underlying_close_at_close"] = underlying_close
        trade["realized_option_pnl"] = round(realized_option_pnl, 2)
        if realized_stock_pnl is not None:
            trade["realized_stock_pnl"] = round(realized_stock_pnl, 2)
        if close_price is not None:
            trade["close_price"] = round(close_price, 6)
        if close_fees is not None:
            trade["close_fees"] = round(close_fees, 2)
        if close_reason is not None:
            trade["close_reason"] = close_reason
        return


def _assignment_recovery_diagnostics(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assigned_events = [
        event for event in events if event.get("type") == "ASSIGNED"
    ]
    diagnostics: list[dict[str, Any]] = []
    for assignment in assigned_events:
        ticker = str(assignment.get("ticker") or "")
        assignment_date = _parse_event_date(assignment)
        if assignment_date is None:
            continue
        later_events = [
            event
            for event in events
            if event.get("ticker") == ticker
            and (event_date := _parse_event_date(event)) is not None
            and event_date > assignment_date
        ]
        first_attempt = next(
            (event for event in later_events if _is_covered_call_attempt_event(event)),
            None,
        )
        first_open = next(
            (event for event in later_events if event.get("type") == "OPEN_SHORT_CALL"),
            None,
        )
        first_attempt_date = _parse_event_date(first_attempt) if first_attempt else None
        first_open_date = _parse_event_date(first_open) if first_open else None
        diagnostics.append(
            {
                "assignment_trade_id": assignment.get("trade_id"),
                "ticker": ticker,
                "assignment_date": assignment_date.isoformat(),
                "shares_acquired": assignment.get("shares_acquired"),
                "assignment_strike": assignment.get("strike"),
                "assignment_underlying_close": assignment.get("underlying_close"),
                "first_cc_attempt_date": (
                    first_attempt_date.isoformat() if first_attempt_date else None
                ),
                "first_cc_attempt_type": (
                    first_attempt.get("type") if first_attempt else None
                ),
                "first_cc_attempt_reason": (
                    first_attempt.get("reason") if first_attempt else None
                ),
                "first_cc_opened_date": (
                    first_open_date.isoformat() if first_open_date else None
                ),
                "days_to_first_cc_attempt": (
                    (first_attempt_date - assignment_date).days
                    if first_attempt_date is not None
                    else None
                ),
                "days_to_first_cc_open": (
                    (first_open_date - assignment_date).days
                    if first_open_date is not None
                    else None
                ),
            }
        )
    return diagnostics


def _parse_event_date(event: dict[str, Any] | None) -> date | None:
    if not event:
        return None
    value = event.get("date")
    if not value:
        return None
    return date.fromisoformat(str(value))


def _is_covered_call_attempt_event(event: dict[str, Any]) -> bool:
    if event.get("type") == "OPEN_SHORT_CALL":
        return True
    if event.get("type") != "REJECT":
        return False
    diagnostics = event.get("diagnostics") or {}
    return isinstance(diagnostics, dict) and diagnostics.get("phase") == "covered_call"


def _assignment_recovery_notes(assigned_share_values: list[int]) -> list[str]:
    notes = [
        "assignment_to_cc_matching_uses_ticker_and_date_not_tax_lot_identity",
        "assigned_stock_recovery_pnl_estimate_is_diagnostic_not_an_accounting_identity",
    ]
    if max(assigned_share_values, default=0) > 100:
        notes.append("multi_lot_assigned_stock_detected")
    return notes


def _parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _summarize_candidate_ledger(
    rows: list[dict[str, Any]],
    *,
    start_date: date | None,
    end_date: date | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    csp_rows = [row for row in rows if row.get("phase") == "csp"]
    decision_counts = Counter(str(row.get("decision")) for row in csp_rows)
    reason_counts = Counter(str(row.get("reason")) for row in csp_rows if row.get("reason"))
    selection_stage_counts = Counter(
        str(row.get("selection_stage"))
        for row in csp_rows
        if row.get("selection_stage")
    )
    binding_filter_counts = Counter(
        str(row.get("reason"))
        for row in csp_rows
        if row.get("decision") in {"REJECT", "WATCH"} and row.get("reason")
    )
    mechanical_candidates = [
        row
        for row in csp_rows
        if bool(row.get("candidate_present"))
        and (
            row.get("decision") in {"OPENED", "CANDIDATE"}
            or row.get("selection_stage")
            in {"sizing", "capital", "portfolio", "execution", "capacity"}
        )
    ]
    auto_trade_candidates = [
        row for row in mechanical_candidates if bool(row.get("auto_trade"))
    ]
    quality_filter = _candidate_quality_filter_config(config)
    quality_candidates = [
        row
        for row in mechanical_candidates
        if _is_quality_candidate(row, quality_filter)
    ]
    opened = [row for row in csp_rows if row.get("decision") == "OPENED"]
    selected = [row for row in csp_rows if row.get("selected_for_backtest") is True]
    candidate_dates = {
        row.get("date") for row in mechanical_candidates if row.get("date")
    }
    auto_candidate_dates = {
        row.get("date") for row in auto_trade_candidates if row.get("date")
    }
    scan_dates = {row.get("date") for row in csp_rows if row.get("date")}
    daily_candidate_counts = Counter(
        row.get("date") for row in mechanical_candidates if row.get("date")
    )
    daily_candidate_tickers: dict[str, set[str]] = defaultdict(set)
    daily_candidate_ticker_expirations: dict[str, set[tuple[str, str]]] = defaultdict(set)
    daily_quality_candidate_counts: Counter[str] = Counter()
    daily_quality_candidate_tickers: dict[str, set[str]] = defaultdict(set)
    week_candidate_counts: Counter[str] = Counter()
    week_candidate_tickers: dict[str, set[str]] = defaultdict(set)
    for row in mechanical_candidates:
        row_date = _parse_iso_date(row.get("date"))
        if row_date is None:
            continue
        row_date_text = row_date.isoformat()
        ticker = str(row.get("ticker") or "")
        expiration = str(row.get("expiration") or "")
        if ticker:
            daily_candidate_tickers[row_date_text].add(ticker)
        if ticker and expiration:
            daily_candidate_ticker_expirations[row_date_text].add((ticker, expiration))
        week_key = _iso_week_key(row_date)
        week_candidate_counts[week_key] += 1
        if ticker:
            week_candidate_tickers[week_key].add(ticker)
    for row in quality_candidates:
        row_date = _parse_iso_date(row.get("date"))
        if row_date is None:
            continue
        row_date_text = row_date.isoformat()
        ticker = str(row.get("ticker") or "")
        daily_quality_candidate_counts[row_date_text] += 1
        if ticker:
            daily_quality_candidate_tickers[row_date_text].add(ticker)
    daily_counts = [daily_candidate_counts.get(day, 0) for day in scan_dates]
    daily_unique_ticker_counts = [
        len(daily_candidate_tickers.get(str(day), set())) for day in scan_dates
    ]
    daily_unique_ticker_expiration_counts = [
        len(daily_candidate_ticker_expirations.get(str(day), set())) for day in scan_dates
    ]
    daily_quality_counts = [
        daily_quality_candidate_counts.get(day, 0) for day in scan_dates
    ]
    daily_unique_quality_ticker_counts = [
        len(daily_quality_candidate_tickers.get(str(day), set())) for day in scan_dates
    ]
    daily_quality_pass_rates = [
        daily_quality_candidate_counts.get(day, 0) / daily_candidate_counts.get(day, 0) * 100.0
        for day in scan_dates
        if daily_candidate_counts.get(day, 0) > 0
    ]
    week_counts = list(week_candidate_counts.values())
    weeks = _period_weeks(start_date, end_date)

    def per_week(count: int) -> float | None:
        return round(count / weeks, 4) if weeks else None

    def pct_days_at_least(threshold: int) -> float | None:
        if not daily_counts:
            return None
        return round(
            sum(1 for count in daily_counts if count >= threshold)
            / len(daily_counts)
            * 100.0,
            4,
        )

    def pct_weeks_at_least(threshold: int) -> float | None:
        if not week_counts:
            return None
        return round(
            sum(1 for count in week_counts if count >= threshold)
            / len(week_counts)
            * 100.0,
            4,
        )

    return {
        "schema_version": "candidate_ledger.v2",
        "rows": len(csp_rows),
        "decision_counts": dict(decision_counts.most_common()),
        "reason_counts": dict(reason_counts.most_common()),
        "selection_stage_counts": dict(selection_stage_counts.most_common()),
        "binding_filter_counts": dict(binding_filter_counts.most_common()),
        "top_binding_filters": [
            {"reason": reason, "count": count}
            for reason, count in binding_filter_counts.most_common(15)
        ],
        "mechanical_candidate_count": len(mechanical_candidates),
        "mechanical_auto_trade_candidate_count": len(auto_trade_candidates),
        "quality_candidate_filter": quality_filter,
        "quality_candidate_count": len(quality_candidates),
        "quality_candidate_pass_rate_pct": (
            round(len(quality_candidates) / len(mechanical_candidates) * 100.0, 4)
            if mechanical_candidates
            else None
        ),
        "average_daily_quality_candidate_pass_rate_pct": (
            round(sum(daily_quality_pass_rates) / len(daily_quality_pass_rates), 4)
            if daily_quality_pass_rates
            else None
        ),
        "quality_candidate_days": sum(1 for count in daily_quality_counts if count > 0),
        "median_quality_candidates_per_day": _median(daily_quality_counts),
        "average_quality_candidates_per_day": (
            round(sum(daily_quality_counts) / len(daily_quality_counts), 4)
            if daily_quality_counts
            else None
        ),
        "median_unique_quality_candidate_tickers_per_day": _median(
            daily_unique_quality_ticker_counts
        ),
        "average_unique_quality_candidate_tickers_per_day": (
            round(
                sum(daily_unique_quality_ticker_counts)
                / len(daily_unique_quality_ticker_counts),
                4,
            )
            if daily_unique_quality_ticker_counts
            else None
        ),
        "pct_days_with_3plus_quality_candidates": (
            round(
                sum(1 for count in daily_quality_counts if count >= 3)
                / len(daily_quality_counts)
                * 100.0,
                4,
            )
            if daily_quality_counts
            else None
        ),
        "pct_days_with_5plus_quality_candidates": (
            round(
                sum(1 for count in daily_quality_counts if count >= 5)
                / len(daily_quality_counts)
                * 100.0,
                4,
            )
            if daily_quality_counts
            else None
        ),
        "pct_days_with_3plus_unique_quality_tickers": (
            round(
                sum(1 for count in daily_unique_quality_ticker_counts if count >= 3)
                / len(daily_unique_quality_ticker_counts)
                * 100.0,
                4,
            )
            if daily_unique_quality_ticker_counts
            else None
        ),
        "pct_days_with_5plus_unique_quality_tickers": (
            round(
                sum(1 for count in daily_unique_quality_ticker_counts if count >= 5)
                / len(daily_unique_quality_ticker_counts)
                * 100.0,
                4,
            )
            if daily_unique_quality_ticker_counts
            else None
        ),
        "watch_only_candidate_count": decision_counts.get("WATCH", 0),
        "opened_from_candidate_count": len(opened),
        "selected_for_backtest_count": len(selected),
        "candidate_days": len(candidate_dates),
        "auto_trade_candidate_days": len(auto_candidate_dates),
        "scan_days": len(scan_dates),
        "starvation_days": sum(1 for count in daily_counts if count == 0),
        "median_candidates_per_day": _median(daily_counts),
        "average_candidates_per_day": (
            round(sum(daily_counts) / len(daily_counts), 4)
            if daily_counts
            else None
        ),
        "median_unique_candidate_tickers_per_day": _median(daily_unique_ticker_counts),
        "average_unique_candidate_tickers_per_day": (
            round(sum(daily_unique_ticker_counts) / len(daily_unique_ticker_counts), 4)
            if daily_unique_ticker_counts
            else None
        ),
        "average_unique_candidate_ticker_expirations_per_day": (
            round(
                sum(daily_unique_ticker_expiration_counts)
                / len(daily_unique_ticker_expiration_counts),
                4,
            )
            if daily_unique_ticker_expiration_counts
            else None
        ),
        "pct_days_with_3plus_candidates": pct_days_at_least(3),
        "pct_days_with_5plus_candidates": pct_days_at_least(5),
        "pct_days_with_3plus_unique_tickers": (
            round(
                sum(1 for count in daily_unique_ticker_counts if count >= 3)
                / len(daily_unique_ticker_counts)
                * 100.0,
                4,
            )
            if daily_unique_ticker_counts
            else None
        ),
        "pct_days_with_5plus_unique_tickers": (
            round(
                sum(1 for count in daily_unique_ticker_counts if count >= 5)
                / len(daily_unique_ticker_counts)
                * 100.0,
                4,
            )
            if daily_unique_ticker_counts
            else None
        ),
        "pct_weeks_with_15plus_candidates": pct_weeks_at_least(15),
        "average_unique_candidate_tickers_per_week": (
            round(
                sum(len(tickers) for tickers in week_candidate_tickers.values())
                / len(week_candidate_tickers),
                4,
            )
            if week_candidate_tickers
            else None
        ),
        "mechanical_candidates_per_week": per_week(len(mechanical_candidates)),
        "mechanical_auto_trade_candidates_per_week": per_week(
            len(auto_trade_candidates)
        ),
        "opened_from_candidate_per_week": per_week(len(opened)),
        "candidate_to_trade_ratio_pct": (
            round(len(opened) / len(mechanical_candidates) * 100.0, 4)
            if mechanical_candidates
            else None
        ),
        "auto_candidate_to_trade_ratio_pct": (
            round(len(opened) / len(auto_trade_candidates) * 100.0, 4)
            if auto_trade_candidates
            else None
        ),
    }


def _is_quality_candidate(row: dict[str, Any], quality_filter: dict[str, Any]) -> bool:
    if not bool(row.get("candidate_present")):
        return False
    if quality_filter["require_auto_trade"] and not bool(row.get("auto_trade")):
        return False
    delta = _optional_float(row.get("delta"))
    if delta is None:
        return False
    abs_delta = abs(delta)
    if abs_delta < quality_filter["delta_abs_min"]:
        return False
    if abs_delta > quality_filter["delta_abs_max"]:
        return False
    weekly_return = _optional_float(row.get("weekly_return_on_strike_pct"))
    if weekly_return is None:
        return False
    if weekly_return < quality_filter["min_weekly_return_on_strike_pct"]:
        return False
    max_spread = quality_filter["max_spread_pct_of_mid"]
    if max_spread is not None:
        spread = _optional_float(row.get("spread_pct_of_mid"))
        if spread is None or spread > max_spread:
            return False
    return True


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _iso_week_key(day: date) -> str:
    year, week, _weekday = day.isocalendar()
    return f"{year}-W{week:02d}"


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 4)


def _period_weeks(start_date: date | None, end_date: date | None) -> float | None:
    if start_date is None or end_date is None or end_date < start_date:
        return None
    return max((end_date - start_date).days + 1, 1) / 7.0


def _summarize_backtest(
    *,
    config: dict[str, Any],
    state: BacktestState,
    equity_curve: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
    rejected_reason_counts: Counter[str],
    data_issues: list[dict[str, Any]],
    support_stats: dict[str, Any],
    market_regime_stats: dict[str, Any],
    candidate_ledger: list[dict[str, Any]],
) -> dict[str, Any]:
    ending_equity = state.equity
    starting_equity = state.starting_equity
    trade_status_counts = Counter(str(trade.get("status")) for trade in trades)
    csp_trades = [trade for trade in trades if trade.get("strategy") == "cash_secured_put"]
    cc_trades = [trade for trade in trades if trade.get("strategy") == "covered_call"]
    csp_status_counts = Counter(str(trade.get("status")) for trade in csp_trades)
    cc_status_counts = Counter(str(trade.get("status")) for trade in cc_trades)
    event_counts = Counter(str(event.get("type")) for event in events)
    data_issue_counts = Counter(str(issue.get("type")) for issue in data_issues)
    price_space_break_category_counts = Counter(
        str(issue.get("classification", {}).get("category"))
        for issue in data_issues
        if issue.get("type") == "price_space_break"
        and isinstance(issue.get("classification"), dict)
    )
    price_space_break_action_counts = Counter(
        str(issue.get("classification", {}).get("action"))
        for issue in data_issues
        if issue.get("type") == "price_space_break"
        and isinstance(issue.get("classification"), dict)
    )
    max_drawdown_pct = _max_drawdown_pct([float(row["equity"]) for row in equity_curve])
    utilization_values = [
        float(row["capital_utilization_pct"])
        for row in equity_curve
        if row.get("capital_utilization_pct") is not None
    ]
    reserved_utilization_values = [
        float(row["reserved_assignment_cash_pct"])
        for row in equity_curve
        if row.get("reserved_assignment_cash_pct") is not None
    ]
    assigned_stock_utilization_values = [
        float(row["assigned_stock_utilization_pct"])
        for row in equity_curve
        if row.get("assigned_stock_utilization_pct") is not None
    ]
    cash_pct_values = [
        float(row["cash_pct"])
        for row in equity_curve
        if row.get("cash_pct") is not None
    ]
    entry_spreads = [
        float(trade["entry_spread_pct_of_mid"])
        for trade in trades
        if trade.get("entry_spread_pct_of_mid") is not None
    ]
    entry_fill_discounts = [
        float(trade["entry_fill_discount_pct_of_mid"])
        for trade in trades
        if trade.get("entry_fill_discount_pct_of_mid") is not None
    ]
    uncovered_share_values = [
        int(row.get("uncovered_assigned_shares") or 0) for row in equity_curve
    ]
    assigned_share_values = [
        int(row.get("assigned_shares") or 0) for row in equity_curve
    ]
    assignment_diagnostics = _assignment_recovery_diagnostics(events)
    csp_realized_option_pnl = sum(
        float(trade.get("realized_option_pnl") or 0.0) for trade in csp_trades
    )
    cc_realized_option_pnl = sum(
        float(trade.get("realized_option_pnl") or 0.0) for trade in cc_trades
    )
    cc_called_away_stock_pnl = sum(
        float(trade.get("realized_stock_pnl") or 0.0) for trade in cc_trades
    )
    last_equity_row = equity_curve[-1] if equity_curve else {}
    open_assigned_stock_unrealized_pnl = float(
        last_equity_row.get("stock_unrealized_pnl") or 0.0
    )
    open_assigned_stock_unrealized_pnl_after_premiums = float(
        last_equity_row.get("stock_unrealized_pnl_after_premiums") or 0.0
    )
    assignments_with_cc_opened = sum(
        1
        for item in assignment_diagnostics
        if item.get("first_cc_opened_date") is not None
    )
    csp_assignment_cash_values = [
        float(trade.get("assignment_cash_required") or 0.0)
        for trade in csp_trades
        if float(trade.get("assignment_cash_required") or 0.0) > 0
    ]
    csp_contract_counts = []
    for trade in csp_trades:
        try:
            contracts = int(float(trade.get("contracts") or 0))
        except (TypeError, ValueError):
            contracts = 0
        if contracts > 0:
            csp_contract_counts.append(contracts)
    candidate_ledger_summary = _summarize_candidate_ledger(
        candidate_ledger,
        start_date=_parse_iso_date(equity_curve[0]["date"]) if equity_curve else None,
        end_date=_parse_iso_date(equity_curve[-1]["date"]) if equity_curve else None,
        config=config,
    )
    support_diagnostics = _finalize_support_stats(support_stats)
    support_attempts = int(support_diagnostics.get("attempts") or 0)
    scan_days = int(candidate_ledger_summary.get("scan_days") or 0)
    data_issue_count = len(data_issues)
    return {
        "starting_equity": round(starting_equity, 2),
        "ending_equity": round(ending_equity, 2),
        "total_return_pct": (
            round((ending_equity / starting_equity - 1.0) * 100.0, 4)
            if starting_equity
            else None
        ),
        "max_drawdown_pct": max_drawdown_pct,
        "realized_option_pnl": round(state.realized_option_pnl, 2),
        "realized_stock_pnl": round(state.realized_stock_pnl, 2),
        "csp_realized_option_pnl": round(csp_realized_option_pnl, 2),
        "cc_realized_option_pnl": round(cc_realized_option_pnl, 2),
        "cc_called_away_stock_pnl": round(cc_called_away_stock_pnl, 2),
        "open_assigned_stock_unrealized_pnl": round(
            open_assigned_stock_unrealized_pnl,
            2,
        ),
        "open_assigned_stock_unrealized_pnl_after_premiums": round(
            open_assigned_stock_unrealized_pnl_after_premiums,
            2,
        ),
        "assigned_stock_recovery_pnl_estimate": round(
            cc_realized_option_pnl
            + cc_called_away_stock_pnl
            + open_assigned_stock_unrealized_pnl,
            2,
        ),
        "total_fees": round(state.total_fees, 2),
        "opened_short_puts": len(csp_trades),
        "expired_worthless": csp_status_counts.get("EXPIRED_WORTHLESS", 0),
        "assigned": csp_status_counts.get("ASSIGNED", 0),
        "closed_short_puts": (
            csp_status_counts.get("CLOSED_PROFIT_TARGET", 0)
            + csp_status_counts.get("CLOSED_MANAGE_DTE", 0)
        ),
        "csp_profit_target_closes": csp_status_counts.get("CLOSED_PROFIT_TARGET", 0),
        "csp_manage_dte_closes": csp_status_counts.get("CLOSED_MANAGE_DTE", 0),
        "csp_expired_worthless_rate_pct": (
            round(
                csp_status_counts.get("EXPIRED_WORTHLESS", 0)
                / len(csp_trades)
                * 100.0,
                4,
            )
            if csp_trades
            else None
        ),
        "csp_assignment_rate_pct": (
            round(
                csp_status_counts.get("ASSIGNED", 0)
                / len(csp_trades)
                * 100.0,
                4,
            )
            if csp_trades
            else None
        ),
        "csp_realized_option_pnl_per_trade": (
            round(csp_realized_option_pnl / len(csp_trades), 2)
            if csp_trades
            else None
        ),
        "average_csp_assignment_cash_required": (
            round(sum(csp_assignment_cash_values) / len(csp_assignment_cash_values), 2)
            if csp_assignment_cash_values
            else None
        ),
        "max_csp_assignment_cash_required": (
            round(max(csp_assignment_cash_values), 2)
            if csp_assignment_cash_values
            else None
        ),
        "average_csp_contracts_per_trade": (
            round(sum(csp_contract_counts) / len(csp_contract_counts), 4)
            if csp_contract_counts
            else None
        ),
        "max_csp_contracts_per_trade": max(csp_contract_counts) if csp_contract_counts else None,
        "opened_covered_calls": len(cc_trades),
        "expired_covered_calls": cc_status_counts.get("EXPIRED_WORTHLESS", 0),
        "called_away": cc_status_counts.get("CALLED_AWAY", 0),
        "closed_covered_calls": (
            cc_status_counts.get("CALL_CLOSED_PROFIT_TARGET", 0)
            + cc_status_counts.get("CALL_CLOSED_MANAGE_DTE", 0)
        ),
        "cc_profit_target_closes": cc_status_counts.get(
            "CALL_CLOSED_PROFIT_TARGET",
            0,
        ),
        "cc_manage_dte_closes": cc_status_counts.get("CALL_CLOSED_MANAGE_DTE", 0),
        "open_short_puts": len(state.open_short_puts),
        "open_short_calls": len(state.open_short_calls),
        "long_stock_positions": sum(1 for stock in state.stocks.values() if stock.shares > 0),
        "uncovered_assigned_days": sum(1 for value in uncovered_share_values if value > 0),
        "uncovered_assigned_share_days": sum(uncovered_share_values),
        "average_uncovered_assigned_shares": (
            round(sum(uncovered_share_values) / len(uncovered_share_values), 4)
            if uncovered_share_values
            else 0.0
        ),
        "max_uncovered_assigned_shares": (
            max(uncovered_share_values) if uncovered_share_values else 0
        ),
        "assignments_with_cc_opened": assignments_with_cc_opened,
        "assignments_without_cc_opened": max(
            0,
            csp_status_counts.get("ASSIGNED", 0) - assignments_with_cc_opened,
        ),
        "assignment_recovery_diagnostics": assignment_diagnostics,
        "assignment_recovery_diagnostic_notes": _assignment_recovery_notes(
            assigned_share_values
        ),
        "reserved_assignment_cash": round(state.reserved_assignment_cash, 2),
        "trade_status_counts": dict(sorted(trade_status_counts.items())),
        "csp_trade_status_counts": dict(sorted(csp_status_counts.items())),
        "cc_trade_status_counts": dict(sorted(cc_status_counts.items())),
        "event_counts": dict(sorted(event_counts.items())),
        "rejected_reason_counts": dict(rejected_reason_counts.most_common()),
        "candidate_ledger_diagnostics": candidate_ledger_summary,
        "support_diagnostics": support_diagnostics,
        "market_regime_diagnostics": _finalize_market_regime_stats(
            market_regime_stats
        ),
        "data_issue_count": data_issue_count,
        "data_issue_counts": dict(sorted(data_issue_counts.items())),
        "data_issues_per_100_scan_days": (
            round(data_issue_count / scan_days * 100.0, 4)
            if scan_days
            else None
        ),
        "data_issue_rate_pct_of_support_attempts": (
            round(data_issue_count / support_attempts * 100.0, 4)
            if support_attempts
            else None
        ),
        "price_space_break_category_counts": dict(
            sorted(price_space_break_category_counts.items())
        ),
        "price_space_break_action_counts": dict(
            sorted(price_space_break_action_counts.items())
        ),
        "average_capital_utilization_pct": (
            round(sum(utilization_values) / len(utilization_values), 4)
            if utilization_values
            else 0.0
        ),
        "average_daily_capital_utilization_pct": (
            round(sum(utilization_values) / len(utilization_values), 4)
            if utilization_values
            else 0.0
        ),
        "max_capital_utilization_pct": (
            round(max(utilization_values), 4) if utilization_values else 0.0
        ),
        "max_daily_capital_utilization_pct": (
            round(max(utilization_values), 4) if utilization_values else 0.0
        ),
        "average_csp_reserved_utilization_pct": (
            round(sum(reserved_utilization_values) / len(reserved_utilization_values), 4)
            if reserved_utilization_values
            else 0.0
        ),
        "max_csp_reserved_utilization_pct": (
            round(max(reserved_utilization_values), 4)
            if reserved_utilization_values
            else 0.0
        ),
        "average_assigned_stock_utilization_pct": (
            round(
                sum(assigned_stock_utilization_values)
                / len(assigned_stock_utilization_values),
                4,
            )
            if assigned_stock_utilization_values
            else 0.0
        ),
        "max_assigned_stock_utilization_pct": (
            round(max(assigned_stock_utilization_values), 4)
            if assigned_stock_utilization_values
            else 0.0
        ),
        "average_cash_pct": (
            round(sum(cash_pct_values) / len(cash_pct_values), 4)
            if cash_pct_values
            else 0.0
        ),
        "min_cash_pct": round(min(cash_pct_values), 4) if cash_pct_values else 0.0,
        "max_cash_pct": round(max(cash_pct_values), 4) if cash_pct_values else 0.0,
        "cash_idle_days": sum(1 for row in equity_curve if row.get("cash_idle")),
        "days_above_50pct_cash": sum(
            1
            for row in equity_curve
            if row.get("cash_pct") is not None and float(row["cash_pct"]) > 50.0
        ),
        "days_below_25pct_capital_utilization": sum(
            1
            for value in utilization_values
            if value < 25.0
        ),
        "average_entry_spread_pct_of_mid": (
            round(sum(entry_spreads) / len(entry_spreads), 6)
            if entry_spreads
            else None
        ),
        "average_entry_fill_discount_pct_of_mid": (
            round(sum(entry_fill_discounts) / len(entry_fill_discounts), 6)
            if entry_fill_discounts
            else None
        ),
    }


def _max_drawdown_pct(equity_values: list[float]) -> float:
    peak: float | None = None
    max_drawdown = 0.0
    for value in equity_values:
        peak = value if peak is None else max(peak, value)
        if peak > 0:
            max_drawdown = min(max_drawdown, value / peak - 1.0)
    return round(max_drawdown * 100.0, 4)


def _serialize_short_put(position: ShortPutPosition) -> dict[str, Any]:
    payload = asdict(position)
    payload["entry_date"] = position.entry_date.isoformat()
    payload["expiration"] = position.expiration.isoformat()
    return payload


def _serialize_short_call(position: ShortCallPosition) -> dict[str, Any]:
    payload = asdict(position)
    payload["entry_date"] = position.entry_date.isoformat()
    payload["expiration"] = position.expiration.isoformat()
    return payload


def _serialize_stock(position: StockPosition) -> dict[str, Any]:
    return {
        "ticker": position.ticker,
        "shares": position.shares,
        "cost_basis_total": round(position.cost_basis_total, 2),
        "premium_credit_total": round(position.premium_credit_total, 2),
        "average_cost": (
            round(position.average_cost, 4) if position.average_cost is not None else None
        ),
        "adjusted_average_cost": (
            round(position.adjusted_average_cost, 4)
            if position.adjusted_average_cost is not None
            else None
        ),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _render_report(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "# Wheel Backtest Phase 2",
        "",
        f"- Version: {result['version']}",
        f"- Range: {result['date_range']['start']} to {result['date_range']['end']}",
        f"- Universe: {', '.join(result['universe'])}",
        f"- Starting equity: ${summary['starting_equity']:,.2f}",
        f"- Ending equity: ${summary['ending_equity']:,.2f}",
        f"- Total return: {summary['total_return_pct']}%",
        f"- Max drawdown: {summary['max_drawdown_pct']}%",
        f"- Opened CSPs: {summary['opened_short_puts']}",
        f"- Expired worthless: {summary['expired_worthless']}",
        f"- Assigned: {summary['assigned']}",
        f"- Opened covered calls: {summary['opened_covered_calls']}",
        f"- Covered calls expired worthless: {summary['expired_covered_calls']}",
        f"- Called away: {summary['called_away']}",
        f"- Realized option PnL: ${summary['realized_option_pnl']:,.2f}",
        f"- Realized stock PnL: ${summary['realized_stock_pnl']:,.2f}",
        f"- Total fees: ${summary['total_fees']:,.2f}",
        f"- Data issues: {summary['data_issue_count']}",
        f"- Fundamental profile: {summary.get('fundamental_profile', 'technical_only')}",
        f"- CC risk profile: {summary.get('cc_risk_profile', 'strict')}",
        f"- Fundamental would-rejects: {summary.get('fundamental_would_reject_count', 0)}",
        f"- Fundamental warnings: {summary.get('fundamental_warn_count', 0)}",
        f"- Uncovered assigned days: {summary.get('uncovered_assigned_days', 0)}",
        f"- Uncovered assigned share-days: {summary.get('uncovered_assigned_share_days', 0)}",
        f"- CC realized option PnL: ${summary.get('cc_realized_option_pnl', 0):,.2f}",
        f"- Open assigned stock unrealized PnL: ${summary.get('open_assigned_stock_unrealized_pnl', 0):,.2f}",
        f"- Assigned stock recovery PnL estimate: ${summary.get('assigned_stock_recovery_pnl_estimate', 0):,.2f}",
        f"- Execution model: {summary.get('execution_model', 'unknown')}",
        f"- Execution fill policy: {summary.get('execution_fill_policy', 'unknown')}",
        f"- Avg entry spread pct of mid: {summary.get('average_entry_spread_pct_of_mid')}",
        f"- Avg entry fill discount pct of mid: {summary.get('average_entry_fill_discount_pct_of_mid')}",
        f"- Avg daily capital utilization: {summary.get('average_daily_capital_utilization_pct')}%",
        f"- Max daily capital utilization: {summary.get('max_daily_capital_utilization_pct')}%",
        f"- Average cash pct: {summary.get('average_cash_pct')}%",
        f"- Cash idle days: {summary.get('cash_idle_days')}",
        f"- Days above 50% cash: {summary.get('days_above_50pct_cash')}",
        f"- Mechanical CSP candidates: {summary.get('candidate_ledger_diagnostics', {}).get('mechanical_candidate_count')}",
        f"- Mechanical AUTO candidates: {summary.get('candidate_ledger_diagnostics', {}).get('mechanical_auto_trade_candidate_count')}",
        f"- Days with 3+ candidates: {summary.get('candidate_ledger_diagnostics', {}).get('pct_days_with_3plus_candidates')}%",
        f"- Starvation days: {summary.get('candidate_ledger_diagnostics', {}).get('starvation_days')}",
        "",
        "## Assumptions",
        "",
    ]
    lines.extend(f"- {assumption}" for assumption in result["assumptions"])
    if summary.get("rejected_reason_counts"):
        lines.extend(["", "## Top Rejections", ""])
        for reason, count in list(summary["rejected_reason_counts"].items())[:20]:
            lines.append(f"- {reason}: {count}")
    fundamentals = result.get("fundamental_diagnostics") or {}
    if fundamentals.get("profile") != "technical_only":
        lines.extend(["", "## Fundamental Diagnostics", ""])
        lines.append(f"- Profile: {fundamentals.get('profile')}")
        lines.append(f"- Snapshots: {fundamentals.get('snapshot_count')}")
        lines.append(f"- Would-reject count: {fundamentals.get('would_reject_count')}")
        lines.append(f"- Warning count: {fundamentals.get('warn_count')}")
        reason_counts = fundamentals.get("would_reject_reason_counts") or {}
        if reason_counts:
            lines.extend(["", "### Top Fundamental Would-Rejects", ""])
            for reason, count in list(reason_counts.items())[:15]:
                lines.append(f"- {reason}: {count}")
    return "\n".join(lines) + "\n"


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=_json_default)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, OptionQuote):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
