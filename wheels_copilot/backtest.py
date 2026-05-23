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
from .support import analyze_support


BACKTEST_VERSION = "phase_two_csp_cc_execution_v2"
DEFAULT_LOOKBACK_CALENDAR_DAYS = 430
DEFAULT_SLIPPAGE_PCT = 0.05
DEFAULT_OPTION_FEE_PER_CONTRACT = 0.10
DEFAULT_RISK_FREE_RATE = 0.04
FUNDAMENTAL_PROFILES = {
    "technical_only",
    "fundamentals_warn",
    "fundamentals_strict_financials",
    "fundamentals_strict_all",
}
CC_RISK_PROFILES = {"strict", "warn_unknown_dates"}
STRICT_FINANCIAL_REASON_PREFIXES = (
    "recent_move_",
    "pe_ratio_non_positive",
    "pe_ratio_at_or_above_",
    "positive_quarters_",
    "positive_years_",
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
) -> dict[str, Any]:
    if schedule != "daily":
        raise ValueError("phase one backtest only supports daily schedule")
    if end < start:
        raise ValueError("end date must be on or after start date")
    fundamental_profile = _normalize_fundamental_profile(fundamental_profile)
    cc_risk_profile = _normalize_cc_risk_profile(cc_risk_profile, config)
    if fundamental_profile == "technical_only":
        historical_fundamentals = None

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
    stock_bars = data.load_stock_bars(tickers, history_start, end)
    if fundamental_profile != "technical_only" and historical_fundamentals is None:
        historical_fundamentals = build_historical_fundamental_store(
            config=config,
            cache_dir=fundamentals_cache_dir,
            env_file=fundamentals_env_file,
            timeout_seconds=fundamentals_timeout_seconds,
        )
    if historical_fundamentals is not None and fundamental_profile != "technical_only":
        historical_fundamentals.preload(tickers, start, end)

    bars_by_day = _bars_by_day(stock_bars)
    pre_start_split_issues = _detect_pre_start_split_issues(
        stock_bars,
        start=start,
        ratio_low=split_ratio_low,
        ratio_high=split_ratio_high,
    )
    blocked_tickers = set(pre_start_split_issues)
    data_issues: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for ticker, issues in pre_start_split_issues.items():
        for issue in issues:
            data_issues.append(
                {
                    "date": issue["date"],
                    "ticker": ticker,
                    "type": "price_space_break",
                    "details": issue,
                }
            )
            events.append(
                {
                    "date": start.isoformat(),
                    "ticker": ticker,
                    "type": "PRICE_SPACE_BREAK_BLOCK",
                    "reason": "pre_start_price_space_break_in_lookback_window",
                    "details": issue,
                }
            )

    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    rejected_reason_counts: Counter[str] = Counter()
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
    quantity = max(1, int(config.get("trade_planner", {}).get("default_contract_quantity", 1)))
    if max_orders_per_day is None:
        max_orders_per_day = int(config.get("execution", {}).get("max_orders_per_run", 3))
    execution_model = build_backtest_execution_model(config, slippage_pct=slippage_pct)
    cc_backtest_config = _covered_call_backtest_config(config, cc_risk_profile)

    for day in run_days:
        todays_bars = bars_by_day.get(day, {})
        for ticker, bar in todays_bars.items():
            issue = _same_day_price_space_break(
                previous_bar_by_ticker.get(ticker),
                bar,
                ratio_low=split_ratio_low,
                ratio_high=split_ratio_high,
            )
            if issue is not None and ticker not in blocked_tickers:
                blocked_tickers.add(ticker)
                data_issue = {
                    "date": day.isoformat(),
                    "ticker": ticker,
                    "type": "price_space_break",
                    "details": issue,
                }
                data_issues.append(data_issue)
                events.append(
                    {
                        "date": day.isoformat(),
                        "ticker": ticker,
                        "type": "PRICE_SPACE_BREAK_BLOCK",
                        "reason": "same_day_price_space_break",
                        "details": issue,
                    }
                )

        orders_opened_today = 0
        if todays_bars:
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
            for ticker in tickers:
                if orders_opened_today >= max_orders_per_day:
                    break
                if ticker in blocked_tickers:
                    continue
                if ticker not in todays_bars:
                    _reject(
                        events,
                        rejected_reason_counts,
                        day,
                        ticker,
                        "missing_stock_bar_on_scan_day",
                    )
                    continue
                if _has_open_short_put(state, ticker):
                    continue
                if state.stocks.get(ticker, StockPosition(ticker)).shares >= 100:
                    continue

                fundamental_context = _fundamental_context(
                    historical_fundamentals=historical_fundamentals,
                    profile=fundamental_profile,
                    ticker=ticker,
                    day=day,
                    bars=stock_bars.get(ticker, []),
                    config=config,
                )
                if fundamental_context is not None:
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
                        _reject(
                            events,
                            rejected_reason_counts,
                            day,
                            ticker,
                            _gate_primary_reason("fundamental_gate", fundamental_gate),
                            diagnostics=_fundamental_context_payload(fundamental_context),
                        )
                        continue

                candidate, support_summary, rejection_summary = _select_candidate_for_day(
                    data=data,
                    ticker=ticker,
                    day=day,
                    bars=stock_bars.get(ticker, []),
                    config=config,
                    dte_min=dte_min,
                    dte_max=dte_max,
                    slippage_pct=slippage_pct,
                    risk_free_rate=risk_free_rate,
                    execution_model=execution_model,
                )
                if candidate is None:
                    reason = _primary_rejection_reason(rejection_summary)
                    _reject(
                        events,
                        rejected_reason_counts,
                        day,
                        ticker,
                        reason,
                        support=support_summary,
                        diagnostics={"csp_rejection_summary": rejection_summary},
                    )
                    continue
                if not candidate.auto_trade:
                    _reject(
                        events,
                        rejected_reason_counts,
                        day,
                        ticker,
                        "candidate_watch_only",
                        support=support_summary,
                        diagnostics=_candidate_diagnostics(candidate),
                    )
                    continue

                candidate_earnings_gate: GateResult | None = None
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
                        _reject(
                            events,
                            rejected_reason_counts,
                            day,
                            ticker,
                            _gate_primary_reason(
                                "csp_earnings_gate",
                                candidate_earnings_gate,
                            ),
                            support=support_summary,
                            diagnostics={
                                **_fundamental_context_payload(fundamental_context),
                                "earnings_gate": asdict(candidate_earnings_gate),
                                "candidate": _candidate_diagnostics(candidate),
                            },
                        )
                        continue

                scaled_candidate = _scaled_candidate(candidate, quantity)
                available_cash_for_assignment = state.cash - state.reserved_assignment_cash
                if available_cash_for_assignment < scaled_candidate.assignment_cash_required:
                    _reject(
                        events,
                        rejected_reason_counts,
                        day,
                        ticker,
                        "insufficient_cash_secured_capacity",
                        support=support_summary,
                        diagnostics={
                            "cash": round(state.cash, 2),
                            "reserved_assignment_cash": round(
                                state.reserved_assignment_cash, 2
                            ),
                            "available_cash_for_assignment": round(
                                available_cash_for_assignment, 2
                            ),
                            "new_assignment_cash_required": round(
                                scaled_candidate.assignment_cash_required, 2
                            ),
                        },
                    )
                    continue
                marked_equity = _mark_state_equity(state, latest_close, data, day)
                portfolio_gate, portfolio_diagnostics = evaluate_portfolio_risk(
                    ticker,
                    scaled_candidate,
                    _portfolio_snapshot(state, marked_equity),
                    config,
                    required=True,
                )
                if portfolio_gate is None or portfolio_gate.status == "REJECT":
                    reason = (
                        portfolio_gate.reasons[0]
                        if portfolio_gate and portfolio_gate.reasons
                        else "portfolio_risk_reject"
                    )
                    _reject(
                        events,
                        rejected_reason_counts,
                        day,
                        ticker,
                        reason,
                        support=support_summary,
                        diagnostics={
                            "portfolio_gate": asdict(portfolio_gate) if portfolio_gate else None,
                            "portfolio_risk": portfolio_diagnostics,
                        },
                    )
                    continue

                opened = _open_short_put(
                    state=state,
                    candidate=scaled_candidate,
                    day=day,
                    ticker=ticker,
                    contracts=quantity,
                    option_fee_per_contract=option_fee_per_contract,
                    execution_model=execution_model,
                    support_summary=support_summary,
                    trades=trades,
                    events=events,
                    diagnostics={
                        **(
                            _fundamental_context_payload(fundamental_context)
                            if fundamental_context is not None
                            else {}
                        ),
                        **(
                            {"earnings_gate": asdict(candidate_earnings_gate)}
                            if candidate_earnings_gate is not None
                            else {}
                        ),
                        "portfolio_gate": asdict(portfolio_gate),
                        "portfolio_risk": portfolio_diagnostics,
                    },
                )
                if opened:
                    orders_opened_today += 1

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
        state=state,
        equity_curve=equity_curve,
        trades=trades,
        events=events,
        rejected_reason_counts=rejected_reason_counts,
        data_issues=data_issues,
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
            "No early assignment, early profit-taking, rolling, or 21-DTE management is modeled in Phase 1.",
            "Short puts are held to expiration and settle by underlying close: close < strike assigns, otherwise expires worthless.",
            "Covered calls are held to expiration and settle by underlying close: close > strike is called away, otherwise expires worthless.",
            "Corporate action or price-space breaks are guarded by large historical close-to-close and same-day open-to-previous-close ratio detection.",
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
            "contract_quantity": quantity,
            "fundamental_profile": fundamental_profile,
            "cc_risk_profile": cc_risk_profile,
            "fundamentals_cache_dir": (
                str(fundamentals_cache_dir)
                if fundamental_profile != "technical_only"
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
) -> tuple[CspCandidate | None, dict[str, Any] | None, dict[str, int]]:
    support_bars = [bar for bar in bars if bar.date < day]
    if len(support_bars) < 30:
        return None, None, {"insufficient_support_history": 1}
    try:
        support = analyze_support(support_bars, config)
    except ValueError as exc:
        return None, None, {f"support_error:{exc}": 1}

    support_summary = {
        "tradable": support.tradable,
        "current_price": support.current_price,
        "atr14": support.atr14,
        "trend_passed": support.trend.passed,
        "trend_reasons": support.trend.reasons,
        "selected_zone": (
            {
                "method": support.selected_zone.method,
                "center": support.selected_zone.center,
                "bottom": support.selected_zone.bottom,
                "top": support.selected_zone.top,
                "score": support.selected_zone.score,
                "touches": support.selected_zone.touches,
                "rejections": support.selected_zone.rejections,
                "last_touch_date": (
                    support.selected_zone.last_touch_date.isoformat()
                    if support.selected_zone.last_touch_date
                    else None
                ),
            }
            if support.selected_zone
            else None
        ),
        "reasons": support.reasons,
    }
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
        return None, support_summary, {"no_fillable_put_options": 1}

    result = evaluate_csp_candidates(options, support, config, risk_free_rate)
    if result.candidate is None:
        return None, support_summary, result.rejection_summary or {"no_csp_candidate": 1}
    return result.candidate, support_summary, result.rejection_summary


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
            bars=stock_bars.get(ticker, []),
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
        "reserved_assignment_cash": round(state.reserved_assignment_cash, 2),
        "open_short_puts": len(state.open_short_puts),
        "open_short_calls": len(state.open_short_calls),
        "long_stock_positions": sum(1 for stock in state.stocks.values() if stock.shares > 0),
        "capital_utilization_pct": (
            round(state.reserved_assignment_cash / state.equity * 100.0, 4)
            if state.equity > 0
            else None
        ),
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
) -> dict[str, Any] | None:
    if profile == "technical_only" or historical_fundamentals is None:
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
    return False


def _should_block_csp_earnings_gate(profile: str, gate: GateResult) -> bool:
    return profile == "fundamentals_strict_all" and gate.status == "REJECT"


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
    if profile == "fundamentals_strict_financials" and ex_dividend_gate.status == "REJECT":
        return ex_dividend_gate
    return None


def _is_strict_financial_reason(reason: str) -> bool:
    return any(reason.startswith(prefix) for prefix in STRICT_FINANCIAL_REASON_PREFIXES)


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


def _summarize_backtest(
    *,
    state: BacktestState,
    equity_curve: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
    rejected_reason_counts: Counter[str],
    data_issues: list[dict[str, Any]],
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
    max_drawdown_pct = _max_drawdown_pct([float(row["equity"]) for row in equity_curve])
    utilization_values = [
        float(row["capital_utilization_pct"])
        for row in equity_curve
        if row.get("capital_utilization_pct") is not None
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
        "opened_covered_calls": len(cc_trades),
        "expired_covered_calls": cc_status_counts.get("EXPIRED_WORTHLESS", 0),
        "called_away": cc_status_counts.get("CALLED_AWAY", 0),
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
        "data_issue_count": len(data_issues),
        "data_issue_counts": dict(sorted(data_issue_counts.items())),
        "average_capital_utilization_pct": (
            round(sum(utilization_values) / len(utilization_values), 4)
            if utilization_values
            else 0.0
        ),
        "max_capital_utilization_pct": (
            round(max(utilization_values), 4) if utilization_values else 0.0
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
