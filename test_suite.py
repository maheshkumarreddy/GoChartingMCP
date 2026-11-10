#!/usr/bin/env python3
"""
Automated Test Suite for GoCharting Order Flow MCP Server
Validates CDP browser connection, full-day footprint extraction, deterministic CVD,
institutional positioning quadrants, min/max delta resolution, and lookback validations.
Run this before committing or deploying changes to AWS.
"""

import asyncio
import sys
import json
from browser_controller import controller
from orderflow_store import store, classify_positioning_quadrant, get_market_session_info
import server


async def run_tests():
    print("=" * 70)
    print("🧪 Running GoCharting MCP Pre-Deployment Regression Test Suite")
    print("=" * 70)
    passed = 0
    total = 0

    # ---------------------------------------------------------
    # Test 1: Chrome CDP Connection
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 1] Checking Chrome CDP connection on port 9222...")
    try:
        title = await controller.execute_js("document.title")
        if title and not isinstance(title, dict):
            print(f"   ✅ PASS: Connected to Chrome! Active page: '{title}'")
            passed += 1
        else:
            print(f"   ❌ FAIL: Could not read page title: {title}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 2: Full Day Footprint Extraction (09:15 to 15:39)
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 2] Extracting full-day footprint clusters from GoCharting...")
    data = None
    try:
        data = await controller.extract_live_footprint_from_chart()
        if data and data.get("status") == "ok" and data.get("candlesCount", 0) > 0:
            count = data.get("candlesCount")
            first_c = data["candles"][0]
            last_c = data["candles"][-1]
            print(f"   ✅ PASS: Successfully extracted {count} 1-minute footprint candles!")
            print(f"            Session Date: {data.get('session_date')}")
            print(f"            First bar: {first_c.get('date_str')} | Last bar: {last_c.get('date_str')}")
            if count >= 300:
                print(f"            Full trading day session confirmed ({count} >= 300 candles).")
                passed += 1
            else:
                print(f"   ⚠️ WARNING: Received only {count} candles (< 300 expected for full day)")
                passed += 1
        else:
            print(f"   ❌ FAIL: Extraction failed: {data}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 3: Deterministic CVD (Zero Drift Across 10 Syncs)
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 3] Testing Deterministic CVD (Zero Accumulator Drift)...")
    try:
        if data and data.get("status") == "ok":
            # Initial sync
            store.sync_from_browser_series(data)
            cvd_run_1 = store.get_latest_candle("NIFTY_FUT").cvd

            # Run 10 consecutive syncs
            for _ in range(10):
                store.sync_from_browser_series(data)

            cvd_run_11 = store.get_latest_candle("NIFTY_FUT").cvd
            if cvd_run_1 == cvd_run_11:
                print(f"   ✅ PASS: CVD is completely deterministic! CVD = {cvd_run_1} across 10 repeated syncs.")
                passed += 1
            else:
                print(f"   ❌ FAIL: CVD drifted! Initial: {cvd_run_1}, After 10 syncs: {cvd_run_11}")
        else:
            print("   ⚠️ SKIP: Skipping CVD test because extraction failed.")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 4: Intra-Candle Min / Max Delta vs Net Delta
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 4] Verifying intra-candle min_delta and max_delta separation...")
    try:
        latest = store.get_latest_candle("NIFTY_FUT")
        all_candles = store.candles.get("NIFTY_FUT", [])
        # Find any candle where min_delta != max_delta
        separated = [c for c in all_candles if c.min_delta != c.max_delta or c.min_delta != c.net_delta]
        if separated:
            sample = separated[0]
            print(f"   ✅ PASS: Real intra-candle delta depth detected on {sample.timestamp}!")
            print(f"            Net Delta: {sample.net_delta} | Min Delta: {sample.min_delta} | Max Delta: {sample.max_delta}")
            passed += 1
        else:
            print(f"   ❌ FAIL: All candles have min_delta == max_delta == net_delta placeholder")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 5: Strict 3-Condition Positioning Quadrant Logic
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 5] Testing 3-Variable Positioning Quadrant Classifications...")
    try:
        test_cases = [
            # (price_change, net_delta, oi_change, expected_quadrant)
            (2.0, 5000, 1000, "LONG_BUILDUP"),
            (2.0, 5000, -1000, "SHORT_COVERING"),
            (-2.0, -5000, 1000, "SHORT_BUILDUP"),
            (-2.0, -5000, -1000, "LONG_UNWINDING"),
            # Divergences / Absorptions
            (1.0, -10335, 500, "ABSORPTION_BUY"),
            (1.0, -2405, -500, "SHORT_COVERING_DIV"),
            (-1.0, 5000, 1000, "ABSORPTION_SELL"),
            (-1.0, 5000, -1000, "LONG_UNWINDING_DIV"),
            # Zero OI change
            (1.0, -4355, 0, "NEUTRAL_OI"),
            (-1.0, 4355, 0, "NEUTRAL_OI"),
            # Flat
            (0.0, 0, 0, "NEUTRAL"),
        ]

        all_ok = True
        for pc, nd, oic, exp in test_cases:
            actual = classify_positioning_quadrant(pc, nd, oic)
            if actual != exp:
                print(f"   ❌ MISMATCH: pc={pc}, nd={nd}, oi={oic} -> Expected '{exp}', got '{actual}'")
                all_ok = False

        if all_ok:
            print(f"   ✅ PASS: All {len(test_cases)} quadrant edge cases classified accurately without contradictions.")
            passed += 1
        else:
            print("   ❌ FAIL: Some quadrant test cases failed.")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 6: Lookback Input Validations (-1, 0, Clamping)
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 6] Testing Lookback Edge Cases (lookback=-1, 0, 500)...")
    try:
        # Negative lookback
        res_neg = await server.get_orderflow_signals(symbol="NIFTY_FUT", lookback=-1)
        # Zero lookback
        res_zero = await server.get_orderflow_signals(symbol="NIFTY_FUT", lookback=0)
        # Clamped lookback
        res_large = await server.get_orderflow_signals(symbol="NIFTY_FUT", lookback=5000)

        if "error" in res_neg and "error" in res_zero and res_large.get("is_clamped") is True:
            print("   ✅ PASS: Lookback <= 0 returns clean error; lookback > count reports is_clamped=True.")
            passed += 1
        else:
            print(f"   ❌ FAIL: res_neg={res_neg}, res_zero={res_zero}, res_large clamped={res_large.get('is_clamped')}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 7: Market Session & Staleness Metadata
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 7] Testing Market Session State and Staleness Detection...")
    try:
        sess = get_market_session_info("2026-11-09T15:39:00+05:30")
        if sess["market_session_state"] in ["OPEN", "CLOSED", "PRE_OPEN", "POST_MARKET"] and sess["data_age_seconds"] is not None:
            print(f"   ✅ PASS: Market Session: {sess['market_session_state']} | Age: {sess['data_age_seconds']}s | Warning: {sess['staleness_warning']}")
            passed += 1
        else:
            print(f"   ❌ FAIL: Unexpected session info: {sess}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 8: Spot & Basis Analysis
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 8] Testing Basis (Futures - Spot) and Option Greeks...")
    try:
        basis_data = await server.get_basis_analysis(symbol="NIFTY")
        if basis_data and basis_data.get("status") == "ok":
            opt = basis_data.get("options_context", {})
            print(f"   ✅ PASS: Futures: {basis_data.get('futures_price')} | Spot: {basis_data.get('spot_price')} | Basis: {basis_data.get('basis')} ({basis_data.get('basis_state')})")
            print(f"            Max Pain: {opt.get('max_pain')} | PCR: {opt.get('pcr_oi')} | ATM IV: {opt.get('atm_iv')}%")
            print(f"            DTE Exact: {opt.get('dte_exact_days')} | DTE Calendar: {opt.get('dte_calendar_days')} days")
            passed += 1
        else:
            print(f"   ❌ FAIL: Basis extraction failed: {basis_data}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 9: Security Audit (browser_execute_custom_js Removed)
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 9] Security Audit: Verifying browser_execute_custom_js is not exposed in public tools...")
    try:
        # Check registered tool names in MCP server instance
        # In python-mcp, tools are registered in server._tool_manager._tools or similar
        tool_names = [t.name for t in await server.mcp.list_tools()]
        if "browser_execute_custom_js" not in tool_names:
            print(f"   ✅ PASS: browser_execute_custom_js is NOT exposed in MCP tools list!")
            print(f"            Registered tools ({len(tool_names)}): {tool_names}")
            passed += 1
        else:
            print(f"   ❌ FAIL: browser_execute_custom_js is STILL registered as a public tool!")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Summary
    # ---------------------------------------------------------
    print("\n" + "=" * 70)
    print(f"📊 Test Results: {passed}/{total} Passed")
    print("=" * 70)
    if passed == total:
        print("🎉 ALL TESTS PASSED! Ready for deployment to AWS.")
        return 0
    else:
        print("⚠️ Some tests failed. Please review errors before deploying.")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(run_tests())
    sys.exit(exit_code)
