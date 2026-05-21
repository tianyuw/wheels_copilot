from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from .config import load_config
from .csp_selector import evaluate_csp_candidates
from .market_data import fetch_daily_bars, fetch_put_chain
from .support import analyze_support


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run Markus wheel CSP selector")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--config", default="config/markus_wheel.yaml")
    parser.add_argument("--period", default="1y")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))
    bars = fetch_daily_bars(args.ticker, period=args.period)
    if not bars:
        raise SystemExit(f"No daily bars returned for {args.ticker}")
    support = analyze_support(bars, cfg)
    options = fetch_put_chain(
        args.ticker,
        int(cfg["csp_selector"]["dte_min"]),
        int(cfg["csp_selector"]["dte_max"]),
        as_of=date.today(),
    )
    selection = evaluate_csp_candidates(options, support, cfg)
    payload = _payload(args.ticker.upper(), support, selection, len(options))

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_text(payload)
    return 0


def _payload(ticker: str, support, selection, option_count: int) -> dict:
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
    return {
        "ticker": ticker,
        "current_price": round(support.current_price, 2),
        "trend_passed": support.trend.passed,
        "trend_reasons": support.trend.reasons,
        "atr14": round(support.atr14, 2) if support.atr14 is not None else None,
        "support_tradable": support.tradable,
        "selected_support": asdict(selected) if selected else None,
        "top_support_zones": [asdict(z) for z in support.zones[:5]],
        "option_count": option_count,
        "candidate": candidate_payload,
        "csp_policy": selection.policy_name,
        "rejection_summary": selection.rejection_summary,
        "reasons": support.reasons,
    }


def _print_text(payload: dict) -> None:
    print(f"{payload['ticker']} dry run")
    print(f"  price: {payload['current_price']}")
    print(f"  trend passed: {payload['trend_passed']}")
    print(f"  support tradable: {payload['support_tradable']}")
    support = payload["selected_support"]
    if support:
        print(
            "  support: "
            f"{support['method']} {support['bottom']:.2f}-{support['top']:.2f} "
            f"score={support['score']:.1f}"
        )
    else:
        print("  support: none")
    candidate = payload["candidate"]
    if candidate:
        option = candidate["option"]
        print(
            "  CSP: "
            f"{option['expiration']} {option['strike']}P "
            f"mid={option['mid']:.2f} delta={candidate['delta']:.3f} "
            f"bucket={candidate['delta_bucket']} auto={candidate['auto_trade']}"
        )
        print(
            "  premium: "
            f"${option['mid'] * 100:.0f} / contract, "
            f"ROC={candidate['weekly_return_on_strike_pct']:.2f}%"
        )
    else:
        print("  CSP: no eligible candidate")
        if payload["rejection_summary"]:
            print("  CSP rejection summary:")
            for reason, count in list(payload["rejection_summary"].items())[:8]:
                print(f"    {reason}: {count}")
    for reason in payload["reasons"]:
        print(f"  reason: {reason}")


if __name__ == "__main__":
    raise SystemExit(main())
