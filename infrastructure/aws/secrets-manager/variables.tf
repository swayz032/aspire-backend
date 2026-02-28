# =============================================================================
# Aspire Secrets Manager — Variables
# =============================================================================
# All secret values come from terraform.tfvars (gitignored) or CI/CD.
# NEVER commit actual secret values to version control.
# =============================================================================

variable "aws_account_id" {
  description = "AWS account ID"
  type        = string
  default     = "843479649294"
}

variable "environment" {
  description = "Environment name (prod, dev)"
  type        = string
  default     = "prod"
}

# --- Stripe ---
variable "stripe_restricted_key" {
  description = "Stripe restricted API key"
  type        = string
  sensitive   = true
}

variable "stripe_secret_key" {
  description = "Stripe secret API key"
  type        = string
  sensitive   = true
}

variable "stripe_publishable_key" {
  description = "Stripe publishable key"
  type        = string
  sensitive   = true
}

variable "stripe_webhook_secret" {
  description = "Stripe webhook signing secret"
  type        = string
  sensitive   = true
}

# --- Supabase ---
variable "supabase_service_role_key" {
  description = "Supabase service role JWT"
  type        = string
  sensitive   = true
}

variable "supabase_jwt_secret" {
  description = "Supabase JWT signing secret"
  type        = string
  sensitive   = true
  default     = ""
}

# --- OpenAI ---
variable "openai_api_key" {
  description = "OpenAI API key"
  type        = string
  sensitive   = true
}

# --- Twilio ---
variable "twilio_account_sid" {
  description = "Twilio Account SID"
  type        = string
  sensitive   = true
}

variable "twilio_api_key" {
  description = "Twilio API key SID"
  type        = string
  sensitive   = true
}

variable "twilio_api_secret" {
  description = "Twilio API key secret"
  type        = string
  sensitive   = true
}

variable "twilio_auth_token" {
  description = "Twilio auth token (for key management API calls)"
  type        = string
  sensitive   = true
}

# --- Internal ---
variable "token_signing_secret" {
  description = "JWT/capability token signing secret"
  type        = string
  sensitive   = true
}

variable "token_encryption_key" {
  description = "AES-256 encryption key for IMAP credentials etc."
  type        = string
  sensitive   = true
}

variable "n8n_hmac_secret" {
  description = "HMAC secret for n8n webhook intake-activation"
  type        = string
  sensitive   = true
}

variable "n8n_eli_webhook_secret" {
  description = "HMAC secret for n8n Eli email triage webhook"
  type        = string
  sensitive   = true
}

variable "n8n_sarah_webhook_secret" {
  description = "HMAC secret for n8n Sarah call handler webhook"
  type        = string
  sensitive   = true
}

variable "n8n_nora_webhook_secret" {
  description = "HMAC secret for n8n Nora meeting summary webhook"
  type        = string
  sensitive   = true
}

variable "domain_rail_hmac_secret" {
  description = "HMAC secret for Domain Rail service"
  type        = string
  sensitive   = true
}

variable "gateway_internal_key" {
  description = "Gateway internal service-to-service key"
  type        = string
  sensitive   = true
}

# --- Providers ---
variable "elevenlabs_key" {
  description = "ElevenLabs API key"
  type        = string
  sensitive   = true
}

variable "deepgram_key" {
  description = "Deepgram API key"
  type        = string
  sensitive   = true
}

variable "livekit_key" {
  description = "LiveKit API key"
  type        = string
  sensitive   = true
}

variable "livekit_secret" {
  description = "LiveKit API secret"
  type        = string
  sensitive   = true
}

variable "anam_key" {
  description = "Anam API key"
  type        = string
  sensitive   = true
}

variable "tavily_key" {
  description = "Tavily API key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "brave_key" {
  description = "Brave Search API key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "google_maps_key" {
  description = "Google Maps API key"
  type        = string
  sensitive   = true
  default     = ""
}
