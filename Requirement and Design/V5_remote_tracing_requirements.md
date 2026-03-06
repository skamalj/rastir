# Rastir -- Remote Tool Distributed Tracing

## Requirements & Design Specification (Authoritative)

This document defines the complete, non-ambiguous specification for
annotation-driven distributed tracing of remote tools in Rastir.

This specification MUST be followed exactly. No alternative naming,
behavior, or hidden instrumentation is permitted.

  ---------------
  1\. OBJECTIVE
  ---------------

Enable distributed tracing across:

Agent Process → Remote Tool Process (MCP server)

Using annotation-driven instrumentation with:

-   Zero manual header management by users
-   Zero manual context propagation by users
-   No monkey-patching of third-party libraries
-   No hidden instrumentation inside unrelated functions
-   Clear execution boundary ownership

  -----------------------------------------------
  2\. FINAL PUBLIC ANNOTATION NAMES (MANDATORY)
  -----------------------------------------------

Client Side: @agent @trace_remote_tools

Server Side: @mcp_endpoint

No alternative names are allowed.

  ------------------------------
  3\. USER EXPERIENCE CONTRACT
  ------------------------------

Client Example:

    from rastir import configure, agent_span, trace_remote_tools

    configure(service="my-agent", env="prod")

    @agent_span(agent_name="research_agent")
    async def run():

        @trace_remote_tools
        async def get_tools():
            async with MCPClient("http://localhost:8001/mcp") as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    return await session.list_tools(), session
            # Returns (tool list, session) — decorator wraps session.call_tool()

        tools, session = await get_tools()
        # session.call_tool() now auto-injects W3C trace context via _meta
        result = await session.call_tool("search", {"query": "hello"})

Server Example:

    from rastir import configure, mcp_endpoint
    from mcp.server.fastmcp import FastMCP

    configure(service="my-mcp-server", env="prod")
    mcp = FastMCP("tools")

    @mcp.tool()
    @mcp_endpoint
    async def search(query: str, ctx: Context) -> str:
        # Trace context automatically extracted from ctx.request_context.meta
        # Span created as child of client trace
        return await do_search(query)

Decorator Order on MCP Server:
    @mcp.tool()      ← outermost: registers wrapped function with MCP
    @mcp_endpoint    ← innermost: extracts trace context, creates span

User MUST NOT:
- Manually inject trace headers
- Wrap tools manually
- Pass trace metadata manually
- Modify transport layer manually

  ------------------------------------
  4\. ARCHITECTURAL RESPONSIBILITIES
  ------------------------------------

4.1 @agent / @agent_span

Responsibilities:
- Create root span
- Set span_type="agent"
- Activate OTEL context
- No transport logic
- No tool wrapping

Span Naming:
- Default: function name
- Attribute: span_type="agent"

4.2 @trace_remote_tools

Responsibilities:
- Intercept function return value
- Detect MCP ClientSession object (in return value or args)
- Wrap session.call_tool() to inject W3C trace context via _meta
- Return original value (tools, session, etc.) transparently

It MUST NOT:
- Perform extraction
- Modify tool metadata / tool schemas
- Change tool argument types

4.3 session.call_tool() Wrapper Behavior (Internal Only)

When the wrapped session.call_tool() is invoked:

1.  Create client-side tool span
    -   Name: tool name (from call_tool `name` arg)
    -   Attribute: span_type="tool"
    -   Attribute: remote="true"
2.  Create trace carrier (dict)
3.  Call OTEL inject(carrier) to populate W3C traceparent/tracestate
4.  Merge carrier into the `meta` kwarg of call_tool()
    -   If user passes their own `meta`, merge (trace keys take precedence)
    -   The MCP SDK sends `meta` as `_meta` in JSON-RPC params
5.  Invoke original session.call_tool()
6.  Record errors if raised (mark span status=ERROR)
7.  End span

Carrier Propagation Channel:

    MCP SDK session.call_tool(meta={"traceparent": "...", "tracestate": "..."})

    Serialized as JSON-RPC params._meta — transport-agnostic (stdio, SSE, HTTP)

No custom carrier key needed. MCP's native _meta is used directly.

  ----------------------------------
  5\. SERVER SIDE -- @mcp_endpoint
  ----------------------------------

Responsibilities:

1.  Detect MCP Context parameter in the wrapped function's signature
2.  At invocation, extract _meta from ctx.request_context.meta
3.  If _meta contains traceparent:
      - Extract W3C context using OTEL extract(carrier)
      - Create span as child of extracted context
    Else:
      - Create new root span
4.  Span attributes:
      - span_type="tool"
      - remote="false"
      - tool_name=function name
5.  Execute underlying function (with original args including ctx)
6.  Record errors if raised

Extraction Source:

    ctx.request_context.meta  →  {"traceparent": "...", "tracestate": "..."}

No carrier removal needed — _meta is separate from tool arguments
and never reaches the user function's business parameters.

  --------------------------------
  6\. CARRIER FORMAT (MANDATORY)
  --------------------------------

Carrier is W3C trace context fields inside MCP's _meta:

Wire format (JSON-RPC):

    {
      "method": "tools/call",
      "params": {
        "name": "search",
        "arguments": {"query": "hello"},
        "_meta": {
          "traceparent": "00-abcdef1234567890abcdef1234567890-abcdef1234567890-01",
          "tracestate": "rastir=..."
        }
      }
    }

Use OTEL inject/extract only with this carrier dict.
No custom trace_id passing allowed.
No JSON string embedding allowed.

  ---------------------------------
  7\. NAMING CONVENTIONS (STRICT)
  ---------------------------------

Span Types Allowed:

-   agent
-   tool
-   llm
-   retrieval
-   infra
-   system

Remote tool CLIENT spans MUST use:

    span_type="tool"
    remote="true"

Remote tool SERVER spans MUST use:

    span_type="tool"
    remote="false"

  ---------------------------------
  8\. ERROR HANDLING REQUIREMENTS
  ---------------------------------

Client Wrapper:
- Any exception must mark span status=ERROR
- Exception must be re-raised

Server Endpoint:
- Any exception must mark span status=ERROR
- Exception must propagate normally

No swallowing of exceptions.

  -------------------------------
  9\. TRANSPORT AGNOSTIC DESIGN
  -------------------------------

This design uses MCP's native _meta field which is:

-   Part of JSON-RPC params (not transport headers)
-   Supported by all MCP transports: stdio, SSE, streamable HTTP
-   Preserved by the MCP Python SDK on both client and server
-   Accepted as `meta` kwarg in session.call_tool()
-   Accessible as ctx.request_context.meta on server

No transport-specific logic inside annotations.

  ----------------
  10\. NON-GOALS
  ----------------

This design explicitly does NOT include:

-   Automatic HTTP monkey-patching
-   Automatic gRPC interception
-   Automatic queue interception
-   Implicit instrumentation
-   Magic global state modification

  --------------------------
  11\. EXTENSIBILITY RULES
  --------------------------

Future additions MAY include:

-   trace_remote_llm
-   trace_remote_retrieval

But naming pattern MUST follow:

    trace_remote_<category>

Server side pattern MUST follow:

    <protocol>_endpoint

  -------------------------------------------
  12\. IMPLEMENTATION CHECKLIST (MANDATORY)
  -------------------------------------------

Developer must verify:

[ ] @agent creates root span
[ ] @trace_remote_tools wraps session.call_tool()
[ ] Wrapper injects W3C trace context via OTEL inject() into meta
[ ] Meta is passed as `meta` kwarg to session.call_tool()
[ ] MCP SDK serializes meta as _meta in JSON-RPC params
[ ] @mcp_endpoint detects Context parameter
[ ] @mcp_endpoint extracts _meta from ctx.request_context.meta
[ ] Server span created with extracted context (child of client)
[ ] Errors propagate correctly on both sides
[ ] No hidden wrapping in unrelated functions
[ ] No transport coupling inside annotations
[ ] Works with stdio, SSE, and streamable HTTP transports

  -------------------------
  13\. TRACE FLOW SUMMARY
  -------------------------

    Agent Span
    └── Tool Client Span (remote=true)
          └── Tool Server Span (remote=false)

This is the only accepted distributed trace topology.

  -------------------------
  14\. MCP SDK INTEGRATION
  -------------------------

Client side — session.call_tool() signature (MCP Python SDK):

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: float | None = None,
        progress_callback: ProgressFnT | None = None,
        *,
        meta: RequestParamsMeta | None = None,
    ) -> CallToolResult

RequestParamsMeta accepts arbitrary keys (TypedDict with extra_items=Any).

Server side — Context access path:

    ctx.request_context.meta  →  RequestParamsMeta | None

  -------------------------
  15\. INTEGRATION TEST
  -------------------------

Test must use:
- Real MCP server (FastMCP) running a tool
- Real LangGraph agent with Gemini model calling the MCP tool
- Verify trace_id propagation: client span and server span share same trace
- Verify parent-child: server span's parent_id == client span's span_id
- Verify span attributes: remote="true" on client, remote="false" on server

------------------------------------------------------------------------

END OF SPECIFICATION
