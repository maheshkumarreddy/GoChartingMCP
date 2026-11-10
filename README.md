# GoCharting Order Flow MCP Server (for Muse AI & LLM Agents)

High-performance **Model Context Protocol (MCP)** server providing **1-minute Footprint**, **Tick Delta ladders**, and **Order Flow pattern signals** for Indian Nifty Futures, Bank Nifty Futures, and Options from GoCharting.

---

## 🌟 Key Features

1. **Token-Efficient Tick Delta Ladders**:
   Instead of bulky Bid $\times$ Ask matrices, the server calculates price-by-price net delta:
   $$\text{Delta} = \text{Aggressive Market Buyers} - \text{Aggressive Market Sellers}$$
   This cuts context token usage by over 60% while providing instant clarity to the LLM.
2. **Dynamic Tick Sizing**:
   Dynamically scales tick intervals for **Nifty** (1.0 / 2.0 / 5.0 pt), **Bank Nifty** (5.0 / 10.0 / 20.0 pt), or Options directly from GoCharting's metadata.
3. **Multi-Chart & Options Switching**:
   Supports dynamic chart switching via `switch_chart` (e.g. LLM spots Nifty Future setup $\to$ queries `NIFTY 24300 CE` order flow).
4. **Fast In-Memory Architecture**:
   Stores sliding 1-minute candle history in RAM (< 10 MB usage) with sub-millisecond query latency.
5. **SSE Transport for Muse AI**:
   Exposes standard Server-Sent Events (SSE) at `http://localhost:8765/sse`.

---

## 📁 Project Structure

```
/Users/mac/Desktop/GoChartingMCP/
├── server.py              # Main MCP Server (FastAPI + MCPServer with SSE)
├── orderflow_store.py     # In-memory engine, Tick Delta calculator & signals
├── browser_hook.js        # Tampermonkey / Chrome Userscript for GoCharting
├── test_client.py         # Verification and test suite
└── .venv/                 # Python 3.11 Virtual Environment
```

---

## 🚀 How to Run

### Step 1: Launch the Dedicated "Bot" Chrome Browser
We have created an isolated Chrome instance so your personal browsing remains 100% separate:
```bash
cd /Users/mac/Desktop/GoChartingMCP
./launch_bot_chrome.sh
```
* Uses an isolated profile in `bot_chrome_profile/`.
* Your personal Chrome profile, tabs, and history are **never touched**.
* Log in to GoCharting **once** in this dedicated browser window, open your Nifty Futures chart, and set it to **1-minute footprint**.

### Step 2: Install Browser Hook in the Bot Browser
1. In the newly opened Bot Chrome window, install the **Tampermonkey** extension.
2. Create a new script, paste the contents of `browser_hook.js`, and save.
3. Refresh the GoCharting tab.

### Step 3: Start the MCP Server
```bash
cd /Users/mac/Desktop/GoChartingMCP
./.venv/bin/python3 server.py
```
The server will start on port `8765`:
* **MCP SSE Endpoint**: `http://localhost:8765/sse`
* **Browser Bridge**: `http://localhost:8765/api/feed`
* **Health Check**: `http://localhost:8765/api/status`

---

## 🤖 Configuring Muse AI to Use This MCP Server

In your **Muse AI Agent settings / MCP configuration**, add the MCP server using SSE:

```json
{
  "mcpServers": {
    "gocharting-orderflow": {
      "url": "http://localhost:8765/sse",
      "transport": "sse"
    }
  }
}
```

---

## 🛠️ Available MCP Tools for Muse AI

| Tool | Parameters | Description |
| :--- | :--- | :--- |
| `get_latest_candle` | `symbol="NIFTY_FUT"` | OHLCV, Net Delta, Min/Max Delta, POC price, CVD, and instant signals. |
| `get_delta_ladder` | `symbol="NIFTY_FUT"`, `candle_index=0` | Price-by-price **Tick Delta matrix** with flagged POC and imbalances. |
| `get_orderflow_signals` | `symbol="NIFTY_FUT"`, `lookback=5` | Multi-candle delta convergence/divergence and absorption patterns. |
| `switch_chart` | `symbol="NIFTY 24300 CE"`, `timeframe="1m"` | Dispatches a chart switch command to GoCharting for a requested strike/future. |
| `list_active_charts` | None | Lists all symbols currently streaming in memory. |

