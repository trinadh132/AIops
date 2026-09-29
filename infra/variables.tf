variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  description = "Name prefix for every resource, and the SSM path (/<project>/...)."
  type        = string
  default     = "ops-agent"
}

variable "image_tag" {
  description = "Tag of both images in ECR (CI pushes the git SHA). Repositories are immutable, so a new tag is a new deploy."
  type        = string
}

variable "alert_email" {
  description = "Receives approval requests. SNS emails a confirmation link first; nothing arrives until it's clicked."
  type        = string
}

variable "budget_email" {
  description = "Receives AWS Budgets alerts."
  type        = string
}

variable "budget_limit_usd" {
  type    = number
  default = 20
}

variable "admin_cidr" {
  description = "Only this CIDR can reach the mock service on 8080 (to inject failures). Your IP as x.x.x.x/32."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.admin_cidr)) && var.admin_cidr != "0.0.0.0/0"
    error_message = "admin_cidr must be a valid CIDR and not 0.0.0.0/0: /admin has no auth."
  }
}

variable "auto_remediate" {
  description = "false = every fix needs human approval (shadow mode). Flip once diagnoses have earned trust."
  type        = bool
  default     = false
}

variable "enable_mcp" {
  description = "Deploy the MCP server as a Lambda Function URL."
  type        = bool
  default     = true
}

variable "mcp_write_tools" {
  description = "Expose inject_failure/request_approval over MCP."
  type        = bool
  default     = false
}

variable "secret_version" {
  description = "Bump to regenerate the HMAC and MCP tokens (write-only attributes only re-send on version change)."
  type        = number
  default     = 1
}

variable "log_retention_days" {
  type    = number
  default = 7
}

variable "alarm_thresholds" {
  description = "Signal events per minute that trip each failure mode's alarm."
  type        = map(number)
  default = {
    CONNECTION_POOL_EXHAUSTION = 5
    DB_DEADLOCK                = 5
    SLOW_DOWNSTREAM_DEPENDENCY = 3
    MEMORY_LEAK                = 6 # growth ticks every 5s: ~30s of sustained growth
    DISK_FULL                  = 5
    THREAD_POOL_EXHAUSTION     = 5
    BAD_DEPLOY_ERROR_SPIKE     = 5
    CONFIG_DRIFT               = 5
    RETRY_STORM                = 5
    OOM_KILL                   = 1
  }
}
