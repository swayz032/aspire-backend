# =============================================================================
# Aspire Secrets Manager — Outputs
# =============================================================================

output "kms_key_arn" {
  description = "KMS key ARN for secret encryption"
  value       = aws_kms_key.secrets.arn
}

output "kms_key_alias" {
  description = "KMS key alias"
  value       = aws_kms_alias.secrets.name
}

# --- Secret ARNs (for rotation config + IAM policies) ---

output "secret_arns" {
  description = "Map of secret name to ARN"
  value = {
    stripe    = aws_secretsmanager_secret.stripe.arn
    supabase  = aws_secretsmanager_secret.supabase.arn
    openai    = aws_secretsmanager_secret.openai.arn
    twilio    = aws_secretsmanager_secret.twilio.arn
    internal  = aws_secretsmanager_secret.internal.arn
    providers = aws_secretsmanager_secret.providers.arn
  }
}

# --- IAM Credentials (for Railway env vars) ---

output "secrets_reader_access_key_id" {
  description = "Access key ID for aspire-secrets-reader (set in Railway)"
  value       = aws_iam_access_key.secrets_reader.id
}

output "secrets_reader_secret_access_key" {
  description = "Secret access key for aspire-secrets-reader (set in Railway)"
  value       = aws_iam_access_key.secrets_reader.secret
  sensitive   = true
}

output "n8n_trigger_access_key_id" {
  description = "Access key ID for n8n rotation trigger (set in n8n env)"
  value       = aws_iam_access_key.n8n_rotation_trigger.id
}

output "n8n_trigger_secret_access_key" {
  description = "Secret access key for n8n rotation trigger"
  value       = aws_iam_access_key.n8n_rotation_trigger.secret
  sensitive   = true
}

# --- Rotation Execution Role ---

output "rotation_execution_role_arn" {
  description = "IAM role ARN for rotation Lambda/Step Functions"
  value       = aws_iam_role.rotation_execution.arn
}
