from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .csp_selector import evaluate_csp_candidates
from .historical_data import HistoricalDataStore, detect_price_space_breaks
from .models import (
    BrokerAccountSnapshot,
    BrokerPosition,
    CspCandidate,
    OptionQuote,
    PortfolioSnapshot,
    PriceBar,
)
from .portfolio_risk import evaluate_portfolio_risk
from .support import analyze_support


BACKTEST_VERSION = "phase_one_csp_only_v1"
DEFAULT_LOOKBACK_CALENDAR_DAYS = 430
DEFAULT_SLIPPAGE_PCT = 0.05
DEFAULT_OPTION_FEE_PER_CONTRACT = 0.10
DEFAULT_RISK_FREE_RATE = 0.04


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
class StockPosition:
    ticker: str
    shares: int = 0
    cost_basis_total: float = 0.0

    @property
    def average_cost(self) -> float | None:
        if self.shares <= 0:
            return None
        return self.cost_basis_total / self.shares


@dataclass
class BacktestState:
    starting_equity: float
    cash: float
    equity: float
    open_short_puts: list[ShortPutPosition]
    stocks: dict[str, StockPosition]
    realized_option_pnl: float = 0.0
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
) -> dict[str, Any]:
    if schedule != "daily":
        raise ValueError("phase one backtest only supports daily schedule")
    if end < start:
        raise ValueError("end date must be on or after start date")

    tickers = sorted({str(ticker).strip().upper() for ticker in universe if str(ticker).strip()})
    if not tickers:
        raise ValueError("backtest universe is empty")

    starting_equity = float(config.get("account", {}).get("starting_equity", 500000.0))
    state = BacktestState(
        starting_equity=starting_equity,
        cash=starting_equity,
        equity=starting_equity,
        open_short_puts=[],
        stocks={ticker: StockPosition(ticker=ticker) for ticker in tickers},
    )

    history_start = start - timedelta(days=lookback_calendar_days)
    stock_bars = data.load_stock_bars(tickers, history_start, end)
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
    quantity = max(1, int(config.get("trade_planner", {}).get("default_contract_quantity", 1)))
    if max_orders_per_day is None:
        max_orders_per_day = int(config.get("execution", {}).get("max_orders_per_run", 3))

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
                    _reject(
                        events,
                        rejected_reason_counts,
                        day,
                        ticker,
                        "phase_one_covered_call_workflow_not_enabled",
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
                    support_summary=support_summary,
                    trades=trades,
                    events=events,
                    diagnostics={
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
    return {
        "version": BACKTEST_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "phase_one_csp_only",
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "universe": tickers,
        "assumptions": [
            "Phase 1 only opens cash-secured puts; covered calls are intentionally out of scope.",
            "Support/trend signals use only bars strictly before the scan date.",
            "Short put entry assumes the prior-close signal can be executed at the scan-day option day-aggregate open price with a slippage haircut.",
            "Day-aggregate option volume above zero is treated as necessary but not sufficient fillability evidence.",
            "Delta filters inferred from day-aggregate option prices are approximate in Phase 1.",
            "No early assignment, early profit-taking, rolling, or 21-DTE management is modeled in Phase 1.",
            "Short puts are held to expiration and settle by underlying close: close < strike assigns, otherwise expires worthless.",
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
        },
        "config_snapshot": config,
        "summary": summary,
        "data_issues": data_issues,
        "trades": trades,
        "events": events,
        "equity_curve": equity_curve,
        "open_positions": {
            "short_puts": [_serialize_short_put(position) for position in state.open_short_puts],
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
    support_summary: dict[str, Any] | None,
    trades: list[dict[str, Any]],
    events: list[dict[str, Any]],
    diagnostics: dict[str, Any],
) -> bool:
    option = candidate.option
    entry_price = option.mid
    if entry_price <= 0:
        return False
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
            "gross_credit": gross_credit,
            "fees": fees,
            "support": support_summary,
            "diagnostics": diagnostics,
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
            stock.shares += assigned_shares
            stock.cost_basis_total += assignment_cost
            state.cash -= assignment_cost
            state.realized_option_pnl += position.gross_credit - position.fees
            _close_trade(
                trades,
                position.trade_id,
                status="ASSIGNED",
                close_date=day,
                underlying_close=underlying_close,
                realized_option_pnl=position.gross_credit - position.fees,
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
            if data_issues is not None and position.expiration > day:
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
    return state.cash + stock_value - option_liability


def _daily_equity_row(
    state: BacktestState,
    day: date,
    latest_close: dict[str, float],
) -> dict[str, Any]:
    stock_value = sum(
        position.shares * latest_close.get(ticker, 0.0)
        for ticker, position in state.stocks.items()
        if position.shares > 0
    )
    return {
        "date": day.isoformat(),
        "cash": round(state.cash, 2),
        "equity": round(state.equity, 2),
        "stock_value": round(stock_value, 2),
        "reserved_assignment_cash": round(state.reserved_assignment_cash, 2),
        "open_short_puts": len(state.open_short_puts),
        "long_stock_positions": sum(1 for stock in state.stocks.values() if stock.shares > 0),
        "capital_utilization_pct": (
            round(state.reserved_assignment_cash / state.equity * 100.0, 4)
            if state.equity > 0
            else None
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
) -> None:
    for trade in trades:
        if trade["trade_id"] != trade_id:
            continue
        trade["status"] = status
        trade["close_date"] = close_date.isoformat()
        trade["underlying_close_at_close"] = underlying_close
        trade["realized_option_pnl"] = round(realized_option_pnl, 2)
        return


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
    event_counts = Counter(str(event.get("type")) for event in events)
    data_issue_counts = Counter(str(issue.get("type")) for issue in data_issues)
    max_drawdown_pct = _max_drawdown_pct([float(row["equity"]) for row in equity_curve])
    utilization_values = [
        float(row["capital_utilization_pct"])
        for row in equity_curve
        if row.get("capital_utilization_pct") is not None
    ]
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
        "total_fees": round(state.total_fees, 2),
        "opened_short_puts": len(trades),
        "expired_worthless": trade_status_counts.get("EXPIRED_WORTHLESS", 0),
        "assigned": trade_status_counts.get("ASSIGNED", 0),
        "open_short_puts": len(state.open_short_puts),
        "long_stock_positions": sum(1 for stock in state.stocks.values() if stock.shares > 0),
        "reserved_assignment_cash": round(state.reserved_assignment_cash, 2),
        "trade_status_counts": dict(sorted(trade_status_counts.items())),
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


def _serialize_stock(position: StockPosition) -> dict[str, Any]:
    return {
        "ticker": position.ticker,
        "shares": position.shares,
        "cost_basis_total": round(position.cost_basis_total, 2),
        "average_cost": (
            round(position.average_cost, 4) if position.average_cost is not None else None
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
        "# Wheel Backtest Phase 1",
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
        f"- Total fees: ${summary['total_fees']:,.2f}",
        f"- Data issues: {summary['data_issue_count']}",
        "",
        "## Assumptions",
        "",
    ]
    lines.extend(f"- {assumption}" for assumption in result["assumptions"])
    if summary.get("rejected_reason_counts"):
        lines.extend(["", "## Top Rejections", ""])
        for reason, count in list(summary["rejected_reason_counts"].items())[:20]:
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
