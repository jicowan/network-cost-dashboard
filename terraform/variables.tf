# -----------------------------------------------------------------------------
# Required Variables
# -----------------------------------------------------------------------------

variable "region" {
  description = "AWS region"
  type        = string
}

variable "monitor_name" {
  description = "Name of the Network Flow Monitor monitor"
  type        = string
}

variable "eks_cluster_name" {
  description = "Name of the EKS cluster (used for monitor resource ARN)"
  type        = string
}

# -----------------------------------------------------------------------------
# Optional Variables
# -----------------------------------------------------------------------------

variable "s3_bucket_name" {
  description = "S3 bucket name for cost data. If not specified, uses <account_id>-eks-network-costs"
  type        = string
  default     = ""
}

variable "s3_prefix" {
  description = "S3 key prefix for cost data"
  type        = string
  default     = "network-cost-data"
}

variable "athena_database" {
  description = "Athena/Glue database name"
  type        = string
  default     = "network_costs"
}

variable "lambda_function_name" {
  description = "Name for the Lambda function"
  type        = string
  default     = "eks-network-cost-exporter"
}

variable "schedule_name" {
  description = "Name for the EventBridge schedule"
  type        = string
  default     = "eks-network-cost-hourly"
}

variable "has_s3_endpoint" {
  description = "Whether a VPC Gateway Endpoint exists for S3 (affects pricing calculation)"
  type        = bool
  default     = false
}

variable "has_dynamodb_endpoint" {
  description = "Whether a VPC Gateway Endpoint exists for DynamoDB (affects pricing calculation)"
  type        = bool
  default     = false
}

variable "create_vpc_endpoints" {
  description = "Whether to create VPC Gateway Endpoints for S3 and DynamoDB"
  type        = bool
  default     = false
}

variable "vpc_id" {
  description = "VPC ID for creating gateway endpoints (required if create_vpc_endpoints=true)"
  type        = string
  default     = ""
}

variable "route_table_ids" {
  description = "Route table IDs for gateway endpoints (required if create_vpc_endpoints=true)"
  type        = list(string)
  default     = []
}

variable "data_retention_days" {
  description = "Number of days to retain cost data in S3"
  type        = number
  default     = 90
}

variable "lambda_memory_size" {
  description = "Lambda memory size in MB"
  type        = number
  default     = 512
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 300
}

variable "query_limit" {
  description = "Maximum flows to retrieve per category (NFM API limit is 500)"
  type        = number
  default     = 500
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
