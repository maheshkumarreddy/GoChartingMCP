"""
Browser Controller for GoCharting via Chrome DevTools Protocol (CDP).
Supports simultaneous multi-tab streaming:
- Connects to multiple GoCharting tabs concurrently (e.g. NIFTY Futures tab + ATM Option tab)
- Programmatic control: change symbols, timeframes, open tools, and launch new chart tabs
- Extracts full-day 1-minute footprint candles from ALL open tabs in parallel
- Extracts Spot underlying, Basis, and Options chain Greek context
- Captures live chart screenshots
"""

import json
import base64
import asyncio
import re
import urllib.request
from urllib.parse import quote
import websockets
from typing import Dict, Any, Optional, List, Tuple


CDP_PORT = 9222

FOOTPRINT_EXTRACTION_JS = """
(() => {
    try {
        const c0 = window.rootReduxStore?.getState()?.chartComponentList?.[0];
        const canvasRef = c0?.store?.getState()?.transient?.chartCanvasRef?.current;
        if (!canvasRef) return JSON.stringify({ error: "canvasRef not ready" });

        const dm = canvasRef.dataManager;
        const c = canvasRef.getGlobalProps().store.getState();
        const main = c0?.dataStore?.getState()?.main;
        const mainData = main ? main.mainChartData || [] : [];

        // Build OHLC and Open Interest lookup map keyed by exact timestamp
        const ohlcMap = {};
        for (let b of mainData) {
            const ts = b.luxon_date?.ts || (b.date_str ? new Date(b.date_str).getTime() : null);
            if (ts) {
                ohlcMap[ts] = b;
            }
        }

        // Retrieve footprint data from timeSeriesStoreMap
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

        if (!fpMap) return JSON.stringify({ error: "Footprint data map not found in timeSeriesStoreMap." });

        const dates = Object.keys(fpMap).sort();
        const latestDate = dates[dates.length - 1];
        const fpForDate = fpMap[latestDate];
        const candles = fpForDate.candles || [];
        const clusterMap = new Map();
        const tpo = dm.tpo || { value: 6, precision: 1 };

        const results = [];
        for (let candle of candles) {
            const candleDate = new Date(candle.date);
            const ts = candleDate.getTime();
            
            dm.buildClusterForCandle(canvasRef, fpForDate, candle, { date: candleDate }, tpo, clusterMap);
            const cluster = clusterMap.get(ts);
            const ohlc = ohlcMap[ts] || {};
            const volTotals = cluster?.candleLevelTotals?.volume || {};
            const priceDivisor = cluster?.price_divisor || 10;

            const levels = [];
            if (cluster && cluster.clusterForCandle) {
                for (let [lvl, val] of cluster.clusterForCandle.entries()) {
                    levels.push({
                        price: lvl / priceDivisor,
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

            const pocPrice = volTotals.poc ? volTotals.poc / priceDivisor : (levels.length > 0 ? levels[0].price : 0);
            const buyVol = volTotals.buy !== undefined ? volTotals.buy : levels.reduce((s, x) => s + x.buy_vol, 0);
            const sellVol = volTotals.sell !== undefined ? volTotals.sell : levels.reduce((s, x) => s + x.sell_vol, 0);
            const netDelta = buyVol - sellVol;
            const minDelta = volTotals.deltaMin !== undefined ? volTotals.deltaMin : netDelta;
            const maxDelta = volTotals.deltaMax !== undefined ? volTotals.deltaMax : netDelta;

            results.push({
                timestamp: candleDate.toISOString(),
                date_str: candle.date,
                open: ohlc.open !== undefined ? ohlc.open : (levels.length > 0 ? levels[0].price : 0),
                high: ohlc.high !== undefined ? ohlc.high : (levels.length > 0 ? levels[levels.length - 1].price : 0),
                low: ohlc.low !== undefined ? ohlc.low : (levels.length > 0 ? levels[0].price : 0),
                close: ohlc.close !== undefined ? ohlc.close : (levels.length > 0 ? levels[levels.length - 1].price : 0),
                volume: ohlc.volume !== undefined ? ohlc.volume : (buyVol + sellVol),
                total_buy_vol: buyVol,
                total_sell_vol: sellVol,
                net_delta: netDelta,
                min_delta: minDelta,
                max_delta: maxDelta,
                open_interest: ohlc.open_interest !== undefined ? ohlc.open_interest : 0,
                oi_change: ohlc.oiChange !== undefined ? ohlc.oiChange : 0,
                oi_day_change: ohlc.oiDayChange !== undefined ? ohlc.oiDayChange : 0,
                oi_day_percent: ohlc.oiDayPercent !== undefined ? ohlc.oiDayPercent : 0,
                poc_price: pocPrice,
                levels: levels
            });
        }

        const store = c0?.store?.getState();
        const sec = store?.security || c?.security || {};
        const rawSym = sec.symbol ? (sec.exchange ? sec.exchange + ":" + sec.symbol : sec.symbol) : (document.title.includes("@") ? document.title.split("@")[1].trim() : "NSE:NIFTY-I");
        let cleanSymbol = rawSym;
        if (sec.segment === "OPTIONS" || sec.option_type) {
            const oType = sec.option_type === "PUT" ? "PE" : (sec.option_type === "CALL" ? "CE" : sec.option_type);
            cleanSymbol = `NIFTY ${sec.strike_price || ""} ${oType}`.trim();
        } else if (sec.segment === "FUTURE" || rawSym.includes("-I")) {
            cleanSymbol = rawSym.includes("BANK") ? "BANKNIFTY_FUT" : "NIFTY_FUT";
        }

        return JSON.stringify({
            status: "ok",
            symbol: cleanSymbol,
            raw_symbol: rawSym,
            contract_name: sec.name || cleanSymbol,
            session_date: latestDate,
            candlesCount: results.length,
            candles: results
        });
    } catch(e) {
        return JSON.stringify({ error: e.toString() });
    }
})()
"""


class GoChartingBrowserController:
    def __init__(self, port: int = CDP_PORT):
        self.port = port

    def get_all_gocharting_tabs(self) -> List[Dict[str, Any]]:
        """Returns metadata and websocket URLs for ALL open GoCharting tabs in Chrome."""
        try:
            url = f"http://localhost:{self.port}/json/list"
            req = urllib.request.urlopen(url, timeout=3)
            pages = json.loads(req.read().decode())
            return [p for p in pages if "gocharting.com" in p.get("url", "") and p.get("type") == "page"]
        except Exception as e:
            print(f"[CDP Controller] Failed to connect to Chrome on port {self.port}: {e}")
            return []

    def _get_page_ws_url(self) -> Optional[str]:
        tabs = self.get_all_gocharting_tabs()
        return tabs[0]["webSocketDebuggerUrl"] if tabs else None

    async def execute_js_on_tab(self, ws_url: str, expression: str) -> Any:
        """Executes JavaScript inside a specific tab via CDP."""
        try:
            async with websockets.connect(ws_url, max_size=25_000_000) as ws:
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
        except Exception as e:
            return {"error": str(e)}

    async def execute_js(self, expression: str, tab_index: int = 0) -> Any:
        """Executes JavaScript inside the primary GoCharting tab via CDP."""
        tabs = self.get_all_gocharting_tabs()
        if not tabs:
            return {"error": "Bot Chrome instance not detected on port 9222. Ensure launch_bot_chrome.sh is running."}
        idx = min(tab_index, len(tabs) - 1)
        return await self.execute_js_on_tab(tabs[idx]["webSocketDebuggerUrl"], expression)

    async def open_new_tab(self, url: str = "https://gocharting.com/terminal/chart/yDoy4eLHt") -> Dict[str, Any]:
        """
        Opens a new simultaneous GoCharting chart tab in Chrome.
        Allows having Tab 1 on NIFTY Futures and Tab 2 on an ATM Option strike.
        """
        try:
            new_tab_url = f"http://localhost:{self.port}/json/new?{quote(url, safe=':/?=')}"
            req = urllib.request.Request(new_tab_url, method="PUT")
            res = urllib.request.urlopen(req, timeout=5)
            tab_info = json.loads(res.read().decode())
            return {
                "status": "ok",
                "message": "Opened new GoCharting tab successfully.",
                "tab_id": tab_info.get("id"),
                "url": tab_info.get("url")
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to open new tab: {e}"}

    def find_tab_by_target(self, target: Optional[str] = None, tab_index: Optional[int] = None) -> int:
        """Finds the correct tab index based on target role ('OPTION' or 'FUTURES') or explicit index."""
        tabs = self.get_all_gocharting_tabs()
        if not tabs:
            return 0
        if tab_index is not None:
            return min(max(0, tab_index), len(tabs) - 1)
        if target:
            target_upper = target.upper()
            if target_upper in ["OPTION", "OPTIONS", "OPT", "STRIKE"]:
                for idx, t in enumerate(tabs):
                    title_upper = t.get("title", "").upper()
                    if " CE" in title_upper or " PE" in title_upper or "CE" in title_upper or "PE" in title_upper:
                        return idx
                return 1 if len(tabs) > 1 else 0
            elif target_upper in ["FUT", "FUTURES", "FUTURE"]:
                for idx, t in enumerate(tabs):
                    title_upper = t.get("title", "").upper()
                    if "-I" in title_upper or "FUT" in title_upper:
                        return idx
                return 0
        return 0

    @staticmethod
    def parse_option_symbol(symbol: str) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """Parses option strike, type (CE/PE), and optional expiry date from inputs like:
        '25000 PE', '25050 CE 2026-11-23', 'NIFTY 17 NOV 25100 CE', 'NSE:NIFTY26N1725000PE'.
        """
        # Look for explicit YYYY-MM-DD
        expiry_match = re.search(r'(\d{4}-\d{2}-\d{2})', symbol)
        expiry = expiry_match.group(1) if expiry_match else None

        # Look for DD-MMM (e.g. 17 NOV, 23 NOV, 17-NOV)
        if not expiry:
            exp_text_match = re.search(r'(\d{1,2})\s*[-_ ]?\s*(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)', symbol, re.I)
            if exp_text_match:
                day = exp_text_match.group(1).zfill(2)
                month = exp_text_match.group(2).upper()
                expiry = f"{day}-{month}"

        # Look for strike & type
        m = re.search(r'(\d{5})\s*(CE|PE|CALL|PUT)', symbol, re.I)
        if m:
            strike = m.group(1)
            t = m.group(2).upper()
            opt_type = 'CE' if ('CE' in t or 'CALL' in t) else 'PE'
            return True, strike, opt_type, expiry

        m2 = re.search(r'(CE|PE|CALL|PUT)\s*(\d{5})', symbol, re.I)
        if m2:
            t = m2.group(1).upper()
            strike = m2.group(2)
            opt_type = 'CE' if ('CE' in t or 'CALL' in t) else 'PE'
            return True, strike, opt_type, expiry

        return False, None, None, None

    async def change_symbol(
        self,
        symbol: str,
        expiry: Optional[str] = None,
        tab_index: Optional[int] = None,
        target_tab: Optional[str] = None
    ) -> Dict[str, Any]:
        """Switches the chart symbol and optional expiry in the specified GoCharting tab (defaults to target_tab='OPTION' if multiple tabs exist).
        Supports fully automated strike and expiry selection from the GoCharting NIFTY OPTIONS Chain modal."""
        is_opt, strike, opt_type, parsed_expiry = self.parse_option_symbol(symbol)
        final_expiry = expiry or parsed_expiry

        if target_tab is None and is_opt:
            target_tab = "OPTION"

        idx = self.find_tab_by_target(target=target_tab, tab_index=tab_index)

        if is_opt and strike and opt_type:
            is_call_js = "true" if opt_type == "CE" else "false"
            expiry_js = f'"{final_expiry}"' if final_expiry else "null"
            js = f"""
            (async () => {{
                const strike = "{strike}";
                const isCall = {is_call_js};
                const targetExpiry = {expiry_js};

                const isChainModalOpen = () => {{
                    const header = document.querySelector('.call-header');
                    return !!header && header.offsetParent !== null;
                }};

                // 1. Open Options Chain modal if not open
                if (!isChainModalOpen()) {{
                    let searchDialog = document.querySelector('[class*="erbwhql110"]');
                    if (!searchDialog || searchDialog.clientHeight < 100) {{
                        const searchBtn = document.querySelector('[aria-label="Search symbol..."]') ||
                                          document.querySelector('.ohlc-symbol-button') ||
                                          document.querySelector('.css-1gi9g8n');
                        if (searchBtn) {{
                            searchBtn.click();
                            await new Promise(r => setTimeout(r, 600));
                        }}
                    }}

                    const optTab = Array.from(document.querySelectorAll('button')).find(b => 
                        (b.textContent || '').trim() === 'Options'
                    );
                    if (optTab) {{
                        optTab.click();
                        await new Promise(r => setTimeout(r, 400));
                    }}

                    const optRow = Array.from(document.querySelectorAll('[class*="erbwhql46"], [class*="erbwhql38"]')).find(el => 
                        (el.textContent || '').includes('NIFTY OPTIONS')
                    );
                    if (optRow) {{
                        optRow.click();
                        for (let i = 0; i < 20; i++) {{
                            await new Promise(r => setTimeout(r, 200));
                            if (isChainModalOpen()) break;
                        }}
                    }}
                }}

                if (!isChainModalOpen()) {{
                    return {{ status: 'failed', message: 'Could not open Options Chain modal', tab_index: {idx} }};
                }}

                // 2. Select Expiry if specified
                if (targetExpiry) {{
                    const expiryBtns = Array.from(document.querySelectorAll('button')).filter(b => 
                        /\\d{{4}}-\\d{{2}}-\\d{{2}}/.test((b.textContent || '').trim())
                    );
                    const expMatch = expiryBtns.find(b => {{
                        const t = b.textContent.trim().toUpperCase();
                        const req = targetExpiry.toUpperCase();
                        if (t === req || req.includes(t) || t.includes(req)) return true;
                        if (req.includes("17") && t.endsWith("-17")) return true;
                        if (req.includes("23") && t.endsWith("-23")) return true;
                        if (req.includes("01") && (t.endsWith("-01") || t.endsWith("-1"))) return true;
                        if (req.includes("08") && (t.endsWith("-08") || t.endsWith("-8"))) return true;
                        if (req.includes("29") && t.endsWith("-29")) return true;
                        return false;
                    }});
                    if (expMatch) {{
                        expMatch.click();
                        await new Promise(r => setTimeout(r, 800));
                    }}
                }}

                // 3. Find target strike row in table
                const rows = Array.from(document.querySelectorAll('tr'));
                const targetRow = rows.find(r => {{
                    const sCell = r.querySelector('.strike');
                    return sCell && sCell.textContent.trim() === strike;
                }});

                if (!targetRow) {{
                    return {{ status: 'failed', message: 'Strike ' + strike + ' not found in options chain table', totalRows: rows.length, tab_index: {idx} }};
                }}

                const targetCell = isCall ? targetRow.querySelector('.call') : targetRow.querySelector('.put');
                if (!targetCell) {{
                    return {{ status: 'failed', message: 'Column cell not found for strike ' + strike, tab_index: {idx} }};
                }}

                const clickTarget = targetCell.querySelector('span') || targetCell;
                clickTarget.scrollIntoView({{ block: 'center', inline: 'center' }});
                clickTarget.click();
                await new Promise(r => setTimeout(r, 600));

                const closeBtn = document.querySelector('.css-1mft2lf');
                if (closeBtn) closeBtn.click();

                return {{
                    status: 'ok',
                    method: 'options_chain_selection',
                    symbol: '{symbol}',
                    strike: strike,
                    type: isCall ? 'CE' : 'PE',
                    expiry: targetExpiry || 'current',
                    selectedContract: clickTarget.textContent.trim(),
                    tab_index: {idx}
                }};
            }})()
            """
        else:
            js = f"""
            (async () => {{
                // Dismiss options chain modal if open
                const closeBtn = Array.from(document.querySelectorAll('button, span')).find(b => 
                    (b.textContent === '×' || b.textContent === '✕') && b.className && b.className.includes('css-')
                );
                if (closeBtn) {{
                    closeBtn.click();
                    await new Promise(r => setTimeout(r, 400));
                }}

                const searchBtn = document.querySelector('[aria-label="Search symbol..."]') ||
                                  document.querySelector('.ohlc-symbol-button') ||
                                  document.querySelector('.popovers-trigger button') ||
                                  document.querySelector('.css-1gi9g8n') ||
                                  document.querySelector('.symbol-title');
                if (searchBtn) {{
                    searchBtn.click();
                    await new Promise(r => setTimeout(r, 400));
                    
                    const input = document.querySelector('.symbol-search-input input') ||
                                  document.querySelector('input[placeholder*="Search"]') ||
                                  document.querySelector('input[type="text"]');
                    if (input) {{
                        input.focus();
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                        if (nativeSetter) {{
                            nativeSetter.call(input, '{symbol}');
                        }} else {{
                            input.value = '{symbol}';
                        }}
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        await new Promise(r => setTimeout(r, 600));
                        
                        const firstResult = document.querySelector('.symbol-item') || document.querySelector('[data-qa*="search-result"]');
                        if (firstResult) {{
                            firstResult.click();
                        }} else {{
                            input.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }}));
                        }}
                        return {{ status: "ok", symbol: "{symbol}", tab_index: {idx} }};
                    }}
                }}
                return {{ status: "failed", message: "Search element not found", tab_index: {idx} }};
            }})()
            """
        return await self.execute_js(js, tab_index=idx)

    async def change_timeframe(self, interval: str = "1m", tab_index: int = 0) -> Dict[str, Any]:
        """Changes chart timeframe in the specified tab."""
        js = f"""
        (async () => {{
            const intervalBtn = document.querySelector('.interval') || document.querySelector('.ohlc-interval-button');
            if (intervalBtn) {{
                intervalBtn.click();
                await new Promise(r => setTimeout(r, 300));
                
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
        return await self.execute_js(js, tab_index=tab_index)

    async def open_tool_or_study(self, tool_name: str, tab_index: int = 0) -> Dict[str, Any]:
        """Opens a study or indicator dialog in the specified tab."""
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
        return await self.execute_js(js, tab_index=tab_index)

    async def extract_footprint_from_single_tab(self, ws_url: str) -> Dict[str, Any]:
        """Extracts footprint candles from a specific tab websocket URL."""
        res_str = await self.execute_js_on_tab(ws_url, FOOTPRINT_EXTRACTION_JS)
        if isinstance(res_str, str):
            try:
                return json.loads(res_str)
            except Exception:
                return {"raw": res_str}
        return res_str or {}

    async def extract_live_footprint_from_all_tabs(self) -> List[Dict[str, Any]]:
        """
        Extracts 1-minute footprint candles from ALL open GoCharting tabs concurrently.
        Supports simultaneous multi-tab streaming (e.g., NIFTY Futures tab + ATM Option tab).
        """
        tabs = self.get_all_gocharting_tabs()
        if not tabs:
            return []

        tasks = [self.extract_footprint_from_single_tab(t["webSocketDebuggerUrl"]) for t in tabs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_series = []
        for r in results:
            if isinstance(r, dict) and r.get("status") == "ok":
                valid_series.append(r)
        return valid_series

    async def extract_live_footprint_from_chart(self) -> Dict[str, Any]:
        """Extracts footprint from the primary tab (or first valid tab)."""
        series = await self.extract_live_footprint_from_all_tabs()
        return series[0] if series else {}

    async def extract_spot_and_options_context(self, symbol: str = "NIFTY") -> Dict[str, Any]:
        """
        Extracts real-time Spot underlying price, Basis (Futures - Spot), Max Pain,
        PCR (Put-Call Ratio), and ATM IV from GoCharting's underlyingStoreMap.
        """
        js = """
        (() => {
            try {
                const c0 = window.rootReduxStore?.getState()?.chartComponentList?.[0];
                const store = c0?.store?.getState();
                const rootStore = store?.transient?.rootStore?.getState();
                const usm = rootStore?.transient?.underlyingStoreMap;

                let foundKey = null;
                if (usm) {
                    for (let k of usm.keys()) {
                        if (k.includes('NIFTY') || k.includes('SPOT')) {
                            foundKey = k;
                            break;
                        }
                    }
                }
                if (!foundKey) return { error: 'No underlying spot data currently loaded in GoCharting.' };

                const sub = usm.get(foundKey);
                const state = sub?.getState ? sub.getState() : sub;
                const oc = state?.options_chain;
                const firstSeries = oc?.seriesData?.[0] || {};

                // Current futures close price from main chart
                const mainData = c0?.dataStore?.getState()?.main?.mainChartData;
                const lastFutureBar = mainData ? mainData[mainData.length - 1] : null;
                const futuresPrice = lastFutureBar ? lastFutureBar.close : null;
                const spotPrice = firstSeries.underlying_price || null;
                const basis = (futuresPrice !== null && spotPrice !== null) ? Number((futuresPrice - spotPrice).toFixed(2)) : null;
                const basisPct = (basis !== null && spotPrice) ? Number(((basis / spotPrice) * 100).toFixed(4)) : null;
                const basisBps = (basis !== null && spotPrice) ? Number(((basis / spotPrice) * 10000).toFixed(2)) : null;

                let basisState = 'PARITY';
                if (basis !== null) {
                    if (basis > 1.0) basisState = 'PREMIUM';
                    else if (basis < -1.0) basisState = 'DISCOUNT';
                }

                // Calculate ATM strike anchored to Spot Price vs Futures Price
                const strikeStep = 50;
                const spotAtmStrike = spotPrice ? Math.round(spotPrice / strikeStep) * strikeStep : null;
                const futuresAtmStrike = futuresPrice ? Math.round(futuresPrice / strikeStep) * strikeStep : null;

                const rawDte = firstSeries.dte;
                const dteExactDays = rawDte !== undefined && rawDte !== null ? Number(rawDte.toFixed(1)) : null;
                const dteCalendarDays = rawDte !== undefined && rawDte !== null ? Math.ceil(rawDte) : null;

                return {
                    status: 'ok',
                    futures_symbol: store?.security?.ticker || 'NSE:NIFTY-I',
                    underlying_symbol: foundKey,
                    futures_price: futuresPrice,
                    spot_price: spotPrice,
                    basis: basis,
                    basis_pct: basisPct,
                    basis_percent: basisPct,
                    basis_bps: basisBps,
                    basis_state: basisState,
                    basis_unit_note: 'basis is points (futures - spot). basis_pct is percentage value (-0.0665 means -0.0665%). basis_bps is basis points (-6.65 bps).',
                    strike_guidance: {
                        spot_atm_strike: spotAtmStrike,
                        futures_atm_strike: futuresAtmStrike,
                        strike_difference: (futuresAtmStrike && spotAtmStrike) ? futuresAtmStrike - spotAtmStrike : 0,
                        note: 'Always anchor option strikes to spot_atm_strike for accurate Greeks and intrinsic value.'
                    },
                    options_context: {
                        max_pain: firstSeries.maxPain,
                        pcr_oi: firstSeries.pcr_oi,
                        pcr_volume: firstSeries.pcr_vol,
                        pcr_note: 'GoCharting calculates PCR from active traded strike levels in its options chain series.',
                        atm_iv: firstSeries.atmiv,
                        dte_exact_days: dteExactDays,
                        dte_calendar_days: dteCalendarDays,
                        nearest_expiry_display: firstSeries.displayFormat,
                        dte_note: 'dte_exact_days indicates trading fraction remaining; dte_calendar_days is calendar days to expiry date.'
                    }
                };
            } catch(e) {
                return { error: e.toString() };
            }
        })()
        """
        res = await self.execute_js(js)
        return res if isinstance(res, dict) else {"error": str(res)}

    async def capture_screenshot(self, output_path: str = "chart_screenshot.png", tab_index: int = 0) -> Dict[str, Any]:
        """Captures a full screenshot of the active GoCharting window."""
        tabs = self.get_all_gocharting_tabs()
        if not tabs:
            return {"error": "Browser not connected"}
        idx = min(tab_index, len(tabs) - 1)
        ws_url = tabs[idx]["webSocketDebuggerUrl"]

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
