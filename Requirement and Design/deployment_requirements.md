# Deployment Requirements --- Rastir (Single Package Model)

## 1. Objective

Define deployment and usage requirements for Rastir as a single package
containing both:

-   Client instrumentation library
-   Observability server

The system must be easy to deploy, stateless, and free from external
database dependencies.

------------------------------------------------------------------------

# 2. Installation Requirements

## 2.1 Base Installation

``` bash
pip install rastir
```

Must include: - Client decorators - Configuration API - HTTP transport

## 2.2 Optional Extras

``` bash
pip install rastir[server]
pip install rastir[otel]
```

Server and OTEL exporter may be optional extras to keep base install
lightweight.

------------------------------------------------------------------------

# 3. Client Usage Requirements

## 3.1 Minimal Setup

``` python
from rastir import configure

configure(
    service="my-ai-service",
    env="prod",
    push_url="http://localhost:8080/v1/telemetry"
)
```

Requirements:

-   Python 3.9+
-   No external infrastructure dependency
-   Async + sync supported
-   Must work in FastAPI, Flask, Lambda, background workers

------------------------------------------------------------------------

# 4. Server Runtime Requirements

## 4.1 Local Development

Server must run with:

``` bash
rastir-server
```

OR

``` bash
python -m rastir.server
```

Default behavior:

-   Listen on 0.0.0.0:8080
-   Enable:
    -   POST /v1/telemetry
    -   GET /metrics
    -   GET /health
    -   GET /ready
-   No config file required in dev mode

------------------------------------------------------------------------

## 4.2 Production Configuration

Configuration sources:

1.  CLI flags
2.  YAML config file
3.  Environment variables

Example:

``` bash
RASTIR_PORT=8080
RASTIR_MAX_TRACES=20000
RASTIR_OTLP_ENDPOINT=http://tempo:4318
```

YAML config must be optional, not mandatory.

------------------------------------------------------------------------

# 5. Infrastructure Requirements

The server must require:

-   Python runtime
-   No external database
-   No Redis
-   No Kafka
-   No filesystem persistence

Optional integrations:

-   Prometheus (scrapes /metrics)
-   OTEL backend (Tempo/Jaeger/etc.)

Server must function standalone without OTEL backend.

------------------------------------------------------------------------

# 6. Container Deployment

Official Docker image must:

-   Require no persistent volume
-   Require no init job
-   Require no migrations
-   Start with a single process

Example:

``` bash
docker run -p 8080:8080 rastir/server
```

------------------------------------------------------------------------

# 7. Kubernetes Deployment

Minimum required objects:

-   Deployment
-   Service

Optional:

-   ServiceMonitor (Prometheus)

Must NOT require:

-   StatefulSet
-   PersistentVolumeClaim

Server must be fully stateless.

------------------------------------------------------------------------

# 8. Scaling Requirements

Horizontal scaling must be supported.

When scaled:

-   Prometheus scrapes each instance independently
-   OTEL backend aggregates traces
-   No shared state required between instances

------------------------------------------------------------------------

# 9. Resource Requirements

## 9.1 Memory

Memory bounded by configuration:

-   max_traces
-   queue size
-   metric label limits

Default footprint target: 100--200 MB.

## 9.2 CPU

-   O(1) metric updates
-   Async OTLP batching
-   No blocking ingestion path

------------------------------------------------------------------------

# 10. Security Requirements

Server must support:

-   Optional API key validation
-   TLS termination (external or built-in)
-   Request payload size limit
-   Label length limits

Client must support:

-   API key header
-   Timeout configuration

------------------------------------------------------------------------

# 11. Operational Requirements

Server must expose:

-   GET /health
-   GET /ready
-   ingestion_rejections_total metric
-   exporter_failures_total metric

No admin UI required for v1.

------------------------------------------------------------------------

# 12. Upgrade & Compatibility

-   No schema migrations required
-   Backward-compatible ingestion within major version
-   Stateless restart safe
-   No persistent storage upgrades needed

------------------------------------------------------------------------

# 13. Core Deployment Promise

Rastir must guarantee:

-   Zero external DB dependencies
-   Single command server startup
-   Stateless design
-   Horizontal scalability
-   Production-safe defaults
-   Minimal operational complexity

End of Deployment Requirements.
