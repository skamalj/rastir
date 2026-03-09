#!/bin/sh
# ---------------------------------------------------------------------------
# entrypoint.sh — Download config from S3 (if configured), then start Prometheus
# ---------------------------------------------------------------------------
# When S3_CONFIG_URI is set (e.g. s3://my-bucket/prometheus/), syncs the
# contents to /etc/prometheus/ before starting Prometheus.
#
# When S3_CONFIG_URI is not set, Prometheus starts with whatever config
# is already at /etc/prometheus/ (e.g. bind-mounted local files).
# ---------------------------------------------------------------------------
set -e

CONFIG_DIR="/etc/prometheus"

if [ -n "$S3_CONFIG_URI" ]; then
  echo "Syncing config from $S3_CONFIG_URI → $CONFIG_DIR"
  aws s3 sync "$S3_CONFIG_URI" "$CONFIG_DIR" --no-progress
  echo "Config sync complete"
fi

exec /bin/prometheus "$@"
