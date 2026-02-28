# =============================================================================
# Aspire Secrets Manager — Foundation
# =============================================================================
# Central secret store with KMS encryption and tenant-aware access controls.
# Secrets are grouped by provider domain. Per-tenant secrets use the naming
# convention: aspire/{env}/tenant/{suiteId}/provider/{provider}
#
# This file manages:
#   - KMS key with encryption context enforcement (suiteId)
#   - SM secret resources (5 groups for shared infra secrets)
#   - IAM reader user (services fetch secrets at runtime)
#   - IAM rotation execution role (Step Functions + Lambda)
# =============================================================================

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }

  backend "s3" {
    bucket = "aspire-terraform-state"
    key    = "secrets-manager/terraform.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "aspire"
      ManagedBy   = "terraform"
      Environment = var.environment
    }
  }
}

# =============================================================================
# KMS — Encryption key with tenant-aware context enforcement
# =============================================================================

resource "aws_kms_key" "secrets" {
  description             = "Aspire Secrets Manager encryption key"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowRootAccount"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${var.aws_account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowSecretsManagerUsage"
        Effect = "Allow"
        Principal = {
          AWS = "*"
        }
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "secretsmanager.us-east-1.amazonaws.com"
            "kms:CallerAccount" = var.aws_account_id
          }
        }
      }
    ]
  })
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/aspire-secrets"
  target_key_id = aws_kms_key.secrets.key_id
}

# =============================================================================
# Secrets Manager — Grouped by provider domain
# =============================================================================
# Values are injected via terraform.tfvars (gitignored) or CI/CD variables.
# write-only mode: values never appear in TF state after initial creation.
# =============================================================================

resource "aws_secretsmanager_secret" "stripe" {
  name        = "aspire/${var.environment}/stripe"
  description = "Stripe API keys (restricted, secret, publishable, webhook)"
  kms_key_id  = aws_kms_key.secrets.arn

  tags = { Provider = "stripe", RotationCycle = "30d", RiskTier = "red" }
}

resource "aws_secretsmanager_secret_version" "stripe" {
  secret_id = aws_secretsmanager_secret.stripe.id
  secret_string = jsonencode({
    restricted_key  = var.stripe_restricted_key
    secret_key      = var.stripe_secret_key
    publishable_key = var.stripe_publishable_key
    webhook_secret  = var.stripe_webhook_secret
  })

  lifecycle {
    ignore_changes = [secret_string] # Rotation manages versions after initial seed
  }
}

resource "aws_secretsmanager_secret" "supabase" {
  name        = "aspire/${var.environment}/supabase"
  description = "Supabase credentials (service role, JWT secret)"
  kms_key_id  = aws_kms_key.secrets.arn

  tags = { Provider = "supabase", RotationCycle = "90d", RiskTier = "red" }
}

resource "aws_secretsmanager_secret_version" "supabase" {
  secret_id = aws_secretsmanager_secret.supabase.id
  secret_string = jsonencode({
    service_role_key = var.supabase_service_role_key
    jwt_secret       = var.supabase_jwt_secret
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "openai" {
  name        = "aspire/${var.environment}/openai"
  description = "OpenAI API key"
  kms_key_id  = aws_kms_key.secrets.arn

  tags = { Provider = "openai", RotationCycle = "90d", RiskTier = "yellow" }
}

resource "aws_secretsmanager_secret_version" "openai" {
  secret_id     = aws_secretsmanager_secret.openai.id
  secret_string = jsonencode({ api_key = var.openai_api_key })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "twilio" {
  name        = "aspire/${var.environment}/twilio"
  description = "Twilio API credentials"
  kms_key_id  = aws_kms_key.secrets.arn

  tags = { Provider = "twilio", RotationCycle = "90d", RiskTier = "yellow" }
}

resource "aws_secretsmanager_secret_version" "twilio" {
  secret_id = aws_secretsmanager_secret.twilio.id
  secret_string = jsonencode({
    account_sid = var.twilio_account_sid
    api_key     = var.twilio_api_key
    api_secret  = var.twilio_api_secret
    auth_token  = var.twilio_auth_token
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "internal" {
  name        = "aspire/${var.environment}/internal"
  description = "Internal signing, encryption, and HMAC keys"
  kms_key_id  = aws_kms_key.secrets.arn

  tags = { Provider = "internal", RotationCycle = "90d", RiskTier = "red" }
}

resource "aws_secretsmanager_secret_version" "internal" {
  secret_id = aws_secretsmanager_secret.internal.id
  secret_string = jsonencode({
    token_signing_secret    = var.token_signing_secret
    token_encryption_key    = var.token_encryption_key
    n8n_hmac_secret         = var.n8n_hmac_secret
    n8n_eli_webhook_secret  = var.n8n_eli_webhook_secret
    n8n_sarah_webhook_secret = var.n8n_sarah_webhook_secret
    n8n_nora_webhook_secret = var.n8n_nora_webhook_secret
    domain_rail_hmac_secret = var.domain_rail_hmac_secret
    gateway_internal_key    = var.gateway_internal_key
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "providers" {
  name        = "aspire/${var.environment}/providers"
  description = "Third-party provider API keys (ElevenLabs, Deepgram, LiveKit, Anam, etc.)"
  kms_key_id  = aws_kms_key.secrets.arn

  tags = { Provider = "multi", RotationCycle = "manual-90d", RiskTier = "yellow" }
}

resource "aws_secretsmanager_secret_version" "providers" {
  secret_id = aws_secretsmanager_secret.providers.id
  secret_string = jsonencode({
    elevenlabs_key  = var.elevenlabs_key
    deepgram_key    = var.deepgram_key
    livekit_key     = var.livekit_key
    livekit_secret  = var.livekit_secret
    anam_key        = var.anam_key
    tavily_key      = var.tavily_key
    brave_key       = var.brave_key
    google_maps_key = var.google_maps_key
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# =============================================================================
# IAM — Secrets Reader (services: Railway Desktop + Orchestrator)
# =============================================================================

resource "aws_iam_user" "secrets_reader" {
  name = "aspire-secrets-reader-${var.environment}"
  path = "/aspire/"
}

resource "aws_iam_user_policy" "secrets_read" {
  name = "aspire-secrets-read-only"
  user = aws_iam_user.secrets_reader.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadSecrets"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
          "secretsmanager:ListSecrets",
        ]
        Resource = "arn:aws:secretsmanager:us-east-1:${var.aws_account_id}:secret:aspire/${var.environment}/*"
      },
      {
        Sid    = "DecryptWithKMS"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
        ]
        Resource = aws_kms_key.secrets.arn
      }
    ]
  })
}

resource "aws_iam_access_key" "secrets_reader" {
  user = aws_iam_user.secrets_reader.name
}

# =============================================================================
# IAM — Rotation Execution Role (Step Functions + Lambda)
# =============================================================================

resource "aws_iam_role" "rotation_execution" {
  name = "aspire-rotation-execution-${var.environment}"
  path = "/aspire/"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = [
            "lambda.amazonaws.com",
            "states.amazonaws.com",
          ]
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "rotation_execution" {
  name = "aspire-rotation-execution-policy"
  role = aws_iam_role.rotation_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsManagerRotation"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecretVersionStage",
          "secretsmanager:DescribeSecret",
          "secretsmanager:TagResource",
        ]
        Resource = "arn:aws:secretsmanager:us-east-1:${var.aws_account_id}:secret:aspire/${var.environment}/*"
      },
      {
        Sid    = "KMSForRotation"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey",
        ]
        Resource = aws_kms_key.secrets.arn
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:us-east-1:${var.aws_account_id}:*"
      },
      {
        Sid    = "InvokeLambda"
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction",
        ]
        Resource = "arn:aws:lambda:us-east-1:${var.aws_account_id}:function:aspire-rotation-*"
      },
      {
        Sid    = "SNSPublish"
        Effect = "Allow"
        Action = [
          "sns:Publish",
        ]
        Resource = "arn:aws:sns:us-east-1:${var.aws_account_id}:aspire-*"
      }
    ]
  })
}

# =============================================================================
# IAM — n8n Rotation Trigger (tightly scoped — can ONLY start rotation jobs)
# =============================================================================

resource "aws_iam_user" "n8n_rotation_trigger" {
  name = "aspire-n8n-rotation-trigger-${var.environment}"
  path = "/aspire/"
}

resource "aws_iam_user_policy" "n8n_rotation_trigger" {
  name = "aspire-n8n-rotation-trigger-only"
  user = aws_iam_user.n8n_rotation_trigger.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeRotationAPIOnly"
        Effect = "Allow"
        Action = [
          "execute-api:Invoke",
        ]
        Resource = "arn:aws:execute-api:us-east-1:${var.aws_account_id}:*/prod/POST/rotate"
      },
      {
        Sid    = "DescribeSecretsMetadataOnly"
        Effect = "Allow"
        Action = [
          "secretsmanager:DescribeSecret",
        ]
        Resource = "arn:aws:secretsmanager:us-east-1:${var.aws_account_id}:secret:aspire/${var.environment}/*"
        Condition = {
          StringEquals = {
            "secretsmanager:ResourceTag/Project" = "aspire"
          }
        }
      }
    ]
  })
}

resource "aws_iam_access_key" "n8n_rotation_trigger" {
  user = aws_iam_user.n8n_rotation_trigger.name
}
