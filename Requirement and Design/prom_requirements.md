# Prometheus Requirements for Rastir Observability Stack

## 1. Purpose

This document defines the functional and operational requirements for
deploying Prometheus as part of the Rastir observability stack.

The goal is to ensure that both ECS-based and Kubernetes-based
deployments behave identically even though they will be implemented by
different developers. The requirements below are therefore
platform-agnostic while specifying the Prometheus configuration and
resource expectations.

Prometheus will serve as the metrics store and exemplar provider for
Grafana dashboards and trace correlation.

------------------------------------------------------------------------

## 2. Scope

Prometheus in Rastir is responsible for:

-   storing system and application metrics
-   storing exemplars for metric → trace linking
-   serving queries to Grafana
-   retaining metrics for long-term analysis

Prometheus is **not responsible for**:

-   trace storage
-   log storage
-   distributed tracing

These responsibilities belong to other systems (e.g., AWS X-Ray or other
tracing backends).

------------------------------------------------------------------------

## 3. System Characteristics

  Parameter                   Value
  --------------------------- ----------------------------
  Number of time series       \~350
  Metric ingestion interval   10 seconds
  Metric retention            6 months
  Exemplar sampling           up to 100%
  Expected metrics per day    \~3M
  Deployment type             Single Prometheus instance

------------------------------------------------------------------------

## 4. Resource Requirements

  Resource       Requirement
  -------------- -----------------------------------
  CPU            1 vCPU minimum
  Memory         2 GB RAM
  Disk storage   100 GB persistent storage
  Network        low bandwidth (\<10 Mbps typical)

Typical runtime memory consumption:

  Component            Estimated Memory
  -------------------- ------------------
  Prometheus runtime   \~200 MB
  Head block metrics   \~4 MB
  Exemplar buffer      \~5 MB
  Query buffers        \~100 MB

Typical runtime memory: **\~300--400 MB**

------------------------------------------------------------------------

## 5. Storage Requirements

Prometheus must use **persistent storage**.

  Data Type   Retention
  ----------- --------------------------
  Metrics     6 months
  Exemplars   tied to metric retention

Estimated disk usage:

  Data                     Estimated Size
  ------------------------ ----------------
  Metrics                  \~3 GB
  Exemplars (worst case)   \~60 GB
  Operational overhead     \~20 GB

Recommended storage allocation: **100 GB**

------------------------------------------------------------------------

## 6. Exemplar Configuration

Prometheus must be configured with:

    --storage.exemplars.max-per-series=128

Behavior:

Prometheus stores a ring buffer of exemplars per series.

For Rastir:

-   10 second ingestion
-   128 exemplar buffer

Coverage window:

    128 × 10 seconds ≈ 21 minutes

Memory impact:

    350 series × 128 exemplars = 44,800 exemplars
    ≈ 4–5 MB RAM

------------------------------------------------------------------------

## 7. Retention Configuration

Metrics retention:

    --storage.tsdb.retention.time=180d

Note: Prometheus does not support separate exemplar retention.

------------------------------------------------------------------------

## 8. WAL Configuration

Enable WAL compression:

    --storage.tsdb.wal-compression

Benefits:

-   reduced disk usage
-   improved compaction performance
-   lower IO overhead

------------------------------------------------------------------------

## 9. TSDB Block Behavior

Prometheus writes metrics into **2-hour blocks**.

Lifecycle:

incoming samples → WAL → head block → 2 hour block flush → compaction

This process is automatic.

------------------------------------------------------------------------

## 10. Metric Labeling Requirements

Metrics **must not include high-cardinality labels**.

Forbidden labels:

-   trace_id
-   request_id
-   session_id
-   user_id
-   timestamp
-   uuid

Trace IDs must only appear in **exemplars**, not labels.

Correct:

    metric.observe(value, exemplar={trace_id})

Incorrect:

    metric{trace_id="abc123"}

------------------------------------------------------------------------

## 11. Deployment Requirements

  Property             Requirement
  -------------------- -------------------
  Instances            1
  Restart policy       automatic restart
  Health checks        enabled
  Persistent storage   required

Prometheus must restart without data loss.

------------------------------------------------------------------------

## 12. Networking Requirements

Prometheus must expose:

    port 9090

Consumers:

-   Grafana dashboards
-   operational health checks
-   debugging tools

------------------------------------------------------------------------

## 13. Monitoring Prometheus

Prometheus internal metrics must be available:

Examples:

-   prometheus_tsdb_head_series
-   prometheus_tsdb_head_samples_appended_total
-   prometheus_tsdb_wal_fsync_duration_seconds

These help monitor ingestion rate, WAL health, and compaction
performance.

------------------------------------------------------------------------

## 14. Expected System Limits

  Parameter            Safe Range
  -------------------- ------------
  Series count         ≤ 5,000
  Exemplars retained   ≤ 100M
  Disk usage           ≤ 80 GB
  Memory usage         ≤ 1 GB

Current Rastir workload is far below these limits.

------------------------------------------------------------------------

## 15. Required Startup Flags

Prometheus must start with:

    --storage.tsdb.retention.time=180d
    --storage.exemplars.max-per-series=128
    --storage.tsdb.wal-compression

------------------------------------------------------------------------

## 16. Deployment-Agnostic Design

This specification is independent of deployment platform.

It must support:

-   ECS deployments
-   Kubernetes deployments
-   other container runtimes

Platform-specific concerns (service discovery, storage drivers,
networking) are handled by deployment teams.

------------------------------------------------------------------------

## 17. Summary

  Parameter            Value
  -------------------- ----------------
  RAM                  2 GB
  CPU                  1 vCPU
  Storage              100 GB
  Metric retention     180 days
  Exemplar buffer      128 per series
  Ingestion interval   10 seconds

This configuration provides stable operation with large safety margins
for the Rastir workload.
