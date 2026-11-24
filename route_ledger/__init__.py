"""RouteLedger: Capacity-Aware Multi-Hop Router, Double-Entry Ledger & NOC Workflow Engine.

Zero-dependency Python 3.10+ reference package combining:
  1. `CapacityAwareRouter` (`route_ledger.network_router`):
     Constrained Dijkstra + residual 2-way flow-splitting router over a dynamic 28-node
     LEO satellite / regional gateway / fulfillment mesh under link SNR degradation and outages.
  2. `IdempotentSettlementLedger` (`route_ledger.double_entry_ledger`):
     SQLite-backed double-entry usage billing & inventory reservation ledger with SHA-256
     idempotency deduplication, integer minor-unit regional tax calculation, out-of-order
     timestamp handling, and raw metering reconciliation.
  3. `OperationalWorkflowEngine` (`route_ledger.noc_workflow`):
     Bounded (<= 3 checks) diagnostic decision-tree runner isolating root causes and safe
     remediation actions across LEO NOC, billing reconciliation, and fulfillment floor alarms.
"""

from .double_entry_ledger import (
    IdempotencyConflictError,
    IdempotentSettlementLedger,
    NaiveFloatBillingProcessor,
    ReconciliationReport,
    SettlementReceipt,
    UsageOrderEvent,
    compute_regional_charge_cents,
    generate_settlement_workload,
    run_ledger_comparison,
)
from .network_router import (
    CapacityAwareRouter,
    LinkDegradationEvent,
    LinkState,
    RouteAllocation,
    StaticShortestPathRouter,
    TrafficDemand,
    build_default_28_node_mesh,
    generate_traffic_workload,
    run_router_comparison,
)
from .noc_workflow import (
    DiagnosticResult,
    IncidentTelemetrySnapshot,
    OperationalWorkflowEngine,
    UnstructuredLogScannerBaseline,
    generate_incident_workload,
    run_workflow_comparison,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "CapacityAwareRouter",
    "DiagnosticResult",
    "IdempotencyConflictError",
    "IdempotentSettlementLedger",
    "IncidentTelemetrySnapshot",
    "LinkDegradationEvent",
    "LinkState",
    "NaiveFloatBillingProcessor",
    "OperationalWorkflowEngine",
    "ReconciliationReport",
    "RouteAllocation",
    "SettlementReceipt",
    "StaticShortestPathRouter",
    "TrafficDemand",
    "UnstructuredLogScannerBaseline",
    "UsageOrderEvent",
    "build_default_28_node_mesh",
    "generate_incident_workload",
    "generate_settlement_workload",
    "generate_traffic_workload",
    "run_router_comparison",
    "run_workflow_comparison",
]
