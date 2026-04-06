# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

output "s3_bucket_name" {
  description = "S3 bucket name for cost data"
  value       = aws_s3_bucket.cost_data.id
}

output "s3_bucket_arn" {
  description = "S3 bucket ARN"
  value       = aws_s3_bucket.cost_data.arn
}

output "lambda_function_name" {
  description = "Lambda function name"
  value       = aws_lambda_function.exporter.function_name
}

output "lambda_function_arn" {
  description = "Lambda function ARN"
  value       = aws_lambda_function.exporter.arn
}

output "lambda_role_arn" {
  description = "Lambda execution role ARN"
  value       = aws_iam_role.lambda.arn
}

output "schedule_arn" {
  description = "EventBridge schedule ARN"
  value       = aws_scheduler_schedule.hourly.arn
}

output "athena_database" {
  description = "Athena/Glue database name"
  value       = aws_glue_catalog_database.network_costs.name
}

output "athena_details_table" {
  description = "Athena details table name"
  value       = aws_glue_catalog_table.details.name
}

output "athena_summary_table" {
  description = "Athena summary table name"
  value       = aws_glue_catalog_table.summary.name
}

output "test_lambda_command" {
  description = "Command to test the Lambda function"
  value       = "aws lambda invoke --function-name ${aws_lambda_function.exporter.function_name} --region ${local.region} --payload '{}' /dev/stdout"
}

output "ui_run_command" {
  description = "Command to run the Streamlit UI"
  value       = "cd ui && make run S3_BUCKET=${aws_s3_bucket.cost_data.id} ATHENA_DB=${aws_glue_catalog_database.network_costs.name}"
}
