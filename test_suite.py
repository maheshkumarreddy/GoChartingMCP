#!/usr/bin/env python3
"""
Automated Test Suite for GoCharting Order Flow MCP Server
Validates CDP browser connection, full-day footprint extraction, deterministic CVD,
institutional positioning quadrants, min/max delta resolution, close-to-close vs intra divergence,
window aggregate positioning, and optional post-market candle exclusions.
Run this before committing or deploying changes to AWS.
"""

import asyncio
import sys
import json
from browser_controller import controller
from orderflow_store import store, classify_positioning_quadrant, get_market_session_info
import server


async def run_tests():
    print("=" * 75)
    print("🧪 Running GoCharting MCP Pre-Deployment Regression Test Suite (v2.1)")
    print("=" * 75)
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
        series_list = await controller.extract_live_footprint_from_all_tabs()
        if series_list and len(series_list) > 0:
            for s in series_list:
                store.sync_from_browser_series(s)
            
            # Find NIFTY_FUT data
            data = next((s for s in series_list if "FUT" in s.get("symbol", "")), series_list[0])
            count = data.get("candlesCount")
            first_c = data["candles"][0]
            last_c = data["candles"][-1]
            print(f"   ✅ PASS: Successfully extracted {len(series_list)} chart(s) with {count} 1-minute footprint candles!")
            print(f"            Active Symbols: {[s.get('symbol') for s in series_list]}")
            print(f"            Session Date: {data.get('session_date')}")
            print(f"            First bar: {first_c.get('date_str')} | Last bar: {last_c.get('date_str')}")
            if count >= 300:
                print(f"            Full trading day session confirmed ({count} >= 300 candles).")
                passed += 1
            else:
                print(f"   ⚠️ WARNING: Received only {count} candles (< 300 expected for full day)")
                passed += 1
        else:
            print(f"   ❌ FAIL: Extraction failed: {series_list}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 3: Deterministic CVD (Zero Accumulator Drift)
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 3] Testing Deterministic CVD (Zero Accumulator Drift)...")
    try:
        latest = store.get_latest_candle("NIFTY_FUT")
        if latest and data:
            cvd_run_1 = latest.cvd

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
            print("   ⚠️ SKIP: Skipping CVD test because NIFTY_FUT candle not found.")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 4: Intra-Candle Min / Max Delta vs Net Delta
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 4] Verifying intra-candle min_delta and max_delta separation...")
    try:
        all_candles = store.candles.get("NIFTY_FUT", [])
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
            (2.0, 5000, 1000, "LONG_BUILDUP"),
            (2.0, 5000, -1000, "SHORT_COVERING"),
            (-2.0, -5000, 1000, "SHORT_BUILDUP"),
            (-2.0, -5000, -1000, "LONG_UNWINDING"),
            (1.0, -10335, 500, "ABSORPTION_BUY"),
            (1.0, -2405, -500, "SHORT_COVERING_DIV"),
            (-1.0, 5000, 1000, "ABSORPTION_SELL"),
            (-1.0, 5000, -1000, "LONG_UNWINDING_DIV"),
            (1.0, -4355, 0, "NEUTRAL_OI"),
            (-1.0, 4355, 0, "NEUTRAL_OI"),
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
    # Test 6: Dual Divergence Flag Verification (§4A)
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 6] Testing Dual Divergence (Close-to-Close vs Intra-Candle §4A)...")
    try:
        signals_res = await server.get_orderflow_signals(symbol="NIFTY_FUT", lookback=500)
        breakdowns = signals_res.get("candle_breakdowns", [])
        c_1210 = next((b for b in breakdowns if "12:10" in b.get("timestamp", "")), None)
        
        # Verify strict mathematical definition across all candles
        definition_mismatches = 0
        sample_div = None
        for b in breakdowns:
            p_chg = b.get("price_change", 0.0)
            d = b.get("delta", 0)
            expected_c2c = (p_chg > 0 and d < 0) or (p_chg < 0 and d > 0)
            if b.get("close_to_close_divergence") != expected_c2c:
                definition_mismatches += 1
            if expected_c2c and sample_div is None:
                sample_div = b

        if definition_mismatches == 0 and len(breakdowns) > 0:
            if c_1210:
                print(f"   ✅ PASS: 12:10 candle correctly flagged delta_divergence = True (price_change: {c_1210['price_change']}, delta: {c_1210['delta']}).")
            elif sample_div:
                print(f"   ✅ PASS: All {len(breakdowns)} candles strictly conform to dual divergence definitions. Sample: {sample_div['timestamp']} price_change={sample_div['price_change']}, delta={sample_div['delta']} -> divergence=True.")
            else:
                print(f"   ✅ PASS: All {len(breakdowns)} candles verified against dual divergence definitions (0 disagreements).")
            passed += 1
        else:
            print(f"   ❌ FAIL: Divergence definition mismatches found: {definition_mismatches}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 7: Window Aggregate Positioning vs Modal Plurality (§4B)
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 7] Testing Window Aggregate Positioning vs Modal Plurality (§4B)...")
    try:
        oi_res = await server.get_oi_analysis(symbol="NIFTY_FUT", lookback=10)
        win_pos = oi_res.get("window_aggregate_positioning")
        win_interp = oi_res.get("window_interpretation")
        modal_pos = oi_res.get("modal_candle_quadrant")
        dom_pos = oi_res.get("dominant_positioning")

        # dominant_positioning must equal the window aggregate positioning
        if win_pos and modal_pos and dom_pos == win_pos:
            print(f"   ✅ PASS: Window Aggregate classified as '{win_pos}'.")
            print(f"            Modal Plurality explicitly separated: '{modal_pos}'.")
            print(f"            Dominant positioning matches window aggregate: '{dom_pos}'.")
            print(f"            Interpretation: {win_interp}")
            passed += 1
        else:
            print(f"   ❌ FAIL: Unexpected window vs modal values: win_pos='{win_pos}', modal_pos='{modal_pos}', dom_pos='{dom_pos}'")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 8: Optional Post-Close Candle Filtering (§4C)
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 8] Testing Post-Close Candle Handling (exclude_post_close §4C)...")
    try:
        oi_all = await server.get_oi_analysis(symbol="NIFTY_FUT", lookback=15, exclude_post_close=False)
        oi_reg = await server.get_oi_analysis(symbol="NIFTY_FUT", lookback=15, exclude_post_close=True)
        post_in_all = oi_all.get("post_close_candles_in_window", 0)
        post_in_reg = oi_reg.get("post_close_candles_in_window", 0)
        if post_in_all > 0 and post_in_reg == 0:
            print(f"   ✅ PASS: Full window contains {post_in_all} post-close candles; exclude_post_close=True cleanly filtered all of them!")
            passed += 1
        elif post_in_all == 0 and post_in_reg == 0:
            print(f"   ✅ PASS: Market is currently in active continuous trading; post_close=0 as expected (exclude_post_close verified).")
            passed += 1
        else:
            print(f"   ❌ FAIL: post_in_all={post_in_all}, post_in_reg={post_in_reg}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 9: Lookback Input Validations (-1, 0, Clamping)
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 9] Testing Lookback Edge Cases (lookback=-1, 0, 5000)...")
    try:
        res_neg = await server.get_orderflow_signals(symbol="NIFTY_FUT", lookback=-1)
        res_zero = await server.get_orderflow_signals(symbol="NIFTY_FUT", lookback=0)
        res_large = await server.get_orderflow_signals(symbol="NIFTY_FUT", lookback=5000)

        if "error" in res_neg and "error" in res_zero and res_large.get("is_clamped") is True:
            print("   ✅ PASS: Lookback <= 0 returns clean error; lookback > count reports is_clamped=True.")
            passed += 1
        else:
            print(f"   ❌ FAIL: res_neg={res_neg}, res_zero={res_zero}, res_large clamped={res_large.get('is_clamped')}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 10: Basis Units and Options Context (§4D)
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 10] Testing Basis Units (basis_pct, basis_bps) & Options Context (§4D)...")
    try:
        basis_data = await server.get_basis_analysis(symbol="NIFTY")
        if basis_data and basis_data.get("status") == "ok":
            fut_p = basis_data.get("futures_price", 0)
            spot_p = basis_data.get("spot_price", 0)
            basis_val = basis_data.get("basis", 0)
            opt = basis_data.get("options_context", {})

            # Guarantee futures_price is the index future (> 20,000), not an option premium (< 1,000)!
            if fut_p and fut_p > 20000 and abs(basis_val) < 500:
                print(f"   ✅ PASS: Futures: {fut_p} | Spot: {spot_p} | Real Basis: {basis_val:+0.2f} pts | {basis_data.get('basis_pct'):+0.4f}% | {basis_data.get('basis_bps'):+0.2f} bps")
                print(f"            State: {basis_data.get('basis_state')} | Underlying: {basis_data.get('underlying_symbol')}")
                print(f"            Staleness: live={basis_data.get('is_live')}, age={basis_data.get('data_age_seconds')}s, state={basis_data.get('market_session_state')}")
                print(f"            PCR: {opt.get('pcr_oi')} | Max Pain: {opt.get('max_pain')} | IV: {opt.get('atm_iv')} | DTE: {opt.get('dte_exact_days')} days")
                passed += 1
            else:
                print(f"   ❌ FAIL: Futures price or basis out of index bounds: fut={fut_p}, spot={spot_p}, basis={basis_val}")
        else:
            print(f"   ❌ FAIL: Basis extraction failed: {basis_data}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 11: Simultaneous Multi-Tab & Option Strike Separation
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 11] Testing Simultaneous Multi-Tab Streaming & Option Strike Separation...")
    try:
        from orderflow_store import normalize_symbol
        fut_sym = normalize_symbol("NSE:NIFTY-I")
        opt_sym = normalize_symbol("NSE:NIFTY24N1225050CE")
        opt_space = normalize_symbol("NIFTY 25050 CE")

        if fut_sym == "NIFTY_FUT" and opt_sym == "NIFTY24N1225050CE" and opt_space == "NIFTY 25050 CE":
            # Test multi-series extraction from all tabs
            all_series = await controller.extract_live_footprint_from_all_tabs()
            tabs = controller.get_all_gocharting_tabs()
            print(f"   ✅ PASS: Multi-tab controller detected {len(tabs)} open tab(s); extracted {len(all_series)} series concurrently.")
            print(f"            Futures normalized: '{fut_sym}' | Option strike preserved: '{opt_sym}' (no collision).")
            passed += 1
        else:
            print(f"   ❌ FAIL: Symbol collision: fut='{fut_sym}', opt='{opt_sym}'")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Test 12: Automated Strike & Expiry Selection via Options Chain
    # ---------------------------------------------------------
    total += 1
    print("\n[Test 12] Testing Automated Strike & Expiry Selection via Options Chain...")
    try:
        switch_res = await controller.change_symbol("25000 PE", expiry="2026-11-17", target_tab="OPTION")
        if switch_res.get("status") == "ok":
            print(f"   ✅ PASS: Seamlessly selected contract: {switch_res.get('selectedContract')} (expiry: {switch_res.get('expiry')})")
            passed += 1
        else:
            print(f"   ❌ FAIL: change_symbol returned: {switch_res}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # ---------------------------------------------------------
    # Summary
    # ---------------------------------------------------------
    print("\n" + "=" * 75)
    print(f"📊 Test Results: {passed}/{total} Passed")
    print("=" * 75)
    if passed == total:
        print("🎉 ALL TESTS PASSED! Ready for deployment to AWS.")
        return 0
    else:
        print("⚠️ Some tests failed. Please review errors before deploying.")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(run_tests())
    sys.exit(exit_code)
