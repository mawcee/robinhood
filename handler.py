"""
Robinhood Agentic Trading - AWS Lambda Handler
----------------------------------------------
Takes a string prompt, connects to Robinhood's MCP server via Claude,
and executes at least one trade per run.

Deploy this to Lambda and trigger it daily via EventBridge.
"""

import json
import os
import time
import boto3
import anthropic
import httpx
from datetime import datetime


# ── Config ────────────────────────────────────────────────────────────────────

ROBINHOOD_MCP_URL = "https://agent.robinhood.com/mcp/trading"
ANTHROPIC_MODEL   = "claude-sonnet-4-6"

# Pulled from env vars (set via Lambda or Secrets Manager)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
RH_AUTH_TOKEN     = os.environ.get("RH_AUTH_TOKEN")      # OAuth token from local auth step
RH_ACCOUNT_NUMBER = os.environ.get("RH_ACCOUNT_NUMBER")  # Your Agentic account number


# ── MCP Client ────────────────────────────────────────────────────────────────

def get_mcp_headers() -> dict:
    """Auth headers for the Robinhood MCP server."""
    return {
        "Authorization": f"Bearer {RH_AUTH_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def _parse_mcp_response(resp: httpx.Response) -> dict:
    """Handle both application/json and text/event-stream MCP responses."""
    body = resp.text.strip()
    if not body:
        return {}
    # SSE responses start with "data:" lines
    if resp.headers.get("content-type", "").startswith("text/event-stream") or body.startswith("data:"):
        for line in reversed(body.splitlines()):
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    try:
                        return json.loads(data)
                    except json.JSONDecodeError:
                        continue
        return {}
    return json.loads(body)


def call_mcp_tool(tool_name: str, tool_input: dict) -> dict:
    """
    Directly invoke a Robinhood MCP tool.
    The MCP server uses Streamable HTTP transport (JSON-RPC 2.0).
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": tool_input,
        },
    }

    resp = httpx.post(
        ROBINHOOD_MCP_URL,
        headers=get_mcp_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return _parse_mcp_response(resp)


def list_mcp_tools() -> list[dict]:
    """Discover what tools Robinhood's MCP server exposes."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }
    resp = httpx.post(
        ROBINHOOD_MCP_URL,
        headers=get_mcp_headers(),
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = _parse_mcp_response(resp)
    return data.get("result", {}).get("tools", [])


# ── Claude Agent Loop ─────────────────────────────────────────────────────────

def run_trading_agent(prompt: str) -> dict:
    """
    Run Claude as an agentic loop against the Robinhood MCP.
    Claude will call MCP tools autonomously until it's done.

    Returns a summary dict with trades made and reasoning.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 1. Discover available Robinhood tools
    rh_tools = list_mcp_tools()
    print(f"[agent] Discovered {len(rh_tools)} Robinhood MCP tools")

    # 2. Convert MCP tool definitions → Anthropic tool format
    anthropic_tools = []
    for tool in rh_tools:
        anthropic_tools.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool.get("inputSchema", {"type": "object", "properties": {}}),
        })

    # 3. Account state placeholders
    market_date = datetime.utcnow().strftime("%Y-%m-%d")
    cash_available = os.environ.get("CASH_AVAILABLE", "unknown")
    positions = os.environ.get("CURRENT_POSITIONS", "unknown")
    open_orders = os.environ.get("OPEN_ORDERS", "unknown")
    max_daily_risk_pct = os.environ.get("MAX_DAILY_RISK_PCT", "5")
    max_position_size_pct = os.environ.get("MAX_POSITION_SIZE_PCT", "10")
    allowed_instruments = os.environ.get("ALLOWED_INSTRUMENTS", "US equities and ETFs")
    initial_capital = os.environ.get("INITIAL_CAPITAL", "50")

    # 4. System prompt
    system = f"""You are a disciplined equity trader managing a ${initial_capital} compounding fund. Your sole objective is to grow this initial capital over time through smart, conservative trading. You have access to real-time market data, news, and a trade execution API.

## FUND RULES
- Initial capital: ${initial_capital}
- Total portfolio value = cash + current positions (fetch via get_portfolio)
- NEVER invest more than the total current portfolio value
- ALL profits stay in the account and compound — no withdrawals
- Size every trade as a percentage of total portfolio value, not just cash

## ACCOUNT STATE
- Date: {market_date}
- Cash available: {cash_available}
- Current positions: {positions}
- Open orders: {open_orders}
- Max daily risk: {max_daily_risk_pct}% per trade, {max_position_size_pct}% max position size
- Allowed instruments: {allowed_instruments}

## PROCESS
1. Call get_portfolio to get real current cash + portfolio value
2. Check macro regime (risk-on/off, VIX, yield curve)
3. Identify the strongest sector today
4. Pick 1-2 stock candidates — check fundamentals, technicals, and catalyst
5. Score conviction 1-10. Size: 8-10→full, 6-7→75%, 4-5→50%, 1-3→buy SPY minimally
6. Place the trade with place_equity_order (you MAY review_equity_order first, but MUST follow with place_equity_order)

## HARD RULES
1. Never exceed {max_daily_risk_pct}% of total portfolio value risk on one trade
2. Never exceed {max_position_size_pct}% of total portfolio value in one position
3. Only trade {allowed_instruments}
4. Never fabricate data
5. ALWAYS place at least one real trade via place_equity_order — no exceptions. No conviction? Buy a small amount of SPY.
6. Prefer limit orders when spread or volatility is elevated
7. Don't add to losing positions
8. Capital preservation comes first — a small gain beats a large loss

## OUTPUT
Return ONLY valid JSON:
{{
  "scratchpad": {{"macro": "...", "sector": "...", "candidates": ["T1","T2"], "bear_case": "...", "conviction": 7, "portfolio_value": 50.00}},
  "orders": [{{"symbol": "AAPL", "side": "buy", "quantity": 10, "order_type": "limit", "time_in_force": "day", "limit_price": 213.50, "stop_price": null}}],
  "rationale": "2-3 sentences on the fundamental, technical, and sentiment case"
}}"""

    messages = [{"role": "user", "content": prompt}]
    trades_made = []
    final_text = ""
    iterations = 0
    max_iterations = 20  # safety cap

    # 4. Agentic loop
    while iterations < max_iterations:
        iterations += 1
        print(f"[agent] Iteration {iterations}")

        for attempt in range(5):
            try:
                response = client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=4096,
                    system=system,
                    tools=anthropic_tools,
                    messages=messages,
                )
                break
            except anthropic.RateLimitError as e:
                wait = 15 * (2 ** attempt)
                print(f"[agent] Rate limited — retrying in {wait}s (attempt {attempt+1}/5)")
                time.sleep(wait)
                if attempt == 4:
                    raise

        # Append assistant response to history
        messages.append({"role": "assistant", "content": response.content})

        # Check stop reason
        if response.stop_reason == "end_turn":
            final_text = " ".join(b.text for b in response.content if hasattr(b, "text"))
            if len(trades_made) == 0:
                print("[agent] No trade placed — forcing one.")
                messages.append({
                    "role": "user",
                    "content": "You have not placed any trade yet. You MUST call place_equity_order exactly once right now. If you have no strong conviction, buy $25 of SPY. Do not respond with text — call the tool immediately.",
                })
                continue
            print("[agent] Agent finished naturally.")
            break

        if response.stop_reason != "tool_use":
            print(f"[agent] Unexpected stop_reason: {response.stop_reason}")
            break

        # Process tool calls
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name  = block.name
            tool_input = block.input
            tool_use_id = block.id

            print(f"[agent] Calling MCP tool: {tool_name}({json.dumps(tool_input)})")

            try:
                mcp_response = call_mcp_tool(tool_name, tool_input)
                result_content = json.dumps(mcp_response.get("result", mcp_response))

                # Track the trade; allow exactly one per run
                if tool_name == "place_equity_order":
                    trades_made.append({
                        "tool": tool_name,
                        "input": tool_input,
                        "result": mcp_response.get("result"),
                        "timestamp": datetime.utcnow().isoformat(),
                    })

            except Exception as e:
                result_content = json.dumps({"error": str(e)})
                print(f"[agent] Tool call failed: {e}")

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": result_content,
            })

        # Feed results back
        messages.append({"role": "user", "content": tool_results})

    return {
        "trades_made": trades_made,
        "trade_count": len(trades_made),
        "agent_summary": final_text,
        "iterations": iterations,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ── Lambda Entrypoint ─────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    Lambda entrypoint.

    Event schema:
    {
        "prompt": "Buy $50 of AAPL every day the market is open."
    }

    Can also be triggered by EventBridge with a fixed prompt stored in env:
    {
        "source": "aws.events"
    }
    """
    print(f"[handler] Event: {json.dumps(event)}")

    # Resolve prompt: from event, env var, or EventBridge default
    if "prompt" in event:
        prompt = event["prompt"]
    elif os.environ.get("DEFAULT_PROMPT"):
        prompt = os.environ["DEFAULT_PROMPT"]
    else:
        prompt = "Check my portfolio and make one conservative equity trade based on current market conditions."

    print(f"[handler] Running with prompt: {prompt}")

    # Validate required env vars
    missing = [v for v in ["ANTHROPIC_API_KEY", "RH_AUTH_TOKEN", "RH_ACCOUNT_NUMBER"] if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {missing}")

    try:
        result = run_trading_agent(prompt)

        # Warn if no trades were actually placed
        if result["trade_count"] == 0:
            print("[handler] WARNING: Agent completed without placing any trades!")
            result["warning"] = "No trades were confirmed. Review agent_summary for details."

        print(f"[handler] Done. Trades made: {result['trade_count']}")
        print(f"[handler] Summary: {result['agent_summary'][:500]}")

        return {
            "statusCode": 200,
            "body": json.dumps(result, indent=2),
        }

    except Exception as e:
        print(f"[handler] FATAL: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }
