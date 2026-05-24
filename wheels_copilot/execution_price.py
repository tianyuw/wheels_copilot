from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .models import OptionQuote


EXECUTION_MODEL_DAY_AGG_SYNTHETIC_SPREAD = "day_agg_synthetic_spread"
EXECUTION_MODEL_ZERO_SPREAD_LEGACY = "zero_spread_legacy"
DEFAULT_SCAN_TIMING = "previous_close_signal_next_open_fill"
DEFAULT_REFERENCE_PRICE_SOURCE = "option_day_agg_open"
DEFAULT_FILL_POLICY = "mid"
DEFAULT_CALIBRATION_STATUS = "v0_uncalibrated"


@dataclass(frozen=True)
class SyntheticSpreadConfig:
    min_spread_pct_of_mid: float = 0.08
    min_spread_dollars: float = 0.0
    max_spread_pct_of_mid: float = 0.40
    low_premium_threshold: float = 0.50
    low_premium_extra_pct: float = 0.05
    low_volume_threshold: int = 50
    low_volume_extra_pct: float = 0.05
    wide_otm_pct_threshold: float = 0.15
    wide_otm_extra_pct: float = 0.03


@dataclass(frozen=True)
class BacktestExecutionModel:
    model: str = EXECUTION_MODEL_DAY_AGG_SYNTHETIC_SPREAD
    scan_timing: str = DEFAULT_SCAN_TIMING
    fill_policy: str = DEFAULT_FILL_POLICY
    reference_price_source: str = DEFAULT_REFERENCE_PRICE_SOURCE
    calibration_status: str = DEFAULT_CALIBRATION_STATUS
    reference_price_adjustment_pct: float = 0.0
    fill_price_penalty_dollars: float = 0.0
    fill_price_penalty_pct_of_mid: float = 0.0
    synthetic_spread: SyntheticSpreadConfig = field(default_factory=SyntheticSpreadConfig)

    def metadata(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "scan_timing": self.scan_timing,
            "fill_policy": self.fill_policy,
            "reference_price_source": self.reference_price_source,
            "calibration_status": self.calibration_status,
            "reference_price_adjustment_pct": self.reference_price_adjustment_pct,
            "fill_price_penalty_dollars": self.fill_price_penalty_dollars,
            "fill_price_penalty_pct_of_mid": self.fill_price_penalty_pct_of_mid,
            "synthetic_spread": asdict(self.synthetic_spread),
        }


def build_backtest_execution_model(
    config: Mapping[str, Any],
    *,
    slippage_pct: float = 0.0,
) -> BacktestExecutionModel:
    cfg = config.get("backtest_execution") or {}
    if not isinstance(cfg, Mapping):
        cfg = {}
    spread_cfg = cfg.get("synthetic_spread") or {}
    if not isinstance(spread_cfg, Mapping):
        spread_cfg = {}
    model = str(cfg.get("model") or EXECUTION_MODEL_DAY_AGG_SYNTHETIC_SPREAD)
    if model not in {
        EXECUTION_MODEL_DAY_AGG_SYNTHETIC_SPREAD,
        EXECUTION_MODEL_ZERO_SPREAD_LEGACY,
    }:
        raise ValueError(f"unsupported backtest execution model: {model}")
    fill_policy = str(cfg.get("fill_policy") or DEFAULT_FILL_POLICY)
    if fill_policy not in {"mid", "bid", "ask"}:
        raise ValueError(f"unsupported backtest fill policy: {fill_policy}")
    return BacktestExecutionModel(
        model=model,
        scan_timing=str(cfg.get("scan_timing") or DEFAULT_SCAN_TIMING),
        fill_policy=fill_policy,
        reference_price_source=str(
            cfg.get("reference_price_source") or DEFAULT_REFERENCE_PRICE_SOURCE
        ),
        calibration_status=str(
            cfg.get("calibration_status") or DEFAULT_CALIBRATION_STATUS
        ),
        reference_price_adjustment_pct=max(0.0, float(slippage_pct or 0.0)),
        fill_price_penalty_dollars=max(
            0.0, float(cfg.get("fill_price_penalty_dollars", 0.0))
        ),
        fill_price_penalty_pct_of_mid=max(
            0.0, float(cfg.get("fill_price_penalty_pct_of_mid", 0.0))
        ),
        synthetic_spread=SyntheticSpreadConfig(
            min_spread_pct_of_mid=float(
                spread_cfg.get("min_spread_pct_of_mid", 0.08)
            ),
            min_spread_dollars=float(spread_cfg.get("min_spread_dollars", 0.0)),
            max_spread_pct_of_mid=float(
                spread_cfg.get("max_spread_pct_of_mid", 0.40)
            ),
            low_premium_threshold=float(
                spread_cfg.get("low_premium_threshold", 0.50)
            ),
            low_premium_extra_pct=float(
                spread_cfg.get("low_premium_extra_pct", 0.05)
            ),
            low_volume_threshold=int(spread_cfg.get("low_volume_threshold", 50)),
            low_volume_extra_pct=float(spread_cfg.get("low_volume_extra_pct", 0.05)),
            wide_otm_pct_threshold=float(
                spread_cfg.get("wide_otm_pct_threshold", 0.15)
            ),
            wide_otm_extra_pct=float(spread_cfg.get("wide_otm_extra_pct", 0.03)),
        ),
    )


def modeled_quote_from_reference(
    *,
    reference_price: float,
    option_type: str,
    strike: float,
    stock_price: float | None,
    volume: int | None,
    execution_model: BacktestExecutionModel | None,
) -> tuple[float, float, float]:
    model = execution_model or BacktestExecutionModel(
        model=EXECUTION_MODEL_ZERO_SPREAD_LEGACY
    )
    adjusted_mid = reference_price * max(0.0, 1.0 - model.reference_price_adjustment_pct)
    if adjusted_mid <= 0:
        return 0.0, 0.0, 0.0
    if model.model == EXECUTION_MODEL_ZERO_SPREAD_LEGACY:
        return adjusted_mid, adjusted_mid, 0.0

    spread_pct = synthetic_spread_pct(
        reference_price=adjusted_mid,
        option_type=option_type,
        strike=strike,
        stock_price=stock_price,
        volume=volume,
        config=model.synthetic_spread,
    )
    half_spread = adjusted_mid * spread_pct / 2.0
    bid = max(0.0, adjusted_mid - half_spread)
    ask = adjusted_mid + half_spread
    return bid, ask, spread_pct


def synthetic_spread_pct(
    *,
    reference_price: float,
    option_type: str,
    strike: float,
    stock_price: float | None,
    volume: int | None,
    config: SyntheticSpreadConfig,
) -> float:
    fixed_floor_pct = (
        max(0.0, config.min_spread_dollars) / reference_price
        if reference_price > 0
        else 0.0
    )
    spread = max(0.0, config.min_spread_pct_of_mid, fixed_floor_pct)
    if reference_price <= config.low_premium_threshold:
        spread += max(0.0, config.low_premium_extra_pct)
    if volume is not None and volume < config.low_volume_threshold:
        spread += max(0.0, config.low_volume_extra_pct)
    if _otm_pct(option_type, strike, stock_price) >= config.wide_otm_pct_threshold:
        spread += max(0.0, config.wide_otm_extra_pct)
    cap = max(config.min_spread_pct_of_mid, fixed_floor_pct, config.max_spread_pct_of_mid)
    return min(spread, cap)


def entry_fill_price(
    option: OptionQuote,
    execution_model: BacktestExecutionModel,
    *,
    side: str = "sell",
) -> float | None:
    policy = execution_model.fill_policy
    fill = None
    if policy == "mid":
        fill = option.executable_mid
    elif side == "sell":
        if policy == "bid":
            fill = option.bid if option.bid > 0 else None
        elif policy == "ask":
            fill = option.ask if option.ask > 0 else None
    else:
        if policy == "bid":
            fill = option.ask if option.ask > 0 else None
        elif policy == "ask":
            fill = option.bid if option.bid > 0 else None
    if fill is None:
        return None
    return _apply_fill_penalty(option, execution_model, fill=fill, side=side)


def _apply_fill_penalty(
    option: OptionQuote,
    execution_model: BacktestExecutionModel,
    *,
    fill: float,
    side: str,
) -> float | None:
    mid = option.executable_mid
    pct_penalty = (
        max(0.0, execution_model.fill_price_penalty_pct_of_mid) * mid
        if mid is not None and mid > 0
        else 0.0
    )
    penalty = max(0.0, execution_model.fill_price_penalty_dollars) + pct_penalty
    if penalty <= 0:
        return fill
    if side == "sell":
        adjusted = fill - penalty
        return adjusted if adjusted > 0 else None
    return fill + penalty


def entry_fill_diagnostics(
    option: OptionQuote,
    execution_model: BacktestExecutionModel,
    *,
    side: str = "sell",
) -> dict[str, Any]:
    fill = entry_fill_price(option, execution_model, side=side)
    market_mid = option.executable_mid
    discount = None
    if fill is not None and market_mid is not None and market_mid > 0:
        discount = (market_mid - fill) / market_mid
    return {
        "execution_model": execution_model.model,
        "fill_policy": execution_model.fill_policy,
        "reference_price_source": execution_model.reference_price_source,
        "calibration_status": execution_model.calibration_status,
        "market_bid": option.bid,
        "market_ask": option.ask,
        "market_mid": market_mid,
        "spread_pct_of_mid": option.spread_pct_of_mid,
        "fill_price": fill,
        "fill_discount_pct_of_mid": discount,
    }


def _otm_pct(option_type: str, strike: float, stock_price: float | None) -> float:
    if stock_price is None or stock_price <= 0:
        return 0.0
    if option_type == "put":
        return max(0.0, (stock_price - strike) / stock_price)
    if option_type == "call":
        return max(0.0, (strike - stock_price) / stock_price)
    return 0.0
