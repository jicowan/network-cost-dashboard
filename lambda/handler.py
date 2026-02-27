"""
Network Cost Monitor - Lambda handler

Periodically queries CloudWatch Network Flow Monitor for top contributors
across all cost-relevant destination categories, and writes the results
to S3 as newline-delimited JSON partitioned by date and hour.
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

nfm = boto3.client("networkflowmonitor")
s3 = boto3.client("s3")
ssm = boto3.client("ssm")
athena = boto3.client("athena")

MONITOR_NAME = os.environ["MONITOR_NAME"]
S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX", "network-cost-data")
QUERY_LIMIT = int(os.environ.get("QUERY_LIMIT", "500"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
POLL_MAX_ATTEMPTS = int(os.environ.get("POLL_MAX_ATTEMPTS", "24"))
RATES_PARAMETER = os.environ.get(
    "RATES_PARAMETER", "/network-costs/rates-per-gb"
)
ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "network_costs")
ATHENA_OUTPUT = os.environ.get(
    "ATHENA_OUTPUT", f"s3://{S3_BUCKET}/athena-results/"
)

# Destination categories that incur cost (skip INTRA_AZ since it's free)
COST_CATEGORIES = [
    "INTER_AZ",
    "INTER_VPC",
    "INTER_REGION",
    "AMAZON_S3",
    "AMAZON_DYNAMODB",
    "UNCLASSIFIED",
]

# Fallback defaults if SSM parameter is missing (us-east-1 / us-west-2)
DEFAULT_RATES_PER_GB = {
    "INTRA_AZ": 0.00,
    "INTER_AZ": 0.02,        # $0.01 each direction
    "INTER_VPC": 0.02,       # same as inter-AZ if cross-AZ
    "INTER_REGION": 0.02,    # varies by region pair
    "AMAZON_S3": 0.00,       # free via gateway endpoint
    "AMAZON_DYNAMODB": 0.00, # free via gateway endpoint
    "UNCLASSIFIED": 0.09,    # internet egress (first 10TB tier)
}


def load_rates():
    """Load per-GB rates from SSM Parameter Store, falling back to defaults."""
    try:
        resp = ssm.get_parameter(Name=RATES_PARAMETER)
        rates = json.loads(resp["Parameter"]["Value"])
        logger.info("Loaded rates from SSM parameter %s", RATES_PARAMETER)
        return rates
    except ssm.exceptions.ParameterNotFound:
        logger.warning(
            "SSM parameter %s not found, using default rates", RATES_PARAMETER
        )
        return DEFAULT_RATES_PER_GB
    except Exception:
        logger.exception("Failed to load rates from SSM, using defaults")
        return DEFAULT_RATES_PER_GB


def handler(event, context):
    """
    Triggered by EventBridge on a schedule (e.g. hourly).
    Queries the last hour of data from Network Flow Monitor.
    """
    now = datetime.now(timezone.utc)

    # Default: query the previous hour
    end_time = now.replace(minute=0, second=0, microsecond=0)
    start_time = end_time - timedelta(hours=1)

    # Allow override from event (useful for backfills)
    if "start_time" in event:
        start_time = datetime.fromisoformat(event["start_time"])
    if "end_time" in event:
        end_time = datetime.fromisoformat(event["end_time"])

    rates = load_rates()

    logger.info(
        "Querying %s from %s to %s",
        MONITOR_NAME,
        start_time.isoformat(),
        end_time.isoformat(),
    )

    all_contributors = []

    for category in COST_CATEGORIES:
        contributors = query_top_contributors(
            start_time, end_time, category, rates
        )
        all_contributors.extend(contributors)
        logger.info(
            "Category %s: %d contributors, %.2f GB",
            category,
            len(contributors),
            sum(c["bytes"] for c in contributors) / (1024**3),
        )

    if not all_contributors:
        logger.info("No data returned for this period")
        return {"status": "empty", "period": start_time.isoformat()}

    # Build namespace-level summary
    summary = build_namespace_summary(all_contributors)

    # Write detailed records to S3 (one file per hour)
    date_partition = start_time.strftime("%Y-%m-%d")
    hour_partition = start_time.strftime("%H")

    detail_key = (
        f"{S3_PREFIX}/details/"
        f"date={date_partition}/hour={hour_partition}/data.json"
    )
    write_ndjson_to_s3(all_contributors, detail_key)

    # Write namespace summary to S3
    summary_key = (
        f"{S3_PREFIX}/summary/"
        f"date={date_partition}/hour={hour_partition}/summary.json"
    )
    write_ndjson_to_s3(summary, summary_key)

    # Register partitions in Athena
    add_partition("details", date_partition, hour_partition)
    add_partition("summary", date_partition, hour_partition)

    total_cost = sum(row["estimated_cost_usd"] for row in summary)
    logger.info(
        "Wrote %d detail records and %d summary records. "
        "Estimated cost for period: $%.4f",
        len(all_contributors),
        len(summary),
        total_cost,
    )

    return {
        "status": "ok",
        "period_start": start_time.isoformat(),
        "period_end": end_time.isoformat(),
        "total_contributors": len(all_contributors),
        "estimated_cost_usd": round(total_cost, 4),
    }


def query_top_contributors(start_time, end_time, category, rates):
    """Start a top-contributors query, poll for completion, return results."""
    resp = nfm.start_query_monitor_top_contributors(
        monitorName=MONITOR_NAME,
        startTime=start_time.isoformat(),
        endTime=end_time.isoformat(),
        metricName="DATA_TRANSFERRED",
        destinationCategory=category,
        limit=QUERY_LIMIT,
    )
    query_id = resp["queryId"]

    # Poll until results are ready
    for attempt in range(POLL_MAX_ATTEMPTS):
        try:
            status_resp = nfm.get_query_status_monitor_top_contributors(
                monitorName=MONITOR_NAME,
                queryId=query_id,
            )
            status = status_resp.get("status", "UNKNOWN")

            if status == "SUCCEEDED":
                break
            elif status == "FAILED":
                logger.error(
                    "Query failed for category %s: %s", category, status_resp
                )
                return []

            time.sleep(POLL_INTERVAL)
        except nfm.exceptions.ResourceNotFoundException:
            time.sleep(POLL_INTERVAL)
    else:
        logger.error(
            "Query timed out for category %s after %d attempts",
            category,
            POLL_MAX_ATTEMPTS,
        )
        return []

    # Paginate through results
    contributors = []
    paginator = nfm.get_paginator(
        "get_query_results_monitor_top_contributors"
    )

    for page in paginator.paginate(
        monitorName=MONITOR_NAME, queryId=query_id
    ):
        for c in page.get("topContributors", []):
            contributors.append(
                flatten_contributor(c, category, start_time, rates)
            )

    return contributors


def flatten_contributor(contributor, category, period_start, rates):
    """Flatten a top-contributor record into a simple dict for storage."""
    k8s = contributor.get("kubernetesMetadata", {})

    bytes_transferred = contributor.get("value", 0)
    gb = bytes_transferred / (1024**3)
    rate = rates.get(category, 0.0)

    return {
        "period_start": period_start.isoformat(),
        "destination_category": category,
        # Local (source) info
        "local_ip": contributor.get("localIp", ""),
        "local_az": contributor.get("localAz", ""),
        "local_vpc_id": contributor.get("localVpcId", ""),
        "local_subnet_id": contributor.get("localSubnetId", ""),
        "local_instance_id": contributor.get("localInstanceId", ""),
        "local_region": contributor.get("localRegion", ""),
        # Remote (destination) info
        "remote_ip": contributor.get("remoteIp", ""),
        "remote_az": contributor.get("remoteAz", ""),
        "remote_vpc_id": contributor.get("remoteVpcId", ""),
        "remote_subnet_id": contributor.get("remoteSubnetId", ""),
        "remote_instance_id": contributor.get("remoteInstanceId", ""),
        "remote_region": contributor.get("remoteRegion", ""),
        # Kubernetes metadata
        "local_pod_name": k8s.get("localPodName", ""),
        "local_pod_namespace": k8s.get("localPodNamespace", ""),
        "local_service_name": k8s.get("localServiceName", ""),
        "remote_pod_name": k8s.get("remotePodName", ""),
        "remote_pod_namespace": k8s.get("remotePodNamespace", ""),
        "remote_service_name": k8s.get("remoteServiceName", ""),
        # Traffic and NAT
        "snat_ip": contributor.get("snatIp", ""),
        "dnat_ip": contributor.get("dnatIp", ""),
        "target_port": contributor.get("targetPort", 0),
        # Traversed constructs (NAT GW, TGW, etc.)
        "traversed_constructs": json.dumps([
            {
                "component_id": tc.get("componentId", ""),
                "component_type": tc.get("componentType", ""),
                "service_name": tc.get("serviceName", ""),
            }
            for tc in contributor.get("traversedConstructs", [])
        ]),
        # Metrics
        "bytes": bytes_transferred,
        "gb": round(gb, 6),
        "rate_per_gb": rate,
        "estimated_cost_usd": round(gb * rate, 6),
    }


def build_namespace_summary(contributors):
    """Aggregate contributors into a namespace-level cost summary."""
    agg = defaultdict(lambda: defaultdict(float))

    for c in contributors:
        ns = c["local_pod_namespace"] or "(non-pod)"
        cat = c["destination_category"]
        key = (ns, cat)
        agg[key]["bytes"] += c["bytes"]
        agg[key]["estimated_cost_usd"] += c["estimated_cost_usd"]

    summary = []
    for (namespace, category), metrics in agg.items():
        gb = metrics["bytes"] / (1024**3)
        summary.append({
            "period_start": contributors[0]["period_start"],
            "namespace": namespace,
            "destination_category": category,
            "total_bytes": int(metrics["bytes"]),
            "total_gb": round(gb, 4),
            "estimated_cost_usd": round(metrics["estimated_cost_usd"], 4),
        })

    summary.sort(key=lambda x: x["estimated_cost_usd"], reverse=True)
    return summary


def write_ndjson_to_s3(records, key):
    """Write records as newline-delimited JSON to S3."""
    body = "\n".join(json.dumps(r) for r in records)
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
    )
    logger.info("Wrote %d records to s3://%s/%s", len(records), S3_BUCKET, key)


def add_partition(table_name, date_partition, hour_partition):
    """Register a partition in the Athena/Glue catalog."""
    location = (
        f"s3://{S3_BUCKET}/{S3_PREFIX}/{table_name}/"
        f"date={date_partition}/hour={hour_partition}/"
    )
    query = (
        f"ALTER TABLE {ATHENA_DATABASE}.network_cost_{table_name} "
        f"ADD IF NOT EXISTS PARTITION (date='{date_partition}', hour='{hour_partition}') "
        f"LOCATION '{location}'"
    )
    try:
        athena.start_query_execution(
            QueryString=query,
            ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
        )
        logger.info("Added partition date=%s/hour=%s to %s",
                    date_partition, hour_partition, table_name)
    except Exception:
        logger.exception("Failed to add partition for %s", table_name)
