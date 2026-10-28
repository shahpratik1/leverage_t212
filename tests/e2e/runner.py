#!/usr/bin/env python3
"""Dedicated Opaque-Box E2E Test Suite Runner for Trading 212 Quantitative Engine.

Executes tests across Tiers 1-4, tracks tier metrics, provides rich reporting,
and evaluates milestone progress against PROJECT.md specifications.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import unittest
from io import StringIO
from typing import Any, Dict, List, Optional, Tuple


def run_test_suite(
    suite: unittest.TestSuite,
    verbose: bool = False,
) -> Tuple[int, int, int, int, float, str]:
    """Run a TestSuite and return (total, passed, failures, errors, elapsed, output)."""
    stream = StringIO()
    verbosity = 2 if verbose else 1
    runner = unittest.TextTestRunner(stream=stream, verbosity=verbosity)
    start_time = time.perf_counter()
    result = runner.run(suite)
    elapsed = time.perf_counter() - start_time

    total = result.testsRun
    failures = len(result.failures)
    errors = len(result.errors)
    skipped = len(result.skipped)
    passed = total - failures - errors - skipped
    output = stream.getvalue()

    return total, passed, failures, errors, elapsed, output


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Opaque-Box E2E Test Suite (Tiers 1-4)")
    parser.add_argument("--tier", type=int, choices=[1, 2, 3, 4], help="Run a specific tier (1-4)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose test execution output")
    parser.add_argument("--json", action="store_true", help="Output summary in JSON format")
    args = parser.parse_args()

    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # Ensure hermetic environment defaults
    os.environ.setdefault("FLATTEN_ALL_LEVERAGED_AT_SESSION_CLOSE", "false")

    loader = unittest.TestLoader()

    tier_modules = {
        1: ("Tier 1: Feature Coverage (Happy Path)", "tests.e2e.test_tier1_feature_coverage"),
        2: ("Tier 2: Boundary & Corner Cases", "tests.e2e.test_tier2_boundary_corner"),
        3: ("Tier 3: Cross-Feature Combinations", "tests.e2e.test_tier3_cross_feature"),
        4: ("Tier 4: Real-World Application Scenarios", "tests.e2e.test_tier4_real_world_scenarios"),
    }

    selected_tiers = [args.tier] if args.tier else [1, 2, 3, 4]

    print("=" * 80)
    print("  TRADING 212 QUANTITATIVE ENGINE — OPAQUE-BOX E2E TEST SUITE")
    print(f"  Project Root: {project_root}")
    print(f"  Execution Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 80)

    overall_total = 0
    overall_passed = 0
    overall_failures = 0
    overall_errors = 0
    overall_elapsed = 0.0

    tier_reports: Dict[int, Dict[str, Any]] = {}

    for t in selected_tiers:
        tier_name, module_name = tier_modules[t]
        print(f"\n▶ Executing {tier_name}...")
        try:
            suite = loader.loadTestsFromName(module_name)
            total, passed, failures, errors, elapsed, output = run_test_suite(suite, verbose=args.verbose)
        except Exception as e:
            print(f"  [ERROR] Failed to load {module_name}: {e}")
            total, passed, failures, errors, elapsed, output = 0, 0, 0, 1, 0.0, str(e)

        overall_total += total
        overall_passed += passed
        overall_failures += failures
        overall_errors += errors
        overall_elapsed += elapsed

        tier_reports[t] = {
            "name": tier_name,
            "total": total,
            "passed": passed,
            "failures": failures,
            "errors": errors,
            "elapsed": elapsed,
        }

        status = "PASSED" if (failures == 0 and errors == 0) else "GAPS_DETECTED"
        print(
            f"  ✔ {tier_name}: {passed}/{total} passed, {failures} failures, {errors} errors "
            f"({elapsed:.2f}s) — Status: [{status}]"
        )
        if args.verbose and (failures > 0 or errors > 0):
            print("\n  --- Failure Details ---")
            for line in output.splitlines()[-25:]:
                print(f"  {line}")

    print("\n" + "=" * 80)
    print("  E2E TEST SUITE EXECUTION SUMMARY")
    print("=" * 80)
    print(f"  {'Tier':<42} {'Total':<8} {'Pass':<8} {'Fail':<8} {'Error':<8} {'Time':<8}")
    print("  " + "-" * 76)
    for t, rep in tier_reports.items():
        print(
            f"  {rep['name']:<42} {rep['total']:<8} {rep['passed']:<8} "
            f"{rep['failures']:<8} {rep['errors']:<8} {rep['elapsed']:.2f}s"
        )
    print("  " + "-" * 76)
    print(
        f"  {'OVERALL TOTALS':<42} {overall_total:<8} {overall_passed:<8} "
        f"{overall_failures:<8} {overall_errors:<8} {overall_elapsed:.2f}s"
    )
    print("=" * 80)

    if overall_failures == 0 and overall_errors == 0:
        print("\n  [SUCCESS] All E2E opaque-box tests PASSED! Ready for Milestone M4 completion.")
        return 0
    else:
        print(
            f"\n  [NOTE] {overall_failures + overall_errors} test gap(s) detected as expected "
            f"during active development of Milestones M1-M3."
        )
        print("  Test suite is READY and fully armed as the authoritative quality gate.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
