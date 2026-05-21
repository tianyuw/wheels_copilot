from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class PriceBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class PivotLow:
    index: int
    date: date
    price: float


@dataclass
class SupportZone:
    method: str
    center: float
    bottom: float
    top: float
    touches: int = 0
    rejections: int = 0
    last_touch_date: date | None = None
    broken_recently: bool = False
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def distance_below(self, price: float) -> float:
        return max(0.0, price - self.top)


@dataclass
class TrendCheck:
    passed: bool
    current_price: float
    sma200: float | None
    sma200_slope: float | None
    reasons: list[str] = field(default_factory=list)


@dataclass
class SupportAnalysis:
    trend: TrendCheck
    zones: list[SupportZone]
    selected_zone: SupportZone | None
    atr14: float | None
    current_price: float
    min_score_to_trade: float
    reasons: list[str] = field(default_factory=list)

    @property
    def tradable(self) -> bool:
        return (
            self.trend.passed
            and self.selected_zone is not None
            and self.selected_zone.score >= self.min_score_to_trade
        )


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    expiration: date
    dte: int
    strike: float
    bid: float
    ask: float
    last: float
    implied_volatility: float | None = None
    open_interest: int | None = None
    volume: int | None = None
    delta: float | None = None
    quote_timestamp: datetime | None = None
    trade_timestamp: datetime | None = None
    data_feed: str | None = None

    @property
    def mid(self) -> float:
        executable = self.executable_mid
        if executable is not None:
            return executable
        return self.last

    @property
    def executable_mid(self) -> float | None:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return None

    @property
    def spread_pct_of_mid(self) -> float | None:
        mid = self.executable_mid
        if mid is None or mid <= 0 or self.ask <= 0 or self.bid <= 0:
            return None
        return (self.ask - self.bid) / mid


@dataclass
class FundamentalSnapshot:
    ticker: str
    quote_type: str | None = None
    short_name: str | None = None
    long_name: str | None = None
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    market_cap: float | None = None
    pe_ratio: float | None = None
    dividend_yield: float | None = None
    quarterly_net_income: list[float] = field(default_factory=list)
    annual_net_income: list[float] = field(default_factory=list)
    next_earnings_date: date | None = None
    recent_move_pct: float | None = None

    @property
    def is_etf(self) -> bool:
        if self.quote_type:
            return self.quote_type.strip().upper() == "ETF"
        text = " ".join(
            str(x or "") for x in (self.short_name, self.long_name)
        ).lower()
        return "etf" in text or "exchange traded fund" in text


@dataclass
class GateResult:
    status: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status != "REJECT"

    @property
    def manual_review_required(self) -> bool:
        return self.status == "WARN" or bool(self.warnings)


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    status: str | None = None
    equity: float | None = None
    cash: float | None = None
    buying_power: float | None = None
    options_trading_level: int | None = None
    trading_blocked: bool = False
    account_blocked: bool = False
    account_id: str | None = None
    account_number: str | None = None


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float
    asset_class: str | None = None
    side: str | None = None
    market_value: float | None = None
    cost_basis: float | None = None
    underlying_symbol: str | None = None
    option_type: str | None = None
    expiration: date | None = None
    strike: float | None = None

    @property
    def active_underlying(self) -> str:
        return (self.underlying_symbol or self.symbol).upper()

    @property
    def is_option(self) -> bool:
        return self.underlying_symbol is not None

    @property
    def is_short_put(self) -> bool:
        return self.option_type == "put" and self.qty < 0

    @property
    def assignment_cash_required(self) -> float:
        if not self.is_short_put or self.strike is None:
            return 0.0
        return abs(self.qty) * self.strike * 100.0


@dataclass(frozen=True)
class BrokerOrder:
    id: str
    symbol: str
    side: str | None
    qty: float
    status: str | None = None
    asset_class: str | None = None
    order_class: str | None = None
    position_intent: str | None = None
    limit_price: float | None = None
    submitted_at: str | None = None
    parent_order_id: str | None = None
    underlying_symbol: str | None = None
    option_type: str | None = None
    expiration: date | None = None
    strike: float | None = None

    @property
    def active_underlying(self) -> str:
        return (self.underlying_symbol or self.symbol).upper()

    @property
    def is_sell_put(self) -> bool:
        intent = (self.position_intent or "").lower()
        return (
            self.option_type == "put"
            and (self.side or "").lower() == "sell"
            and self.parent_order_id is None
            and intent not in {"sell_to_close", "stc", "close"}
        )

    @property
    def assignment_cash_required(self) -> float:
        if not self.is_sell_put or self.strike is None:
            return 0.0
        return self.qty * self.strike * 100.0


@dataclass(frozen=True)
class PortfolioSnapshot:
    account: BrokerAccountSnapshot
    positions: list[BrokerPosition] = field(default_factory=list)
    open_orders: list[BrokerOrder] = field(default_factory=list)
    source: str = "unknown"
    fetched_at: str | None = None


@dataclass
class CspCandidate:
    option: OptionQuote
    support_zone: SupportZone
    delta: float
    delta_bucket: str
    auto_trade: bool
    weekly_return_on_strike_pct: float
    assignment_cash_required: float
    reasons: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class RejectedCspOption:
    option: OptionQuote
    reasons: list[str]


@dataclass
class CspSelectionResult:
    candidate: CspCandidate | None
    rejected: list[RejectedCspOption] = field(default_factory=list)
    policy_name: str | None = None
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def rejection_summary(self) -> dict[str, int]:
        summary: dict[str, int] = {}
        for rejected in self.rejected:
            for reason in rejected.reasons:
                summary[reason] = summary.get(reason, 0) + 1
        return dict(sorted(summary.items(), key=lambda item: (-item[1], item[0])))
