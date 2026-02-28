#!/usr/bin/env python3
"""Fix Grafana dashboard queries: remove label selectors from global metrics."""
import json, re, os

# Metrics that are GLOBAL GAUGES (no service/env labels)
GLOBAL_METRICS = [
    'rastir_queue_size',
    'rastir_queue_utilization_percent',
    'rastir_memory_bytes',
    'rastir_trace_store_size',
    'rastir_active_traces',
    'rastir_ingestion_rate',
    'rastir_evaluation_queue_size',
    'rastir_evaluation_queue_utilization_percent',
    'rastir_backpressure_warnings_total',
    'rastir_spans_dropped_by_backpressure_total',
]

def fix_query(expr):
    changed = False
    for metric in GLOBAL_METRICS:
        pattern = re.compile(r'\b' + re.escape(metric) + r'\{[^}]*\}')
        if pattern.search(expr):
            expr = pattern.sub(metric, expr)
            changed = True
    return expr, changed

dashboard_dir = os.path.join(os.path.dirname(__file__), '..', 'grafana', 'dashboards')

def walk_panels(panels, fname, counter):
    for panel in panels:
        if 'panels' in panel:
            walk_panels(panel['panels'], fname, counter)
        for target in panel.get('targets', []):
            if 'expr' not in target:
                continue
            old_expr = target['expr']
            new_expr, changed = fix_query(old_expr)
            if changed:
                print(f'  [{fname}] {old_expr}')
                print(f'        -> {new_expr}')
                target['expr'] = new_expr
                counter[0] += 1

for fname in sorted(os.listdir(dashboard_dir)):
    if not fname.endswith('.json'):
        continue
    fpath = os.path.join(dashboard_dir, fname)
    with open(fpath) as f:
        data = json.load(f)
    counter = [0]
    walk_panels(data.get('panels', []), fname, counter)
    if counter[0] > 0:
        with open(fpath, 'w') as f:
            json.dump(data, f, indent=2)
        print(f'  Saved {fname} ({counter[0]} fixes)')
    else:
        print(f'  {fname}: no changes needed')
print('Done!')
