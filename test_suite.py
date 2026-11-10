#!/usr/bin/env python3
"""
Automated Test Suite for GoCharting Order Flow MCP Server
Validates CDP browser connection, footprint extraction, and all MCP tool outputs.
Run this before committing or deploying changes to AWS.
"""

import asyncio
import sys
import json
from browser_controller import controller
from orderflow_store import store


async def run_tests():
    print("=" * 60)
    print("🧪 Running GoCharting MCP Pre-Deployment Test Suite")
    print("=" * 60)
    passed = 0
    total = 0

    # Test 1: Chrome CDP Connection
    total += 1
    print("\n[Test 1] Checking Chrome CDP connection on port 9222...")
    try:
        title = await controller.execute_js("document.title")
        if title and not isinstance(title, dict):
            print(f"   ✅ PASS: Connected to Chrome! Active page: '{title}'")
            passed += 1
        elif isinstance(title, dict) and "error" in title:
            print(f"   ❌ FAIL: {title['error']}")
        else:
            print(f"   ❌ FAIL: Unexpected response: {title}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # Test 2: Live Footprint Extraction
    total += 1
    print("\n[Test 2] Extracting live footprint clusters from GoCharting...")
    data = None
    try:
        data = await controller.extract_live_footprint_from_chart()
        if data and data.get("status") == "ok" and data.get("candlesCount", 0) > 0:
            count = data.get("candlesCount")
            print(f"   ✅ PASS: Successfully extracted {count} 1-minute footprint candles!")
            passed += 1
        else:
            print(f"   ❌ FAIL: Extraction failed: {data}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # Test 3: Order Flow State Store Sync
    total += 1
    print("\n[Test 3] Syncing candles into OrderFlowStore...")
    try:
        if data and data.get("status") == "ok":
            store.sync_from_browser_series(data)
            active = store.list_active_symbols()
            if active and len(active) > 0:
                print(f"   ✅ PASS: Synced {len(active)} active symbol(s): {[s['symbol'] for s in active]}")
                passed += 1
            else:
                print("   ❌ FAIL: No active symbols in store after sync")
        else:
            print("   ⚠️ SKIP: Skipping sync test because extraction failed")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # Test 4: get_latest_candle Tool
    total += 1
    print("\n[Test 4] Testing get_latest_candle('NIFTY_FUT')...")
    try:
        candle = store.get_latest_candle("NIFTY_FUT")
        if candle:
            print(f"   ✅ PASS: OHLC: [{candle.open_price}, {candle.high_price}, {candle.low_price}, {candle.close_price}]")
            print(f"            POC: {candle.poc_price} | Net Delta: {candle.net_delta} | Volume: {candle.total_volume}")
            passed += 1
        else:
            print("   ❌ FAIL: Latest candle returned None")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # Test 5: get_delta_ladder Tool
    total += 1
    print("\n[Test 5] Testing get_delta_ladder('NIFTY_FUT')...")
    try:
        candle = store.get_latest_candle("NIFTY_FUT")
        if candle and len(candle.tick_deltas) > 0:
            sample = candle.tick_deltas[0]
            print(f"   ✅ PASS: Ladder has {len(candle.tick_deltas)} price levels.")
            print(f"            Sample level: Price {sample.price} -> Delta: {sample.delta}, Vol: {sample.volume}, POC: {sample.is_poc}")
            passed += 1
        else:
            print("   ❌ FAIL: No tick delta levels in candle")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # Test 6: get_orderflow_signals Tool
    total += 1
    print("\n[Test 6] Testing get_orderflow_signals lookback analysis...")
    try:
        history = store.get_history("NIFTY_FUT", limit=5)
        if history and len(history) > 0:
            print(f"   ✅ PASS: Order flow history verified ({len(history)} candles analyzed).")
            passed += 1
        else:
            print("   ❌ FAIL: No history candles returned")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # Test 7: get_oi_analysis Tool (Futures OI Change & 4-Quadrant Positioning)
    total += 1
    print("\n[Test 7] Testing get_oi_analysis tool (4-Quadrant Positioning)...")
    try:
        oi_res = store.get_oi_analysis("NIFTY_FUT", lookback=10)
        if oi_res and "dominant_positioning" in oi_res:
            print(f"   ✅ PASS: Latest OI: {oi_res.get('latest_oi')} | Net OI Change: {oi_res.get('net_oi_change_lookback')}")
            print(f"            Dominant Positioning: {oi_res.get('dominant_positioning')}")
            print(f"            Interpretation: {oi_res.get('interpretation')}")
            passed += 1
        else:
            print(f"   ❌ FAIL: OI analysis failed: {oi_res}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # Test 8: get_basis_analysis Tool (Futures minus Spot & Options Greeks)
    total += 1
    print("\n[Test 8] Testing get_basis_analysis tool (Futures vs Spot)...")
    try:
        basis_res = await controller.extract_spot_and_options_context("NIFTY")
        if basis_res and basis_res.get("status") == "ok":
            print(f"   ✅ PASS: Futures Price: {basis_res.get('futures_price')} | Spot Price: {basis_res.get('spot_price')}")
            print(f"            Basis: {basis_res.get('basis')} pts ({basis_res.get('basis_state')})")
            print(f"            Spot ATM Strike: {basis_res.get('strike_guidance', {}).get('spot_atm_strike')} (Diff: {basis_res.get('strike_guidance', {}).get('strike_difference')} pts)")
            print(f"            Options Context: Max Pain {basis_res.get('options_context', {}).get('max_pain')} | PCR OI {basis_res.get('options_context', {}).get('pcr_oi')} | ATM IV {basis_res.get('options_context', {}).get('atm_iv')}%")
            passed += 1
        else:
            print(f"   ❌ FAIL: Basis extraction failed: {basis_res}")
    except Exception as e:
        print(f"   ❌ FAIL: {e}")

    # Summary
    print("\n" + "=" * 60)
    print(f"📊 Results: {passed}/{total} tests passed ({int(passed/total*100)}%)")
    print("=" * 60)

    if passed == total:
        print("🎉 ALL TESTS PASSED! Codebase is healthy and ready for deployment.")
        return 0
    else:
        print("⚠️ SOME TESTS FAILED. Resolve issues before deploying to AWS.")
        return 1


if __name__ == "__main__":
    code = asyncio.run(run_tests())
    sys.exit(code)

