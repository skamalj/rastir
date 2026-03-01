# Integration Tests — E2E with Full Infrastructure

## Philosophy

All integration tests **MUST** be end-to-end tests running against the full
infrastructure stack. Tests verify that spans produced by Rastir decorators
flow through the complete pipeline and arrive in Tempo with correct
attributes. Prometheus metric counters are also verified.

**Pipeline under test:**

```
Client decorator → Background exporter → Rastir collector (HTTP)
                                              ├── Prometheus metrics  (/metrics)
                                              └── OTLP export → Tempo  (traces)
```

In-process span capture (mocking `enqueue_span`) is allowed only for
fast-feedback **unit** tests, not integration tests.

---

## Infrastructure Requirements

| Component          | Endpoint                  | Purpose                        |
|--------------------|---------------------------|--------------------------------|
| Rastir collector   | `http://localhost:8080`   | Span ingestion, metrics, OTLP  |
| Tempo              | `http://localhost:3200`   | Trace storage & query          |
| Prometheus         | `http://localhost:9090`   | Metric scraping verification   |

Start the collector with OTLP forwarding:

```bash
RASTIR_SERVER_CONFIG=rastir-server-config.yaml PYTHONPATH=src \
    conda run -n llmobserve python -m rastir.server
```

---

## Annotation Specification

Every decorator sets specific attributes on spans. The e2e tests verify
that **all** of these appear in Tempo with correct values.

### Base Span Fields (all span types)

| Field         | Type   | Description                                  |
|---------------|--------|----------------------------------------------|
| `name`        | string | Span name (function name or override)        |
| `trace_id`    | string | 32-char hex, shared by all spans in a trace  |
| `span_id`     | string | Unique span identifier                       |
| `parent_id`   | string | Parent span's span_id (empty for root)       |
| `span_type`   | string | One of: trace, agent, llm, tool, retrieval, metric |
| `status`      | string | "OK" or "ERROR"                              |

### `@trace`

| Attribute     | Type   | Source      | Description                           |
|---------------|--------|-------------|---------------------------------------|
| `span_type`   | string | auto        | `"trace"`                             |
| `emit_metric` | bool   | param       | `true` when `emit_metric=True`        |

### `@agent`

| Attribute     | Type   | Source      | Description                           |
|---------------|--------|-------------|---------------------------------------|
| `span_type`   | string | auto        | `"agent"`                             |
| `agent_name`  | string | param       | Agent name (param or function name)   |

### `@llm`

| Attribute     | Type   | Source            | Description                     |
|---------------|--------|-------------------|---------------------------------|
| `span_type`   | string | auto              | `"llm"`                         |
| `model`       | string | param or adapter  | Model name                      |
| `provider`    | string | param or adapter  | Provider name                   |
| `agent`       | string | context           | Parent agent name (if under @agent) |
| `tokens_input`| int    | adapter           | Input token count               |
| `tokens_output`| int   | adapter           | Output token count              |
| `finish_reason`| string| adapter           | LLM finish reason               |

### `@tool`

| Attribute     | Type   | Source      | Description                           |
|---------------|--------|-------------|---------------------------------------|
| `span_type`   | string | auto        | `"tool"`                              |
| `tool_name`   | string | param       | Tool name (param or function name)    |
| `agent`       | string | context     | Parent agent name                     |
| `model`       | string | context     | Inherited from @llm via context       |
| `provider`    | string | context     | Inherited from @llm via context       |

### `@retrieval`

| Attribute              | Type   | Source      | Description                  |
|------------------------|--------|-------------|------------------------------|
| `span_type`            | string | auto        | `"retrieval"`                |
| `agent`                | string | context     | Parent agent name            |
| `model`                | string | context     | Inherited from @llm          |
| `provider`             | string | context     | Inherited from @llm          |
| `retrieved_documents_count` | int | result | Number of retrieved documents |

### `@metric`

| Attribute     | Type   | Source      | Description                           |
|---------------|--------|-------------|---------------------------------------|
| `span_type`   | string | auto        | `"metric"`                            |
| `metric_name` | string | param       | Metric base name                      |

### `@trace_remote_tools` (client-side MCP)

| Attribute     | Type   | Source      | Description                           |
|---------------|--------|-------------|---------------------------------------|
| `span_type`   | string | auto        | `"tool"`                              |
| `tool_name`   | string | call arg    | MCP tool name                         |
| `remote`      | string | auto        | `"true"`                              |
| `agent`       | string | context     | Parent agent name                     |
| `model`       | string | context     | Inherited from @llm or set_current_model |
| `provider`    | string | context     | Inherited from @llm or set_current_provider |

### `@mcp_endpoint` (server-side MCP)

| Attribute     | Type   | Source      | Description                           |
|---------------|--------|-------------|---------------------------------------|
| `span_type`   | string | auto        | `"tool"`                              |
| `tool_name`   | string | auto        | Server function name                  |
| `remote`      | string | auto        | `"false"`                             |

The server span's `trace_id` and `parent_id` are propagated from the
client via `rastir_trace_id` / `rastir_span_id` in tool arguments.

---

## Attribute Pipeline

All span attributes are prefixed with `rastir.` in OTLP/Tempo:

| Python type | OTLP value type | Example                          |
|-------------|-----------------|----------------------------------|
| `str`       | `stringValue`   | `rastir.model = "gemini-2.5"`    |
| `int`       | `intValue`      | `rastir.tokens_input = 150`      |
| `float`     | `doubleValue`   | `rastir.duration = 1.23`         |
| `bool`      | `boolValue`     | `rastir.emit_metric = true`      |
| `list/dict` | **dropped**     | Silently filtered by OTLP export |

---

## Parent-Child Hierarchy

Decorators create a span tree via context propagation:

```
@agent  (root, span_type=agent)
└── @llm  (parent=agent, span_type=llm)
    ├── @tool  (parent=llm, span_type=tool)
    └── @retrieval  (parent=llm, span_type=retrieval)
```

For MCP remote tracing:

```
@agent  (root)
└── Client Tool Span  (parent=agent, remote="true")
      └── Server Tool Span  (parent=client, remote="false", same trace_id)
```

---

## Test Inventory

| # | Test Name                                    | Decorators Tested                           | Verifies                                          |
|---|----------------------------------------------|---------------------------------------------|---------------------------------------------------|
| 1 | `test_agent_llm_tool_retrieval_annotations`  | @agent, @llm, @tool, @retrieval             | All attributes + parent-child hierarchy in Tempo   |
| 2 | `test_trace_and_metric_annotations`          | @trace, @metric                             | span_type, emit_metric, metric_name in Tempo       |
| 3 | `test_mcp_endpoint_annotations`              | @trace_remote_tools, @mcp_endpoint, @agent  | remote=true/false, model, provider, parent-child   |
| 4 | `test_mcp_without_endpoint`                  | @trace_remote_tools (client only)           | Only client span, no server span in Tempo          |
| 5 | `test_prometheus_metrics`                    | @agent, @trace_remote_tools, @mcp_endpoint  | Counter increments for spans and tool calls         |
| 6 | `test_langgraph_full_stack`                  | Full AI agent + MCP                         | Real LLM agent with both server types (optional)   |

---

## Running

```bash
# Full suite (needs GOOGLE_API_KEY for test 6)
GOOGLE_API_KEY=... PYTHONPATH=src \
    python -m pytest tests/integration/test_mcp_e2e.py -v -s

# Skip LangGraph test
PYTHONPATH=src \
    python -m pytest tests/integration/test_mcp_e2e.py -v -s -k "not langgraph"
```
