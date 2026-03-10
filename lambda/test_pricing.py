#!/usr/bin/env python3
"""
Test script for dynamic pricing functions.

Run locally to verify pricing API calls work before deploying:
    cd lambda
    python test_pricing.py
"""

import os
import sys

# Set environment variables before importing handler
os.environ.setdefault("MONITOR_NAME", "test")
os.environ.setdefault("S3_BUCKET", "test")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("HAS_S3_ENDPOINT", "false")
os.environ.setdefault("HAS_DYNAMODB_ENDPOINT", "false")

from handler import (
    get_data_transfer_price,
    get_nat_gateway_rate,
    fetch_dynamic_rates,
    calculate_tiered_cost,
    REGION_TO_LOCATION,
    DEFAULT_EGRESS_TIERS,
)


def test_region_mapping():
    """Test that region mapping is correct."""
    print("\n=== Testing Region Mapping ===")
    test_regions = ["us-west-2", "us-east-1", "eu-west-1", "ap-northeast-1"]

    for region in test_regions:
        location = REGION_TO_LOCATION.get(region)
        if location:
            print(f"  {region} -> {location}")
        else:
            print(f"  {region} -> NOT FOUND (will use defaults)")

    return True


def test_intra_region_pricing():
    """Test fetching inter-AZ (IntraRegion) pricing."""
    print("\n=== Testing IntraRegion Pricing ===")

    prices = get_data_transfer_price("IntraRegion", "us-west-2")

    if prices:
        print(f"  Found {len(prices)} price tier(s):")
        for p in prices:
            print(f"    ${p['price_per_gb']:.4f}/GB ({p['description'][:60]}...)")
        return True
    else:
        print("  WARNING: No prices returned (will use defaults)")
        return False


def test_inter_region_pricing():
    """Test fetching inter-region pricing."""
    print("\n=== Testing InterRegion Pricing ===")

    prices = get_data_transfer_price("InterRegion Outbound", "us-west-2", "us-east-1")

    if prices:
        print(f"  Found {len(prices)} price tier(s):")
        for p in prices:
            print(f"    ${p['price_per_gb']:.4f}/GB ({p['description'][:60]}...)")
        return True
    else:
        print("  WARNING: No prices returned (will use defaults)")
        return False


def test_internet_egress_pricing():
    """Test fetching internet egress (AWS Outbound) pricing with tiers."""
    print("\n=== Testing Internet Egress Pricing (Tiered) ===")

    prices = get_data_transfer_price("AWS Outbound", "us-west-2")

    if prices:
        # Sort by begin_range
        prices.sort(key=lambda x: x["begin_range"])
        print(f"  Found {len(prices)} price tier(s):")
        for p in prices:
            end = "Inf" if p["end_range"] == float("inf") else f"{p['end_range']:.0f}"
            print(f"    {p['begin_range']:.0f}-{end} GB: ${p['price_per_gb']:.4f}/GB")
        return True
    else:
        print("  WARNING: No prices returned (will use defaults)")
        return False


def test_nat_gateway_rate():
    """Test fetching NAT Gateway processing rate."""
    print("\n=== Testing NAT Gateway Rate ===")

    rate = get_nat_gateway_rate("us-west-2")
    print(f"  NAT Gateway rate: ${rate:.4f}/GB")

    if rate > 0:
        return True
    else:
        print("  WARNING: Rate is $0 (may be incorrect)")
        return False


def test_fetch_dynamic_rates():
    """Test the complete rate fetching function."""
    print("\n=== Testing Complete Rate Fetch ===")

    rates, egress_tiers = fetch_dynamic_rates("us-west-2")

    print("  Rates:")
    for category, rate in sorted(rates.items()):
        print(f"    {category}: ${rate:.4f}/GB")

    print("\n  Egress Tiers:")
    for tier in egress_tiers:
        end = "Inf" if tier["end_gb"] == float("inf") else f"{tier['end_gb']:.0f}"
        print(f"    {tier['begin_gb']:.0f}-{end} GB: ${tier['price_per_gb']:.4f}/GB")

    return bool(rates)


def test_tiered_cost_calculation():
    """Test the tiered cost calculation function."""
    print("\n=== Testing Tiered Cost Calculation (Fresh Start) ===")

    test_cases = [
        (1 * (1024**3), "1 GB"),           # 1 GB
        (100 * (1024**3), "100 GB"),       # 100 GB
        (5000 * (1024**3), "5 TB"),        # 5 TB (within first tier)
        (15000 * (1024**3), "15 TB"),      # 15 TB (spans first two tiers)
        (100000 * (1024**3), "100 TB"),    # 100 TB (spans multiple tiers)
    ]

    for bytes_val, label in test_cases:
        cost = calculate_tiered_cost(bytes_val, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=0)
        gb = bytes_val / (1024**3)
        effective_rate = cost / gb if gb > 0 else 0
        print(f"  {label:>10}: ${cost:>12,.2f} (effective rate: ${effective_rate:.4f}/GB)")

    return True


def test_cumulative_tiered_pricing():
    """Test tiered cost calculation with cumulative monthly usage."""
    print("\n=== Testing Cumulative Tiered Pricing ===")

    # Tier boundaries from DEFAULT_EGRESS_TIERS:
    # Tier 1: 0-10,240 GB @ $0.09/GB
    # Tier 2: 10,240-51,200 GB @ $0.085/GB
    # Tier 3: 51,200-153,600 GB @ $0.07/GB
    # Tier 4: 153,600+ GB @ $0.05/GB

    GB = 1024**3
    all_passed = True

    # Test 1: Fresh start - 100 GB should be all at $0.09/GB
    print("\n  Test 1: Fresh start (0 cumulative), transfer 100 GB")
    cost = calculate_tiered_cost(100 * GB, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=0)
    expected = 100 * 0.09  # $9.00
    passed = abs(cost - expected) < 0.01
    print(f"    Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    all_passed = all_passed and passed

    # Test 2: Already at 10,000 GB, transfer 500 GB (crosses into tier 2)
    print("\n  Test 2: 10,000 GB cumulative, transfer 500 GB (crosses tier 1->2)")
    cumulative_before = 10000 * GB
    new_bytes = 500 * GB
    cost = calculate_tiered_cost(new_bytes, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=cumulative_before)
    # 240 GB in tier 1 @ $0.09 + 260 GB in tier 2 @ $0.085
    expected = (240 * 0.09) + (260 * 0.085)  # $21.60 + $22.10 = $43.70
    passed = abs(cost - expected) < 0.01
    print(f"    240 GB @ $0.09 + 260 GB @ $0.085 = ${expected:.2f}")
    print(f"    Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    all_passed = all_passed and passed

    # Test 3: Already in tier 2 (20,000 GB), transfer 100 GB (stays in tier 2)
    print("\n  Test 3: 20,000 GB cumulative (tier 2), transfer 100 GB (stays in tier 2)")
    cumulative_before = 20000 * GB
    new_bytes = 100 * GB
    cost = calculate_tiered_cost(new_bytes, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=cumulative_before)
    expected = 100 * 0.085  # $8.50
    passed = abs(cost - expected) < 0.01
    print(f"    Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    all_passed = all_passed and passed

    # Test 4: At tier 2/3 boundary (51,000 GB), transfer 500 GB (crosses into tier 3)
    print("\n  Test 4: 51,000 GB cumulative, transfer 500 GB (crosses tier 2->3)")
    cumulative_before = 51000 * GB
    new_bytes = 500 * GB
    cost = calculate_tiered_cost(new_bytes, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=cumulative_before)
    # 200 GB in tier 2 @ $0.085 + 300 GB in tier 3 @ $0.07
    expected = (200 * 0.085) + (300 * 0.07)  # $17.00 + $21.00 = $38.00
    passed = abs(cost - expected) < 0.01
    print(f"    200 GB @ $0.085 + 300 GB @ $0.07 = ${expected:.2f}")
    print(f"    Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    all_passed = all_passed and passed

    # Test 5: Already in tier 4 (200,000 GB), transfer 1000 GB
    print("\n  Test 5: 200,000 GB cumulative (tier 4), transfer 1000 GB")
    cumulative_before = 200000 * GB
    new_bytes = 1000 * GB
    cost = calculate_tiered_cost(new_bytes, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=cumulative_before)
    expected = 1000 * 0.05  # $50.00
    passed = abs(cost - expected) < 0.01
    print(f"    Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    all_passed = all_passed and passed

    # Test 6: Verify cumulative tracking produces same total as single calculation
    print("\n  Test 6: Verify cumulative tracking matches single calculation")
    # Calculate 15 TB in one go
    single_cost = calculate_tiered_cost(15000 * GB, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=0)
    # Calculate same amount in 3 chunks: 5TB + 5TB + 5TB
    chunk1 = calculate_tiered_cost(5000 * GB, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=0)
    chunk2 = calculate_tiered_cost(5000 * GB, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=5000 * GB)
    chunk3 = calculate_tiered_cost(5000 * GB, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=10000 * GB)
    cumulative_cost = chunk1 + chunk2 + chunk3
    passed = abs(single_cost - cumulative_cost) < 0.01
    print(f"    Single 15TB calculation: ${single_cost:.2f}")
    print(f"    3x 5TB chunks: ${chunk1:.2f} + ${chunk2:.2f} + ${chunk3:.2f} = ${cumulative_cost:.2f}")
    print(f"    Match: {'✓' if passed else '✗'}")
    all_passed = all_passed and passed

    return all_passed


def main():
    print("=" * 60)
    print("Dynamic Pricing Test Suite")
    print("=" * 60)

    tests = [
        ("Region Mapping", test_region_mapping),
        ("IntraRegion Pricing", test_intra_region_pricing),
        ("InterRegion Pricing", test_inter_region_pricing),
        ("Internet Egress Pricing", test_internet_egress_pricing),
        ("NAT Gateway Rate", test_nat_gateway_rate),
        ("Complete Rate Fetch", test_fetch_dynamic_rates),
        ("Tiered Cost Calculation", test_tiered_cost_calculation),
        ("Cumulative Tiered Pricing", test_cumulative_tiered_pricing),
    ]

    results = []
    for name, test_fn in tests:
        try:
            result = test_fn()
            results.append((name, result))
        except Exception as e:
            print(f"\n  ERROR: {e}")
            results.append((name, False))

    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)

    all_passed = True
    for name, passed in results:
        status = "PASS" if passed else "WARN"
        if not passed:
            all_passed = False
        print(f"  [{status}] {name}")

    print()
    if all_passed:
        print("All tests passed! Safe to deploy.")
        return 0
    else:
        print("Some tests had warnings. Review output above.")
        print("The Lambda will fall back to default rates if API calls fail.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
