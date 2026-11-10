"""
GoCharting Order Flow MCP Server
Exposes 1-minute candle Footprint, Tick Delta ladders, and Order Flow signals
via Model Context Protocol (MCP) with:
  - Streamable HTTP at /mcp  (Antigravity / modern clients)
  - Legacy SSE at /sse        (Claude Desktop, Muse AI, & legacy clients)
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

from orderflow_store import store, get_market_session_info
from browser_controller import controller

# Admin token from environment (if set, browser-modifying actions require this token)
MCP_ADMIN_TOKEN = os.environ.get("MCP_ADMIN_TOKEN", "").strip()

# Initialize MCP Server
mcp = MCPServer(name="GoCharting-OrderFlow-MCP")


async def auto_sync_if_needed():
    """Fetches latest footprint data from ALL open GoCharting tabs via CDP in parallel."""
    try:
        series_list = await controller.extract_live_footprint_from_all_tabs()
        for series in series_list:
            if series and series.get("status") == "ok":
                store.sync_from_browser_series(series)
    except Exception as e:
        print(f"[AutoSync Warning] {e}")


def _verify_admin_access(token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """If MCP_ADMIN_TOKEN is configured in environment, gate browser-modifying actions."""
    if MCP_ADMIN_TOKEN:
        if not token or token.strip() != MCP_ADMIN_TOKEN:
            return {
                "status": "error",
                "message": "Unauthorized: Browser-modifying tools require a valid admin_token configured in MCP_ADMIN_TOKEN."
            }
    return None


# ==========================================
# BROWSER AUTOMATION TOOLS (SAFE ACTIONS)
# ==========================================

@mcp.tool()
async def browser_sync_now() -> Dict[str, Any]:
    """
    Forces an immediate live sync of order flow footprint candles directly
    from ALL open GoCharting browser tabs.
    Returns synced symbols, candle counts, market session status, and data staleness indicators.
    """
    await auto_sync_if_needed()
    active = store.list_active_symbols()
    session_info = get_market_session_info(active[0]["latest_time"] if active else None)
    return {
        "status": "ok",
        "synced_symbols": [s["symbol"] for s in active],
        "active_charts": active,
        "market_session_state": session_info["market_session_state"],
        "is_live": session_info["is_live"],
        "data_age_seconds": session_info["data_age_seconds"],
        "staleness_warning": session_info["staleness_warning"]
    }


@mcp.tool()
async def browser_open_chart_tab(url: str = "https://gocharting.com/terminal/chart/yDoy4eLHt", admin_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Opens an additional simultaneous GoCharting chart tab in Chrome (e.g. for an ATM option strike
    or secondary contract) without disturbing existing open tabs.
    Both tabs stream and cache in parallel without any tab switching.
    Optionally gated by admin_token if MCP_ADMIN_TOKEN environment variable is set.
    """
    auth_err = _verify_admin_access(admin_token)
    if auth_err:
        return auth_err
    return await controller.open_new_tab(url)


@mcp.tool()
async def browser_change_symbol(
    symbol: str,
    expiry: Optional[str] = None,
    target_tab: Optional[str] = "OPTION",
    tab_index: Optional[int] = None,
    admin_token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Directly changes the chart symbol and optional expiry in the GoCharting browser window
    (e.g., '25000 PE', '25050 CE', '25100 CE', 'NSE:BANKNIFTY-I').
    
    Parameters:
    - symbol: Target ticker or contract name (e.g. '25000 PE', '25050 CE', 'NIFTY 25100 CE', '25000 PE 2026-11-23').
    - expiry: Optional target expiry date (e.g. '2026-11-17', '2026-11-23', '17-NOV', '23-NOV').
              Defaults to current active expiry if not specified.
    - target_tab: Target tab to update ('OPTION' to change the option tab without touching Futures; 'FUTURES' to change futures).
    - tab_index: Optional explicit index (0, 1) of the tab to retarget.
    - admin_token: Required if MCP_ADMIN_TOKEN is configured in environment.
    """
    auth_err = _verify_admin_access(admin_token)
    if auth_err:
        return auth_err
    return await controller.change_symbol(symbol=symbol, expiry=expiry, tab_index=tab_index, target_tab=target_tab)


@mcp.tool()
async def browser_change_timeframe(interval: str = "1m", admin_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Changes the candle interval in the GoCharting browser window
    (e.g., '1m', '3m', '5m', '15m', '30m', '1h', '1D').
    Optionally gated by admin_token if MCP_ADMIN_TOKEN environment variable is set.
    """
    auth_err = _verify_admin_access(admin_token)
    if auth_err:
        return auth_err
    return await controller.change_timeframe(interval)


@mcp.tool()
async def browser_open_tool(tool_name: str, admin_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Opens an indicator, study, or tool in the GoCharting browser window
    (e.g., 'VWAP', 'CVD', 'Profile', 'RSI').
    Optionally gated by admin_token if MCP_ADMIN_TOKEN environment variable is set.
    """
    auth_err = _verify_admin_access(admin_token)
    if auth_err:
        return auth_err
    return await controller.open_tool_or_study(tool_name)


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
    Returns OHLCV, Net Delta, intra-candle Min/Max Delta, POC price, CVD,
    Open Interest changes, Institutional Positioning Quadrant, and market session state.
    """
    await auto_sync_if_needed()
    candle = store.get_latest_candle(symbol)
    if not candle:
        return {
            "error": f"No 1-minute data yet for {symbol}.",
            "available_symbols": [s["symbol"] for s in store.list_active_symbols()],
            "tip": "Ensure GoCharting has the chart open with the browser hook enabled, or call switch_chart."
        }

    session_info = get_market_session_info(candle.timestamp)

    return {
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
        "timestamp": candle.timestamp,
        "open": candle.open_price,
        "high": candle.high_price,
        "low": candle.low_price,
        "close": candle.close_price,
        "prev_close": candle.prev_close_price,
        "price_change": candle.price_change,
        "volume": candle.total_volume,
        "buy_volume": candle.total_buy_vol,
        "sell_volume": candle.total_sell_vol,
        "net_delta": candle.net_delta,
        "min_delta": candle.min_delta,
        "max_delta": candle.max_delta,
        "open_interest": candle.open_interest,
        "oi_change": candle.oi_change,
        "oi_day_change": candle.oi_day_change,
        "positioning_quadrant": candle.positioning_quadrant,
        "poc_price": candle.poc_price,
        "cvd": candle.cvd,
        "is_post_close": candle.is_post_close,
        "post_close_note": "Post-market settlement/auction trade (15:31-15:40 IST), not active continuous trading." if candle.is_post_close else None,
        "signals": candle.signals,
        "market_session_state": session_info["market_session_state"],
        "is_live": session_info["is_live"],
        "data_age_seconds": session_info["data_age_seconds"],
        "current_time_ist": session_info["current_time_ist"],
        "staleness_warning": session_info["staleness_warning"]
    }


@mcp.tool()
async def get_delta_ladder(symbol: str = "NIFTY_FUT", candle_index: int = 0) -> Dict[str, Any]:
    """
    Returns the price-by-price Tick Delta matrix for the specified 1-minute candle.
    Each price level lists the net delta (Buy Vol - Sell Vol), total volume, and flags for POC and imbalances.
    candle_index=0 is the current/latest candle; 1 is the previous closed candle.
    """
    if candle_index < 0:
        return {"error": f"Invalid candle_index '{candle_index}'. Must be an integer >= 0."}

    await auto_sync_if_needed()
    history = store.get_history(symbol, limit=candle_index + 1)
    if not history or len(history) <= candle_index:
        return {"error": f"Candle at index {candle_index} not found for {symbol}."}

    candle = history[candle_index]
    
    ladder = []
    for lvl in candle.tick_deltas:
        ladder.append({
            "price": lvl.price,
            "delta": lvl.delta,
            "buy_vol": lvl.buy_vol,
            "sell_vol": lvl.sell_vol,
            "vol": lvl.volume,
            "trades": lvl.trades,
            "is_poc": lvl.is_poc,
            "buy_imbalance": lvl.is_buy_imbalance,
            "sell_imbalance": lvl.is_sell_imbalance
        })

    session_info = get_market_session_info(candle.timestamp)

    return {
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
        "timestamp": candle.timestamp,
        "close": candle.close_price,
        "net_candle_delta": candle.net_delta,
        "min_delta": candle.min_delta,
        "max_delta": candle.max_delta,
        "cvd": candle.cvd,
        "poc_price": candle.poc_price,
        "tick_count": len(ladder),
        "tick_delta_ladder": ladder,
        "market_session_state": session_info["market_session_state"],
        "is_live": session_info["is_live"],
        "data_age_seconds": session_info["data_age_seconds"]
    }


@mcp.tool()
async def get_orderflow_signals(symbol: str = "NIFTY_FUT", lookback: int = 5, exclude_post_close: bool = False) -> Dict[str, Any]:
    """
    Analyzes order flow across the last N 1-minute candles for the symbol.
    Detects Cumulative Volume Delta (CVD) divergence, aggressive absorption,
    and stacked buy/sell imbalances.

    Divergence Definitions:
    - delta_divergence (close-to-close): True when (close - prev_close) opposes net_delta sign.
    - intra_candle_divergence: True when (close - open) opposes net_delta sign.
    Both are explicitly returned for full verification.
    """
    if lookback <= 0:
        return {
            "error": f"Invalid lookback value '{lookback}'. Lookback must be a positive integer >= 1.",
            "requested_lookback": lookback
        }

    await auto_sync_if_needed()
    all_candles = store.candles.get(symbol, [])
    if exclude_post_close:
        all_candles = [c for c in all_candles if not c.is_post_close]

    if not all_candles:
        return {"error": f"No history found for {symbol}."}

    candles = all_candles[:lookback]
    is_clamped = len(all_candles) < lookback

    recent_signals = []
    total_delta = 0
    post_close_count = 0

    for c in reversed(candles):
        total_delta += c.net_delta
        if c.is_post_close:
            post_close_count += 1
        recent_signals.append({
            "timestamp": c.timestamp,
            "open": c.open_price,
            "high": c.high_price,
            "low": c.low_price,
            "close": c.close_price,
            "prev_close": c.prev_close_price,
            "price_change": c.price_change,
            "delta": c.net_delta,
            "min_delta": c.min_delta,
            "max_delta": c.max_delta,
            "poc": c.poc_price,
            "delta_divergence": c.signals.get("delta_divergence", False),
            "close_to_close_divergence": c.signals.get("close_to_close_divergence", False),
            "intra_candle_divergence": c.signals.get("intra_candle_divergence", False),
            "absorption_low": c.signals.get("absorption_at_low", False),
            "absorption_high": c.signals.get("absorption_at_high", False),
            "buy_imbalances": c.signals.get("stacked_buy_imbalance", []),
            "sell_imbalances": c.signals.get("stacked_sell_imbalance", []),
            "is_post_close": c.is_post_close
        })

    latest = candles[0]
    first = candles[-1]
    price_change = round(latest.close_price - first.prev_close_price, 2)

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

    session_info = get_market_session_info(latest.timestamp)

    return {
        "symbol": symbol,
        "requested_lookback": lookback,
        "candles_analyzed": len(candles),
        "is_clamped": is_clamped,
        "exclude_post_close": exclude_post_close,
        "post_close_candles_in_window": post_close_count,
        "bias": bias,
        "net_lookback_delta": total_delta,
        "lookback_price_change": price_change,
        "current_cvd": latest.cvd,
        "divergence_definitions": {
            "delta_divergence": "Close-to-Close vs Delta sign disagreement (standard order flow benchmark)",
            "intra_candle_divergence": "Open-to-Close vs Delta sign disagreement (intra-minute candle benchmark)"
        },
        "candle_breakdowns": recent_signals,
        "market_session_state": session_info["market_session_state"],
        "is_live": session_info["is_live"],
        "data_age_seconds": session_info["data_age_seconds"],
        "staleness_warning": session_info["staleness_warning"]
    }


@mcp.tool()
async def get_oi_analysis(symbol: str = "NIFTY_FUT", lookback: int = 10, exclude_post_close: bool = False) -> Dict[str, Any]:
    """
    Analyzes 1-minute Open Interest (OI) changes alongside Net Delta to categorize
    institutional positioning into canonical quadrants.
    Returns both:
    - window_aggregate_positioning: Full lookback window aggregate flow (net delta + price change + OI change)
    - modal_candle_quadrant: The plurality quadrant across individual bars in the window
    """
    if lookback <= 0:
        return {
            "error": f"Invalid lookback value '{lookback}'. Lookback must be a positive integer >= 1.",
            "requested_lookback": lookback
        }

    await auto_sync_if_needed()
    return store.get_oi_analysis(symbol, lookback, exclude_post_close=exclude_post_close)


@mcp.tool()
async def get_basis_analysis(symbol: str = "NIFTY") -> Dict[str, Any]:
    """
    Calculates Basis (Futures Price - Spot Index Price) in real time.
    Reveals whether Futures are leading or following Spot, determines Premium vs Discount,
    and provides Spot-anchored ATM strike guidance for accurate option pricing.
    Also returns Max Pain, Put-Call Ratio (PCR), ATM Implied Volatility (IV), and exact DTE.
    """
    return await controller.extract_spot_and_options_context(symbol)


@mcp.tool()
def switch_chart(symbol: str, segment: str = "FUT", timeframe: str = "1m", admin_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Requests the browser bridge to switch the active GoCharting chart to the target
    Futures contract or Option strike (e.g., 'NIFTY 24300 CE', 'BANKNIFTY 52000 PE', 'BANKNIFTY_FUT').
    Optionally gated by admin_token if MCP_ADMIN_TOKEN environment variable is set.
    """
    auth_err = _verify_admin_access(admin_token)
    if auth_err:
        return auth_err

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
    with their latest price, delta, CVD, candle counts, and market session state.
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
        admin_token = data.get("admin_token")
        auth_err = _verify_admin_access(admin_token)
        if auth_err:
            return JSONResponse(auth_err, status_code=403)
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

# Official MCP SSE Starlette app with 0.0.0.0 host binding
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
