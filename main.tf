# terraform/main.tf
# Deploys the EventBridge rule that fires the Lambda daily at market open
# Usage: terraform init && terraform apply

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region"       { default = "us-east-1" }
variable "function_name"    { default = "robinhood-trading-agent" }
variable "daily_prompt"     { default = "Audit my current positions, sell anything that is no longer the best use of capital, and deploy all available settled cash into today's single strongest opportunity." }

# ── IAM Role for Lambda ───────────────────────────────────────────────────────

resource "aws_iam_role" "lambda_role" {
  name = "robinhood-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Optional: allow Lambda to read from Secrets Manager
resource "aws_iam_role_policy" "secrets_access" {
  name = "robinhood-secrets-access"
  role = aws_iam_role.lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = "arn:aws:secretsmanager:${var.aws_region}:*:secret:robinhood-agent/*"
    }]
  })
}

# ── EventBridge Rule: Fire at 9:31 AM ET (14:31 UTC) on weekdays ─────────────

resource "aws_cloudwatch_event_rule" "daily_trade" {
  name                = "robinhood-daily-trade"
  description         = "Trigger trading agent at market open (M-F)"
  # 9:31 AM ET = 14:31 UTC (13:31 during EDT)
  # Adjust for DST: use 14:31 (EST) and accept ~1h offset in summer
  schedule_expression = "cron(31 14 ? * MON-FRI *)"
}

resource "aws_cloudwatch_event_target" "lambda_target" {
  rule      = aws_cloudwatch_event_rule.daily_trade.name
  target_id = "RobinhoodTradingAgent"
  arn       = data.aws_lambda_function.trading_agent.arn

  input = jsonencode({
    prompt = var.daily_prompt
    source = "eventbridge-scheduled"
  })
}

data "aws_lambda_function" "trading_agent" {
  function_name = var.function_name
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_trade.arn
}

# ── CloudWatch Log Group ──────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "agent_logs" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = 30
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "lambda_role_arn" {
  value       = aws_iam_role.lambda_role.arn
  description = "Use this ARN when creating the Lambda function"
}

output "eventbridge_rule_arn" {
  value = aws_cloudwatch_event_rule.daily_trade.arn
}
