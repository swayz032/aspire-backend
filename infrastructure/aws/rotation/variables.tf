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

variable "alert_email" {
  description = "Email for rotation alerts"
  type        = string
  default     = "security@aspireos.app"
}
