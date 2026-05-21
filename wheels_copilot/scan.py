from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .csp_selector import evaluate_csp_candidates
from .gates import evaluate_earnings_gate, evaluate_fundamentals
from .market_data import fetch_daily_bars, fetch_fundamental_snapshot, fetch_put_chain
from .models import GateResult, PortfolioSnapshot
from .portfolio_risk import evaluate_portfolio_risk, summarize_portfolio_snapshot
from .support import analyze_support
from .trade_planner import build_shadow_orders, build_trade_proposals


STATUS_ORDER = {
    "AUTO_TRADE": 0,
    "WATCH": 1,
    "REJECT": 2,
    "ERROR": 3,
}


def scan_watchlist(
    config: dict[str, Any],
    tickers: list[str] | None = None,
    period: str = "1y",
    as_of: date | None = None,
    portfolio_snapshot: PortfolioSnapshot | None = None,
    portfolio_required: bool = False,
    portfolio_error: str | None = None,
) -> dict[str, Any]:
    as_of = as_of or date.today()
    tickers = tickers or list(config.get("watchlist", {}).get("tickers", []))
    normalized = [t.strip().upper() for t in tickers if t and t.strip()]

    results = []
    for ticker in normalized:
        try:
            results.append(
                scan_ticker(
                    ticker,
                    config,
                    period=period,
                    as_of=as_of,
                    portfolio_snapshot=portfolio_snapshot,
                    portfolio_required=portfolio_required,
                    portfolio_error=portfolio_error,
                )
            )
        except Exception as exc:
            results.append(_error_payload(ticker, repr(exc)))
    results.sort(
        key=lambda row: (
            STATUS_ORDER.get(row["status"], 99),
            -float(row.get("support_score") or 0),
            row["ticker"],
        )
    )

    counts = Counter(row["status"] for row in results)
    return {
        "scan_date": as_of.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": period,
        "ticker_count": len(normalized),
        "portfolio": summarize_portfolio_snapshot(portfolio_snapshot, portfolio_error),
        "summary": dict(sorted(counts.items())),
        "results": results,
    }


def scan_ticker(
    ticker: str,
    config: dict[str, Any],
    period: str = "1y",
    as_of: date | None = None,
    portfolio_snapshot: PortfolioSnapshot | None = None,
    portfolio_required: bool = False,
    portfolio_error: str | None = None,
) -> dict[str, Any]:
    as_of = as_of or date.today()
    ticker = ticker.strip().upper()
    try:
        bars = fetch_daily_bars(ticker, period=period, config=config)
        if not bars:
            return _error_payload(ticker, "no_daily_bars")
        fundamental_snapshot = fetch_fundamental_snapshot(ticker, bars=bars, as_of=as_of)
        fundamental_gate = evaluate_fundamentals(fundamental_snapshot, config)
        if fundamental_gate.status == "REJECT":
            return _gate_reject_payload(
                ticker=ticker,
                current_price=bars[-1].close,
                fundamental_snapshot=fundamental_snapshot,
                fundamental_gate=fundamental_gate,
                earnings_gate=None,
                portfolio_gate=None,
                portfolio_risk=None,
                status_reason="fundamental gate rejected",
            )

        support = analyze_support(bars, config)
        options = fetch_put_chain(
            ticker,
            int(config["csp_selector"]["dte_min"]),
            int(config["csp_selector"]["dte_max"]),
            as_of=as_of,
            config=config,
        )
        earnings_gate, earnings_allowed_options = evaluate_earnings_gate(
            fundamental_snapshot, options, as_of=as_of
        )
        if earnings_gate.status == "REJECT":
            payload = _payload(
                ticker,
                support,
                evaluate_csp_candidates([], support, config),
                len(options),
                fundamental_snapshot=fundamental_snapshot,
                fundamental_gate=fundamental_gate,
                earnings_gate=earnings_gate,
                earnings_filtered_option_count=0,
                portfolio_gate=None,
                portfolio_risk=None,
            )
            payload["status"] = "REJECT"
            payload["status_reason"] = "earnings gate rejected"
            return payload
        selection = evaluate_csp_candidates(earnings_allowed_options, support, config)
        portfolio_gate, portfolio_risk = evaluate_portfolio_risk(
            ticker,
            selection.candidate,
            portfolio_snapshot,
            config,
            required=portfolio_required,
            portfolio_error=portfolio_error,
        )
        payload = _payload(
            ticker,
            support,
            selection,
            len(options),
            fundamental_snapshot=fundamental_snapshot,
            fundamental_gate=fundamental_gate,
            earnings_gate=earnings_gate,
            earnings_filtered_option_count=len(earnings_allowed_options),
            portfolio_gate=portfolio_gate,
            portfolio_risk=portfolio_risk,
        )
        payload["status"] = classify_scan_result(payload)
        payload["status_reason"] = _status_reason(payload)
        return payload
    except Exception as exc:
        return _error_payload(ticker, repr(exc))


def classify_scan_result(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return "ERROR"
    if _gate_rejected(payload):
        return "REJECT"
    candidate = payload.get("candidate")
    if candidate and candidate.get("auto_trade") and not payload.get("manual_review_required"):
        return "AUTO_TRADE"
    if candidate:
        return "WATCH"
    if payload.get("support_tradable"):
        return "WATCH"
    return "REJECT"


def write_scan_outputs(
    scan: dict[str, Any],
    output_dir: Path,
    config: dict[str, Any] | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    trade_proposals = build_trade_proposals(scan, config)
    shadow_orders = build_shadow_orders(trade_proposals, config)
    paths = {
        "json": output_dir / "scan_results.json",
        "markdown": output_dir / "scan_report.md",
        "csv": output_dir / "scan_summary.csv",
        "trade_proposals": output_dir / "trade_proposals.json",
        "shadow_orders": output_dir / "shadow_orders.json",
    }
    paths["json"].write_text(
        json.dumps(scan, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    paths["markdown"].write_text(render_markdown_report(scan), encoding="utf-8")
    _write_csv(scan, paths["csv"])
    paths["trade_proposals"].write_text(
        json.dumps(trade_proposals, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    paths["shadow_orders"].write_text(
        json.dumps(shadow_orders, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return paths


def resolve_output_dir(base: Path, overwrite: bool = False) -> Path:
    if overwrite or not base.exists() or not any(base.iterdir()):
        return base
    suffix = datetime.now().strftime("run-%H%M%S")
    return base / suffix


def render_markdown_report(scan: dict[str, Any]) -> str:
    lines = [
        f"# Markus Wheel Daily Dry Run - {scan['scan_date']}",
        "",
        f"- Generated: `{scan['generated_at']}`",
        f"- Period: `{scan['period']}`",
        f"- Tickers: `{scan['ticker_count']}`",
        f"- Summary: `{scan['summary']}`",
    ]
    portfolio = scan.get("portfolio")
    if portfolio:
        if portfolio.get("error"):
            lines.append(f"- Portfolio: `ERROR {portfolio['error']}`")
        else:
            lines.append(
                "- Portfolio: "
                f"`cash={_fmt(portfolio.get('cash'), 0)}, "
                f"equity={_fmt(portfolio.get('equity'), 0)}, "
                f"reserved_assignment_cash={_fmt(portfolio.get('reserved_assignment_cash'), 0)}, "
                f"positions={portfolio.get('position_count')}, "
                f"open_orders={portfolio.get('open_order_count')}`"
            )
    lines.extend(
        [
            "",
            "| Status | Ticker | Price | Fundamental | Earnings | Portfolio | Support Score | CSP Candidate | Rejection Summary |",
            "|---|---:|---:|---|---|---|---:|---|---|",
        ]
    )
    for row in scan["results"]:
        lines.append(_markdown_table_row(row))

    lines.extend(["", "## Details", ""])
    for row in scan["results"]:
        lines.extend(_detail_section(row))
    return "\n".join(lines).rstrip() + "\n"


def _payload(
    ticker: str,
    support,
    selection,
    option_count: int,
    fundamental_snapshot=None,
    fundamental_gate: GateResult | None = None,
    earnings_gate: GateResult | None = None,
    earnings_filtered_option_count: int | None = None,
    portfolio_gate: GateResult | None = None,
    portfolio_risk: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected = support.selected_zone
    candidate = selection.candidate
    candidate_payload = None
    if candidate:
        candidate_payload = asdict(candidate)
        candidate_payload["option"]["mid"] = candidate.option.mid
        candidate_payload["option"]["executable_mid"] = candidate.option.executable_mid
        candidate_payload["option"]["spread_pct_of_mid"] = (
            candidate.option.spread_pct_of_mid
        )
    selected_payload = asdict(selected) if selected else None
    return {
        "ticker": ticker,
        "current_price": round(support.current_price, 2),
        "trend_passed": support.trend.passed,
        "trend_reasons": support.trend.reasons,
        "atr14": round(support.atr14, 2) if support.atr14 is not None else None,
        "support_tradable": support.tradable,
        "support_score": round(selected.score, 1) if selected else None,
        "selected_support": selected_payload,
        "top_support_zones": [asdict(z) for z in support.zones[:5]],
        "option_count": option_count,
        "earnings_filtered_option_count": earnings_filtered_option_count,
        "candidate": candidate_payload,
        "csp_policy": selection.policy_name,
        "rejection_summary": selection.rejection_summary,
        "fundamental_snapshot": asdict(fundamental_snapshot) if fundamental_snapshot else None,
        "fundamental_gate": asdict(fundamental_gate) if fundamental_gate else None,
        "earnings_gate": asdict(earnings_gate) if earnings_gate else None,
        "portfolio_gate": asdict(portfolio_gate) if portfolio_gate else None,
        "portfolio_risk": portfolio_risk,
        "manual_review_required": _manual_review_required(
            fundamental_gate,
            earnings_gate,
            portfolio_gate,
        ),
        "reasons": support.reasons,
        "error": None,
    }


def _error_payload(ticker: str, error: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "status": "ERROR",
        "status_reason": error,
        "current_price": None,
        "trend_passed": False,
        "trend_reasons": [],
        "atr14": None,
        "support_tradable": False,
        "support_score": None,
        "selected_support": None,
        "top_support_zones": [],
        "option_count": 0,
        "earnings_filtered_option_count": None,
        "candidate": None,
        "csp_policy": None,
        "rejection_summary": {},
        "fundamental_snapshot": None,
        "fundamental_gate": None,
        "earnings_gate": None,
        "portfolio_gate": None,
        "portfolio_risk": None,
        "manual_review_required": False,
        "reasons": [],
        "error": error,
    }


def _status_reason(payload: dict[str, Any]) -> str:
    if payload.get("error"):
        return payload["error"]
    if _gate_rejected(payload):
        return _gate_reason(payload)
    candidate = payload.get("candidate")
    if candidate and candidate.get("auto_trade"):
        option = candidate["option"]
        if payload.get("manual_review_required"):
            return "candidate found but gates require manual review"
        return f"auto CSP candidate {option['expiration']} {option['strike']}P"
    if candidate:
        return "candidate found but not auto-tradable"
    if payload.get("support_tradable"):
        return "technical setup tradable but no eligible CSP"
    reasons = payload.get("reasons") or payload.get("trend_reasons") or []
    return "; ".join(reasons[:3]) if reasons else "technical setup rejected"


def _markdown_table_row(row: dict[str, Any]) -> str:
    candidate = row.get("candidate")
    candidate_text = "-"
    if candidate:
        option = candidate["option"]
        candidate_text = (
            f"{option['expiration']} {option['strike']}P "
            f"mid {_fmt(option.get('executable_mid') or option.get('mid'))} "
            f"delta {_fmt(candidate['delta'], 3)}"
        )
    return (
        f"| {_md_cell(row['status'])} | {_md_cell(row['ticker'])} | {_fmt(row.get('current_price'))} | "
        f"{_md_cell(_gate_status(row.get('fundamental_gate')))} | "
        f"{_md_cell(_gate_status(row.get('earnings_gate')))} | "
        f"{_md_cell(_gate_status(row.get('portfolio_gate')))} | "
        f"{_fmt(row.get('support_score'), 1)} | "
        f"{candidate_text} | {_md_cell(_top_rejections(row.get('rejection_summary') or {}))} |"
    )


def _detail_section(row: dict[str, Any]) -> list[str]:
    lines = [
        f"### {row['ticker']} - {row['status']}",
        "",
        f"- Reason: {row.get('status_reason') or '-'}",
        f"- Price: `{_fmt(row.get('current_price'))}`",
        f"- Trend passed: `{row.get('trend_passed')}`",
        f"- Support tradable: `{row.get('support_tradable')}`",
        f"- Option contracts checked: `{row.get('option_count')}`",
    ]
    if row.get("fundamental_gate"):
        lines.append(
            f"- Fundamental gate: `{_gate_status(row['fundamental_gate'])}`"
        )
    if row.get("earnings_gate"):
        lines.append(f"- Earnings gate: `{_gate_status(row['earnings_gate'])}`")
    if row.get("portfolio_gate"):
        lines.append(f"- Portfolio gate: `{_gate_status(row['portfolio_gate'])}`")
    if row.get("portfolio_risk"):
        risk = row["portfolio_risk"]
        lines.append(
            "- Portfolio risk: "
            f"`assignment_cash={_fmt(risk.get('assignment_cash_required'), 0)}, "
            f"projected_reserved={_fmt(risk.get('projected_reserved_assignment_cash'), 0)}, "
            f"cash_after_reserve={_fmt(risk.get('projected_cash_after_reserve'), 0)}, "
            f"active_tickers={risk.get('projected_active_ticker_count')}`"
        )
    if row.get("manual_review_required"):
        lines.append("- Manual review required: `True`")
    snapshot = row.get("fundamental_snapshot")
    if snapshot:
        lines.append(
            "- Fundamentals: "
            f"`market_cap={_fmt(snapshot.get('market_cap'), 0)}, "
            f"pe={_fmt(snapshot.get('pe_ratio'), 2)}, "
            f"dividend_yield={_fmt(snapshot.get('dividend_yield'), 4)}, "
            f"next_earnings={snapshot.get('next_earnings_date') or '-'}`"
        )
    support = row.get("selected_support")
    if support:
        lines.append(
            f"- Selected support: `{support['method']} "
            f"{_fmt(support['bottom'])}-{_fmt(support['top'])}, "
            f"score {_fmt(support['score'], 1)}`"
        )
    candidate = row.get("candidate")
    if candidate:
        option = candidate["option"]
        lines.extend(
            [
                f"- CSP: `{option['expiration']} {option['strike']}P`",
                f"- Premium: `${(option.get('executable_mid') or option.get('mid') or 0) * 100:.0f}` per contract",
                f"- Delta bucket: `{candidate['delta_bucket']}`",
                f"- Weekly ROC: `{_fmt(candidate['weekly_return_on_strike_pct'], 2)}%`",
                f"- Auto trade: `{candidate['auto_trade']}`",
            ]
        )
    if row.get("rejection_summary"):
        lines.append(f"- Rejections: `{_top_rejections(row['rejection_summary'], limit=8)}`")
    if row.get("trend_reasons"):
        lines.append(f"- Trend reasons: `{'; '.join(row['trend_reasons'])}`")
    if row.get("reasons"):
        lines.append(f"- Support reasons: `{'; '.join(row['reasons'])}`")
    lines.append("")
    return lines


def _write_csv(scan: dict[str, Any], path: Path) -> None:
    fields = [
        "status",
        "ticker",
        "current_price",
        "support_score",
        "support_method",
        "support_bottom",
        "support_top",
        "fundamental_status",
        "earnings_status",
        "portfolio_status",
        "manual_review_required",
        "next_earnings_date",
        "assignment_cash_required",
        "projected_reserved_assignment_cash",
        "projected_cash_after_reserve",
        "candidate_expiration",
        "candidate_strike",
        "candidate_delta",
        "candidate_mid",
        "candidate_auto_trade",
        "status_reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in scan["results"]:
            support = row.get("selected_support") or {}
            snapshot = row.get("fundamental_snapshot") or {}
            risk = row.get("portfolio_risk") or {}
            candidate = row.get("candidate") or {}
            option = candidate.get("option") or {}
            writer.writerow(
                {
                    "status": row.get("status"),
                    "ticker": row.get("ticker"),
                    "current_price": row.get("current_price"),
                    "support_score": row.get("support_score"),
                    "support_method": support.get("method"),
                    "support_bottom": support.get("bottom"),
                    "support_top": support.get("top"),
                    "fundamental_status": (row.get("fundamental_gate") or {}).get("status"),
                    "earnings_status": (row.get("earnings_gate") or {}).get("status"),
                    "portfolio_status": (row.get("portfolio_gate") or {}).get("status"),
                    "manual_review_required": row.get("manual_review_required"),
                    "next_earnings_date": snapshot.get("next_earnings_date"),
                    "assignment_cash_required": risk.get("assignment_cash_required"),
                    "projected_reserved_assignment_cash": risk.get(
                        "projected_reserved_assignment_cash"
                    ),
                    "projected_cash_after_reserve": risk.get("projected_cash_after_reserve"),
                    "candidate_expiration": option.get("expiration"),
                    "candidate_strike": option.get("strike"),
                    "candidate_delta": candidate.get("delta"),
                    "candidate_mid": option.get("executable_mid") or option.get("mid"),
                    "candidate_auto_trade": candidate.get("auto_trade"),
                    "status_reason": row.get("status_reason"),
                }
            )


def _top_rejections(summary: dict[str, int], limit: int = 3) -> str:
    if not summary:
        return "-"
    return ", ".join(
        f"{reason}:{count}"
        for reason, count in list(summary.items())[:limit]
    )


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _md_cell(value: Any) -> str:
    text = _fmt(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "\\`")


def _gate_reject_payload(
    ticker: str,
    current_price: float,
    fundamental_snapshot,
    fundamental_gate: GateResult,
    earnings_gate: GateResult | None,
    portfolio_gate: GateResult | None,
    portfolio_risk: dict[str, Any] | None,
    status_reason: str,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "status": "REJECT",
        "status_reason": status_reason,
        "current_price": round(current_price, 2),
        "trend_passed": None,
        "trend_reasons": [],
        "atr14": None,
        "support_tradable": False,
        "support_score": None,
        "selected_support": None,
        "top_support_zones": [],
        "option_count": 0,
        "earnings_filtered_option_count": None,
        "candidate": None,
        "csp_policy": None,
        "rejection_summary": {},
        "fundamental_snapshot": asdict(fundamental_snapshot),
        "fundamental_gate": asdict(fundamental_gate),
        "earnings_gate": asdict(earnings_gate) if earnings_gate else None,
        "portfolio_gate": asdict(portfolio_gate) if portfolio_gate else None,
        "portfolio_risk": portfolio_risk,
        "manual_review_required": _manual_review_required(
            fundamental_gate,
            earnings_gate,
            portfolio_gate,
        ),
        "reasons": fundamental_gate.reasons,
        "error": None,
    }


def _manual_review_required(
    fundamental_gate: GateResult | None,
    earnings_gate: GateResult | None,
    portfolio_gate: GateResult | None = None,
) -> bool:
    return any(
        gate.manual_review_required
        for gate in (fundamental_gate, earnings_gate, portfolio_gate)
        if gate is not None
    )


def _gate_status(gate: dict[str, Any] | None) -> str:
    if not gate:
        return "-"
    parts = [str(gate.get("status") or "-")]
    warnings = gate.get("warnings") or []
    reasons = gate.get("reasons") or []
    detail = warnings or reasons
    if detail:
        parts.append(": " + "; ".join(str(x) for x in detail[:2]))
    return "".join(parts)


def _gate_rejected(payload: dict[str, Any]) -> bool:
    return any(
        (payload.get(name) or {}).get("status") == "REJECT"
        for name in ("fundamental_gate", "earnings_gate", "portfolio_gate")
    )


def _gate_reason(payload: dict[str, Any]) -> str:
    for name in ("fundamental_gate", "earnings_gate", "portfolio_gate"):
        gate = payload.get(name) or {}
        if gate.get("status") == "REJECT":
            reasons = gate.get("reasons") or []
            return "; ".join(str(r) for r in reasons) or f"{name} rejected"
    return "gate rejected"
