#!/usr/bin/env python3
"""
Standalone unit tests for flow deduplication logic.

Run: cd lambda && python3 test_deduplication.py
"""

import sys
from collections import defaultdict

# Categories needing dedup (same as handler.py)
CATEGORIES_NEEDING_DEDUP = {"INTER_AZ", "INTER_VPC", "INTER_REGION"}

# Traffic direction classification (same as handler.py)
EGRESS_CATEGORIES = {"UNCLASSIFIED", "AMAZON_S3", "AMAZON_DYNAMODB"}
INTERNAL_CATEGORIES = {"INTER_AZ", "INTER_VPC", "INTER_REGION", "INTRA_AZ"}


def get_traffic_direction(category: str) -> str:
    """Copy of function from handler.py for standalone testing."""
    if category in EGRESS_CATEGORIES:
        return "egress"
    return "internal"


def get_namespace_attribution(contributor: dict) -> str:
    """Copy of function from handler.py for standalone testing."""
    if contributor.get("local_pod_namespace"):
        return contributor["local_pod_namespace"]
    instance_id = contributor.get("local_instance_id")
    if instance_id:
        return f"node:{instance_id}"
    return "(unattributed)"


def deduplicate_flows(contributors: list) -> list:
    """Copy of function from handler.py for standalone testing."""
    needs_dedup = []
    no_dedup = []

    for flow in contributors:
        if flow["destination_category"] in CATEGORIES_NEEDING_DEDUP:
            needs_dedup.append(flow)
        else:
            no_dedup.append(flow)

    if not needs_dedup:
        return contributors

    flow_groups = defaultdict(list)
    for flow in needs_dedup:
        ip_pair = tuple(sorted([flow["local_ip"], flow["remote_ip"]]))
        key = (
            flow["period_start"],
            flow["destination_category"],
            ip_pair,
            flow["target_port"],
        )
        flow_groups[key].append(flow)

    deduplicated = []
    for key, flows in flow_groups.items():
        if len(flows) > 1:
            flows_with_metadata = [f for f in flows if f.get("local_pod_namespace")]
            if flows_with_metadata:
                deduplicated.append(flows_with_metadata[0])
            else:
                deduplicated.append(flows[0])
        else:
            deduplicated.append(flows[0])

    return deduplicated + no_dedup


def make_flow(local_ip, remote_ip, category, port, bytes_val,
              local_ns="", local_instance="i-abc123", period="2024-01-01T00:00:00"):
    """Helper to create a flow dict."""
    return {
        "period_start": period,
        "destination_category": category,
        "local_ip": local_ip,
        "remote_ip": remote_ip,
        "local_pod_namespace": local_ns,
        "local_instance_id": local_instance,
        "target_port": port,
        "bytes": bytes_val,
        "estimated_cost_usd": bytes_val / (1024**3) * 0.02,
    }


def test_mirrored_flows_deduplicated():
    """Mirrored INTER_AZ flows should be deduplicated."""
    print("\nTest 1: Mirrored INTER_AZ flows are deduplicated")

    flows = [
        make_flow("192.168.1.10", "192.168.2.20", "INTER_AZ", 5432, 5_000_000, local_ns="app"),
        make_flow("192.168.2.20", "192.168.1.10", "INTER_AZ", 5432, 5_000_000, local_ns="db"),
    ]

    result = deduplicate_flows(flows)
    passed = len(result) == 1
    print(f"  Input: 2 mirrored flows, Output: {len(result)} flow(s) {'✓' if passed else '✗'}")
    return passed


def test_unclassified_not_deduplicated():
    """UNCLASSIFIED flows should NOT be deduplicated (no agent on remote)."""
    print("\nTest 2: UNCLASSIFIED flows are not deduplicated")

    flows = [
        make_flow("192.168.1.10", "52.1.2.3", "UNCLASSIFIED", 443, 1_000_000),
        make_flow("192.168.1.11", "52.1.2.4", "UNCLASSIFIED", 443, 2_000_000),
    ]

    result = deduplicate_flows(flows)
    passed = len(result) == 2
    print(f"  Input: 2 UNCLASSIFIED flows, Output: {len(result)} flow(s) {'✓' if passed else '✗'}")
    return passed


def test_different_ports_not_deduplicated():
    """Flows with different ports should not be deduplicated."""
    print("\nTest 3: Different ports are not deduplicated")

    flows = [
        make_flow("192.168.1.10", "192.168.2.20", "INTER_AZ", 5432, 5_000_000),
        make_flow("192.168.1.10", "192.168.2.20", "INTER_AZ", 8080, 3_000_000),
    ]

    result = deduplicate_flows(flows)
    passed = len(result) == 2
    print(f"  Input: 2 flows (different ports), Output: {len(result)} flow(s) {'✓' if passed else '✗'}")
    return passed


def test_prefers_flow_with_metadata():
    """When deduplicating, prefer the flow with K8s metadata."""
    print("\nTest 4: Prefers flow with K8s metadata")

    flows = [
        make_flow("192.168.1.10", "192.168.2.20", "INTER_AZ", 5432, 5_000_000, local_ns=""),
        make_flow("192.168.2.20", "192.168.1.10", "INTER_AZ", 5432, 5_000_000, local_ns="myapp"),
    ]

    result = deduplicate_flows(flows)
    passed = len(result) == 1 and result[0]["local_pod_namespace"] == "myapp"
    print(f"  Kept flow has namespace: '{result[0]['local_pod_namespace']}' {'✓' if passed else '✗'}")
    return passed


def test_mixed_categories():
    """Mixed categories: only INTER_* deduplicated."""
    print("\nTest 5: Mixed categories - only INTER_* deduplicated")

    flows = [
        # Mirrored INTER_AZ pair -> should become 1
        make_flow("192.168.1.10", "192.168.2.20", "INTER_AZ", 5432, 5_000_000),
        make_flow("192.168.2.20", "192.168.1.10", "INTER_AZ", 5432, 5_000_000),
        # UNCLASSIFIED -> stays as 2
        make_flow("192.168.1.10", "52.1.2.3", "UNCLASSIFIED", 443, 1_000_000),
        make_flow("192.168.1.11", "52.1.2.4", "UNCLASSIFIED", 443, 2_000_000),
    ]

    result = deduplicate_flows(flows)
    inter_az = [f for f in result if f["destination_category"] == "INTER_AZ"]
    unclassified = [f for f in result if f["destination_category"] == "UNCLASSIFIED"]

    passed = len(inter_az) == 1 and len(unclassified) == 2
    print(f"  INTER_AZ: {len(inter_az)} (expected 1), UNCLASSIFIED: {len(unclassified)} (expected 2) {'✓' if passed else '✗'}")
    return passed


def test_namespace_attribution_with_namespace():
    """Namespace attribution prefers local_pod_namespace."""
    print("\nTest 6: Namespace attribution - has namespace")

    flow = make_flow("192.168.1.10", "192.168.2.20", "INTER_AZ", 5432, 5_000_000,
                     local_ns="production", local_instance="i-abc123")

    ns = get_namespace_attribution(flow)
    passed = ns == "production"
    print(f"  Attribution: '{ns}' {'✓' if passed else '✗'}")
    return passed


def test_namespace_attribution_fallback_to_instance():
    """Falls back to instance ID when no namespace."""
    print("\nTest 7: Namespace attribution - falls back to instance ID")

    flow = make_flow("192.168.1.10", "52.1.2.3", "UNCLASSIFIED", 443, 1_000_000,
                     local_ns="", local_instance="i-xyz789")

    ns = get_namespace_attribution(flow)
    passed = ns == "node:i-xyz789"
    print(f"  Attribution: '{ns}' {'✓' if passed else '✗'}")
    return passed


def test_namespace_attribution_unattributed():
    """Returns (unattributed) when no metadata available."""
    print("\nTest 8: Namespace attribution - unattributed")

    flow = {"local_pod_namespace": "", "local_instance_id": ""}

    ns = get_namespace_attribution(flow)
    passed = ns == "(unattributed)"
    print(f"  Attribution: '{ns}' {'✓' if passed else '✗'}")
    return passed


def test_direction_egress_categories():
    """External traffic categories should be classified as egress."""
    print("\nTest 9: Direction - egress categories")

    results = []
    for category in ["UNCLASSIFIED", "AMAZON_S3", "AMAZON_DYNAMODB"]:
        direction = get_traffic_direction(category)
        passed = direction == "egress"
        results.append(passed)
        print(f"  {category}: '{direction}' {'✓' if passed else '✗'}")

    return all(results)


def test_direction_internal_categories():
    """Internal cluster traffic should be classified as internal."""
    print("\nTest 10: Direction - internal categories")

    results = []
    for category in ["INTER_AZ", "INTER_VPC", "INTER_REGION", "INTRA_AZ"]:
        direction = get_traffic_direction(category)
        passed = direction == "internal"
        results.append(passed)
        print(f"  {category}: '{direction}' {'✓' if passed else '✗'}")

    return all(results)


def main():
    print("=" * 60)
    print("Flow Deduplication Test Suite")
    print("=" * 60)
    print(f"\nCategories needing dedup: {CATEGORIES_NEEDING_DEDUP}")

    tests = [
        test_mirrored_flows_deduplicated,
        test_unclassified_not_deduplicated,
        test_different_ports_not_deduplicated,
        test_prefers_flow_with_metadata,
        test_mixed_categories,
        test_namespace_attribution_with_namespace,
        test_namespace_attribution_fallback_to_instance,
        test_namespace_attribution_unattributed,
        test_direction_egress_categories,
        test_direction_internal_categories,
    ]

    results = []
    for test_fn in tests:
        try:
            results.append(test_fn())
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()
            results.append(False)

    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Results: {passed}/{total} tests passed")
    print("=" * 60)

    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
