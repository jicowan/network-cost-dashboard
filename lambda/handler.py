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
from typing import Optional

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

nfm = boto3.client("networkflowmonitor")
s3 = boto3.client("s3")
athena = boto3.client("athena")
pricing = boto3.client("pricing", region_name="us-east-1")

MONITOR_NAME = os.environ["MONITOR_NAME"]
S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX", "network-cost-data")
QUERY_LIMIT = int(os.environ.get("QUERY_LIMIT", "500"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
POLL_MAX_ATTEMPTS = int(os.environ.get("POLL_MAX_ATTEMPTS", "24"))
ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "network_costs")
ATHENA_OUTPUT = os.environ.get(
    "ATHENA_OUTPUT", f"s3://{S3_BUCKET}/athena-results/"
)
# Set to "true" if VPC Gateway Endpoints exist for S3/DynamoDB (traffic is free)
# Set to "false" if no gateway endpoints (traffic goes through NAT Gateway @ $0.045/GB)
HAS_S3_ENDPOINT = os.environ.get("HAS_S3_ENDPOINT", "false").lower() == "true"
HAS_DYNAMODB_ENDPOINT = os.environ.get("HAS_DYNAMODB_ENDPOINT", "false").lower() == "true"

# Current AWS region for pricing lookups (AWS_REGION is auto-set by Lambda runtime)
AWS_REGION = os.environ.get("AWS_REGION") or boto3.session.Session().region_name or "us-west-2"

# Destination categories that incur cost (skip INTRA_AZ since it's free)
COST_CATEGORIES = [
    "INTER_AZ",
    "INTER_VPC",
    "INTER_REGION",
    "AMAZON_S3",
    "AMAZON_DYNAMODB",
    "UNCLASSIFIED",
]

# Map AWS region codes to location names used in Pricing API
REGION_TO_LOCATION = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-north-1": "EU (Stockholm)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "sa-east-1": "South America (Sao Paulo)",
    "ca-central-1": "Canada (Central)",
    "me-south-1": "Middle East (Bahrain)",
    "af-south-1": "Africa (Cape Town)",
}

# Fallback defaults if pricing API fails (us-east-1 / us-west-2 rates)
DEFAULT_RATES_PER_GB = {
    "INTRA_AZ": 0.00,
    "INTER_AZ": 0.02,        # $0.01 each direction
    "INTER_VPC": 0.02,       # same as inter-AZ if cross-AZ
    "INTER_REGION": 0.02,    # varies by region pair
    "AMAZON_S3": 0.045,      # NAT Gateway processing (no gateway endpoint)
    "AMAZON_DYNAMODB": 0.045, # NAT Gateway processing (no gateway endpoint)
    "UNCLASSIFIED": 0.09,    # internet egress (first 10TB tier)
}

# Internet egress tiers (used for UNCLASSIFIED category)
# These are fetched dynamically but cached here as fallback
DEFAULT_EGRESS_TIERS = [
    {"begin_gb": 0, "end_gb": 10240, "price_per_gb": 0.09},
    {"begin_gb": 10240, "end_gb": 51200, "price_per_gb": 0.085},
    {"begin_gb": 51200, "end_gb": 153600, "price_per_gb": 0.07},
    {"begin_gb": 153600, "end_gb": float("inf"), "price_per_gb": 0.05},
]

# Cache for pricing data (persists across warm Lambda invocations)
_pricing_cache = {
    "rates": None,
    "egress_tiers": None,
    "timestamp": None,
}
PRICING_CACHE_TTL_SECONDS = 3600  # Refresh pricing every hour


def get_data_transfer_price(
    transfer_type: str,
    from_region: str,
    to_region: Optional[str] = None,
) -> list[dict]:
    """
    Get data transfer pricing from AWS Pricing API.

    Args:
        transfer_type: One of 'IntraRegion', 'AWS Inbound', 'AWS Outbound',
                      'InterRegion Inbound', 'InterRegion Outbound'
        from_region: Source region code (e.g., 'us-west-2')
        to_region: Destination region code (required for InterRegion transfers)

    Returns:
        List of pricing info dicts with price_per_gb, begin_range, end_range
    """
    from_location = REGION_TO_LOCATION.get(from_region)
    if not from_location:
        logger.warning("Unknown region %s, cannot fetch pricing", from_region)
        return []

    filters = [
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Data Transfer"},
        {"Type": "TERM_MATCH", "Field": "transferType", "Value": transfer_type},
        {"Type": "TERM_MATCH", "Field": "fromLocation", "Value": from_location},
    ]

    if to_region:
        to_location = REGION_TO_LOCATION.get(to_region)
        if to_location:
            filters.append(
                {"Type": "TERM_MATCH", "Field": "toLocation", "Value": to_location}
            )

    # Use AWSDataTransfer for internet egress, AmazonEC2 for intra-region
    service_code = "AWSDataTransfer" if transfer_type == "AWS Outbound" else "AmazonEC2"

    results = []
    try:
        paginator = pricing.get_paginator("get_products")
        for page in paginator.paginate(ServiceCode=service_code, Filters=filters):
            for price_item in page["PriceList"]:
                product = json.loads(price_item)
                on_demand = product.get("terms", {}).get("OnDemand", {})

                for term in on_demand.values():
                    for dimension in term["priceDimensions"].values():
                        begin_range = dimension.get("beginRange", "0")
                        end_range = dimension.get("endRange", "Inf")
                        results.append({
                            "price_per_gb": float(dimension["pricePerUnit"].get("USD", "0")),
                            "begin_range": float(begin_range) if begin_range != "Inf" else float("inf"),
                            "end_range": float(end_range) if end_range != "Inf" else float("inf"),
                            "description": dimension.get("description", ""),
                        })
    except Exception:
        logger.exception("Failed to fetch pricing for %s", transfer_type)

    return results


def get_nat_gateway_rate(region: str) -> float:
    """Get NAT Gateway data processing rate for a region."""
    location = REGION_TO_LOCATION.get(region)
    if not location:
        return 0.045  # Default NAT Gateway rate

    try:
        resp = pricing.get_products(
            ServiceCode="AmazonEC2",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "NAT Gateway"},
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                {"Type": "TERM_MATCH", "Field": "usagetype", "Value": f"{region.replace('-', '').upper()}-NatGateway-Bytes"},
            ],
            MaxResults=10,
        )
        for price_item in resp.get("PriceList", []):
            product = json.loads(price_item)
            on_demand = product.get("terms", {}).get("OnDemand", {})
            for term in on_demand.values():
                for dimension in term["priceDimensions"].values():
                    price = float(dimension["pricePerUnit"].get("USD", "0"))
                    if price > 0:
                        return price
    except Exception:
        logger.exception("Failed to fetch NAT Gateway rate")

    return 0.045  # Fallback default


def fetch_dynamic_rates(region: str) -> tuple[dict, list]:
    """
    Fetch current data transfer rates from AWS Pricing API.

    Returns:
        Tuple of (rates_dict, egress_tiers_list)
    """
    rates = {
        "INTRA_AZ": 0.00,  # Always free
    }
    egress_tiers = []

    # Inter-AZ (IntraRegion) pricing
    intra_region = get_data_transfer_price("IntraRegion", region)
    if intra_region:
        # IntraRegion is charged per direction, so $0.01 * 2 = $0.02
        rate = intra_region[0]["price_per_gb"]
        rates["INTER_AZ"] = rate * 2  # Both directions
        rates["INTER_VPC"] = rate * 2  # Same as inter-AZ when cross-AZ
    else:
        rates["INTER_AZ"] = DEFAULT_RATES_PER_GB["INTER_AZ"]
        rates["INTER_VPC"] = DEFAULT_RATES_PER_GB["INTER_VPC"]

    # Inter-Region pricing (use us-east-1 as representative destination)
    inter_region = get_data_transfer_price("InterRegion Outbound", region, "us-east-1")
    if inter_region:
        rates["INTER_REGION"] = inter_region[0]["price_per_gb"]
    else:
        rates["INTER_REGION"] = DEFAULT_RATES_PER_GB["INTER_REGION"]

    # S3/DynamoDB - depends on whether gateway endpoints exist
    if HAS_S3_ENDPOINT:
        rates["AMAZON_S3"] = 0.00
    else:
        # Without gateway endpoint, traffic goes through NAT Gateway
        nat_rate = get_nat_gateway_rate(region)
        rates["AMAZON_S3"] = nat_rate

    if HAS_DYNAMODB_ENDPOINT:
        rates["AMAZON_DYNAMODB"] = 0.00
    else:
        nat_rate = get_nat_gateway_rate(region)
        rates["AMAZON_DYNAMODB"] = nat_rate

    # Internet egress (AWS Outbound) - tiered pricing
    egress_prices = get_data_transfer_price("AWS Outbound", region)
    if egress_prices:
        # Sort by begin_range to get proper tier order
        egress_prices.sort(key=lambda x: x["begin_range"])
        for p in egress_prices:
            egress_tiers.append({
                "begin_gb": p["begin_range"],
                "end_gb": p["end_range"],
                "price_per_gb": p["price_per_gb"],
            })
        # Use first tier rate as the flat rate for simple lookups
        rates["UNCLASSIFIED"] = egress_prices[0]["price_per_gb"]
    else:
        rates["UNCLASSIFIED"] = DEFAULT_RATES_PER_GB["UNCLASSIFIED"]
        egress_tiers = DEFAULT_EGRESS_TIERS.copy()

    return rates, egress_tiers


def load_rates() -> tuple[dict, list]:
    """
    Load per-GB rates, using cached dynamic pricing or fetching fresh rates.

    Returns:
        Tuple of (rates_dict, egress_tiers_list)
    """
    global _pricing_cache

    now = time.time()

    # Check if cache is still valid
    if (
        _pricing_cache["rates"] is not None
        and _pricing_cache["timestamp"] is not None
        and (now - _pricing_cache["timestamp"]) < PRICING_CACHE_TTL_SECONDS
    ):
        logger.info("Using cached pricing data")
        return _pricing_cache["rates"], _pricing_cache["egress_tiers"]

    # Try to fetch dynamic rates
    logger.info("Fetching dynamic pricing from AWS Pricing API for region %s", AWS_REGION)
    try:
        rates, egress_tiers = fetch_dynamic_rates(AWS_REGION)
        _pricing_cache["rates"] = rates
        _pricing_cache["egress_tiers"] = egress_tiers
        _pricing_cache["timestamp"] = now
        logger.info("Loaded dynamic rates: %s", rates)
        return rates, egress_tiers
    except Exception:
        logger.exception("Failed to fetch dynamic pricing, using defaults")
        return DEFAULT_RATES_PER_GB, DEFAULT_EGRESS_TIERS


def calculate_tiered_cost(total_bytes: int, tiers: list) -> float:
    """
    Calculate cost using tiered pricing based on total volume.

    Args:
        total_bytes: Total bytes transferred
        tiers: List of tier dicts with begin_gb, end_gb, price_per_gb

    Returns:
        Total cost in USD
    """
    total_gb = total_bytes / (1024**3)
    total_cost = 0.0
    remaining_gb = total_gb

    for tier in tiers:
        if remaining_gb <= 0:
            break

        tier_start = tier["begin_gb"]
        tier_end = tier["end_gb"]
        tier_size = tier_end - tier_start

        if total_gb > tier_start:
            # How much of this tier do we use?
            gb_in_tier = min(remaining_gb, tier_size)
            tier_cost = gb_in_tier * tier["price_per_gb"]
            total_cost += tier_cost
            remaining_gb -= gb_in_tier

    return total_cost


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

    rates, egress_tiers = load_rates()

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

    # Recalculate UNCLASSIFIED costs using tiered pricing based on total volume
    unclassified_bytes = sum(
        c["bytes"] for c in all_contributors if c["destination_category"] == "UNCLASSIFIED"
    )
    if unclassified_bytes > 0:
        total_tiered_cost = calculate_tiered_cost(unclassified_bytes, egress_tiers)
        # Distribute cost proportionally across contributors
        for c in all_contributors:
            if c["destination_category"] == "UNCLASSIFIED":
                proportion = c["bytes"] / unclassified_bytes
                c["estimated_cost_usd"] = round(total_tiered_cost * proportion, 6)
                c["rate_per_gb"] = round(total_tiered_cost / (unclassified_bytes / (1024**3)), 6)

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
