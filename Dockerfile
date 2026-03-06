# ---------------------------------------------------------------------------
# Rastir — LLM & Agent Observability Server
# ---------------------------------------------------------------------------
# Multi-stage build: slim final image, no dev deps, no build artefacts.
#
#   docker build -t rastir-server .
#   docker run -p 8080:8080 rastir-server
#
# Configuration via env vars (RASTIR_SERVER_*, RASTIR_*) or a mounted
# YAML file pointed to by RASTIR_SERVER_CONFIG.
# ---------------------------------------------------------------------------

# ---- Stage 1: build wheel ------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /build/dist

# ---- Stage 2: runtime image ----------------------------------------------
FROM python:3.12-slim

LABEL maintainer="Rastir Contributors"
LABEL description="Rastir — stateless LLM & Agent Observability Server"

# Non-root user for security
RUN groupadd -r rastir && useradd -r -g rastir -s /sbin/nologin rastir

WORKDIR /app

# Install the wheel + server extras
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir "$(ls /tmp/*.whl)[server]" \
 && rm -rf /tmp/*.whl

# Bundle default config
COPY rastir-server-config.yaml /app/rastir-server-config.yaml

# Defaults — overridable at runtime
ENV RASTIR_SERVER_HOST=0.0.0.0
ENV RASTIR_SERVER_PORT=8080
ENV RASTIR_SERVER_CONFIG=/app/rastir-server-config.yaml

EXPOSE 8080

USER rastir

HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8080/health').raise_for_status()"

ENTRYPOINT ["rastir-server"]
