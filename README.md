# Robinhood Agentic Trading — AWS Lambda Setup

Automated daily trading via Robinhood's MCP server + Claude + AWS Lambda.

## Architecture

```
EventBridge (daily cron)
    └─▶ Lambda (handler.py)
            └─▶ Claude (claude-sonnet-4-20250514)
                    └─▶ Robinhood MCP (agent.robinhood.com/mcp/trading)
                            └─▶ Your Agentic Account
```

---

## Step 0 — Prerequisites

- Robinhood account with Agentic Trading access ([join waitlist](https://robinhood.com/agentic))
- AWS account with CLI configured (`aws configure`)
- Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- Python 3.12

---

## Step 1 — Authenticate with Robinhood (LOCAL, one-time)

The Robinhood MCP uses OAuth — you can't do the browser flow from Lambda.
Do it once locally, grab the token, store it in AWS.

```bash
pip install mcp anthropic httpx
python local_auth/local_auth.py
```

This opens your browser → log in to Robinhood → authorize the agent.
The script then prints your token and the AWS CLI commands to store it.

> ⚠️ Tokens expire (typically 24h–7 days). You'll need to re-run this
> and update the Lambda env var when it expires. Consider a Lambda
> that refreshes the token automatically if Robinhood supports refresh tokens.

---

## Step 2 — Store secrets in AWS

```bash
# Store in Secrets Manager (recommended)
aws secretsmanager create-secret \
  --name "robinhood-agent/rh-auth-token" \
  --secret-string "YOUR_TOKEN_HERE"

# Or just set directly as Lambda env vars (simpler for testing)
```

---

## Step 3 — Deploy Lambda

```bash
chmod +x deploy.sh

# First time: set your IAM role ARN in deploy.sh or as env var
export LAMBDA_ROLE_ARN="arn:aws:iam::YOUR_ACCOUNT_ID:role/robinhood-lambda-role"

# If you don't have a role yet, create one with Terraform first:
cd terraform && terraform init && terraform apply && cd ..

# Deploy
./deploy.sh robinhood-trading-agent us-east-1
```

---

## Step 4 — Set Lambda Environment Variables

```bash
aws lambda update-function-configuration \
  --function-name robinhood-trading-agent \
  --timeout 300 \
  --environment "Variables={
    ANTHROPIC_API_KEY=sk-ant-YOUR_KEY,
    RH_AUTH_TOKEN=YOUR_ROBINHOOD_TOKEN,
    RH_ACCOUNT_NUMBER=YOUR_AGENTIC_ACCOUNT_NUMBER,
    DEFAULT_PROMPT=Buy $50 of SPY if the market is green, otherwise hold.
  }"
```

---

## Step 5 — Schedule with EventBridge

```bash
cd terraform
terraform apply
```

This creates a cron that fires at 9:31 AM ET every weekday.

Or manually via CLI:
```bash
aws events put-rule \
  --name "robinhood-daily-trade" \
  --schedule-expression "cron(31 14 ? * MON-FRI *)" \
  --state ENABLED

aws events put-targets \
  --rule "robinhood-daily-trade" \
  --targets '[{
    "Id": "1",
    "Arn": "arn:aws:lambda:us-east-1:ACCOUNT:function:robinhood-trading-agent",
    "Input": "{\"prompt\": \"Make one trade based on today'\''s market conditions.\"}"
  }]'
```

---

## Testing

```bash
# Test with custom prompt
aws lambda invoke \
  --function-name robinhood-trading-agent \
  --payload '{"prompt": "Buy $25 of AAPL."}' \
  --cli-binary-format raw-in-base64-out \
  output.json && cat output.json

# Check logs
aws logs tail /aws/lambda/robinhood-trading-agent --follow
```

---

## Prompt examples

Set any of these as `DEFAULT_PROMPT` or pass in the Lambda event:

```
"Buy $50 of SPY every market day."

"Check my portfolio. If tech is down more than 2% today, buy $100 of QQQ. Otherwise hold."

"Look at today's market news and buy $50 of whichever of AAPL, MSFT, GOOGL had the worst week."

"Rebalance my portfolio to 60% SPY, 40% BND using whatever buying power is available."

"Run a momentum strategy: buy $50 of the S&P 500 ETF if it's up in the last 5 days."
```

---

## Token Refresh

Robinhood OAuth tokens expire. Options:
1. **Manual**: Re-run `local_auth.py` and update env var (simplest).
2. **Automated**: If Robinhood provides refresh tokens, build a second Lambda
   that refreshes every 12h and writes to Secrets Manager.
3. **Webhook**: Set up a Robinhood webhook to notify you when auth expires.

---

## Costs (estimated)

| Service | Cost |
|---------|------|
| Lambda (300s × 30 runs/mo) | ~$0.05/mo |
| CloudWatch Logs | ~$0.01/mo |
| Anthropic API (claude-sonnet) | ~$0.10–0.50/run |
| **Total** | **~$3–15/mo** |

---

## ⚠️ Risk Disclaimer

You are responsible for all trades placed by the agent. Monitor your
Robinhood app regularly. Start with small dollar amounts until you've
validated the agent behaves as expected. Robinhood explicitly states
users bear full responsibility for AI-placed trades.
