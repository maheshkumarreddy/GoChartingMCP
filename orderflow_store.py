"""
Order Flow State Store for GoCharting 1-Minute Footprint Data.
Handles dynamic tick calculations, level-by-level delta profiles,
CVD accumulation, POC detection, and pattern analysis.
"""

from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
import datetime


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
    total_volume: int
    total_buy_vol: int
    total_sell_vol: int
    net_delta: int
    min_delta: int
    max_delta: int
    poc_price: float
    cvd: int = 0
    tick_deltas: List[TickDeltaLevel] = field(default_factory=list)
    signals: Dict[str, Any] = field(default_factory=dict)


class MultiChartOrderFlowStore:
    def __init__(self, max_candles_per_symbol: int = 400):
        self.max_candles = max_candles_per_symbol
        # symbol -> list of CandleOrderFlow (newest at index 0 or chronological)
        self.candles: Dict[str, List[CandleOrderFlow]] = {}
        # symbol -> metadata (tick_size, precision, display_tick_size)
        self.symbol_meta: Dict[str, Dict[str, Any]] = {}
        # symbol -> running CVD
        self.running_cvd: Dict[str, int] = {}
        # Pending UI command for browser bridge (e.g. switch symbol)
        self.pending_command: Optional[Dict[str, Any]] = None

    def update_metadata(self, symbol: str, tick_size: float = 0.05,
                        display_tick_size: float = 1.0, precision: int = 2, price_divisor: float = 10.0):
        self.symbol_meta[symbol] = {
            "tick_size": tick_size,
            "display_tick_size": display_tick_size,
            "precision": precision,
            "price_divisor": price_divisor
        }

    def process_raw_candle(self, payload: Dict[str, Any]) -> CandleOrderFlow:
        """
        Parses a raw GoCharting 1-minute footprint payload into a clean CandleOrderFlow
        with dynamic tick delta resolution.
        """
        symbol = payload.get("symbol", "NIFTY_FUT")
        timeframe = payload.get("timeframe", "1m")
        date_str = payload.get("date", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
        # Meta resolution
        meta = self.symbol_meta.get(symbol, {
            "tick_size": 0.05,
            "display_tick_size": 1.0,
            "precision": 2
        })
        disp_tick = meta["display_tick_size"]
        precision = meta["precision"]

        summary = payload.get("ending_summary", {}) or payload.get("summary", {})
        high_raw = summary.get("high", 0)
        low_raw = summary.get("low", 0)

        # Scale divisor for integer levels
        scale_div = float(meta.get("price_divisor", 10.0 if precision == 1 else 100.0))
        
        # High and Low from GoCharting ending_summary are already floats (e.g. 25016.3)
        high = float(high_raw) if high_raw < 50000 else round(high_raw / scale_div, precision)
        low = float(low_raw) if low_raw < 50000 else round(low_raw / scale_div, precision)
        
        # Process footprint levels
        raw_footprint = payload.get("footprint", [])
        tick_levels: List[TickDeltaLevel] = []
        max_vol_at_level = -1
        poc_price = 0.0

        for row in raw_footprint:
            # Handle both dict format {"level", "buy", "sell"} and list format [level, [b_trades, b_vol], [s_trades, s_vol]]
            if isinstance(row, list) and len(row) >= 3:
                level_idx = row[0]
                buy_cluster = {"trades": row[1][0], "volume": row[1][1]} if len(row[1]) >= 2 else {}
                sell_cluster = {"trades": row[2][0], "volume": row[2][1]} if len(row[2]) >= 2 else {}
            elif isinstance(row, dict):
                level_idx = row.get("level", 0)
                buy_cluster = row.get("buy", {})
                sell_cluster = row.get("sell", {})
            else:
                continue

            # Dynamic price mapping
            if isinstance(row, dict) and "price" in row:
                price = float(row["price"])
            elif level_idx > 10000:
                price = round(level_idx / scale_div, precision)
            else:
                price = round(level_idx * disp_tick, precision)
            buy_vol = int(buy_cluster.get("volume", 0))
            sell_vol = int(sell_cluster.get("volume", 0))
            trades = int(buy_cluster.get("trades", 0)) + int(sell_cluster.get("trades", 0))
            
            # Tick Delta = Buy Vol - Sell Vol
            delta = buy_vol - sell_vol
            vol = buy_vol + sell_vol

            if vol > max_vol_at_level:
                max_vol_at_level = vol
                poc_price = price

            tick_levels.append(TickDeltaLevel(
                price=price,
                delta=delta,
                volume=vol,
                trades=trades,
                buy_vol=buy_vol,
                sell_vol=sell_vol
            ))

        # Sort price ascending
        tick_levels.sort(key=lambda x: x.price)

        # Mark POC
        for lvl in tick_levels:
            if abs(lvl.price - poc_price) < 1e-4:
                lvl.is_poc = True

        # Check diagonal imbalances (e.g. 300% ratio)
        for i in range(len(tick_levels) - 1):
            cur = tick_levels[i]
            nxt = tick_levels[i+1]
            # Diagonal buy imbalance: nxt.buy_vol vs cur.sell_vol
            if cur.sell_vol > 0 and (nxt.buy_vol / cur.sell_vol) >= 3.0 and nxt.buy_vol >= 50:
                nxt.is_buy_imbalance = True
            # Diagonal sell imbalance: cur.sell_vol vs nxt.buy_vol
            if nxt.buy_vol > 0 and (cur.sell_vol / nxt.buy_vol) >= 3.0 and cur.sell_vol >= 50:
                cur.is_sell_imbalance = True

        total_buy = summary.get("total_buy", sum(x.buy_vol for x in tick_levels))
        total_sell = summary.get("total_sell", sum(x.sell_vol for x in tick_levels))
        net_delta = summary.get("close_delta", total_buy - total_sell)
        min_delta = summary.get("min_delta", net_delta)
        max_delta = summary.get("max_delta", net_delta)
        total_vol = total_buy + total_sell

        # Running CVD
        prior_cvd = self.running_cvd.get(symbol, 0)
        current_cvd = prior_cvd + net_delta
        self.running_cvd[symbol] = current_cvd

        # Close price approximation from payload or POC
        close_price = payload.get("close", poc_price)
        open_price = payload.get("open", poc_price)

        # Signal detections
        signals = {
            "stacked_buy_imbalance": [lvl.price for lvl in tick_levels if lvl.is_buy_imbalance],
            "stacked_sell_imbalance": [lvl.price for lvl in tick_levels if lvl.is_sell_imbalance],
            "delta_divergence": (close_price > open_price and net_delta < 0) or (close_price < open_price and net_delta > 0),
            "absorption_at_low": (net_delta < 0 and close_price > low + (high - low) * 0.4),
            "absorption_at_high": (net_delta > 0 and close_price < high - (high - low) * 0.4)
        }

        candle = CandleOrderFlow(
            timestamp=date_str,
            symbol=symbol,
            timeframe=timeframe,
            open_price=open_price,
            high_price=high if high > 0 else (tick_levels[-1].price if tick_levels else close_price),
            low_price=low if low > 0 else (tick_levels[0].price if tick_levels else close_price),
            close_price=close_price,
            total_volume=total_vol,
            total_buy_vol=total_buy,
            total_sell_vol=total_sell,
            net_delta=net_delta,
            min_delta=min_delta,
            max_delta=max_delta,
            poc_price=poc_price,
            cvd=current_cvd,
            tick_deltas=tick_levels,
            signals=signals
        )

        if symbol not in self.candles:
            self.candles[symbol] = []
        
        # Insert at front (newest first)
        self.candles[symbol].insert(0, candle)
        if len(self.candles[symbol]) > self.max_candles:
            self.candles[symbol].pop()

        return candle

    def sync_from_browser_series(self, payload: Dict[str, Any]):
        """
        Syncs live footprint data extracted directly from GoCharting's canvas fullCluster & mainChartData.
        """
        raw_symbol = payload.get("symbol", "NSE:NIFTY-I")
        symbol = "NIFTY_FUT" if "NIFTY" in raw_symbol else ("BANKNIFTY_FUT" if "BANKNIFTY" in raw_symbol else raw_symbol)
        
        candles = payload.get("candles", [])
        if not candles:
            return

        self.update_metadata(symbol, tick_size=0.05, display_tick_size=1.0, precision=2)

        for c in candles:
            timestamp = c.get("date_str") or c.get("timestamp")
            open_p = float(c.get("open", 0))
            high_p = float(c.get("high", 0))
            low_p = float(c.get("low", 0))
            close_p = float(c.get("close", 0))
            volume = int(c.get("volume", 0))
            poc_p = float(c.get("poc_price", 0))
            
            raw_levels = c.get("levels", [])
            tick_levels: List[TickDeltaLevel] = []
            
            total_buy = sum(lvl.get("buy_vol", 0) for lvl in raw_levels)
            total_sell = sum(lvl.get("sell_vol", 0) for lvl in raw_levels)
            net_delta = total_buy - total_sell
            
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
            
            # Sort ascending
            tick_levels.sort(key=lambda x: x.price)
            
            # Imbalances
            for i in range(len(tick_levels) - 1):
                cur = tick_levels[i]
                nxt = tick_levels[i+1]
                if cur.sell_vol > 0 and (nxt.buy_vol / cur.sell_vol) >= 3.0 and nxt.buy_vol >= 50:
                    nxt.is_buy_imbalance = True
                if nxt.buy_vol > 0 and (cur.sell_vol / nxt.buy_vol) >= 3.0 and cur.sell_vol >= 50:
                    cur.is_sell_imbalance = True

            # Running CVD
            prior_cvd = self.running_cvd.get(symbol, 0)
            current_cvd = prior_cvd + net_delta
            self.running_cvd[symbol] = current_cvd

            signals = {
                "stacked_buy_imbalance": [lvl.price for lvl in tick_levels if lvl.is_buy_imbalance],
                "stacked_sell_imbalance": [lvl.price for lvl in tick_levels if lvl.is_sell_imbalance],
                "delta_divergence": (close_p > open_p and net_delta < 0) or (close_p < open_p and net_delta > 0),
                "absorption_at_low": (net_delta < 0 and close_p > low_p + (high_p - low_p) * 0.4),
                "absorption_at_high": (net_delta > 0 and close_p < high_p - (high_p - low_p) * 0.4)
            }

            candle_obj = CandleOrderFlow(
                timestamp=timestamp,
                symbol=symbol,
                timeframe="1m",
                open_price=open_p,
                high_price=high_p,
                low_price=low_p,
                close_price=close_p,
                total_volume=volume,
                total_buy_vol=total_buy,
                total_sell_vol=total_sell,
                net_delta=net_delta,
                min_delta=net_delta,
                max_delta=net_delta,
                poc_price=poc_p,
                cvd=current_cvd,
                tick_deltas=tick_levels,
                signals=signals
            )

            if symbol not in self.candles:
                self.candles[symbol] = []
            
            # Check if this candle already exists (by timestamp)
            existing_idx = next((idx for idx, c in enumerate(self.candles[symbol]) if c.timestamp == timestamp), None)
            if existing_idx is not None:
                self.candles[symbol][existing_idx] = candle_obj
            else:
                self.candles[symbol].insert(0, candle_obj)

            if len(self.candles[symbol]) > self.max_candles:
                self.candles[symbol].pop()

    def get_latest_candle(self, symbol: str) -> Optional[CandleOrderFlow]:
        chart = self.candles.get(symbol, [])
        return chart[0] if chart else None

    def get_history(self, symbol: str, limit: int = 10) -> List[CandleOrderFlow]:
        return self.candles.get(symbol, [])[:limit]

    def list_active_symbols(self) -> List[Dict[str, Any]]:
        result = []
        for sym, candles in self.candles.items():
            if candles:
                latest = candles[0]
                result.append({
                    "symbol": sym,
                    "candles_count": len(candles),
                    "latest_time": latest.timestamp,
                    "latest_close": latest.close_price,
                    "latest_delta": latest.net_delta,
                    "cvd": latest.cvd
                })
        return result

    def set_pending_switch_command(self, symbol: str, segment: str = "FUT", timeframe: str = "1m"):
        self.pending_command = {
            "action": "switch_chart",
            "symbol": symbol,
            "segment": segment,
            "timeframe": timeframe,
            "timestamp": datetime.datetime.now().isoformat()
        }

    def pop_pending_command(self) -> Optional[Dict[str, Any]]:
        cmd = self.pending_command
        self.pending_command = None
        return cmd


# Global shared instance
store = MultiChartOrderFlowStore()

