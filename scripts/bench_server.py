#!/usr/bin/env python3
"""Benchmark the Rastir collector server at various request rates.

Usage:
    python bench_server.py
"""

import asyncio
import time
import uuid
import statistics
import aiohttp

SERVER_URL = "http://127.0.0.1:8099/v1/telemetry"
RATES = [500, 1000, 2000, 3000, 4000, 5000]  # requests per second
DURATION = 10  # seconds per rate level
SPANS_PER_REQUEST = 10  # realistic: one agent call ≈ 10 spans


def make_payload(service: str = "bench-svc") -> dict:
    """Build a realistic span batch payload."""
    trace_id = uuid.uuid4().hex
    agent_span_id = uuid.uuid4().hex[:16]
    now = time.time()

    spans = []
    for i in range(SPANS_PER_REQUEST):
        spans.append({
            "name": f"bench.span.{i}",
            "span_type": "llm" if i % 3 == 0 else ("tool" if i % 3 == 1 else "trace"),
            "trace_id": trace_id,
            "span_id": uuid.uuid4().hex[:16],
            "parent_span_id": agent_span_id,
            "start_time": now - 0.5,
            "end_time": now,
            "attributes": {
                "model": "gpt-4o",
                "provider": "openai",
                "agent": "bench_agent",
                "tokens_in": 100,
                "tokens_out": 50,
            },
        })
    # Make first span the agent parent
    spans[0]["name"] = "bench_agent"
    spans[0]["span_type"] = "agent"
    spans[0]["span_id"] = agent_span_id
    spans[0]["parent_span_id"] = None

    return {"service": service, "env": "bench", "version": "0.0.1", "spans": spans}


async def run_at_rate(rate: int, duration: int) -> dict:
    """Send requests at a fixed rate for `duration` seconds. Return stats."""
    total_requests = rate * duration
    interval = 1.0 / rate
    latencies = []
    errors_429 = 0
    errors_other = 0
    succeeded = 0

    connector = aiohttp.TCPConnector(limit=500, limit_per_host=500)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        sem = asyncio.Semaphore(500)  # max concurrent

        async def send_one(idx: int):
            nonlocal errors_429, errors_other, succeeded
            payload = make_payload(service=f"bench-svc-{idx % 5}")
            async with sem:
                t0 = time.monotonic()
                try:
                    async with session.post(SERVER_URL, json=payload) as resp:
                        elapsed = (time.monotonic() - t0) * 1000  # ms
                        latencies.append(elapsed)
                        if resp.status == 202:
                            succeeded += 1
                        elif resp.status == 429:
                            errors_429 += 1
                        else:
                            errors_other += 1
                except Exception:
                    errors_other += 1

        tasks = []
        t_start = time.monotonic()
        for i in range(total_requests):
            # Schedule at the right time
            target_time = t_start + i * interval
            now = time.monotonic()
            delay = target_time - now
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(send_one(i)))

        # Wait for all in-flight requests
        await asyncio.gather(*tasks)

    wall_time = time.monotonic() - t_start
    actual_rps = total_requests / wall_time

    result = {
        "target_rps": rate,
        "actual_rps": round(actual_rps, 1),
        "total_requests": total_requests,
        "succeeded": succeeded,
        "rejected_429": errors_429,
        "errors": errors_other,
        "wall_time_s": round(wall_time, 2),
        "spans_ingested": succeeded * SPANS_PER_REQUEST,
    }

    if latencies:
        result["latency_p50_ms"] = round(statistics.median(latencies), 2)
        result["latency_p95_ms"] = round(sorted(latencies)[int(len(latencies) * 0.95)], 2)
        result["latency_p99_ms"] = round(sorted(latencies)[int(len(latencies) * 0.99)], 2)
        result["latency_max_ms"] = round(max(latencies), 2)

    return result


async def main():
    print(f"Rastir Collector Benchmark")
    print(f"{'='*70}")
    print(f"Endpoint: {SERVER_URL}")
    print(f"Spans per request: {SPANS_PER_REQUEST}")
    print(f"Duration per rate: {DURATION}s")
    print()

    # Quick health check
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:8099/metrics", timeout=aiohttp.ClientTimeout(total=3)) as r:
                if r.status != 200:
                    print("ERROR: Server not responding on port 8099")
                    return
    except Exception:
        print("ERROR: Cannot connect to server on port 8099. Start it first.")
        return

    print(f"{'Rate':>10} {'Actual':>10} {'OK':>8} {'429s':>8} {'Err':>6} "
          f"{'p50ms':>8} {'p95ms':>8} {'p99ms':>8} {'max_ms':>8} {'spans':>10}")
    print("-" * 96)

    for rate in RATES:
        # Small pause between runs to let the server settle
        await asyncio.sleep(2)
        result = await run_at_rate(rate, DURATION)
        print(f"{result['target_rps']:>7}/s "
              f"{result['actual_rps']:>7}/s "
              f"{result['succeeded']:>8} "
              f"{result['rejected_429']:>8} "
              f"{result['errors']:>6} "
              f"{result.get('latency_p50_ms', '-'):>8} "
              f"{result.get('latency_p95_ms', '-'):>8} "
              f"{result.get('latency_p99_ms', '-'):>8} "
              f"{result.get('latency_max_ms', '-'):>8} "
              f"{result['spans_ingested']:>10}")

    print()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
