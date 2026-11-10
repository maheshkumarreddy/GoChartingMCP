"""
Browser Controller for GoCharting via Chrome DevTools Protocol (CDP).
Provides full programmatic control over the dedicated Bot Chrome instance:
- Change symbols (Nifty Futures, Bank Nifty, Options strikes)
- Change timeframes (1m, 3m, 5m, etc.)
- Open tools and studies (VWAP, CVD, Market Profile, OI Profile)
- Modify chart/footprint settings
- Capture live chart screenshots for visual AI inspection
"""

import json
import base64
import asyncio
import urllib.request
import websockets
from typing import Dict, Any, Optional, List


CDP_PORT = 9222


class GoChartingBrowserController:
    def __init__(self, port: int = CDP_PORT):
        self.port = port

    def _get_page_ws_url(self) -> Optional[str]:
        try:
            url = f"http://localhost:{self.port}/json/list"
            req = urllib.request.urlopen(url, timeout=3)
            pages = json.loads(req.read().decode())
            # Find the GoCharting tab
            for p in pages:
                if "gocharting.com" in p.get("url", "") and p.get("type") == "page":
                    return p.get("webSocketDebuggerUrl")
            # If not found, return first active page
            for p in pages:
                if p.get("type") == "page":
                    return p.get("webSocketDebuggerUrl")
        except Exception as e:
            print(f"[CDP Controller] Failed to connect to Chrome on port {self.port}: {e}")
            return None
        return None

    async def execute_js(self, expression: str) -> Any:
        """Executes JavaScript inside the GoCharting tab."""
        ws_url = self._get_page_ws_url()
        if not ws_url:
            return {"error": "Bot Chrome instance not detected on port 9222. Ensure launch_bot_chrome.sh is running."}

        async with websockets.connect(ws_url) as ws:
            req = {
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True
                }
            }
            await ws.send(json.dumps(req))
            resp = json.loads(await ws.recv())
            res = resp.get("result", {}).get("result", {})
            return res.get("value")

    async def change_symbol(self, symbol: str) -> Dict[str, Any]:
        """Switches the chart symbol (e.g. 'NSE:NIFTY-I', 'NSE:BANKNIFTY-I', 'NIFTY 24300 CE')."""
        js = f"""
        (async () => {{
            const searchBtn = document.querySelector('[aria-label="Search symbol..."]') ||
                              document.querySelector('.ohlc-symbol-button') ||
                              document.querySelector('.symbol-title');
            if (searchBtn) {{
                searchBtn.click();
                await new Promise(r => setTimeout(r, 400));
                
                const input = document.querySelector('input[placeholder*="Search"]') ||
                              document.querySelector('.symbol-search-input input') ||
                              document.querySelector('input[type="text"]');
                if (input) {{
                    input.focus();
                    input.value = '{symbol}';
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    await new Promise(r => setTimeout(r, 600));
                    
                    // Press Enter or click first result
                    const firstResult = document.querySelector('.symbol-item') || document.querySelector('[data-qa*="search-result"]');
                    if (firstResult) {{
                        firstResult.click();
                    }} else {{
                        input.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }}));
                    }}
                    return {{ status: "ok", symbol: "{symbol}" }};
                }}
            }}
            return {{ status: "failed", message: "Search element not found" }};
        }})()
        """
        return await self.execute_js(js)

    async def change_timeframe(self, interval: str = "1m") -> Dict[str, Any]:
        """Changes chart timeframe (e.g. '1m', '3m', '5m', '15m')."""
        js = f"""
        (async () => {{
            const intervalBtn = document.querySelector('.interval') || document.querySelector('.ohlc-interval-button');
            if (intervalBtn) {{
                intervalBtn.click();
                await new Promise(r => setTimeout(r, 300));
                
                // Find matching interval item in dropdown
                const items = Array.from(document.querySelectorAll('.dropdown-item, .interval-item, li'));
                const match = items.find(el => el.innerText.trim().toLowerCase() === '{interval.lower()}');
                if (match) {{
                    match.click();
                    return {{ status: "ok", interval: "{interval}" }};
                }}
            }}
            return {{ status: "failed", message: "Interval element not found" }};
        }})()
        """
        return await self.execute_js(js)

    async def open_tool_or_study(self, tool_name: str) -> Dict[str, Any]:
        """Opens a study or indicator dialog (e.g., 'VWAP', 'CVD', 'Profile')."""
        js = f"""
        (async () => {{
            const studyBtn = Array.from(document.querySelectorAll('button, div')).find(el => el.innerText.trim() === 'Study');
            if (studyBtn) {{
                studyBtn.click();
                await new Promise(r => setTimeout(r, 400));
                
                const searchInput = document.querySelector('input[placeholder*="indicator"], input[placeholder*="Search"]');
                if (searchInput) {{
                    searchInput.value = '{tool_name}';
                    searchInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    await new Promise(r => setTimeout(r, 400));
                    
                    const firstOption = document.querySelector('.study-item, .indicator-item, li');
                    if (firstOption) {{
                        firstOption.click();
                        return {{ status: "ok", opened: "{tool_name}" }};
                    }}
                }}
            }}
            return {{ status: "failed", message: "Study button or tool not found" }};
        }})()
        """
        return await self.execute_js(js)

    async def extract_live_footprint_from_chart(self) -> Dict[str, Any]:
        """
        Extracts the exact clustered footprint and OHLC values directly from GoCharting's
        dataManager and timeSeriesStoreMap, matching the browser canvas display 100%.
        """
        js = """
        (() => {
            try {
                const c0 = window.rootReduxStore?.getState()?.chartComponentList?.[0];
                const canvasRef = c0?.store?.getState()?.transient?.chartCanvasRef?.current;
                if (!canvasRef) return JSON.stringify({ error: "canvasRef not ready" });

                const dm = canvasRef.dataManager;
                const c = canvasRef.getGlobalProps().store.getState();
                const main = c0?.dataStore?.getState()?.main;
                const mainData = main ? main.mainChartData || [] : [];

                // Build OHLC lookup map from mainChartData
                const ohlcMap = {};
                for (let i = Math.max(0, mainData.length - 200); i < mainData.length; i++) {
                    const b = mainData[i];
                    if (b && b.date) {
                        ohlcMap[new Date(b.date).getTime()] = b;
                    }
                }

                // First check if fullCluster already has active clusters
                const fc = dm?.additionalData?.fullCluster;
                const results = [];

                if (fc && fc.size > 0) {
                    for (const [timestamp, candleCluster] of fc.entries()) {
                        const ohlc = ohlcMap[timestamp] || {};
                        const levels = [];
                        const clusterMap = candleCluster.clusterForCandle;

                        if (clusterMap) {
                            for (const [lvl, data] of clusterMap.entries()) {
                                levels.push({
                                    price: lvl / candleCluster.price_divisor,
                                    buy_vol: data.volume.buy,
                                    sell_vol: data.volume.sell,
                                    delta: data.volume.buy - data.volume.sell,
                                    total_vol: data.volume.total,
                                    buy_trades: data.trades.buy,
                                    sell_trades: data.trades.sell
                                });
                            }
                        }
                        levels.sort((a, b) => a.price - b.price);

                        const pocPrice = candleCluster.candleLevelTotals ?
                                         candleCluster.candleLevelTotals.volume.poc / candleCluster.price_divisor : 0;

                        results.push({
                            timestamp: new Date(timestamp).toISOString(),
                            date_str: ohlc.date_str || new Date(timestamp).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }),
                            open: ohlc.open !== undefined ? ohlc.open : (levels.length > 0 ? levels[0].price : 0),
                            high: ohlc.high !== undefined ? ohlc.high : (levels.length > 0 ? levels[levels.length - 1].price : 0),
                            low: ohlc.low !== undefined ? ohlc.low : (levels.length > 0 ? levels[0].price : 0),
                            close: ohlc.close !== undefined ? ohlc.close : (levels.length > 0 ? levels[levels.length - 1].price : 0),
                            volume: ohlc.volume !== undefined ? ohlc.volume : levels.reduce((s, x) => s + x.total_vol, 0),
                            poc_price: pocPrice,
                            levels: levels
                        });
                    }
                }

                // If fullCluster was empty, cluster recent candles from timeSeriesStoreMap directly
                if (results.length === 0) {
                    const rootStore = c.transient?.rootStore?.getState();
                    const tsm = rootStore?.transient?.timeSeriesStoreMap;
                    let fpMap = null;

                    if (tsm) {
                        for (let k of tsm.keys()) {
                            const sub = tsm.get(k)?.getState();
                            if (sub?.footprint && Object.keys(sub.footprint).length > 0) {
                                fpMap = sub.footprint;
                                break;
                            }
                        }
                    }

                    if (fpMap) {
                        const dates = Object.keys(fpMap).sort();
                        const latestDate = dates[dates.length - 1];
                        const fpForDate = fpMap[latestDate];
                        const candles = fpForDate.candles || [];
                        const recent = candles.slice(-50); // Last 50 candles
                        const clusterMap = new Map();
                        const tpo = dm.tpo || { value: 6, precision: 1 };

                        for (let candle of recent) {
                            const candleObj = { date: new Date(candle.date) };
                            dm.buildClusterForCandle(canvasRef, fpForDate, candle, candleObj, tpo, clusterMap);
                            const cluster = clusterMap.get(candleObj.date.getTime());

                            const levels = [];
                            if (cluster && cluster.clusterForCandle) {
                                for (let [lvl, val] of cluster.clusterForCandle.entries()) {
                                    levels.push({
                                        price: lvl / cluster.price_divisor,
                                        buy_vol: val.volume.buy,
                                        sell_vol: val.volume.sell,
                                        delta: val.volume.buy - val.volume.sell,
                                        total_vol: val.volume.total,
                                        buy_trades: val.trades.buy,
                                        sell_trades: val.trades.sell
                                    });
                                }
                            }
                            levels.sort((a, b) => a.price - b.price);

                            const pocPrice = cluster?.candleLevelTotals?.volume?.poc ?
                                             cluster.candleLevelTotals.volume.poc / cluster.price_divisor : 0;
                            const ohlc = ohlcMap[candleObj.date.getTime()] || {};

                            results.push({
                                timestamp: candleObj.date.toISOString(),
                                date_str: candle.date,
                                open: ohlc.open !== undefined ? ohlc.open : (levels.length > 0 ? levels[0].price : 0),
                                high: ohlc.high !== undefined ? ohlc.high : (levels.length > 0 ? levels[levels.length - 1].price : 0),
                                low: ohlc.low !== undefined ? ohlc.low : (levels.length > 0 ? levels[0].price : 0),
                                close: ohlc.close !== undefined ? ohlc.close : (levels.length > 0 ? levels[levels.length - 1].price : 0),
                                volume: ohlc.volume !== undefined ? ohlc.volume : levels.reduce((s, x) => s + x.total_vol, 0),
                                poc_price: pocPrice,
                                levels: levels
                            });
                        }
                    }
                }

                const symbol = c.security ? c.security.ticker : "NSE:NIFTY-I";
                return JSON.stringify({
                    status: "ok",
                    symbol: symbol,
                    candlesCount: results.length,
                    candles: results
                });
            } catch(e) {
                return JSON.stringify({ error: e.toString() });
            }
        })()
        """
        res_str = await self.execute_js(js)
        if isinstance(res_str, str):
            try:
                return json.loads(res_str)
            except Exception:
                return {"raw": res_str}
        return res_str or {}

    async def capture_screenshot(self, output_path: str = "/Users/mac/Desktop/GoChartingMCP/chart_screenshot.png") -> Dict[str, Any]:
        """Captures a full screenshot of the active GoCharting window."""
        ws_url = self._get_page_ws_url()
        if not ws_url:
            return {"error": "Browser not connected"}

        async with websockets.connect(ws_url) as ws:
            req = {"id": 2, "method": "Page.captureScreenshot", "params": {"format": "png"}}
            await ws.send(json.dumps(req))
            resp = json.loads(await ws.recv())
            b64_data = resp.get("result", {}).get("data", "")
            if b64_data:
                with open(output_path, "wb") as f:
                    f.write(base64.b64decode(b64_data))
                return {"status": "ok", "saved_to": output_path}
            return {"status": "failed", "message": "No screenshot data returned"}


# Global controller instance
controller = GoChartingBrowserController()

