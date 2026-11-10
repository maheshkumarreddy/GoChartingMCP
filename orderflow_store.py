"""
Order Flow State Store for GoCharting 1-Minute Footprint Data.
Handles dynamic tick calculations, level-by-level delta profiles,
deterministic CVD accumulation, POC detection, and institutional positioning quadrants.
"""

from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
import datetime
from datetime import timezone, timedelta

# IST timezone: UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class TickDeltaLevel:
    price: float
    delta: int
    volume: int
    trades: int
    buy_vol: int
    sell_vol: int
    is_poc: bool = False
    is_buy_imbalance: bool = False
    is_sell_imbalance: bool = False


@dataclass
class CandleOrderFlow:
    timestamp: str
    symbol: str
    timeframe: str
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    prev_close_price: float
    price_change: float
    total_volume: int
    total_buy_vol: int
    total_sell_vol: int
    net_delta: int
    min_delta: int
    max_delta: int
    poc_price: float
    cvd: int = 0
    open_interest: int = 0
    oi_change: int = 0
    oi_day_change: int = 0
    positioning_quadrant: str = "NEUTRAL"
    is_post_close: bool = False
    tick_deltas: List[TickDeltaLevel] = field(default_factory=list)
    signals: Dict[str, Any] = field(default_factory=dict)


def get_market_session_info(latest_candle_time_str: Optional[str] = None) -> Dict[str, Any]:
    """
    Computes market session state and data staleness based on Indian Standard Time (IST).
    NSE Trading Hours:
      09:00 - 09:08: Pre-open
      09:15 - 15:30: Regular Trading Session
      15:30 - 15:40: Post-market settlement / closing auction
      15:40 onwards & Weekends: Closed
    """
    now_ist = datetime.datetime.now(IST)
    weekday = now_ist.weekday()  # 0 = Monday, 6 = Sunday
    is_weekend = weekday >= 5

    current_time_minutes = now_ist.hour * 60 + now_ist.minute

    if is_weekend:
        session_state = "CLOSED"
    elif current_time_minutes < (9 * 60 + 15):
        session_state = "PRE_OPEN" if current_time_minutes >= (9 * 60) else "CLOSED"
    elif current_time_minutes <= (15 * 60 + 30):
        session_state = "OPEN"
    elif current_time_minutes <= (15 * 60 + 40):
        session_state = "POST_MARKET"
    else:
        session_state = "CLOSED"

    data_age_seconds: Optional[int] = None
    is_live = False
    warning = None

    if latest_candle_time_str:
        try:
            clean_str = latest_candle_time_str.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(clean_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            data_age_seconds = int((now_ist - dt).total_seconds())
            if data_age_seconds < 0:
                data_age_seconds = 0
            is_live = (data_age_seconds <= 180) and (session_state in ["OPEN", "POST_MARKET"])
            if data_age_seconds > 300:
                hours = round(data_age_seconds / 3600, 1)
                warning = f"Data is from previous market session ({hours} hours old). Market is currently {session_state}."
        except Exception:
            pass

    return {
        "market_session_state": session_state,
        "is_live": is_live,
        "data_age_seconds": data_age_seconds,
        "current_time_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "staleness_warning": warning
    }


def classify_positioning_quadrant(price_change: float, net_delta: int, oi_change: int) -> str:
    """
    Strict 3-variable classification of institutional positioning:
    Price Change (Close - Prev Close), Net Delta (Aggressive Buy - Sell), and Open Interest Change.

    Canonical Quadrants:
    - LONG_BUILDUP: Price >= 0, Delta > 0, OI > 0 (Fresh aggressive institutional buying)
    - SHORT_COVERING: Price >= 0, Delta > 0, OI < 0 (Short sellers covering / closing shorts)
    - SHORT_BUILDUP: Price <= 0, Delta < 0, OI > 0 (Fresh aggressive institutional selling)
    - LONG_UNWINDING: Price <= 0, Delta < 0, OI < 0 (Long holders liquidating inventory)

    Divergence / Absorption Quadrants (Delta opposes Price):
    - ABSORPTION_BUY: Price >= 0, Delta < 0, OI > 0 (Passive limit buying absorbs aggressive selling)
    - SHORT_COVERING_DIV: Price >= 0, Delta < 0, OI < 0 (Passive short covering under sell pressure)
    - ABSORPTION_SELL: Price <= 0, Delta > 0, OI > 0 (Passive limit selling absorbs aggressive buying)
    - LONG_UNWINDING_DIV: Price <= 0, Delta > 0, OI < 0 (Passive long unwinding under buy pressure)

    Neutral States:
    - NEUTRAL_OI: OI Change is 0 (No contracts added or closed)
    - NEUTRAL: Price flat, Delta 0, OI 0
    """
    if price_change == 0 and net_delta == 0 and oi_change == 0:
        return "NEUTRAL"

    if oi_change == 0:
        return "NEUTRAL_OI"

    if price_change > 0:
        if net_delta > 0:
            return "LONG_BUILDUP" if oi_change > 0 else "SHORT_COVERING"
        elif net_delta < 0:
            return "ABSORPTION_BUY" if oi_change > 0 else "SHORT_COVERING_DIV"
        else:
            return "LONG_BUILDUP" if oi_change > 0 else "SHORT_COVERING"
    elif price_change < 0:
        if net_delta < 0:
            return "SHORT_BUILDUP" if oi_change > 0 else "LONG_UNWINDING"
        elif net_delta > 0:
            return "ABSORPTION_SELL" if oi_change > 0 else "LONG_UNWINDING_DIV"
        else:
            return "SHORT_BUILDUP" if oi_change > 0 else "LONG_UNWINDING"
    else:  # Flat price
        if net_delta > 0:
            return "LONG_BUILDUP" if oi_change > 0 else "SHORT_COVERING"
        elif net_delta < 0:
            return "SHORT_BUILDUP" if oi_change > 0 else "LONG_UNWINDING"
        return "NEUTRAL"


class MultiChartOrderFlowStore:
    def __init__(self, max_candles_per_symbol: int = 500):
        self.max_candles = max_candles_per_symbol
        # symbol -> list of CandleOrderFlow (newest at index 0)
        self.candles: Dict[str, List[CandleOrderFlow]] = {}
        # symbol -> metadata
        self.symbol_meta: Dict[str, Dict[str, Any]] = {}
        # symbol -> running CVD (final cumulative session delta)
        self.running_cvd: Dict[str, int] = {}
        # Pending UI command for browser bridge
        self.pending_command: Optional[Dict[str, Any]] = None

    def update_metadata(self, symbol: str, tick_size: float = 0.05,
                        display_tick_size: float = 1.0, precision: int = 2, price_divisor: float = 10.0):
        self.symbol_meta[symbol] = {
            "tick_size": tick_size,
            "display_tick_size": display_tick_size,
            "precision": precision,
            "price_divisor": price_divisor
        }

    def sync_from_browser_series(self, payload: Dict[str, Any]):
        """
        Syncs live footprint data extracted directly from GoCharting.
        CRITICAL FIX: CVD is computed deterministically starting from the first candle of the session,
        preventing accumulator drift across repeated sync cycles.
        """
        raw_symbol = payload.get("symbol", "NSE:NIFTY-I")
        symbol = "NIFTY_FUT" if "NIFTY" in raw_symbol else ("BANKNIFTY_FUT" if "BANKNIFTY" in raw_symbol else raw_symbol)
        
        raw_candles = payload.get("candles", [])
        if not raw_candles:
            return

        self.update_metadata(symbol, tick_size=0.05, display_tick_size=1.0, precision=2)

        # Sort incoming candles chronologically ascending (oldest first) to ensure deterministic CVD
        def parse_ts(c_item):
            t_str = c_item.get("timestamp") or c_item.get("date_str", "")
            return t_str

        sorted_raw = sorted(raw_candles, key=parse_ts)

        processed_chronological: List[CandleOrderFlow] = []
        running_cvd_accumulator = 0
        prev_close = 0.0

        for idx, c in enumerate(sorted_raw):
            timestamp = c.get("date_str") or c.get("timestamp")
            open_p = float(c.get("open", 0))
            high_p = float(c.get("high", 0))
            low_p = float(c.get("low", 0))
            close_p = float(c.get("close", 0))
            volume = int(c.get("volume", 0))
            poc_p = float(c.get("poc_price", 0))

            if idx == 0:
                prev_close = open_p
            price_change = round(close_p - prev_close, 2)

            raw_levels = c.get("levels", [])
            tick_levels: List[TickDeltaLevel] = []

            total_buy = int(c.get("total_buy_vol", sum(lvl.get("buy_vol", 0) for lvl in raw_levels)))
            total_sell = int(c.get("total_sell_vol", sum(lvl.get("sell_vol", 0) for lvl in raw_levels)))
            net_delta = int(c.get("net_delta", total_buy - total_sell))

            # Min / Max intra-candle delta (real from GoCharting candleLevelTotals)
            min_delta = int(c.get("min_delta", net_delta))
            max_delta = int(c.get("max_delta", net_delta))

            for lvl in raw_levels:
                price = float(lvl.get("price", 0))
                b_vol = int(lvl.get("buy_vol", 0))
                s_vol = int(lvl.get("sell_vol", 0))
                d = b_vol - s_vol
                v = b_vol + s_vol
                is_poc = abs(price - poc_p) < 1e-4

                tick_levels.append(TickDeltaLevel(
                    price=price,
                    delta=d,
                    volume=v,
                    trades=int(lvl.get("buy_trades", 0)) + int(lvl.get("sell_trades", 0)),
                    buy_vol=b_vol,
                    sell_vol=s_vol,
                    is_poc=is_poc
                ))

            # Sort ascending by price
            tick_levels.sort(key=lambda x: x.price)

            # Diagonal imbalances (300% ratio)
            for i in range(len(tick_levels) - 1):
                cur = tick_levels[i]
                nxt = tick_levels[i+1]
                if cur.sell_vol > 0 and (nxt.buy_vol / cur.sell_vol) >= 3.0 and nxt.buy_vol >= 50:
                    nxt.is_buy_imbalance = True
                if nxt.buy_vol > 0 and (cur.sell_vol / nxt.buy_vol) >= 3.0 and cur.sell_vol >= 50:
                    cur.is_sell_imbalance = True

            # Deterministic CVD accumulation from session origin
            running_cvd_accumulator += net_delta
            current_candle_cvd = running_cvd_accumulator

            signals = {
                "stacked_buy_imbalance": [lvl.price for lvl in tick_levels if lvl.is_buy_imbalance],
                "stacked_sell_imbalance": [lvl.price for lvl in tick_levels if lvl.is_sell_imbalance],
                "delta_divergence": (close_p > open_p and net_delta < 0) or (close_p < open_p and net_delta > 0),
                "absorption_at_low": (net_delta < 0 and close_p > low_p + (high_p - low_p) * 0.4),
                "absorption_at_high": (net_delta > 0 and close_p < high_p - (high_p - low_p) * 0.4)
            }

            # OI data from candle
            oi_val = int(c.get("open_interest", 0))
            oi_chg = int(c.get("oi_change", 0))
            oi_day_chg = int(c.get("oi_day_change", 0))

            quadrant = classify_positioning_quadrant(price_change, net_delta, oi_chg)

            # Check if post-market closing auction (15:31 to 15:40 IST)
            is_post = False
            if "15:3" in timestamp or "15:40" in timestamp:
                is_post = True

            candle_obj = CandleOrderFlow(
                timestamp=timestamp,
                symbol=symbol,
                timeframe="1m",
                open_price=open_p,
                high_price=high_p,
                low_price=low_p,
                close_price=close_p,
                prev_close_price=prev_close,
                price_change=price_change,
                total_volume=volume,
                total_buy_vol=total_buy,
                total_sell_vol=total_sell,
                net_delta=net_delta,
                min_delta=min_delta,
                max_delta=max_delta,
                poc_price=poc_p,
                cvd=current_candle_cvd,
                open_interest=oi_val,
                oi_change=oi_chg,
                oi_day_change=oi_day_chg,
                positioning_quadrant=quadrant,
                is_post_close=is_post,
                tick_deltas=tick_levels,
                signals=signals
            )
            processed_chronological.append(candle_obj)
            prev_close = close_p

        # Update final running CVD for symbol
        self.running_cvd[symbol] = running_cvd_accumulator

        # Store in self.candles sorted descending (newest at index 0)
        processed_descending = list(reversed(processed_chronological))
        self.candles[symbol] = processed_descending[:self.max_candles]

    def get_latest_candle(self, symbol: str) -> Optional[CandleOrderFlow]:
        chart = self.candles.get(symbol, [])
        return chart[0] if chart else None

    def get_history(self, symbol: str, limit: int = 10) -> List[CandleOrderFlow]:
        if limit <= 0:
            return []
        return self.candles.get(symbol, [])[:limit]

    def get_oi_analysis(self, symbol: str, lookback: int = 10) -> Dict[str, Any]:
        """
        Analyzes Open Interest changes alongside Net Delta to categorize
        institutional order flow into canonical positioning quadrants:
        - LONG_BUILDUP, SHORT_COVERING, SHORT_BUILDUP, LONG_UNWINDING,
        - ABSORPTION_BUY, ABSORPTION_SELL, SHORT_COVERING_DIV, LONG_UNWINDING_DIV, NEUTRAL_OI.
        """
        if lookback <= 0:
            return {
                "error": f"Invalid lookback value '{lookback}'. Lookback must be a positive integer >= 1.",
                "requested_lookback": lookback
            }

        all_candles = self.candles.get(symbol, [])
        if not all_candles:
            return {"error": f"No candles available for {symbol}."}

        candles = all_candles[:lookback]
        is_clamped = len(all_candles) < lookback

        timeline = []
        quadrant_counts = {
            "LONG_BUILDUP": 0,
            "SHORT_COVERING": 0,
            "SHORT_BUILDUP": 0,
            "LONG_UNWINDING": 0,
            "ABSORPTION_BUY": 0,
            "ABSORPTION_SELL": 0,
            "SHORT_COVERING_DIV": 0,
            "LONG_UNWINDING_DIV": 0,
            "NEUTRAL_OI": 0,
            "NEUTRAL": 0
        }
        total_delta = 0
        total_oi_change = 0

        for c in reversed(candles):
            total_delta += c.net_delta
            total_oi_change += c.oi_change
            q = c.positioning_quadrant
            quadrant_counts[q] = quadrant_counts.get(q, 0) + 1

            timeline.append({
                "timestamp": c.timestamp,
                "close": c.close_price,
                "price_change": c.price_change,
                "delta": c.net_delta,
                "min_delta": c.min_delta,
                "max_delta": c.max_delta,
                "open_interest": c.open_interest,
                "oi_change": c.oi_change,
                "oi_day_change": c.oi_day_change,
                "quadrant": q,
                "is_post_close": c.is_post_close
            })

        latest = candles[0]
        first = candles[-1]
        price_change = round(latest.close_price - first.prev_close_price, 2)

        # Primary positioning theme
        dominant_quadrant = max(
            quadrant_counts.keys(),
            key=lambda k: quadrant_counts.get(k, 0)
        )
        dominant_count = quadrant_counts.get(dominant_quadrant, 0)
        dominant_pct = round((dominant_count / len(candles)) * 100, 1)

        summary_explanation = {
            "LONG_BUILDUP": "Aggressive fresh buyers entering new long positions (Institutional Bullish continuation).",
            "SHORT_COVERING": "Short sellers forced to buy back/liquidate (Short squeeze / exhaustion rally).",
            "SHORT_BUILDUP": "Aggressive institutional sellers initiating fresh short positions (Institutional Bearish continuation).",
            "LONG_UNWINDING": "Long holders liquidating/stop-losses triggering (Long liquidation cascade).",
            "ABSORPTION_BUY": "Passive institutional buyers absorbing aggressive market sell orders (Potential bullish bottoming).",
            "ABSORPTION_SELL": "Passive institutional sellers absorbing aggressive market buy orders (Potential bearish topping).",
            "SHORT_COVERING_DIV": "Passive short covering absorbing market selling.",
            "LONG_UNWINDING_DIV": "Passive long liquidation into market buying.",
            "NEUTRAL_OI": "Zero change in open interest contracts across these candles."
        }.get(dominant_quadrant, "Mixed positioning.")

        session_info = get_market_session_info(latest.timestamp)

        return {
            "symbol": symbol,
            "requested_lookback": lookback,
            "actual_lookback": len(candles),
            "is_clamped": is_clamped,
            "latest_oi": latest.open_interest,
            "net_oi_change_lookback": total_oi_change,
            "net_delta_lookback": total_delta,
            "price_change_lookback": price_change,
            "dominant_positioning": f"{dominant_quadrant} ({dominant_pct}% of candles)",
            "interpretation": summary_explanation,
            "quadrant_distribution": {k: v for k, v in quadrant_counts.items() if v > 0},
            "market_session_state": session_info["market_session_state"],
            "is_live": session_info["is_live"],
            "data_age_seconds": session_info["data_age_seconds"],
            "staleness_warning": session_info["staleness_warning"],
            "candle_timeline": timeline
        }

    def list_active_symbols(self) -> List[Dict[str, Any]]:
        result = []
        for sym, candles in self.candles.items():
            if candles:
                latest = candles[0]
                session_info = get_market_session_info(latest.timestamp)
                result.append({
                    "symbol": sym,
                    "candles_count": len(candles),
                    "latest_time": latest.timestamp,
                    "latest_close": latest.close_price,
                    "latest_delta": latest.net_delta,
                    "min_delta": latest.min_delta,
                    "max_delta": latest.max_delta,
                    "cvd": latest.cvd,
                    "is_live": session_info["is_live"],
                    "data_age_seconds": session_info["data_age_seconds"],
                    "market_session_state": session_info["market_session_state"]
                })
        return result

    def set_pending_switch_command(self, symbol: str, segment: str = "FUT", timeframe: str = "1m"):
        self.pending_command = {
            "action": "switch_chart",
            "symbol": symbol,
            "segment": segment,
            "timeframe": timeframe,
            "timestamp": datetime.datetime.now(IST).isoformat()
        }

    def pop_pending_command(self) -> Optional[Dict[str, Any]]:
        cmd = self.pending_command
        self.pending_command = None
        return cmd


# Global shared instance
store = MultiChartOrderFlowStore()
