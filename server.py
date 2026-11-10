"""
GoCharting Order Flow MCP Server
Exposes 1-minute candle Footprint, Tick Delta ladders, and Order Flow signals
via Model Context Protocol (MCP) with:
  - Streamable HTTP at /mcp  (Antigravity / modern clients)
  - Legacy SSE at /sse        (Claude Desktop / older clients)
"""

import os
import sys
import json
import uvicorn
from typing import Dict, Any, List, Optional
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.middleware.cors import CORSMiddleware

from orderflow_store import store
from browser_controller import controller

# Initialize MCP Server
mcp = MCPServer(name="GoCharting-OrderFlow-MCP")


async def auto_sync_if_needed():
    """Fetches latest footprint data from the dedicated Chrome tab via CDP."""
    try:
        data = await controller.extract_live_footprint_from_chart()
        if data and data.get("status") == "ok":
            store.sync_from_browser_series(data)
    except Exception as e:
        print(f"[AutoSync Warning] {e}")


# ==========================================
# BROWSER AUTOMATION TOOLS (FULL CONTROL)
# ==========================================

@mcp.tool()
async def browser_sync_now() -> Dict[str, Any]:
    """
    Forces an immediate live sync of order flow footprint candles directly
    from the open GoCharting browser tab.
    """
    await auto_sync_if_needed()
    active = store.list_active_symbols()
    return {
        "status": "ok",
        "synced_symbols": [s["symbol"] for s in active],
        "active_charts": active
    }

@mcp.tool()
async def browser_change_symbol(symbol: str) -> Dict[str, Any]:
    """
    Directly changes the chart symbol in the GoCharting browser window
    (e.g., 'NSE:NIFTY-I', 'NSE:BANKNIFTY-I', 'NIFTY 24300 CE').
    """
    return await controller.change_symbol(symbol)


@mcp.tool()
async def browser_change_timeframe(interval: str = "1m") -> Dict[str, Any]:
    """
    Changes the candle interval in the GoCharting browser window
    (e.g., '1m', '3m', '5m', '15m', '30m', '1h', '1D').
    """
    return await controller.change_timeframe(interval)


@mcp.tool()
async def browser_open_tool(tool_name: str) -> Dict[str, Any]:
    """
    Opens an indicator, study, or tool in the GoCharting browser window
    (e.g., 'VWAP', 'CVD', 'Profile', 'RSI').
    """
    return await controller.open_tool_or_study(tool_name)


@mcp.tool()
async def browser_execute_custom_js(javascript_code: str) -> Dict[str, Any]:
    """
    Executes custom JavaScript inside the GoCharting tab to modify any setting,
    footprint cluster configuration, or UI state.
    """
    val = await controller.execute_js(javascript_code)
    return {"status": "ok", "result": val}


@mcp.tool()
async def browser_capture_screenshot() -> Dict[str, Any]:
    """
    Captures a full high-resolution screenshot of the GoCharting chart window
    and saves it to disk for visual analysis.
    """
    return await controller.capture_screenshot()


# ==========================================
# MCP TOOLS FOR MUSE AI & LLM AGENTS
# ==========================================

@mcp.tool()
async def get_latest_candle(symbol: str = "NIFTY_FUT") -> Dict[str, Any]:
    """
    Get the latest 1-minute order flow candle for the specified symbol
    (e.g., 'NIFTY_FUT', 'BANKNIFTY_FUT', or an option strike).
    Returns OHLCV, Net Delta, Min/Max Delta, POC price, CVD, and instant signals.
    """
    await auto_sync_if_needed()
    candle = store.get_latest_candle(symbol)
    if not candle:
        return {
            "error": f"No 1-minute data yet for {symbol}.",
            "available_symbols": [s["symbol"] for s in store.list_active_symbols()],
            "tip": "Ensure GoCharting has the chart open with the browser hook enabled, or call switch_chart."
        }

    return {
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
        "timestamp": candle.timestamp,
        "open": candle.open_price,
        "high": candle.high_price,
        "low": candle.low_price,
        "close": candle.close_price,
        "volume": candle.total_volume,
        "buy_volume": candle.total_buy_vol,
        "sell_volume": candle.total_sell_vol,
        "net_delta": candle.net_delta,
        "min_delta": candle.min_delta,
        "max_delta": candle.max_delta,
        "poc_price": candle.poc_price,
        "cvd": candle.cvd,
        "signals": candle.signals
    }


@mcp.tool()
async def get_delta_ladder(symbol: str = "NIFTY_FUT", candle_index: int = 0) -> Dict[str, Any]:
    """
    Returns the price-by-price Tick Delta matrix for the specified 1-minute candle.
    Each price level lists the net delta (Buy Vol - Sell Vol), total volume, and flags for POC and imbalances.
    candle_index=0 is the current/latest candle; 1 is the previous closed candle.
    """
    await auto_sync_if_needed()
    history = store.get_history(symbol, limit=candle_index + 1)
    if not history or len(history) <= candle_index:
        return {"error": f"Candle at index {candle_index} not found for {symbol}."}

    candle = history[candle_index]
    
    # Format tick deltas compactly for LLM efficiency
    ladder = []
    for lvl in candle.tick_deltas:
        ladder.append({
            "price": lvl.price,
            "delta": lvl.delta,
            "vol": lvl.volume,
            "is_poc": lvl.is_poc,
            "buy_imbalance": lvl.is_buy_imbalance,
            "sell_imbalance": lvl.is_sell_imbalance
        })

    return {
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
        "timestamp": candle.timestamp,
        "close": candle.close_price,
        "net_candle_delta": candle.net_delta,
        "cvd": candle.cvd,
        "poc_price": candle.poc_price,
        "tick_count": len(ladder),
        "tick_delta_ladder": ladder
    }


@mcp.tool()
async def get_orderflow_signals(symbol: str = "NIFTY_FUT", lookback: int = 5) -> Dict[str, Any]:
    """
    Analyzes order flow across the last N 1-minute candles for the symbol.
    Detects Cumulative Volume Delta (CVD) divergence, aggressive absorption,
    and stacked buy/sell imbalances.
    """
    await auto_sync_if_needed()
    candles = store.get_history(symbol, limit=lookback)
    if not candles:
        return {"error": f"No history found for {symbol}."}

    recent_signals = []
    total_delta = 0

    for c in reversed(candles):
        total_delta += c.net_delta
        recent_signals.append({
            "timestamp": c.timestamp,
            "close": c.close_price,
            "delta": c.net_delta,
            "poc": c.poc_price,
            "absorption_low": c.signals.get("absorption_at_low", False),
            "absorption_high": c.signals.get("absorption_at_high", False),
            "delta_divergence": c.signals.get("delta_divergence", False),
            "buy_imbalances": c.signals.get("stacked_buy_imbalance", []),
            "sell_imbalances": c.signals.get("stacked_sell_imbalance", [])
        })

    latest = candles[0]
    first = candles[-1]
    price_change = latest.close_price - first.close_price

    # Market state interpretation
    bias = "NEUTRAL"
    if total_delta > 0 and price_change > 0:
        bias = "BULLISH_CONVERGENCE (Aggressive buying driving prices higher)"
    elif total_delta < 0 and price_change < 0:
        bias = "BEARISH_CONVERGENCE (Aggressive selling driving prices lower)"
    elif total_delta < 0 and price_change > 0:
        bias = "BEARISH_DIVERGENCE (Price rising on negative delta; buyers exhausted or passive absorption)"
    elif total_delta > 0 and price_change < 0:
        bias = "BULLISH_DIVERGENCE (Price falling on positive delta; sellers absorbed)"

    return {
        "symbol": symbol,
        "candles_analyzed": len(candles),
        "bias": bias,
        "net_lookback_delta": total_delta,
        "current_cvd": latest.cvd,
        "candle_breakdowns": recent_signals
    }


@mcp.tool()
def switch_chart(symbol: str, segment: str = "FUT", timeframe: str = "1m") -> Dict[str, Any]:
    """
    Requests the browser bridge to switch the active GoCharting chart to the target
    Futures contract or Option strike (e.g., 'NIFTY 24300 CE', 'BANKNIFTY 52000 PE', 'BANKNIFTY_FUT').
    """
    store.set_pending_switch_command(symbol=symbol, segment=segment, timeframe=timeframe)
    return {
        "status": "COMMAND_QUEUED",
        "message": f"Switch command to {symbol} ({segment}, {timeframe}) sent to browser bridge.",
        "target_symbol": symbol
    }


@mcp.tool()
def list_active_charts() -> Dict[str, Any]:
    """
    Lists all symbols currently streaming and cached in local memory,
    with their latest price, delta, CVD, and candle counts.
    """
    active = store.list_active_symbols()
    return {
        "active_charts_count": len(active),
        "charts": active
    }


# ==========================================
# HTTP ENDPOINTS FOR BROWSER BRIDGE
# ==========================================

@mcp.custom_route("/api/feed", methods=["POST"])
async def receive_feed(request: Request):
    """Receives 1-minute footprint candle JSON from browser hook."""
    try:
        data = await request.json()
        candle = store.process_raw_candle(data)
        return JSONResponse({"status": "ok", "symbol": candle.symbol, "timestamp": candle.timestamp})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@mcp.custom_route("/api/meta", methods=["POST"])
async def receive_meta(request: Request):
    """Receives dynamic tick size and precision metadata from browser hook."""
    try:
        data = await request.json()
        symbol = data.get("symbol", "NIFTY_FUT")
        tick_size = float(data.get("tick_size", 0.05))
        display_tick = float(data.get("display_tick_size", 1.0))
        precision = int(data.get("precision", 2))
        store.update_metadata(symbol, tick_size, display_tick, precision)
        return JSONResponse({"status": "ok", "symbol": symbol})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@mcp.custom_route("/api/command", methods=["GET", "POST"])
async def handle_command(request: Request):
    """Browser hook polls GET; agents can also trigger switch via POST."""
    if request.method == "POST":
        data = await request.json()
        symbol = data.get("symbol", "NIFTY_FUT")
        segment = data.get("segment", "FUT")
        timeframe = data.get("timeframe", "1m")
        store.set_pending_switch_command(symbol=symbol, segment=segment, timeframe=timeframe)
        return JSONResponse({"status": "ok", "queued": symbol})
    
    cmd = store.pop_pending_command()
    if cmd:
        return JSONResponse(cmd)
    return JSONResponse({})


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    return PlainTextResponse("GoCharting Order Flow MCP Server OK")


@mcp.custom_route("/api/status", methods=["GET"])
async def get_status(request: Request):
    """Health check and active symbols overview."""
    await auto_sync_if_needed()
    return JSONResponse({
        "status": "online",
        "service": "GoCharting Order Flow MCP Server",
        "transport": "SSE on /sse",
        "active_symbols": store.list_active_symbols()
    })


# ==========================================
# BUILD SSE APP
# ==========================================

# Use the official MCP SSE Starlette app directly so that the SSE event stream,
# session manager, endpoint event, and /messages/ endpoints operate with 100%
# specification compliance. host="0.0.0.0" disables localhost DNS rebinding
# checks so Cloudflare Tunnels (and Muse AI) connect without friction.
app = mcp.sse_app(host="0.0.0.0")

# Allow CORS so browser extension / remote clients can communicate seamlessly
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def main():
    port = int(os.environ.get("PORT", 8765))
    host = os.environ.get("HOST", "0.0.0.0")
    print("=" * 60)
    print(f"🚀 GoCharting Order Flow MCP Server running on http://localhost:{port}")
    print(f"📡 MCP Streamable HTTP (Antigravity): http://localhost:{port}/mcp")
    print(f"📡 MCP Legacy SSE (Claude Desktop):   http://localhost:{port}/sse")
    print(f"📊 Browser Bridge Feed Endpoint:       http://localhost:{port}/api/feed")
    print(f"🩺 Health Check:                       http://localhost:{port}/health")
    print("=" * 60)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
