# EKS Network Cost Monitor - Terraform Module

This Terraform module deploys the infrastructure for EKS Network Cost Monitor.

## Prerequisites

Before running Terraform, you must:

1. **Enable Network Flow Monitor on your EKS cluster**

   ```bash
   aws eks create-addon \
     --cluster-name <CLUSTER_NAME> \
     --addon-name aws-network-flow-monitoring-agent
   ```

2. **Create a Network Flow Monitor scope and monitor**

   ```bash
   # Create scope
   aws networkflowmonitor create-scope \
     --targets '[{
       "targetIdentifier": {
         "targetId": {"accountId": "<ACCOUNT_ID>"},
         "targetType": "ACCOUNT"
       },
       "region": "<REGION>"
     }]'

   # Create monitor (note the scopeArn from above)
   aws networkflowmonitor create-monitor \
     --monitor-name <MONITOR_NAME> \
     --local-resources type="AWS::EKS::Cluster",identifier="arn:aws:eks:<REGION>:<ACCOUNT_ID>:cluster/<CLUSTER_NAME>" \
     --scope-arn <SCOPE_ARN>
   ```

3. **Create the PyArrow Lambda layer** (required for Parquet support)

   ```bash
   # Use the deploy.sh script to create just the layer
   cd .. && ./deploy.sh --region <REGION> --monitor-name <MONITOR_NAME> --s3-bucket dummy
   # Or manually create the layer with Docker
   ```

## Usage

```hcl
module "eks_network_costs" {
  source = "./terraform"

  region           = "us-west-2"
  monitor_name     = "my-eks-cluster-flow-monitor"
  eks_cluster_name = "my-eks-cluster"

  # Optional: Create VPC Gateway Endpoints for S3/DynamoDB (saves ~$0.045/GB)
  create_vpc_endpoints = true
  vpc_id               = "vpc-12345678"
  route_table_ids      = ["rtb-12345678", "rtb-87654321"]
}
```

## Quick Start

```bash
# 1. Copy example variables
cp terraform.tfvars.example terraform.tfvars

# 2. Edit terraform.tfvars with your values
vim terraform.tfvars

# 3. Initialize and apply
terraform init
terraform plan
terraform apply

# 4. Test the Lambda
aws lambda invoke \
  --function-name eks-network-cost-exporter \
  --region us-west-2 \
  --payload '{}' \
  /dev/stdout

# 5. Run the UI
cd ../ui
make run S3_BUCKET=<bucket_name> ATHENA_DB=network_costs
```

## Important: PyArrow Layer

The Lambda requires a PyArrow layer for Parquet support. This layer is **not created by Terraform** because it requires Docker to build for the Lambda runtime.

After `terraform apply`, add the layer to the Lambda:

```bash
# If you've run deploy.sh before, the layer already exists
LAYER_ARN=$(aws lambda list-layer-versions \
  --layer-name pyarrow-layer \
  --query 'LayerVersions[0].LayerVersionArn' \
  --output text)

aws lambda update-function-configuration \
  --function-name eks-network-cost-exporter \
  --layers $LAYER_ARN
```

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|----------|
| region | AWS region | string | - | yes |
| monitor_name | Network Flow Monitor monitor name | string | - | yes |
| eks_cluster_name | EKS cluster name | string | - | yes |
| s3_bucket_name | S3 bucket name (default: `<account_id>-eks-network-costs`) | string | "" | no |
| athena_database | Athena/Glue database name | string | "network_costs" | no |
| has_s3_endpoint | Whether S3 gateway endpoint exists | bool | false | no |
| has_dynamodb_endpoint | Whether DynamoDB gateway endpoint exists | bool | false | no |
| create_vpc_endpoints | Create VPC gateway endpoints | bool | false | no |
| vpc_id | VPC ID (required if create_vpc_endpoints=true) | string | "" | no |
| route_table_ids | Route table IDs (required if create_vpc_endpoints=true) | list(string) | [] | no |
| data_retention_days | S3 lifecycle expiration days | number | 90 | no |

## Outputs

| Name | Description |
|------|-------------|
| s3_bucket_name | S3 bucket name |
| lambda_function_name | Lambda function name |
| athena_database | Athena database name |
| test_lambda_command | Command to test the Lambda |
| ui_run_command | Command to run the UI |

## Resources Created

- S3 bucket with lifecycle policy
- Lambda function with IAM role
- EventBridge hourly schedule
- Glue database and tables (with partition projection)
- (Optional) VPC Gateway Endpoints for S3 and DynamoDB
