# Getting Started with EKS Network Cost Monitor

This guide walks you through setting up network cost monitoring for your EKS cluster using CloudWatch Network Flow Monitor.

## Prerequisites

- AWS CLI v2
- An EKS cluster (v1.25+)
- Docker (for the UI and Lambda layer)
- kubectl configured for your cluster
- Terraform 1.0+ (if using IaC)

## Step 1: Enable Network Flow Monitor on EKS

Install the Network Flow Monitor agent add-on:

```bash
aws eks create-addon \
  --cluster-name <CLUSTER_NAME> \
  --addon-name aws-network-flow-monitoring-agent
```

Verify agents are running:

```bash
kubectl get pods -n amazon-network-flow-monitor
```

## Step 2: Create a Network Flow Monitor Scope and Monitor

```bash
# Get your account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-west-2
CLUSTER_NAME=my-cluster
MONITOR_NAME=${CLUSTER_NAME}-flow-monitor

# Create scope
aws networkflowmonitor create-scope \
  --targets '[{
    "targetIdentifier": {
      "targetId": {"accountId": "'$ACCOUNT_ID'"},
      "targetType": "ACCOUNT"
    },
    "region": "'$REGION'"
  }]'

# Get the scope ARN from the output, then create the monitor
SCOPE_ARN=<scope-arn-from-above>

aws networkflowmonitor create-monitor \
  --monitor-name $MONITOR_NAME \
  --local-resources type="AWS::EKS::Cluster",identifier="arn:aws:eks:$REGION:$ACCOUNT_ID:cluster/$CLUSTER_NAME" \
  --scope-arn $SCOPE_ARN
```

Wait for the monitor to become active:

```bash
aws networkflowmonitor get-monitor --monitor-name $MONITOR_NAME
# Should show monitorStatus: ACTIVE
```

## Step 3: Deploy the Infrastructure

### Option A: Using Terraform (Recommended)

```bash
cd terraform

# Copy and edit variables
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values

# Deploy
terraform init
terraform apply
```

After Terraform completes, add the PyArrow layer to Lambda:

```bash
# Check if layer exists
LAYER_ARN=$(aws lambda list-layer-versions \
  --layer-name pyarrow-layer \
  --region $REGION \
  --query 'LayerVersions[0].LayerVersionArn' \
  --output text)

# If layer doesn't exist, create it using deploy.sh
if [ "$LAYER_ARN" = "None" ]; then
  cd .. && ./deploy.sh --region $REGION --monitor-name $MONITOR_NAME --s3-bucket temp
fi

# Add layer to Lambda
aws lambda update-function-configuration \
  --function-name eks-network-cost-exporter \
  --region $REGION \
  --layers $LAYER_ARN
```

### Option B: Using deploy.sh

```bash
./deploy.sh \
  --region $REGION \
  --monitor-name $MONITOR_NAME \
  --s3-bucket $ACCOUNT_ID-eks-network-costs
```

This creates: S3 bucket, IAM roles, Lambda (with PyArrow layer), and EventBridge schedule.

Then create Athena tables:

```bash
# Run in Athena console or via CLI
aws athena start-query-execution \
  --query-string "$(cat athena/recreate_tables_parquet.sql | sed 's/<BUCKET_NAME>/'$ACCOUNT_ID'-eks-network-costs/g')" \
  --result-configuration OutputLocation=s3://$ACCOUNT_ID-eks-network-costs/athena-results/
```

## Step 4: Verify Data Collection

Invoke the Lambda manually to test:

```bash
aws lambda invoke \
  --function-name eks-network-cost-exporter \
  --region $REGION \
  --payload '{}' \
  /dev/stdout
```

Expected output:
```json
{"status": "ok", "period_start": "...", "total_contributors": 123, "estimated_cost_usd": 0.0234}
```

Check data in Athena:

```sql
SELECT destination_category, COUNT(*) as flows, SUM(estimated_cost_usd) as cost
FROM network_costs.network_cost_details
WHERE date = date_format(current_date, '%Y-%m-%d')
GROUP BY destination_category;
```

## Step 5: Run the Dashboard

```bash
cd ui
make run S3_BUCKET=$ACCOUNT_ID-eks-network-costs ATHENA_DB=network_costs
```

Open http://localhost:8501 in your browser.

To stop: `make stop`

## Cost Optimization Tips

Based on the data collected:

1. **Enable topology-aware routing** — Services prefer same-AZ endpoints
2. **Co-locate tightly coupled services** — Use pod affinity for same-AZ placement
3. **Add VPC Gateway Endpoints** — Free traffic to S3/DynamoDB (vs ~$0.045/GB via NAT)
4. **Review UNCLASSIFIED traffic** — Internet egress is expensive; consider caching

## Troubleshooting

### No data in Athena

1. Check Lambda logs: `aws logs tail /aws/lambda/eks-network-cost-exporter`
2. Verify monitor is ACTIVE: `aws networkflowmonitor get-monitor --monitor-name $MONITOR_NAME`
3. Check S3 for data: `aws s3 ls s3://$BUCKET/network-cost-data/details/`

### Lambda timeout

Increase timeout to 5 minutes (300s) — the NFM API can be slow.

### Missing K8s metadata

Some flows lack pod/namespace info:
- Host-network pods (`hostNetwork: true`)
- Node-level traffic (kubelet, kube-proxy)
- Traffic captured before pod metadata was available

These appear as `node:<instance-id>` in the namespace column.

## Next Steps

- Set up CloudWatch alarms for cost thresholds
- Connect Athena to QuickSight for visual dashboards
- Deploy the UI to EKS for team access
