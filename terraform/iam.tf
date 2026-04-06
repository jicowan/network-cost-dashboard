# -----------------------------------------------------------------------------
# IAM Role for Lambda Function
# -----------------------------------------------------------------------------

resource "aws_iam_role" "lambda" {
  name = "${var.lambda_function_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })

  tags = local.default_tags
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.lambda_function_name}-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "NetworkFlowMonitorRead"
        Effect = "Allow"
        Action = [
          "networkflowmonitor:StartQueryMonitorTopContributors",
          "networkflowmonitor:GetQueryStatusMonitorTopContributors",
          "networkflowmonitor:GetQueryResultsMonitorTopContributors"
        ]
        Resource = "arn:aws:networkflowmonitor:${local.region}:${local.account_id}:monitor/${var.monitor_name}"
      },
      {
        Sid    = "S3Write"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.cost_data.arn}/${var.s3_prefix}/*"
      },
      {
        Sid    = "S3AthenaResults"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.cost_data.arn}/athena-results/*"
      },
      {
        Sid    = "S3BucketAccess"
        Effect = "Allow"
        Action = ["s3:GetBucketLocation", "s3:ListBucket"]
        Resource = aws_s3_bucket.cost_data.arn
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:*"
      },
      {
        Sid    = "AthenaQueryExecution"
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution",
          "athena:GetQueryExecution"
        ]
        Resource = "arn:aws:athena:${local.region}:${local.account_id}:workgroup/primary"
      },
      {
        Sid    = "GluePartitions"
        Effect = "Allow"
        Action = [
          "glue:GetTable",
          "glue:GetPartition",
          "glue:CreatePartition",
          "glue:BatchCreatePartition"
        ]
        Resource = [
          "arn:aws:glue:${local.region}:${local.account_id}:catalog",
          "arn:aws:glue:${local.region}:${local.account_id}:database/${var.athena_database}",
          "arn:aws:glue:${local.region}:${local.account_id}:table/${var.athena_database}/*"
        ]
      },
      {
        Sid      = "PricingAPIRead"
        Effect   = "Allow"
        Action   = ["pricing:GetProducts", "pricing:GetAttributeValues"]
        Resource = "*"
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# IAM Role for EventBridge Scheduler
# -----------------------------------------------------------------------------

resource "aws_iam_role" "scheduler" {
  name = "${var.schedule_name}-scheduler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "scheduler.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })

  tags = local.default_tags
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke-lambda"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.exporter.arn
    }]
  })
}
