"""RouteLedger v2: Multi-Modal ATROPS Pareto Solver, LEO 5-Subsystem MCP Diagnostics, Flight C++ Gate & ACID Ledger.

Python 3.10+ package combining five deterministic subsystems:
  1. `CapacityAwareRouter` (`route_ledger.network_router`):
     Constrained Dijkstra + residual 2-way flow-splitting router over a dynamic 28-node
     LEO satellite, regional gateway, and fulfillment mesh under link SNR degradation and outages.
  2. `IdempotentSettlementLedger` (`route_ledger.double_entry_ledger`):
     SQLite-backed double-entry usage billing and inventory reservation ledger with SHA-256
     idempotency deduplication, integer minor-unit regional tax calculation, out-of-order
     timestamp handling, and raw metering reconciliation.
  3. `OperationalWorkflowEngine` (`route_ledger.noc_workflow`):
     Bounded (<= 3 checks) diagnostic decision-tree runner isolating root causes and safe
     remediation actions across LEO NOC, billing reconciliation, and fulfillment floor alarms.
  4. `TimeExpandedParetoSolver` & `MultiItemCartSourcingOptimizer` (`route_ledger.atrops_pareto`):
     SCOT-FO multi-modal (`Air / Linehaul / Rail / EV`) 3D Speed/Cost/CO2e Pareto frontier solver
     under Critical Pull Time (`CPT`) cutoffs, combinatorial multi-ASIN split-box cart optimizer,
     and counterfactual LLM schedule-patch validator.
  5. `LeoDiagnosticMCPRegistry` & `JplPowerOfTenVerifier` (`route_ledger.leo_flight_mcp`):
     Typed 5-subsystem LEO connectivity MCP diagnostic suite with launch-gate coverage evaluation
     plus a deterministic NASA/JPL Power-of-10 static C/C++ zero-heap and zero-recursion gate.
"""

from .atrops_pareto import (
    CartItemSpec,
    CartSourcingPlan,
    CounterfactualScheduleValidator,
    MultiItemCartSourcingOptimizer,
    NetworkSchedulePatch,
    ParetoFrontierPoint,
    ParetoRouteCandidate,
    ScheduledModalArc,
    ShipmentWaveDemand,
    TimeExpandedParetoSolver,
    build_scot_multimodal_topology,
    run_atrops_benchmark,
    solve_atrops_pareto_workbench,
)
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
from .leo_flight_mcp import (
    JplPowerOfTenVerifier,
    LeoDiagnosticMCPRegistry,
    LeoOutageIncident,
    MCP_SUBSYSTEM_TOOLS,
    StaticSafetyViolation,
    run_leo_diagnose_workbench,
    run_leo_mcp_benchmark,
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

__version__ = "2.0.0"

__all__ = [
    "__version__",
    "CapacityAwareRouter",
    "CartItemSpec",
    "CartSourcingPlan",
    "CounterfactualScheduleValidator",
    "DiagnosticResult",
    "IdempotencyConflictError",
    "IdempotentSettlementLedger",
    "IncidentTelemetrySnapshot",
    "JplPowerOfTenVerifier",
    "LeoDiagnosticMCPRegistry",
    "LeoOutageIncident",
    "LinkDegradationEvent",
    "LinkState",
    "MCP_SUBSYSTEM_TOOLS",
    "MultiItemCartSourcingOptimizer",
    "NaiveFloatBillingProcessor",
    "NetworkSchedulePatch",
    "OperationalWorkflowEngine",
    "ParetoFrontierPoint",
    "ParetoRouteCandidate",
    "ReconciliationReport",
    "RouteAllocation",
    "ScheduledModalArc",
    "SettlementReceipt",
    "ShipmentWaveDemand",
    "StaticSafetyViolation",
    "StaticShortestPathRouter",
    "TimeExpandedParetoSolver",
    "TrafficDemand",
    "UnstructuredLogScannerBaseline",
    "UsageOrderEvent",
    "build_default_28_node_mesh",
    "build_scot_multimodal_topology",
    "compute_regional_charge_cents",
    "generate_incident_workload",
    "generate_settlement_workload",
    "generate_traffic_workload",
    "run_atrops_benchmark",
    "run_ledger_comparison",
    "run_leo_diagnose_workbench",
    "run_leo_mcp_benchmark",
    "run_router_comparison",
    "run_workflow_comparison",
    "solve_atrops_pareto_workbench",
]
