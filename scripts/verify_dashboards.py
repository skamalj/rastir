#!/usr/bin/env python3
"""
Verify all Grafana dashboard panels by querying Prometheus.

For each panel, extracts the PromQL expression, substitutes template
variables, and queries Prometheus to check if data is returned.

Usage:
    python scripts/verify_dashboards.py [--grafana URL] [--prometheus URL]
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

GRAFANA_URL = "http://localhost:3000"
PROMETHEUS_URL = "http://localhost:9090"
GRAFANA_AUTH = ("admin", "admin")

# Template variable substitutions — "All" values use .* regex
VARIABLE_DEFAULTS = {
    "$service": ".*",
    "$env": ".*",
    "$model": ".*",
    "$provider": ".*",
    "$agent": ".*",
    "$pricing_profile": ".*",
    "$evaluation_type": ".*",
    "$eval_status": "0",       # 0 = Total
    "$period": "week",
    "$__range": "1h",
    "$__rate_interval": "1m",
    "$__interval": "15s",
    "${__from:date:seconds}": "0",
    "${__to:date:seconds}": str(int(__import__("time").time())),
}

SKIP_DASHBOARDS = {"guardrail"}


def grafana_get(path: str) -> dict:
    url = f"{GRAFANA_URL}{path}"
    req = urllib.request.Request(url)
    credentials = f"{GRAFANA_AUTH[0]}:{GRAFANA_AUTH[1]}"
    import base64
    b64 = base64.b64encode(credentials.encode()).decode()
    req.add_header("Authorization", f"Basic {b64}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def prom_query(expr: str) -> bool:
    """Query Prometheus and return True if any data is returned."""
    url = f"{PROMETHEUS_URL}/api/v1/query?query={urllib.parse.quote(expr)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("status") != "success":
                return False
            result = data.get("data", {}).get("result", [])
            return len(result) > 0
    except Exception:
        return False


def substitute_variables(expr: str) -> str:
    """Replace Grafana template variables with test values."""
    result = expr
    # Sort by length descending so longer matches are replaced first
    for var, val in sorted(VARIABLE_DEFAULTS.items(), key=lambda x: -len(x[0])):
        result = result.replace(var, val)
    # Handle ${var:text} or ${var:value} patterns
    result = re.sub(r'\$\{(\w+):text\}', lambda m: VARIABLE_DEFAULTS.get(f"${m.group(1)}", m.group(0)), result)
    result = re.sub(r'\$\{(\w+):value\}', lambda m: VARIABLE_DEFAULTS.get(f"${m.group(1)}", m.group(0)), result)
    return result


def extract_panels(dashboard: dict) -> list:
    """Recursively extract all panels from a dashboard."""
    panels = []
    for panel in dashboard.get("panels", []):
        if panel.get("type") == "row":
            # Rows can contain nested panels
            panels.extend(extract_panels(panel))
        else:
            panels.append(panel)
    return panels


def check_dashboard(uid: str, title: str) -> tuple:
    """Check all panels in a dashboard. Returns (pass_count, fail_count, results)."""
    dashboard_data = grafana_get(f"/api/dashboards/uid/{uid}")
    dashboard = dashboard_data.get("dashboard", {})
    panels = extract_panels(dashboard)

    pass_count = 0
    fail_count = 0
    results = []

    for panel in panels:
        panel_title = panel.get("title", "Untitled")
        panel_type = panel.get("type", "unknown")

        # Skip text/row panels
        if panel_type in ("text", "row", "news"):
            continue

        targets = panel.get("targets", [])
        if not targets:
            continue

        panel_pass = True
        exprs_checked = []

        for target in targets:
            expr = target.get("expr", "")
            if not expr:
                continue

            substituted = substitute_variables(expr)
            exprs_checked.append(substituted)
            has_data = prom_query(substituted)

            if not has_data:
                panel_pass = False

        if not exprs_checked:
            continue

        if panel_pass:
            pass_count += 1
            results.append(("PASS", panel_title))
        else:
            fail_count += 1
            results.append(("FAIL", panel_title, exprs_checked))

    return pass_count, fail_count, results


def main():
    global GRAFANA_URL, PROMETHEUS_URL
    parser = argparse.ArgumentParser(description="Verify Grafana dashboard panels")
    parser.add_argument("--grafana", default=GRAFANA_URL, help="Grafana URL")
    parser.add_argument("--prometheus", default=PROMETHEUS_URL, help="Prometheus URL")
    args = parser.parse_args()

    GRAFANA_URL = args.grafana.rstrip("/")
    PROMETHEUS_URL = args.prometheus.rstrip("/")

    # Get all dashboards
    try:
        search_results = grafana_get("/api/search?type=dash-db")
    except Exception as e:
        print(f"ERROR: Cannot connect to Grafana at {GRAFANA_URL}: {e}")
        sys.exit(1)

    # Verify Prometheus connectivity
    try:
        prom_query("up")
    except Exception as e:
        print(f"ERROR: Cannot connect to Prometheus at {PROMETHEUS_URL}: {e}")
        sys.exit(1)

    total_pass = 0
    total_fail = 0
    dashboard_results = {}

    for dash in sorted(search_results, key=lambda d: d.get("title", "")):
        uid = dash.get("uid", "")
        title = dash.get("title", "Unknown")

        # Skip excluded dashboards
        if any(skip in title.lower() for skip in SKIP_DASHBOARDS):
            print(f"\n{'='*60}")
            print(f"SKIP: {title}")
            print(f"{'='*60}")
            continue

        print(f"\n{'='*60}")
        print(f"Dashboard: {title}")
        print(f"{'='*60}")

        try:
            p, f, results = check_dashboard(uid, title)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        total_pass += p
        total_fail += f
        dashboard_results[title] = (p, f)

        for r in results:
            status = r[0]
            panel_name = r[1]
            marker = "✓" if status == "PASS" else "✗"
            print(f"  {marker} {status}: {panel_name}")
            if status == "FAIL" and len(r) > 2:
                for expr in r[2]:
                    # Show truncated expression
                    display = expr[:100] + "..." if len(expr) > 100 else expr
                    print(f"      expr: {display}")

    # Summary
    total = total_pass + total_fail
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    for title, (p, f) in sorted(dashboard_results.items()):
        t = p + f
        print(f"  {title}: {p}/{t} PASS")
    print(f"\n  TOTAL: {total_pass}/{total} PASS, {total_fail} FAIL")
    print(f"{'='*60}")

    sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    main()
