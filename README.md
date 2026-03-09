# EKS Network Cost Monitor

Estimate per-namespace and per-workload network transfer costs for Amazon EKS clusters using CloudWatch Network Flow Monitor.

## Overview

This solution periodically exports network flow data from CloudWatch Network Flow Monitor into S3, then queries it with Athena to produce cost reports. A Streamlit dashboard provides visualization of costs by namespace, workload, and traffic category.

### Architecture

```
                      AWS Pricing API
                       (dynamic rates)
                            │
EventBridge (hourly) ──→ Lambda ──→ Network Flow Monitor API
                            │           (top 500 per category)
                            │
                            ▼
                     S3 (Parquet, Snappy compressed)
                       ├── details/date=YYYY-MM-DD/hour=HH/
                       └── summary/date=YYYY-MM-DD/hour=HH/
                            │
                            ▼
                     Athena (Glue catalog)
                            │
                            ▼
                     Streamlit UI (local or K8s)
```

### Traffic Categories

The solution tracks these AWS network cost categories:

| Category | Description | Typical Cost |
|----------|-------------|--------------|
| `INTER_AZ` | Cross-AZ traffic within a region | ~$0.01/GB per direction |
| `INTER_VPC` | Cross-VPC traffic | ~$0.01/GB per direction |
| `INTER_REGION` | Cross-region traffic | Varies by region pair |
| `AMAZON_S3` | Traffic to S3 | Free via gateway endpoint |
| `AMAZON_DYNAMODB` | Traffic to DynamoDB | Free via gateway endpoint |
| `UNCLASSIFIED` | Internet egress | ~$0.09/GB (first 10TB) |

## Prerequisites

- An EKS cluster (v1.25+)
- AWS CLI v2
- Docker (for the UI)
- kubectl configured for your cluster

## Quick Start

### 1. Enable Network Flow Monitor on EKS

Install the Network Flow Monitor agent add-on:

```bash
aws eks create-addon \
  --cluster-name <CLUSTER_NAME> \
  --addon-name aws-network-flow-monitoring-agent
```

Verify the agents are running:

```bash
kubectl get pods -n amazon-network-flow-monitor
```

### 2. Create a Network Flow Monitor Scope and Monitor

```bash
# Create scope for your account
aws networkflowmonitor create-scope \
  --targets '[{
    "targetIdentifier": {
      "targetId": {"accountId": "<ACCOUNT_ID>"},
      "targetType": "ACCOUNT"
    },
    "region": "<REGION>"
  }]'

# Note the scopeArn, then create the monitor
aws networkflowmonitor create-monitor \
  --monitor-name <MONITOR_NAME> \
  --local-resources type="AWS::EKS::Cluster",identifier="arn:aws:eks:<REGION>:<ACCOUNT_ID>:cluster/<CLUSTER_NAME>" \
  --scope-arn <SCOPE_ARN>
```

Wait for the monitor to become active:

```bash
aws networkflowmonitor get-monitor --monitor-name <MONITOR_NAME>
# Should show monitorStatus: ACTIVE
```

### 3. Deploy the Lambda Function

The `deploy.sh` script creates all required resources:

```bash
./deploy.sh \
  --region <REGION> \
  --monitor-name <MONITOR_NAME> \
  --s3-bucket <BUCKET_NAME>
```

This creates:
- S3 bucket with 90-day lifecycle policy
- SSM parameter for configurable rates
- IAM roles with least-privilege permissions
- Lambda function
- EventBridge hourly schedule

### 4. Create Athena Tables

Create a database and tables for querying the data:

```sql
CREATE DATABASE IF NOT EXISTS network_costs;

CREATE EXTERNAL TABLE network_costs.network_cost_details (
  period_start          STRING,
  destination_category  STRING,
  local_ip              STRING,
  local_az              STRING,
  local_vpc_id          STRING,
  local_subnet_id       STRING,
  local_instance_id     STRING,
  local_region          STRING,
  remote_ip             STRING,
  remote_az             STRING,
  remote_vpc_id         STRING,
  remote_subnet_id      STRING,
  remote_instance_id    STRING,
  remote_region         STRING,
  local_pod_name        STRING,
  local_pod_namespace   STRING,
  local_service_name    STRING,
  remote_pod_name       STRING,
  remote_pod_namespace  STRING,
  remote_service_name   STRING,
  snat_ip               STRING,
  dnat_ip               STRING,
  target_port           INT,
  traversed_constructs  STRING,
  bytes                 BIGINT,
  gb                    DOUBLE,
  rate_per_gb           DOUBLE,
  estimated_cost_usd    DOUBLE
)
PARTITIONED BY (date STRING, hour STRING)
STORED AS PARQUET
LOCATION 's3://<BUCKET_NAME>/network-cost-data/details/'
TBLPROPERTIES ('parquet.compression'='SNAPPY');

CREATE EXTERNAL TABLE network_costs.network_cost_summary (
  period_start          STRING,
  namespace             STRING,
  destination_category  STRING,
  total_bytes           BIGINT,
  total_gb              DOUBLE,
  estimated_cost_usd    DOUBLE
)
PARTITIONED BY (date STRING, hour STRING)
STORED AS PARQUET
LOCATION 's3://<BUCKET_NAME>/network-cost-data/summary/'
TBLPROPERTIES ('parquet.compression'='SNAPPY');
```

Load existing partitions (only needed once; new partitions are added automatically):

```sql
MSCK REPAIR TABLE network_costs.network_cost_details;
MSCK REPAIR TABLE network_costs.network_cost_summary;
```

### 5. Run the Dashboard

```bash
cd ui/
make run S3_BUCKET=<BUCKET_NAME> ATHENA_DB=network_costs
```

Open http://localhost:8501 in your browser.

## Configuration

### Dynamic Pricing

Rates are fetched automatically from the AWS Pricing API at runtime, so they always reflect current AWS pricing for your region. The Lambda caches pricing data for 1 hour to minimize API calls.

**Pricing sources:**
| Category | Source | Notes |
|----------|--------|-------|
| `INTER_AZ` | Pricing API (`IntraRegion`) | $0.01/GB × 2 directions |
| `INTER_VPC` | Pricing API (`IntraRegion`) | Same as inter-AZ when cross-AZ |
| `INTER_REGION` | Pricing API (`InterRegion Outbound`) | Varies by region pair |
| `AMAZON_S3` | NAT Gateway rate or $0 | Depends on gateway endpoint |
| `AMAZON_DYNAMODB` | NAT Gateway rate or $0 | Depends on gateway endpoint |
| `UNCLASSIFIED` | Pricing API (`AWS Outbound`) | Tiered pricing by volume |

### VPC Gateway Endpoints

If you have VPC Gateway Endpoints for S3 and/or DynamoDB, traffic to those services is free. Configure this during deployment:

```bash
./deploy.sh \
  --region us-west-2 \
  --monitor-name eks-network-costs \
  --s3-bucket <BUCKET_NAME> \
  --has-s3-endpoint true \
  --has-dynamodb-endpoint true
```

Without gateway endpoints, traffic to S3/DynamoDB goes through NAT Gateway and incurs the NAT Gateway data processing charge (~$0.045/GB).

### Internet Egress Tiered Pricing

Internet egress (UNCLASSIFIED category) uses AWS tiered pricing based on total monthly volume:

| Volume | Price per GB |
|--------|--------------|
| First 10 TB | $0.09 |
| Next 40 TB | $0.085 |
| Next 100 TB | $0.07 |
| Over 150 TB | $0.05 |

The Lambda calculates costs using tiered pricing based on the actual volume in each period

### Backfilling Historical Data

Invoke the Lambda with a custom time range:

```bash
aws lambda invoke \
  --function-name eks-network-cost-exporter \
  --cli-binary-format raw-in-base64-out \
  --payload '{"start_time":"2024-01-01T00:00:00+00:00","end_time":"2024-01-01T01:00:00+00:00"}' \
  /dev/stdout
```

## Sample Queries

### Monthly Cost by Namespace

```sql
SELECT
  namespace,
  SUM(estimated_cost_usd) AS monthly_cost,
  SUM(total_gb) AS total_gb
FROM network_costs.network_cost_summary
WHERE date BETWEEN '2024-01-01' AND '2024-01-31'
GROUP BY namespace
ORDER BY monthly_cost DESC;
```

### Top Cross-AZ Flows

```sql
SELECT
  local_pod_namespace,
  local_service_name,
  remote_pod_namespace,
  remote_service_name,
  local_az,
  remote_az,
  SUM(gb) AS total_gb,
  SUM(estimated_cost_usd) AS cost
FROM network_costs.network_cost_details
WHERE destination_category = 'INTER_AZ'
  AND date >= date_format(current_date - interval '1' day, '%Y-%m-%d')
GROUP BY 1, 2, 3, 4, 5, 6
ORDER BY cost DESC
LIMIT 20;
```

### Cost by Category for a Namespace

```sql
SELECT
  destination_category,
  SUM(total_gb) AS total_gb,
  SUM(estimated_cost_usd) AS cost
FROM network_costs.network_cost_summary
WHERE namespace = 'production'
  AND date BETWEEN '2024-01-01' AND '2024-01-31'
GROUP BY destination_category
ORDER BY cost DESC;
```

## Project Structure

```
network-costs/
├── deploy.sh              # Deployment script for Lambda and supporting resources
├── lambda/
│   └── handler.py         # Lambda function
└── ui/
    ├── app.py             # Streamlit dashboard
    ├── Dockerfile
    ├── Makefile
    └── requirements.txt
```

## Limitations

- **Top 500 per category**: Network Flow Monitor returns only the top 500 contributors per destination category per hour. For most clusters this captures 95%+ of traffic, but very large clusters may miss long-tail flows.
- **Hourly granularity**: Data is aggregated hourly; sub-hour analysis is not available.
- **Pod metadata availability**: Some flows (node-level traffic, host-network pods) may not have full Kubernetes metadata.

## Cost Optimization Tips

Based on the data collected, consider these optimizations:

1. **Topology-aware routing**: Enable topology hints so services prefer same-AZ endpoints
2. **Pod placement**: Co-locate tightly-coupled services in the same AZ using affinity rules
3. **VPC endpoints**: Add gateway endpoints for S3/DynamoDB to eliminate NAT costs
4. **Review UNCLASSIFIED traffic**: Internet egress is expensive; consider caching or CDN

## License

MIT
