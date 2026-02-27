# Client Configuration & Push API Requirements

## 1. Objective

Define how the observability client library is configured for:

-   Global metadata (service, env, version)
-   Telemetry push endpoint
-   Authentication
-   Export batching and retry behavior
-   Environment variable fallback
-   Runtime initialization rules

Configuration must be cleanly separated from decorators, adapters, and
span logic.

------------------------------------------------------------------------

## 2. Configuration Ownership

Push configuration belongs exclusively to the **Exporter/Transport
layer**.

Decorators (`@trace`, `@llm`, `@metric`, etc.) must NOT: - Read
environment variables - Know about HTTP endpoints - Manage
authentication - Perform network operations

They only emit spans to an internal queue.

------------------------------------------------------------------------

## 3. Configuration Sources & Precedence

Configuration precedence must follow:

1.  Explicit `configure()` call (highest priority)
2.  Environment variables
3.  Library defaults (lowest priority)

------------------------------------------------------------------------

## 4. Programmatic Configuration API

The library must expose a single initialization function:

``` python
configure(
    service: str | None = None,
    env: str | None = None,
    version: str | None = None,
    push_url: str | None = None,
    api_key: str | None = None,
    batch_size: int | None = None,
    flush_interval: int | None = None,
    timeout: int | None = None,
)
```

**Implementation note:** All parameters are optional (`None` default)
rather than requiring `service` and `env` as positional arguments.
This enables environment-variable-only initialization and allows
`get_config()` to auto-initialize from env vars / defaults without
requiring an explicit `configure()` call.

### Responsibilities

-   Initialize global configuration
-   Initialize exporter
-   Start background batching worker
-   Lock configuration (immutable after initialization)

Configuration must be called at application startup.

------------------------------------------------------------------------

## 5. Environment Variable Fallback

Supported environment variables:

-   LLMOBSERVE_SERVICE
-   LLMOBSERVE_ENV
-   LLMOBSERVE_VERSION
-   LLMOBSERVE_PUSH_URL
-   LLMOBSERVE_API_KEY
-   LLMOBSERVE_BATCH_SIZE
-   LLMOBSERVE_FLUSH_INTERVAL
-   LLMOBSERVE_TIMEOUT
-   LLMOBSERVE_MAX_RETRIES
-   LLMOBSERVE_RETRY_BACKOFF
-   LLMOBSERVE_SHUTDOWN_TIMEOUT

If `configure()` is not called, the library must load from environment.

------------------------------------------------------------------------

## 6. Defaults

If neither `configure()` nor environment variables are provided:

-   Push must be disabled by default
-   Metrics remain local (if applicable)
-   No network calls occur

Push should never default to a remote endpoint implicitly.

------------------------------------------------------------------------

## 7. Global Configuration Object

The library must maintain a single immutable configuration object:

    GlobalConfig
      service
      env
      version
      exporter_config

**Deferred to future version:** `sampling_config` is not included in
the V1 `GlobalConfig`. It will be added when sampling strategies are
implemented.

After initialization, configuration must not change.

------------------------------------------------------------------------

## 8. Exporter Behavior

The exporter must:

-   Batch span events
-   Send via HTTP POST
-   Retry on transient failures
-   Use non-blocking background execution
-   Never block application execution

Transport library: httpx (async-native)

------------------------------------------------------------------------

## 9. Authentication

Support at minimum:

-   API key header (e.g., `x-api-key`)
-   Bearer token (Authorization header)

Authentication configuration must be abstracted from decorators.

------------------------------------------------------------------------

## 10. Telemetry Ingestion Endpoint

Client must send telemetry to:

    POST /v1/telemetry

Payload format (example):

``` json
{
  "service": "ai-service",
  "env": "prod",
  "version": "1.2.0",
  "spans": [
    {
      "trace_id": "...",
      "span_id": "...",
      "parent_id": "...",
      "type": "llm",
      "name": "generate_summary",
      "attributes": { ... },
      "start_time": "...",
      "end_time": "..."
    }
  ]
}
```

Client sends structured span events only. Server derives Prometheus
metrics from spans.

------------------------------------------------------------------------

## 11. Async vs Sync Support

If application is async: - Use async httpx exporter.

If application is sync: - Run exporter in background thread.

Export must never block the decorated function execution path.

------------------------------------------------------------------------

## 11.1 Background Exporter — Interference Analysis

The background exporter (`BackgroundExporter`) is designed to be
invisible to the main application:

-   **Thread safety:** The span queue uses `queue.Queue` (thread-safe).
    Decorators call `put_nowait()` which never blocks. The exporter
    thread calls `get_nowait()`. No shared mutable data structures.
-   **No exception leakage:** All network and serialization errors are
    caught inside the exporter thread. Decorator execution is never
    affected by exporter failures.
-   **Daemon thread:** The exporter runs as `daemon=True`, so it cannot
    keep the process alive if the main thread exits.
-   **GIL impact:** Minimal. The exporter spends most of its time in
    `Event.wait()` (sleeping) or in httpx I/O (which releases the GIL
    during socket operations). CPU-bound work (JSON serialization) is
    brief and bounded by `batch_size`.

**Shutdown delay (known trade-off):**

The exporter registers an `atexit` handler that performs a final flush
of any remaining spans. If the collector is unreachable at shutdown time,
the retry loop can delay process exit by up to:

    shutdown_timeout + final flush retries (backoff sum) ≈ N seconds

With defaults (shutdown_timeout=5s, max_retries=3, retry_backoff=0.5s):

    5 + (0.5 + 1.0 + 2.0) = 8.5s worst case

All three values are configurable via `configure()` or environment
variables (`LLMOBSERVE_SHUTDOWN_TIMEOUT`, `LLMOBSERVE_MAX_RETRIES`,
`LLMOBSERVE_RETRY_BACKOFF`). Users who need instant shutdown can call
`stop_exporter()` explicitly or reduce these values.

------------------------------------------------------------------------

## 12. Failure Handling

If exporter fails: - Retry with backoff - Track internal failure
counter - Drop **newest** events if buffer limit exceeded (reject new
spans when queue is full, rather than evicting oldest) - Never raise
exporter errors into user code

**Implementation note:** The client-side span queue uses `put_nowait()`
with a bounded `queue.Queue`. When full, the **new** span is dropped
(not the oldest). This avoids the overhead of eviction from a FIFO
queue and ensures the queue never blocks the decorated function.

------------------------------------------------------------------------

## 13. Multi-Exporter Support (Future)

Design must allow future support for:

-   Multiple exporters
-   Console exporter
-   File exporter
-   OTEL exporter

But v1 may support a single HTTP exporter.

------------------------------------------------------------------------

## 14. Non-Goals

Configuration system must NOT:

-   Auto-modify runtime behavior after initialization
-   Re-read environment variables dynamically
-   Perform network calls during decorator execution
-   Introduce global mutable state after startup

------------------------------------------------------------------------

## 15. Summary

The client configuration system ensures:

-   Clean separation of concerns
-   Deterministic initialization
-   Async-safe telemetry push
-   Environment-based deployment flexibility
-   Production-safe failure handling

End of Client Configuration Requirements.
