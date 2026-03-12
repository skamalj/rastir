"""Part Two verification runner: generate data for TTFT + rate() panels.

Orchestrates:
  1. Strands streaming TTFT test (5 prompts)  → rastir_ttft_seconds_bucket
  2. LangGraph success requests (5 prompts)   → rate() data (agents, tools, LLM)
  3. Repeat round after a pause               → 2nd data point for rate()
  4. Error generation (5 scenarios)            → error rate panels

Run:
    conda run -n llmobserve PYTHONPATH=src \
        python tests/e2e/run_part2_verification.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

E2E_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(E2E_DIR, "..", "..", "src")
ENV = {**os.environ, "PYTHONPATH": SRC_DIR}


def _run_script(name: str, script: str) -> bool:
    """Run a Python script and return True if it succeeds."""
    path = os.path.join(E2E_DIR, script)
    if not os.path.exists(path):
        print(f"  SKIP: {path} not found")
        return False
    print(f"\n{'='*60}")
    print(f"  Running: {name}")
    print(f"{'='*60}")
    result = subprocess.run(
        [sys.executable, path],
        env=ENV,
        cwd=os.path.join(E2E_DIR, "..", ".."),
    )
    return result.returncode == 0


def main():
    print("=" * 70)
    print("  PART TWO: Generate data for TTFT + rate() dashboard panels")
    print("=" * 70)

    results: list[tuple[str, bool]] = []

    # --- Round 1 ---
    print("\n>>> ROUND 1 <<<")

    ok = _run_script("Strands streaming TTFT (round 1)", "test_strands_streaming_ttft.py")
    results.append(("Strands TTFT (round 1)", ok))

    ok = _run_script("LangGraph success requests (round 1)", "run_success_requests.py")
    results.append(("LangGraph volume (round 1)", ok))

    ok = _run_script("Strands basic e2e", "test_strands_e2e.py")
    results.append(("Strands basic (round 1)", ok))

    ok = _run_script("LangGraph e2e test", "test_langgraph_e2e.py")
    results.append(("LangGraph e2e (round 1)", ok))

    # --- Pause for Prometheus scrape ---
    wait_secs = 20
    print(f"\n>>> Waiting {wait_secs}s for Prometheus scrape interval... <<<")
    time.sleep(wait_secs)

    # --- Round 2 (second data point for rate()) ---
    print("\n>>> ROUND 2 (for rate() panels) <<<")

    ok = _run_script("Strands streaming TTFT (round 2)", "test_strands_streaming_ttft.py")
    results.append(("Strands TTFT (round 2)", ok))

    ok = _run_script("LangGraph success requests (round 2)", "run_success_requests.py")
    results.append(("LangGraph volume (round 2)", ok))

    # --- Error generation ---
    ok = _run_script("Error generation", "generate_errors.py")
    results.append(("Error generation", ok))

    # --- Summary ---
    print("\n" + "=" * 70)
    print("  PART TWO SUMMARY")
    print("=" * 70)
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")

    passed = sum(1 for _, ok in results if ok)
    print(f"\n  {passed}/{len(results)} scripts completed successfully")

    if passed > 0:
        print("\n  Data generated. Wait ~30s for Prometheus scrape, then run:")
        print("    python scripts/verify_dashboards.py")
    print()


if __name__ == "__main__":
    main()
