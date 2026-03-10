#!/usr/bin/env python3
"""
Standalone unit test for cumulative tiered pricing calculation.

Run: python3 test_cumulative_tiers.py
"""

# Default egress tiers (same as handler.py)
DEFAULT_EGRESS_TIERS = [
    {"begin_gb": 0, "end_gb": 10240, "price_per_gb": 0.09},
    {"begin_gb": 10240, "end_gb": 51200, "price_per_gb": 0.085},
    {"begin_gb": 51200, "end_gb": 153600, "price_per_gb": 0.07},
    {"begin_gb": 153600, "end_gb": float("inf"), "price_per_gb": 0.05},
]

GB = 1024**3


def calculate_tiered_cost(
    new_bytes: int,
    tiers: list,
    cumulative_bytes_before: int = 0,
) -> float:
    """
    Calculate cost using tiered pricing based on monthly cumulative volume.

    The cost is calculated for only the new_bytes, but the tier placement
    is determined by where cumulative_bytes_before falls in the tiers.

    Args:
        new_bytes: New bytes transferred in this period
        tiers: List of tier dicts with begin_gb, end_gb, price_per_gb
        cumulative_bytes_before: Bytes already transferred this month (before this period)

    Returns:
        Cost in USD for the new_bytes only
    """
    cumulative_gb_before = cumulative_bytes_before / (1024**3)
    new_gb = new_bytes / (1024**3)
    cumulative_gb_after = cumulative_gb_before + new_gb

    total_cost = 0.0

    for tier in tiers:
        tier_start = tier["begin_gb"]
        tier_end = tier["end_gb"]
        tier_rate = tier["price_per_gb"]

        # Skip tiers we've already passed completely
        if cumulative_gb_before >= tier_end:
            continue

        # Stop if we haven't reached this tier yet
        if cumulative_gb_after <= tier_start:
            break

        # Calculate the portion of new transfer that falls in this tier
        effective_start = max(cumulative_gb_before, tier_start)
        effective_end = min(cumulative_gb_after, tier_end)
        gb_in_tier = effective_end - effective_start

        if gb_in_tier > 0:
            tier_cost = gb_in_tier * tier_rate
            total_cost += tier_cost

    return total_cost


def test_fresh_start():
    """Test 1: Fresh start - 100 GB should be all at $0.09/GB"""
    print("Test 1: Fresh start (0 cumulative), transfer 100 GB")
    cost = calculate_tiered_cost(100 * GB, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=0)
    expected = 100 * 0.09  # $9.00
    passed = abs(cost - expected) < 0.01
    print(f"  Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    return passed


def test_cross_tier_1_to_2():
    """Test 2: Already at 10,000 GB, transfer 500 GB (crosses into tier 2)"""
    print("\nTest 2: 10,000 GB cumulative, transfer 500 GB (crosses tier 1->2)")
    cumulative_before = 10000 * GB
    new_bytes = 500 * GB
    cost = calculate_tiered_cost(new_bytes, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=cumulative_before)
    # 240 GB in tier 1 @ $0.09 + 260 GB in tier 2 @ $0.085
    expected = (240 * 0.09) + (260 * 0.085)  # $21.60 + $22.10 = $43.70
    passed = abs(cost - expected) < 0.01
    print(f"  240 GB @ $0.09 + 260 GB @ $0.085 = ${expected:.2f}")
    print(f"  Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    return passed


def test_stay_in_tier_2():
    """Test 3: Already in tier 2 (20,000 GB), transfer 100 GB (stays in tier 2)"""
    print("\nTest 3: 20,000 GB cumulative (tier 2), transfer 100 GB (stays in tier 2)")
    cumulative_before = 20000 * GB
    new_bytes = 100 * GB
    cost = calculate_tiered_cost(new_bytes, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=cumulative_before)
    expected = 100 * 0.085  # $8.50
    passed = abs(cost - expected) < 0.01
    print(f"  Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    return passed


def test_cross_tier_2_to_3():
    """Test 4: At tier 2/3 boundary (51,000 GB), transfer 500 GB (crosses into tier 3)"""
    print("\nTest 4: 51,000 GB cumulative, transfer 500 GB (crosses tier 2->3)")
    cumulative_before = 51000 * GB
    new_bytes = 500 * GB
    cost = calculate_tiered_cost(new_bytes, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=cumulative_before)
    # 200 GB in tier 2 @ $0.085 + 300 GB in tier 3 @ $0.07
    expected = (200 * 0.085) + (300 * 0.07)  # $17.00 + $21.00 = $38.00
    passed = abs(cost - expected) < 0.01
    print(f"  200 GB @ $0.085 + 300 GB @ $0.07 = ${expected:.2f}")
    print(f"  Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    return passed


def test_tier_4():
    """Test 5: Already in tier 4 (200,000 GB), transfer 1000 GB"""
    print("\nTest 5: 200,000 GB cumulative (tier 4), transfer 1000 GB")
    cumulative_before = 200000 * GB
    new_bytes = 1000 * GB
    cost = calculate_tiered_cost(new_bytes, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=cumulative_before)
    expected = 1000 * 0.05  # $50.00
    passed = abs(cost - expected) < 0.01
    print(f"  Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    return passed


def test_cumulative_equals_single():
    """Test 6: Verify cumulative tracking produces same total as single calculation"""
    print("\nTest 6: Verify cumulative tracking matches single calculation")
    # Calculate 15 TB in one go
    single_cost = calculate_tiered_cost(15000 * GB, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=0)
    # Calculate same amount in 3 chunks: 5TB + 5TB + 5TB
    chunk1 = calculate_tiered_cost(5000 * GB, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=0)
    chunk2 = calculate_tiered_cost(5000 * GB, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=5000 * GB)
    chunk3 = calculate_tiered_cost(5000 * GB, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=10000 * GB)
    cumulative_cost = chunk1 + chunk2 + chunk3
    passed = abs(single_cost - cumulative_cost) < 0.01
    print(f"  Single 15TB calculation: ${single_cost:.2f}")
    print(f"  3x 5TB chunks: ${chunk1:.2f} + ${chunk2:.2f} + ${chunk3:.2f} = ${cumulative_cost:.2f}")
    print(f"  Match: {'✓' if passed else '✗'}")
    return passed


def test_span_multiple_tiers():
    """Test 7: Single transfer that spans multiple tiers"""
    print("\nTest 7: Transfer 50,000 GB from fresh start (spans tiers 1, 2, and part of 3)")
    cost = calculate_tiered_cost(50000 * GB, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=0)
    # 10,240 GB @ $0.09 + 39,760 GB @ $0.085
    expected = (10240 * 0.09) + (39760 * 0.085)
    passed = abs(cost - expected) < 0.01
    print(f"  10,240 GB @ $0.09 = ${10240 * 0.09:.2f}")
    print(f"  39,760 GB @ $0.085 = ${39760 * 0.085:.2f}")
    print(f"  Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    return passed


def test_zero_bytes():
    """Test 8: Zero bytes should cost nothing"""
    print("\nTest 8: Zero bytes transfer")
    cost = calculate_tiered_cost(0, DEFAULT_EGRESS_TIERS, cumulative_bytes_before=5000 * GB)
    expected = 0.0
    passed = abs(cost - expected) < 0.01
    print(f"  Expected: ${expected:.2f}, Got: ${cost:.2f} {'✓' if passed else '✗'}")
    return passed


def main():
    print("=" * 60)
    print("Cumulative Tiered Pricing Test Suite")
    print("=" * 60)
    print("\nTier Structure:")
    print("  Tier 1: 0-10,240 GB @ $0.09/GB")
    print("  Tier 2: 10,240-51,200 GB @ $0.085/GB")
    print("  Tier 3: 51,200-153,600 GB @ $0.07/GB")
    print("  Tier 4: 153,600+ GB @ $0.05/GB")
    print("=" * 60)

    tests = [
        test_fresh_start,
        test_cross_tier_1_to_2,
        test_stay_in_tier_2,
        test_cross_tier_2_to_3,
        test_tier_4,
        test_cumulative_equals_single,
        test_span_multiple_tiers,
        test_zero_bytes,
    ]

    results = []
    for test_fn in tests:
        try:
            results.append(test_fn())
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append(False)

    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Results: {passed}/{total} tests passed")
    print("=" * 60)

    return 0 if all(results) else 1


if __name__ == "__main__":
    exit(main())
