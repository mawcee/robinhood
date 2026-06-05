#!/usr/bin/env python3
"""
local_auth.py — Run this ONCE on your local machine, not on Lambda.
──────────────────────────────────────────────────────────────────────
Handles Robinhood MCP OAuth via the MCP Python SDK's OAuthClientProvider.
Opens your browser for the Robinhood login flow, then extracts and prints
the token so you can store it in AWS.

Usage:
    pip install mcp anthropic httpx
    python3.12 local_auth.py
"""

import asyncio
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
except ImportError:
    print("Install with: pip install mcp")
    sys.exit(1)


ROBINHOOD_MCP_URL = "https://agent.robinhood.com/mcp/trading"
REDIRECT_PORT = 3456
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
TOKEN_FILE = os.path.expanduser("~/.mcp/tokens/robinhood.json")


class FileTokenStorage:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError):
                return {}
        return {}

    def _save(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2)

    async def get_tokens(self) -> OAuthToken | None:
        data = self._load()
        if "tokens" in data:
            return OAuthToken(**data["tokens"])
        return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json")
        self._save(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = self._load()
        if "client_info" in data:
            return OAuthClientInformationFull(**data["client_info"])
        return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json")
        self._save(data)


def start_callback_server(loop: asyncio.AbstractEventLoop):
    future: asyncio.Future = loop.create_future()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/callback":
                params = parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                state = params.get("state", [None])[0]

                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Authentication successful!</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                    b"</body></html>"
                )

                if not future.done():
                    loop.call_soon_threadsafe(future.set_result, (code, state))

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("localhost", REDIRECT_PORT), CallbackHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return future, server


async def authenticate_and_extract_token():
    print("=" * 60)
    print("Robinhood MCP Authentication")
    print("=" * 60)
    print()

    loop = asyncio.get_event_loop()
    callback_future, callback_server = start_callback_server(loop)

    async def redirect_handler(url: str) -> None:
        print("Opening browser for Robinhood OAuth...")
        webbrowser.open(url)
        print("If the browser didn't open, visit this URL manually:")
        print(f"  {url}")
        print()

    async def callback_handler() -> tuple[str, str | None]:
        print("Waiting for OAuth callback on localhost:" + str(REDIRECT_PORT) + "...")
        code, state = await asyncio.wait_for(callback_future, timeout=300)
        callback_server.shutdown()
        print("✓ OAuth callback received.")
        return code, state

    storage = FileTokenStorage(TOKEN_FILE)

    auth = OAuthClientProvider(
        server_url=ROBINHOOD_MCP_URL,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[REDIRECT_URI],
            client_name="Robinhood Trading Agent",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    async with streamablehttp_client(url=ROBINHOOD_MCP_URL, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            print()
            print("✓ Successfully authenticated with Robinhood!")
            print()

            tools_result = await session.list_tools()
            print(f"Available MCP tools ({len(tools_result.tools)}):")
            for tool in tools_result.tools:
                desc = (tool.description or "")[:60]
                print(f"  • {tool.name}: {desc}")
            print()

            account_number = "UNKNOWN"
            print("Fetching your Robinhood Agentic account info...")
            try:
                accounts_result = await session.call_tool("get_accounts", arguments={})
                accounts_data = json.loads(accounts_result.content[0].text)
                accounts = (
                    accounts_data
                    if isinstance(accounts_data, list)
                    else accounts_data.get("results", [])
                )
                agentic = [
                    a for a in accounts
                    if "agentic" in str(a.get("type", "")).lower()
                    or "agentic" in str(a.get("account_type", "")).lower()
                ]
                if agentic:
                    account_number = agentic[0].get("account_number") or agentic[0].get("id")
                    print(f"✓ Found Agentic account: {account_number}")
                elif accounts:
                    account_number = accounts[0].get("account_number") or accounts[0].get("id", "UNKNOWN")
                    print(f"✓ Using first account: {account_number}")
                    print(f"  All accounts: {json.dumps([{k: a[k] for k in ('account_number','id','type','account_type') if k in a} for a in accounts], indent=2)}")
                else:
                    print("⚠ get_accounts returned empty. Enter your Robinhood account number manually.")
                    print("  Find it in the Robinhood app: Account → Settings → Account Information")
                    account_number = input("  Account number: ").strip() or "UNKNOWN"
            except Exception as e:
                print(f"Could not fetch accounts: {e}")
                account_number = "CHECK_ROBINHOOD_APP"

    tokens = await storage.get_tokens()
    token = tokens.access_token if tokens else None

    if not token:
        print("\n⚠ Token not found in storage. Check ~/.mcp/tokens/robinhood.json manually.")
        token = "EXTRACT_MANUALLY"
    else:
        print(f"\n✓ Token saved to: {TOKEN_FILE}")

    print()
    print("=" * 60)
    print("COPY THESE VALUES INTO AWS:")
    print("=" * 60)
    print()
    print('aws secretsmanager create-secret \\')
    print('  --name "robinhood-agent/rh-auth-token" \\')
    print(f'  --secret-string "{token}"')
    print()
    print('aws lambda update-function-configuration \\')
    print('  --function-name robinhood-trading-agent \\')
    print('  --environment "Variables={')
    print('    ANTHROPIC_API_KEY=your_anthropic_key,')
    print(f'    RH_AUTH_TOKEN={token},')
    print(f'    RH_ACCOUNT_NUMBER={account_number},')
    print("    DEFAULT_PROMPT=Buy $50 of SPY if the market is up today otherwise hold")
    print('  }"')
    print()
    print("⚠ IMPORTANT: Tokens expire. Re-run this script when they do.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(authenticate_and_extract_token())
