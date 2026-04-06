# -----------------------------------------------------------------------------
# VPC Gateway Endpoints (Optional)
#
# Gateway endpoints for S3 and DynamoDB make traffic to these services free.
# Without them, traffic goes through NAT Gateway at ~$0.045/GB.
# -----------------------------------------------------------------------------

resource "aws_vpc_endpoint" "s3" {
  count = var.create_vpc_endpoints ? 1 : 0

  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${local.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.route_table_ids

  tags = merge(local.default_tags, {
    Name = "s3-gateway-endpoint"
  })
}

resource "aws_vpc_endpoint" "dynamodb" {
  count = var.create_vpc_endpoints ? 1 : 0

  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${local.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.route_table_ids

  tags = merge(local.default_tags, {
    Name = "dynamodb-gateway-endpoint"
  })
}
