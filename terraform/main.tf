# -----------------------------------------------------------------------------
# EKS Network Cost Monitor - Terraform Module
# -----------------------------------------------------------------------------

terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.0"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  region      = data.aws_region.current.name
  bucket_name = var.s3_bucket_name != "" ? var.s3_bucket_name : "${local.account_id}-eks-network-costs"

  # Determine endpoint flags (true if creating or already exists)
  has_s3_endpoint       = var.create_vpc_endpoints || var.has_s3_endpoint
  has_dynamodb_endpoint = var.create_vpc_endpoints || var.has_dynamodb_endpoint

  default_tags = merge(var.tags, {
    ManagedBy = "terraform"
    Project   = "eks-network-cost-monitor"
  })
}

# -----------------------------------------------------------------------------
# S3 Bucket
# -----------------------------------------------------------------------------

resource "aws_s3_bucket" "cost_data" {
  bucket = local.bucket_name
  tags   = local.default_tags
}

resource "aws_s3_bucket_lifecycle_configuration" "cost_data" {
  bucket = aws_s3_bucket.cost_data.id

  rule {
    id     = "expire-old-cost-data"
    status = "Enabled"

    filter {
      prefix = "${var.s3_prefix}/"
    }

    expiration {
      days = var.data_retention_days
    }
  }
}

resource "aws_s3_bucket_public_access_block" "cost_data" {
  bucket = aws_s3_bucket.cost_data.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -----------------------------------------------------------------------------
# Lambda Function
# -----------------------------------------------------------------------------

data "archive_file" "lambda" {
  type        = "zip"
  source_file = "${path.module}/../lambda/handler.py"
  output_path = "${path.module}/.terraform/lambda_handler.zip"
}

# Check if PyArrow layer exists, create if not
data "aws_lambda_layer_version" "pyarrow" {
  layer_name = "pyarrow-layer"
  count      = 0 # Disabled - layer must be created separately (see README)
}

resource "aws_lambda_function" "exporter" {
  function_name = var.lambda_function_name
  description   = "Exports Network Flow Monitor data to S3 for cost analysis"

  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_size

  role = aws_iam_role.lambda.arn

  # Note: PyArrow layer must be created separately using deploy.sh or manually
  # layers = [data.aws_lambda_layer_version.pyarrow.arn]

  environment {
    variables = {
      MONITOR_NAME          = var.monitor_name
      S3_BUCKET             = aws_s3_bucket.cost_data.id
      S3_PREFIX             = var.s3_prefix
      QUERY_LIMIT           = tostring(var.query_limit)
      ATHENA_DATABASE       = var.athena_database
      ATHENA_OUTPUT         = "s3://${aws_s3_bucket.cost_data.id}/athena-results/"
      HAS_S3_ENDPOINT       = tostring(local.has_s3_endpoint)
      HAS_DYNAMODB_ENDPOINT = tostring(local.has_dynamodb_endpoint)
    }
  }

  tags = local.default_tags

  depends_on = [aws_iam_role_policy.lambda]
}

# -----------------------------------------------------------------------------
# EventBridge Scheduler
# -----------------------------------------------------------------------------

resource "aws_scheduler_schedule" "hourly" {
  name        = var.schedule_name
  description = "Triggers Lambda hourly to export network cost data"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = "rate(1 hour)"

  target {
    arn      = aws_lambda_function.exporter.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = "{}"
  }

  depends_on = [aws_lambda_permission.scheduler]
}

resource "aws_lambda_permission" "scheduler" {
  statement_id  = "AllowEventBridgeScheduler"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.exporter.function_name
  principal     = "scheduler.amazonaws.com"
  source_arn    = "arn:aws:scheduler:${local.region}:${local.account_id}:schedule/default/${var.schedule_name}"
}
