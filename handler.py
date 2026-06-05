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
NOTIFY_EMAIL      = os.environ.get("NOTIFY_EMAIL", "vmassi10@gmail.com")


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
    cash_available = os.environ.get("CASH_AVAILABLE", "unknown — call get_portfolio")
    positions = os.environ.get("CURRENT_POSITIONS", "unknown — call get_portfolio")
    open_orders = os.environ.get("OPEN_ORDERS", "unknown — call get_portfolio")
    allowed_instruments = os.environ.get("ALLOWED_INSTRUMENTS", "US equities and ETFs including fractional shares")
    initial_capital = os.environ.get("INITIAL_CAPITAL", "50")

    # 4. System prompt
    system = f"""You are an aggressive daily rotation trader managing a ${initial_capital} compounding fund. This is a cash account — trades settle T+1, meaning cash from yesterday's sales is available today. Your job every single morning: sell anything that is no longer the best use of capital, then deploy ALL available settled cash into today's single strongest opportunity.

## FUND RULES
- Fixed capital pool: ${initial_capital} starting value — no deposits, no withdrawals, only compounding
- Total portfolio value = settled cash + current positions (fetch via get_portfolio)
- Deploy 100% of available settled cash every run — idle cash is wasted opportunity
- ALL profits stay in and compound — never leave cash sitting

## ACCOUNT STATE
- Date: {market_date}
- Settled cash available: {cash_available}
- Current positions: {positions}
- Open orders: {open_orders}
- Allowed instruments: {allowed_instruments}

## PROCESS — follow every step in order

### STEP 1 — Audit current positions
Call get_portfolio. For each open position ask:
- Is this still the strongest thing I could own today?
- Is it down more than 5% from my entry? → Sell immediately, cut the loss
- Is it up and showing weakness or losing momentum? → Sell, lock the gain
- Is it still the best setup in the market right now? → Hold only if yes
Sell any position that fails this test using place_equity_order (side: sell).

### STEP 2 — Find today's best single setup
With all available settled cash identified, find the ONE best trade for today:
- Scan for the strongest relative strength mover (up more than the broad market, high volume)
- Prioritize stocks with a catalyst today: earnings beat, product launch, analyst upgrade, macro tailwind
- Check sector momentum — rotate into whichever sector is leading today
- Look for a clean technical setup: breakout above resistance, bounce off key support, gap-and-go with volume confirmation
- Score conviction 1–10. Must be 7+ to deploy. If nothing scores 7+, buy fractional shares of QQQ as a placeholder.

### STEP 3 — Size and execute
- Deploy 100% of available settled cash into your chosen stock
- Use fractional shares if needed (${initial_capital} is small — fractional lets you get full exposure)
- Use a limit order priced at or just above the current ask to guarantee a fast fill
- Place the buy via place_equity_order

## HARD RULES
1. ALWAYS sell underperformers first before buying — free up capital
2. ALWAYS deploy all settled cash — never end the run with cash sitting idle
3. Never fabricate prices or portfolio data — always call get_portfolio first
4. One concentrated position at a time — full capital into the best idea, not spread thin
5. Cut losses at -5% without hesitation — small account cannot absorb big drawdowns
6. Let winners run — only sell a winning position if something materially better exists today
7. Use limit orders — protect against bad fills on a small account where slippage matters more

## OUTPUT
Return ONLY valid JSON:
{{
  "scratchpad": {{
    "portfolio_value": 50.00,
    "positions_reviewed": [{{"symbol": "AAPL", "verdict": "sell", "reason": "lost momentum, -3%"}}],
    "todays_pick": "NVDA",
    "catalyst": "...",
    "relative_strength": "...",
    "technical_setup": "...",
    "conviction": 8
  }},
  "orders": [
    {{"symbol": "AAPL", "side": "sell", "quantity": 0.23, "order_type": "limit", "time_in_force": "day", "limit_price": 213.50}},
    {{"symbol": "NVDA", "side": "buy", "quantity": 0.18, "order_type": "limit", "time_in_force": "day", "limit_price": 138.00}}
  ],
  "rationale": "2-3 sentences on why today's pick is the strongest rotation target"
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
                    system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
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


# ── Email Summary ─────────────────────────────────────────────────────────────

def send_summary_email(result: dict, error: str = None):
    """Send a trade summary email via SES after each agent run."""
    ses = boto3.client("ses", region_name="us-east-1")
    date = datetime.utcnow().strftime("%A, %B %d %Y")

    if error:
        subject = f"🔴 Robinhood Agent — Run Failed {datetime.utcnow().strftime('%Y-%m-%d')}"
        body = f"The trading agent encountered a fatal error:\n\n{error}\n\nCheck CloudWatch logs for details."
    else:
        trades = result.get("trades_made", [])
        trade_count = result.get("trade_count", 0)
        summary = result.get("agent_summary", "No summary available.")

        # Build trades section
        if trades:
            trade_lines = []
            for t in trades:
                inp = t.get("input", {})
                res = t.get("result", {})
                side = inp.get("side", "?").upper()
                symbol = inp.get("symbol", "?")
                qty = inp.get("quantity", "?")
                order_type = inp.get("type", inp.get("order_type", "?"))
                limit = inp.get("limit_price", "market")
                filled_price = res.get("average_price") or res.get("price") if res else None
                status = res.get("state", "pending") if res else "unknown"
                line = f"  • {side} {qty} {symbol} @ {filled_price or limit} ({order_type}) — {status}"
                trade_lines.append(line)
            trades_section = "\n".join(trade_lines)
        else:
            trades_section = "  No trades placed this run."

        subject = f"📈 Robinhood Agent — {trade_count} trade(s) placed — {date}"
        body = f"""Daily Trading Agent Summary
===========================
Date: {date}
Trades placed: {trade_count}

TRADES
------
{trades_section}

AGENT REASONING
---------------
{summary}

STATS
-----
Agent iterations: {result.get('iterations', '?')}
Run timestamp: {result.get('timestamp', '?')} UTC
"""

    try:
        ses.send_email(
            Source=NOTIFY_EMAIL,
            Destination={"ToAddresses": [NOTIFY_EMAIL]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )
        print(f"[email] Summary sent to {NOTIFY_EMAIL}")
    except Exception as e:
        print(f"[email] Failed to send summary email: {e}")


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

        send_summary_email(result)

        return {
            "statusCode": 200,
            "body": json.dumps(result, indent=2),
        }

    except Exception as e:
        print(f"[handler] FATAL: {e}")
        send_summary_email({}, error=str(e))
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }
