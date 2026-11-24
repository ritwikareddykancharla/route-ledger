#!/usr/bin/env python3
"""Deterministic benchmark runner (`python3 benchmarks/run_benchmarks.py`).

Executes all three RouteLedger subsystems with `random.Random(42)`, prints a concise
engineering summary table, and writes exact measured outputs to `benchmarks/latest_results.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
import platform
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from route_ledger import __version__  # noqa: E402
from route_ledger.double_entry_ledger import run_ledger_comparison  # noqa: E402
from route_ledger.network_router import run_router_comparison  # noqa: E402
from route_ledger.noc_workflow import run_workflow_comparison  # noqa: E402


def main() -> int:
    t0 = time.perf_counter()
    seed = 42

    router_res = run_router_comparison(num_demands=300, seed=seed)
    ledger_res = run_ledger_comparison(total_events=2000, duplicate_rate=0.15, seed=seed)
    workflow_res = run_workflow_comparison(num_incidents=150, seed=seed)

    total_wall_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    results = {
        "service": "RouteLedger",
        "version": __version__,
        "seed": seed,
        "python_version": platform.python_version(),
        "total_benchmark_wall_ms": total_wall_ms,
        "router_benchmark": router_res,
        "ledger_benchmark": ledger_res,
        "workflow_benchmark": workflow_res,
    }

    out_path = Path(__file__).resolve().parent / "latest_results.json"
    out_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")

    r_static = router_res["static_shortest_path"]
    r_aware = router_res["capacity_aware_router"]
    r_imp = router_res["improvements"]

    l_work = ledger_res["workload"]
    l_idem = ledger_res["idempotent_double_entry_ledger"]
    l_naive = ledger_res["naive_float_baseline"]

    w_eng = workflow_res["structured_workflow_engine"]
    w_base = workflow_res["unstructured_log_scanner_baseline"]
    w_imp = workflow_res["improvements"]

    print("=" * 88)
    print(f"RouteLedger v{__version__} — Deterministic Benchmark Summary (seed={seed})")
    print("=" * 88)
    print(
        f"[1] CapacityAwareRouter (28 nodes, 300 demands, 6 link-degradation/outage events)\n"
        f"    • p99 Latency:       {r_static['p99_latency_ms']:>7.2f} ms (Static)  -> "
        f"{r_aware['p99_latency_ms']:>6.2f} ms (Aware)  [-{r_imp['p99_latency_reduction_pct']:.2f}%]\n"
        f"    • SLA Violations:    {r_static['sla_violations']:>7d} / 300      -> "
        f"{r_aware['sla_violations']:>6d} / 300     [-{r_imp['sla_violation_reduction_pct']:.2f}%]\n"
        f"    • Dropped Bandwidth: {r_static['dropped_bandwidth_mbps']:>7.2f} Mbps      -> "
        f"{r_aware['dropped_bandwidth_mbps']:>6.2f} Mbps     [-{r_imp['packet_drop_reduction_pct']:.2f}%]\n"
        f"    • Reroute Compute:   p50={r_imp['reroute_p50_compute_ms']:.4f} ms | "
        f"p99={r_imp['reroute_p99_compute_ms']:.4f} ms | splits={r_aware['split_flow_count']}"
    )
    print("-" * 88)
    print(
        f"[2] IdempotentSettlementLedger ({l_work['events_processed']} events, "
        f"{l_work['injected_duplicate_retries']} retries [{l_work['duplicate_retry_rate_pct']:.1f}%])\n"
        f"    • Duplicate Charges: {l_naive['duplicate_charges_applied']:>7d} (Naive)   -> "
        f"{l_idem['double_charges_applied']:>6d} (Ledger)  [{l_idem['duplicate_charge_elimination_pct']:.1f}% eliminated]\n"
        f"    • Float Drift Cents: {l_naive['float_rounding_drift_on_settled_set_cents']:>7d} cents    -> "
        f"{l_idem['rounding_drift_cents']:>6d} cents     [SUM(debits)==SUM(credits): {l_idem['double_entry_balanced']}]\n"
        f"    • Out-of-Order Evts: {l_idem['out_of_order_events_handled']:>7d} settled/reconciled cleanly\n"
        f"    • Throughput:        {l_idem['settlement_throughput_events_per_sec']:>7.1f} events/s "
        f"({l_idem['settlement_wall_time_ms']:.2f} ms; recon audit {l_idem['reconciliation_audit_ms']:.2f} ms)"
    )
    print("-" * 88)
    print(
        f"[3] OperationalWorkflowEngine (150 incidents across 5 NOC & fulfillment alarm classes)\n"
        f"    • Root-Cause Acc:    {w_base['diagnostic_accuracy_pct']:>6.2f}% (LogScan) -> "
        f"{w_eng['diagnostic_accuracy_pct']:>6.2f}% (Tree)   [+{w_imp['accuracy_gain_pct_points']:.2f} pp]\n"
        f"    • Steps / Checks:    {w_base['mean_lines_scanned']:>6.2f} lines     -> "
        f"{w_eng['mean_checks_executed']:>6.2f} checks   [-{w_imp['diagnostic_step_reduction_pct']:.2f}% steps; max={w_eng['max_checks_executed']}]\n"
        f"    • Eval Wall Time:    {w_eng['total_eval_time_ms']:.3f} ms total ({w_eng['mean_eval_time_ms']:.4f} ms/incident)"
    )
    print("=" * 88)
    print(f"Wrote {out_path} in {total_wall_ms:.2f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
