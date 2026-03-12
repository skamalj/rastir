"""Shared infrastructure for all e2e test scenarios.

This module provides common utilities that every e2e scenario needs:

  1. API key retrieval — reads from standard env vars with fallbacks
  2. MCP test server lifecycle — start/wait/shared in-process server
  3. Span capture — monkey-patches rastir.queue.enqueue_span
  4. Result tracking — collects pass/fail per scenario and prints summary
  5. Pricing registration — registers known model prices
  6. Rastir configuration — single configure() call with all features

All scenario modules import from here instead of duplicating boilerplate.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import threading
import time
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Timestamped progress logging — import this in every scenario module
# ---------------------------------------------------------------------------
def log(msg: str):
    """Print a timestamped log message. Flush immediately for real-time output."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Path setup — ensure src/ is importable
# ---------------------------------------------------------------------------
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


# ---------------------------------------------------------------------------
# API key retrieval
# ---------------------------------------------------------------------------
def get_gemini_key() -> str:
    """Return GEMINI_API_KEY / GOOGLE_API_KEY or empty string."""
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")


def get_openai_key() -> str:
    """Return API_OPENAI_KEY / OPENAI_API_KEY or empty string."""
    return os.environ.get("API_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY", "")


def require_gemini_key() -> str:
    key = get_gemini_key()
    if not key:
        print("ERROR: GEMINI_API_KEY or GOOGLE_API_KEY not set")
        sys.exit(1)
    return key


def require_openai_key() -> str:
    key = get_openai_key()
    if not key:
        print("ERROR: API_OPENAI_KEY or OPENAI_API_KEY not set")
        sys.exit(1)
    # Many frameworks (CrewAI, LlamaIndex) read OPENAI_API_KEY directly
    os.environ["OPENAI_API_KEY"] = key
    return key


# ---------------------------------------------------------------------------
# Rastir configuration — called once at startup
# ---------------------------------------------------------------------------
_configured = False


def configure_rastir(service: str = "rastir-e2e", **kwargs):
    """Configure rastir with sensible defaults. Safe to call multiple times."""
    global _configured
    if _configured:
        return
    import rastir
    from rastir import configure

    defaults = dict(
        service=service,
        push_url="http://localhost:8080",
        enable_cost_calculation=True,
        evaluation_enabled=True,
        enable_ttft=True,
    )
    defaults.update(kwargs)
    try:
        configure(**defaults)
    except RuntimeError:
        pass  # already configured
    _configured = True

    # Register known model prices (USD per 1M tokens)
    from rastir.config import get_pricing_registry
    pr = get_pricing_registry()
    if pr:
        pr.register("gemini", "gemini-2.5-flash", input_price=0.15, output_price=0.60)
        pr.register("openai", "gpt-4o-mini", input_price=0.15, output_price=0.60)
        pr.register("openai", "gpt-4o-mini-2024-07-18", input_price=0.15, output_price=0.60)
        pr.register("bedrock", "apac.anthropic.claude-sonnet-4-20250514-v1:0",
                     input_price=3.0, output_price=15.0)
        pr.register("bedrock", "us.anthropic.claude-sonnet-4-20250514-v1:0",
                     input_price=3.0, output_price=15.0)
        pr.register("bedrock", "us.amazon.nova-pro-v1:0",
                     input_price=0.80, output_price=3.20)


# ---------------------------------------------------------------------------
# Span capture — intercept enqueue_span to inspect spans in-process
# ---------------------------------------------------------------------------
_span_capture_installed = False
captured_spans: list = []
_orig_enqueue = None


def install_span_capture():
    """Monkey-patch rastir to capture spans for in-process verification.

    Call once after configure_rastir(). Subsequent calls are no-ops.
    Captured spans accumulate in ``common.captured_spans``.
    """
    global _span_capture_installed, _orig_enqueue
    if _span_capture_installed:
        return
    import rastir.queue as _queue
    import rastir.wrapper as _wrapper

    _orig_enqueue = _queue.enqueue_span

    def _capture(span):
        captured_spans.append(span)
        if _orig_enqueue:
            _orig_enqueue(span)

    _queue.enqueue_span = _capture
    _wrapper.enqueue_span = _capture

    # Also patch decorators module if it exists
    try:
        import rastir.decorators as _decorators
        _decorators.enqueue_span = _capture
    except (ImportError, AttributeError):
        pass

    _span_capture_installed = True


def clear_captured_spans():
    """Clear captured spans before a new test scenario."""
    captured_spans.clear()


# ---------------------------------------------------------------------------
# MCP test server management
# ---------------------------------------------------------------------------
_mcp_server_started = False
_mcp_lock = threading.Lock()

# Default port; overridden by start_mcp_server()
MCP_PORT = 19876


def start_mcp_server(port: int = 19876) -> str:
    """Start the MCP test server in a background thread.

    Returns the base URL for the MCP endpoint (e.g. http://127.0.0.1:19876/mcp).
    Safe to call multiple times — only starts one server per process.
    """
    global _mcp_server_started, MCP_PORT
    url = f"http://127.0.0.1:{port}/mcp"

    with _mcp_lock:
        if _mcp_server_started:
            return url
        MCP_PORT = port
        import uvicorn

        spec = importlib.util.spec_from_file_location(
            "mcp_test_server",
            os.path.join(os.path.dirname(__file__), "mcp_test_server.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        app = mod.create_app(port)

        def _run():
            uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
            ).run()

        threading.Thread(target=_run, daemon=True).start()
        if not _wait_for_server(url):
            print(f"FATAL: MCP test server did not start on port {port}")
            sys.exit(1)
        _mcp_server_started = True
        return url


def _wait_for_server(url: str, timeout: float = 10) -> bool:
    """Poll until the server responds or timeout."""
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=2) as c:
                r = c.get(url.replace("/mcp", "/"))
                if r.status_code < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# W3C traceparent header helper (for CrewAI/ADK MCP HTTP calls)
# ---------------------------------------------------------------------------
def traceparent_headers() -> dict[str, str]:
    """Return W3C traceparent headers from the current rastir context."""
    from rastir.remote import traceparent_headers as _tp
    return _tp()


# ---------------------------------------------------------------------------
# MCP JSON-RPC helper (for frameworks that call MCP via HTTP POST)
# ---------------------------------------------------------------------------
def call_mcp_tool(mcp_url: str, tool_name: str, arguments: dict) -> str:
    """Call an MCP tool via JSON-RPC POST and return the text result."""
    import httpx
    hdrs = {"Accept": "application/json", **traceparent_headers()}
    with httpx.Client(timeout=10) as c:
        r = c.post(
            mcp_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
            headers=hdrs,
        )
        data = r.json()
        content = data.get("result", {}).get("content", [{}])
        return content[0].get("text", str(data)) if content else str(data)


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
@dataclass
class TestResults:
    """Collects pass/fail results across scenarios."""
    entries: list[tuple[str, str]] = field(default_factory=list)

    def record(self, label: str, status: str):
        self.entries.append((label, status))

    def passed(self, label: str):
        self.entries.append((label, "PASS ✓"))

    def failed(self, label: str, error: Exception | str):
        self.entries.append((label, f"FAIL ✗ ({error})"))

    def print_summary(self, title: str = "E2E Test Summary"):
        """Print a formatted summary table."""
        print(f"\n{'═' * 60}")
        print(f"  {title}")
        print(f"{'═' * 60}")
        pass_count = 0
        for label, status in self.entries:
            print(f"  {label}: {status}")
            if "PASS" in status:
                pass_count += 1
        print(f"\n  {pass_count}/{len(self.entries)} passed")
        print(f"{'═' * 60}")

    @property
    def all_passed(self) -> bool:
        return all("PASS" in s for _, s in self.entries)


# ---------------------------------------------------------------------------
# Span analysis helpers
# ---------------------------------------------------------------------------
def print_spans(spans: list, *, show_attributes: bool = False):
    """Pretty-print captured spans with timing info."""
    if not spans:
        print("  (no spans captured)")
        return
    t0 = min(s.start_time for s in spans)
    for s in spans:
        agent_attr = s.attributes.get("agent", s.attributes.get("agent_name", ""))
        agent_str = f" agent={agent_attr}" if agent_attr else ""
        cost = s.attributes.get("cost_usd", "")
        cost_str = f" cost=${cost:.6f}" if cost else ""
        rel_start = (s.start_time - t0) * 1000
        dur = (s.end_time - s.start_time) * 1000 if s.end_time else 0
        print(
            f"  - {s.name} ({s.span_type.value}){agent_str}{cost_str}"
            f"  +{rel_start:.0f}ms  dur={dur:.0f}ms"
        )


def count_spans_by_type(spans: list) -> dict[str, int]:
    """Count spans grouped by span_type."""
    counts: dict[str, int] = {}
    for s in spans:
        t = s.span_type.value
        counts[t] = counts.get(t, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Async execution helper
# ---------------------------------------------------------------------------
async def run_async_scenario(label: str, coro, results: TestResults):
    """Run an async coroutine and record pass/fail."""
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"{'─' * 60}")
    try:
        answer = await coro
        text = str(getattr(answer, "raw", answer))
        if isinstance(answer, dict) and "messages" in answer:
            text = str(answer["messages"][-1].content)
        print(f"  ✓ Result: {text[:200]}")
        results.passed(label)
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        results.failed(label, e)
    await asyncio.sleep(1)


def run_sync_scenario(label: str, fn, results: TestResults):
    """Run a sync callable and record pass/fail."""
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"{'─' * 60}")
    try:
        answer = fn()
        text = str(getattr(answer, "raw", answer))
        print(f"  ✓ Result: {text[:200]}")
        results.passed(label)
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        results.failed(label, e)
