# =============================================================================
# Aspire Rotation Control Plane
# =============================================================================
# Architecture:
#   n8n (scheduler) → API Gateway (control plane) → Step Functions (state machine)
#     → Lambda vendor adapters (CreateKey/TestKey/RevokeKey)
#     → Secrets Manager (AWSPENDING → AWSCURRENT)
#     → SNS (receipts + failure notifications)
#
# n8n NEVER holds vendor keys. It has a tightly-scoped IAM credential that
# can ONLY invoke the rotation API and describe secret metadata.
#
# The Step Functions state machine implements the 7-step rotation flow:
#   1. CreateNewKey (vendor adapter)
#   2. WritePendingVersion (AWSPENDING)
#   3. TestNewKey (synthetic API call)
#   4. PromoteToCurrent (AWSCURRENT)
#   5. CutoverVerification (service-level checks)
#   6. RevokeOldKey (after overlap window)
#   7. EmitReceipt (append-only)
# =============================================================================

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

# Use remote state from secrets-manager module for ARNs
data "terraform_remote_state" "secrets" {
  backend = "s3"
  config = {
    bucket = "aspire-terraform-state"
    key    = "secrets-manager/terraform.tfstate"
    region = "us-east-1"
  }
}

locals {
  account_id     = var.aws_account_id
  environment    = var.environment
  rotation_role  = data.terraform_remote_state.secrets.outputs.rotation_execution_role_arn
  kms_key_arn    = data.terraform_remote_state.secrets.outputs.kms_key_arn
  secret_arns    = data.terraform_remote_state.secrets.outputs.secret_arns

  # Rotation schedules per provider
  rotation_config = {
    stripe = {
      secret_arn  = local.secret_arns.stripe
      adapter     = "stripe"
      schedule    = "rate(30 days)"
      risk_tier   = "red"
      overlap_min = 60 # 1-hour dual-key grace period
    }
    twilio = {
      secret_arn  = local.secret_arns.twilio
      adapter     = "twilio"
      schedule    = "rate(90 days)"
      risk_tier   = "yellow"
      overlap_min = 30
    }
    openai = {
      secret_arn  = local.secret_arns.openai
      adapter     = "openai"
      schedule    = "rate(90 days)"
      risk_tier   = "yellow"
      overlap_min = 15
    }
    internal = {
      secret_arn  = local.secret_arns.internal
      adapter     = "internal"
      schedule    = "rate(90 days)"
      risk_tier   = "red"
      overlap_min = 5 # Short grace — dual-key acceptance in code
    }
    supabase = {
      secret_arn  = local.secret_arns.supabase
      adapter     = "supabase"
      schedule    = "rate(90 days)"
      risk_tier   = "red"
      overlap_min = 30
    }
    deepgram = {
      secret_arn  = local.secret_arns.providers
      adapter     = "deepgram"
      schedule    = "rate(90 days)"
      risk_tier   = "yellow"
      overlap_min = 15
    }
  }
}

# =============================================================================
# SNS — Rotation notifications
# =============================================================================

resource "aws_sns_topic" "rotation_events" {
  name = "aspire-rotation-events-${local.environment}"
}

resource "aws_sns_topic_subscription" "rotation_email" {
  topic_arn = aws_sns_topic.rotation_events.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic" "rotation_failures" {
  name = "aspire-rotation-failures-${local.environment}"
}

resource "aws_sns_topic_subscription" "failure_email" {
  topic_arn = aws_sns_topic.rotation_failures.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# =============================================================================
# Lambda — Vendor adapter functions
# =============================================================================

data "archive_file" "rotation_lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../rotation-lambdas"
  output_path = "${path.module}/builds/rotation-lambdas.zip"
}

resource "aws_lambda_function" "rotation_handler" {
  function_name    = "aspire-rotation-handler-${local.environment}"
  filename         = data.archive_file.rotation_lambda.output_path
  source_code_hash = data.archive_file.rotation_lambda.output_base64sha256
  handler          = "handlers.rotation_handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 300 # 5 min — vendor APIs can be slow
  memory_size      = 256
  role             = local.rotation_role

  environment {
    variables = {
      ENVIRONMENT       = local.environment
      SNS_EVENTS_TOPIC  = aws_sns_topic.rotation_events.arn
      SNS_FAILURE_TOPIC = aws_sns_topic.rotation_failures.arn
      KMS_KEY_ARN       = local.kms_key_arn
    }
  }

  layers = [
    "arn:aws:lambda:us-east-1:770693421928:layer:Klayers-p312-requests:17",
    "arn:aws:lambda:us-east-1:770693421928:layer:Klayers-p312-stripe:5",
  ]
}

resource "aws_lambda_function" "age_checker" {
  function_name    = "aspire-secret-age-checker-${local.environment}"
  filename         = data.archive_file.rotation_lambda.output_path
  source_code_hash = data.archive_file.rotation_lambda.output_base64sha256
  handler          = "handlers.age_checker.lambda_handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 128
  role             = local.rotation_role

  environment {
    variables = {
      ENVIRONMENT       = local.environment
      SNS_FAILURE_TOPIC = aws_sns_topic.rotation_failures.arn
    }
  }
}

# Daily age check schedule
resource "aws_cloudwatch_event_rule" "daily_age_check" {
  name                = "aspire-secret-age-daily-${local.environment}"
  schedule_expression = "rate(1 day)"
}

resource "aws_cloudwatch_event_target" "age_check" {
  rule      = aws_cloudwatch_event_rule.daily_age_check.name
  target_id = "age-checker"
  arn       = aws_lambda_function.age_checker.arn
}

resource "aws_lambda_permission" "age_check_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.age_checker.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_age_check.arn
}

# =============================================================================
# Step Functions — Rotation state machine
# =============================================================================

resource "aws_sfn_state_machine" "rotation" {
  name     = "aspire-secret-rotation-${local.environment}"
  role_arn = local.rotation_role

  definition = templatefile("${path.module}/step-function.asl.json", {
    rotation_handler_arn = aws_lambda_function.rotation_handler.arn
    sns_events_topic     = aws_sns_topic.rotation_events.arn
    sns_failure_topic    = aws_sns_topic.rotation_failures.arn
    environment          = local.environment
  })
}

# =============================================================================
# API Gateway — Rotation control plane entry point
# =============================================================================
# n8n (and emergency scripts) call this API to trigger rotation.
# IAM-authenticated — only aspire-n8n-rotation-trigger can invoke.
# =============================================================================

resource "aws_api_gateway_rest_api" "rotation" {
  name        = "aspire-rotation-api-${local.environment}"
  description = "Aspire Secret Rotation Control Plane"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_resource" "rotate" {
  rest_api_id = aws_api_gateway_rest_api.rotation.id
  parent_id   = aws_api_gateway_rest_api.rotation.root_resource_id
  path_part   = "rotate"
}

resource "aws_api_gateway_method" "rotate_post" {
  rest_api_id   = aws_api_gateway_rest_api.rotation.id
  resource_id   = aws_api_gateway_resource.rotate.id
  http_method   = "POST"
  authorization = "AWS_IAM"
}

resource "aws_api_gateway_integration" "rotate_sfn" {
  rest_api_id             = aws_api_gateway_rest_api.rotation.id
  resource_id             = aws_api_gateway_resource.rotate.id
  http_method             = aws_api_gateway_method.rotate_post.http_method
  integration_http_method = "POST"
  type                    = "AWS"
  uri                     = "arn:aws:apigateway:us-east-1:states:action/StartExecution"
  credentials             = local.rotation_role

  request_templates = {
    "application/json" = <<EOF
{
  "stateMachineArn": "${aws_sfn_state_machine.rotation.arn}",
  "input": "$util.escapeJavaScript($input.body)"
}
EOF
  }
}

resource "aws_api_gateway_method_response" "rotate_200" {
  rest_api_id = aws_api_gateway_rest_api.rotation.id
  resource_id = aws_api_gateway_resource.rotate.id
  http_method = aws_api_gateway_method.rotate_post.http_method
  status_code = "200"
}

resource "aws_api_gateway_integration_response" "rotate_200" {
  rest_api_id = aws_api_gateway_rest_api.rotation.id
  resource_id = aws_api_gateway_resource.rotate.id
  http_method = aws_api_gateway_method.rotate_post.http_method
  status_code = aws_api_gateway_method_response.rotate_200.status_code

  depends_on = [aws_api_gateway_integration.rotate_sfn]
}

# Status check endpoint (GET /rotate/status/{executionArn})
resource "aws_api_gateway_resource" "rotate_status" {
  rest_api_id = aws_api_gateway_rest_api.rotation.id
  parent_id   = aws_api_gateway_resource.rotate.id
  path_part   = "status"
}

resource "aws_api_gateway_resource" "rotate_status_id" {
  rest_api_id = aws_api_gateway_rest_api.rotation.id
  parent_id   = aws_api_gateway_resource.rotate_status.id
  path_part   = "{executionArn}"
}

resource "aws_api_gateway_method" "status_get" {
  rest_api_id   = aws_api_gateway_rest_api.rotation.id
  resource_id   = aws_api_gateway_resource.rotate_status_id.id
  http_method   = "GET"
  authorization = "AWS_IAM"

  request_parameters = {
    "method.request.path.executionArn" = true
  }
}

resource "aws_api_gateway_integration" "status_sfn" {
  rest_api_id             = aws_api_gateway_rest_api.rotation.id
  resource_id             = aws_api_gateway_resource.rotate_status_id.id
  http_method             = aws_api_gateway_method.status_get.http_method
  integration_http_method = "POST"
  type                    = "AWS"
  uri                     = "arn:aws:apigateway:us-east-1:states:action/DescribeExecution"
  credentials             = local.rotation_role

  request_templates = {
    "application/json" = <<EOF
{
  "executionArn": "$util.urlDecode($input.params('executionArn'))"
}
EOF
  }
}

resource "aws_api_gateway_deployment" "rotation" {
  rest_api_id = aws_api_gateway_rest_api.rotation.id

  depends_on = [
    aws_api_gateway_integration.rotate_sfn,
    aws_api_gateway_integration.status_sfn,
  ]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.rotation.id
  deployment_id = aws_api_gateway_deployment.rotation.id
  stage_name    = local.environment
}

# =============================================================================
# CloudWatch Alarms — Secret age alerts for manual-rotation providers
# =============================================================================

resource "aws_cloudwatch_metric_alarm" "secret_age" {
  for_each = toset([
    "elevenlabs", "anam", "livekit",
    "tavily", "brave", "google_maps",
  ])

  alarm_name          = "aspire-secret-age-${each.key}-${local.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "SecretAgeDays"
  namespace           = "Aspire/SecretsManager"
  dimensions          = { SecretName = each.key }
  period              = 86400
  statistic           = "Maximum"
  threshold           = 80 # Alert at 80 days, deadline at 90
  alarm_actions       = [aws_sns_topic.rotation_failures.arn]
  alarm_description   = "Secret ${each.key} is >80 days old. Create new key in vendor dashboard, then run: ./scripts/import-key.sh providers ${each.key} <new-value>"
}

# =============================================================================
# CloudWatch Alarms — Rotation execution failures
# =============================================================================

resource "aws_cloudwatch_metric_alarm" "rotation_sfn_failures" {
  alarm_name          = "aspire-rotation-sfn-failures-${local.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ExecutionsFailed"
  namespace           = "AWS/States"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.rotation.arn }
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [aws_sns_topic.rotation_failures.arn]
  alarm_description   = "Secret rotation Step Functions execution failed"
}

resource "aws_cloudwatch_metric_alarm" "rotation_lambda_errors" {
  alarm_name          = "aspire-rotation-lambda-errors-${local.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  dimensions          = { FunctionName = aws_lambda_function.rotation_handler.function_name }
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [aws_sns_topic.rotation_failures.arn]
  alarm_description   = "Rotation handler Lambda errors detected"
}
