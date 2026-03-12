#!/usr/bin/env python3
"""Consolidated e2e test runner for Rastir.

Runs end-to-end tests across all supported frameworks, providers, and modes.
Replaces 23 individual test scripts with a single modular entry point.

Usage:
  # Run all 14 core framework combinations (no errors/eval/ttft)
  python tests/e2e/run_e2e.py

  # Run a single framework
  python tests/e2e/run_e2e.py --framework langgraph
  python tests/e2e/run_e2e.py --framework crewai

  # Run cross-framework error generation
  python tests/e2e/run_e2e.py --errors

  # Run evaluation pipeline (default 50 requests)
  python tests/e2e/run_e2e.py --evaluation
  python tests/e2e/run_e2e.py --evaluation --count 2   # quick mode

  # Run TTFT streaming test
  python tests/e2e/run_e2e.py --ttft

  # Run manual @agent/@llm/@trace decorator tests
  python tests/e2e/run_e2e.py --manual-decorators

  # Run everything
  python tests/e2e/run_e2e.py --all

  # Run dashboard verification suite (2 rounds + errors)
  python tests/e2e/run_e2e.py --verification

  # Combine flags freely
  python tests/e2e/run_e2e.py --framework langgraph --framework strands --errors --ttft

Environment variables:
  GEMINI_API_KEY / GOOGLE_API_KEY   — Required for LangGraph/ADK
  API_OPENAI_KEY / OPENAI_API_KEY   — Required for CrewAI/LlamaIndex
  AWS_REGION / AWS_DEFAULT_REGION   — Required for Strands/Bedrock
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from tests.e2e.common import (
    TestResults,
    configure_rastir,
    install_span_capture,
    start_mcp_server,
)

FRAMEWORKS = ["langgraph", "crewai", "llamaindex", "adk", "strands"]


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _banner(text: str):
    print(f"\n{'=' * 60}")
    print(f"  [{_ts()}] {text}")
    print(f"{'=' * 60}", flush=True)


def _section(text: str):
    print(f"\n{'─' * 60}")
    print(f"  [{_ts()}] {text}")
    print(f"{'─' * 60}", flush=True)


# ===================================================================
#  CLI argument parser
# ===================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Consolidated Rastir e2e test runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--framework", action="append", choices=FRAMEWORKS, default=None,
        help="Run scenarios for a specific framework (repeatable)",
    )
    p.add_argument(
        "--errors", action="store_true",
        help="Run cross-framework error generation scenarios",
    )
    p.add_argument(
        "--evaluation", action="store_true",
        help="Run evaluation pipeline test",
    )
    p.add_argument(
        "--count", type=int, default=50,
        help="Number of evaluation requests (default: 50, use 2 for quick)",
    )
    p.add_argument(
        "--ttft", action="store_true",
        help="Run Strands TTFT streaming test",
    )
    p.add_argument(
        "--manual-decorators", action="store_true",
        help="Run manual @agent/@llm/@trace decorator tests",
    )
    p.add_argument(
        "--all", action="store_true",
        help="Run everything: all frameworks + errors + eval + ttft + decorators",
    )
    p.add_argument(
        "--verification", action="store_true",
        help="Dashboard verification: 2 rounds of TTFT+frameworks, then errors",
    )
    p.add_argument(
        "--include-errors", action="store_true", default=True,
        help="Include per-framework error scenarios within --framework runs (default: True)",
    )
    p.add_argument(
        "--no-include-errors", dest="include_errors", action="store_false",
        help="Exclude per-framework error scenarios from --framework runs",
    )
    return p


# ===================================================================
#  Framework dispatchers — lazy-import to avoid pulling in unused deps
# ===================================================================

async def _run_framework(name: str, results: TestResults, *, include_errors: bool = True):
    """Dispatch to the appropriate scenario module's run_all()."""
    if name == "langgraph":
        from tests.e2e.scenarios.langgraph import run_all
    elif name == "crewai":
        from tests.e2e.scenarios.crewai import run_all
    elif name == "llamaindex":
        from tests.e2e.scenarios.llamaindex import run_all
    elif name == "adk":
        from tests.e2e.scenarios.adk import run_all
    elif name == "strands":
        from tests.e2e.scenarios.strands import run_all
    else:
        print(f"Unknown framework: {name}")
        return
    await run_all(results, include_errors=include_errors)


async def _run_errors(results: TestResults):
    from tests.e2e.scenarios.errors import run_all
    await run_all(results)


async def _run_manual_decorators(results: TestResults):
    from tests.e2e.scenarios.manual_decorators import run_all
    await run_all(results)


async def _run_evaluation(results: TestResults, count: int):
    from tests.e2e.scenarios.evaluation import run_all
    await run_all(results, count=count)


async def _run_ttft(results: TestResults):
    from tests.e2e.scenarios.strands import run_ttft
    await run_ttft(results)


# ===================================================================
#  Verification mode — generates data for rate() dashboard panels
# ===================================================================

async def _run_verification(results: TestResults):
    """Dashboard verification: 2 rounds + errors for rate() panels.

    Round structure:
      1. TTFT streaming (Strands)
      2. All 5 frameworks
      3. Pause 20s for Prometheus scrape
      4. Repeat TTFT + LangGraph + Strands (2nd data point)
      5. Cross-framework errors
    """
    for round_num in (1, 2):
        _banner(f"ROUND {round_num}")

        _section(f"Strands TTFT (round {round_num})")
        await _run_ttft(results)

        if round_num == 1:
            # Full framework sweep in round 1
            for fw in FRAMEWORKS:
                _section(f"{fw.title()} (round {round_num})")
                await _run_framework(fw, results, include_errors=False)
        else:
            # Subset in round 2 for rate() second data point
            for fw in ["langgraph", "strands"]:
                _section(f"{fw.title()} (round {round_num})")
                await _run_framework(fw, results, include_errors=False)

        if round_num == 1:
            wait_secs = 20
            print(f"\n>>> Waiting {wait_secs}s for Prometheus scrape interval... <<<",
                  flush=True)
            await asyncio.sleep(wait_secs)

    # Error generation
    _banner("Error Generation")
    await _run_errors(results)


# ===================================================================
#  Main
# ===================================================================

async def _main(args: argparse.Namespace):
    # Determine what to run
    run_frameworks = args.framework  # None = not specified
    run_errors = args.errors
    run_eval = args.evaluation
    run_ttft_flag = args.ttft
    run_decorators = args.manual_decorators
    run_verification = args.verification

    if args.all:
        run_frameworks = FRAMEWORKS
        run_errors = True
        run_eval = True
        run_ttft_flag = True
        run_decorators = True

    # Default: run all frameworks if nothing specific was requested
    nothing_requested = (
        not run_frameworks and not run_errors and not run_eval
        and not run_ttft_flag and not run_decorators and not run_verification
    )
    if nothing_requested:
        run_frameworks = FRAMEWORKS

    # --- Setup ---
    _banner("Rastir E2E Test Suite")
    configure_rastir()
    install_span_capture()
    mcp_url = start_mcp_server()
    print(f"  MCP server: {mcp_url}")

    results = TestResults()
    test_start = time.monotonic()

    # --- Verification mode (has its own orchestration) ---
    if run_verification:
        await _run_verification(results)
    else:
        # --- Framework scenarios ---
        if run_frameworks:
            for fw in run_frameworks:
                _banner(f"Framework: {fw.title()}")
                await _run_framework(fw, results, include_errors=args.include_errors)

        # --- Manual decorators ---
        if run_decorators:
            _banner("Manual Decorators (@agent/@llm/@trace)")
            await _run_manual_decorators(results)

        # --- Cross-framework errors ---
        if run_errors:
            _banner("Cross-Framework Error Scenarios")
            await _run_errors(results)

        # --- TTFT ---
        if run_ttft_flag:
            _banner("TTFT Streaming (Strands)")
            await _run_ttft(results)

        # --- Evaluation ---
        if run_eval:
            _banner(f"Evaluation Pipeline ({args.count} requests)")
            await _run_evaluation(results, args.count)

    # --- Summary ---
    elapsed = time.monotonic() - test_start
    results.print_summary(f"E2E Test Summary ({elapsed:.1f}s)")

    if not results.all_passed:
        sys.exit(1)


def main():
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
