"""NOC & Floor Troubleshooting Decision-Tree Runner (`route_ledger/noc_workflow.py`).

Executes deterministic, bounded (`<= 3` checks) diagnostic decision trees over live telemetry
snapshots across five operational alarm classes:
  1. `GROUND_GATEWAY_SNR_DROP`
  2. `SATELLITE_HANDOFF_PACKET_LOSS`
  3. `ISP_BILLING_RECONCILIATION_MISMATCH`
  4. `FULFILLMENT_CONVEYOR_BUFFER_JAM`
  5. `INVENTORY_BIN_COUNT_DRIFT`

Compares structured predicate evaluation against sequential unstructured log scanning
(`UnstructuredLogScannerBaseline`) on diagnostic accuracy, checks/lines inspected, and latency.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
import statistics
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


SUPPORTED_ALARM_TYPES: Tuple[str, ...] = (
    "GROUND_GATEWAY_SNR_DROP",
    "SATELLITE_HANDOFF_PACKET_LOSS",
    "ISP_BILLING_RECONCILIATION_MISMATCH",
    "FULFILLMENT_CONVEYOR_BUFFER_JAM",
    "INVENTORY_BIN_COUNT_DRIFT",
)


@dataclass(frozen=True)
class IncidentTelemetrySnapshot:
    """Structured telemetry metrics paired with raw multi-service operational logs."""

    incident_id: str
    alarm_type: str
    region: str
    resource_id: str
    metrics: Dict[str, Any]
    raw_logs: List[str]
    expected_root_cause: Optional[str] = None


@dataclass(frozen=True)
class CheckEvaluationStep:
    """Single deterministic telemetry predicate check executed by the workflow engine."""

    step_index: int
    check_name: str
    expression: str
    observed_value: Any
    passed: bool


@dataclass(frozen=True)
class DiagnosticResult:
    """Root-cause isolation output with audit trace and bounded remediation action."""

    incident_id: str
    alarm_type: str
    root_cause_code: str
    recommended_action: str
    severity: str
    requires_human_approval: bool
    checks_executed: int
    trace: List[CheckEvaluationStep]
    eval_time_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "alarm_type": self.alarm_type,
            "root_cause_code": self.root_cause_code,
            "recommended_action": self.recommended_action,
            "severity": self.severity,
            "requires_human_approval": self.requires_human_approval,
            "checks_executed": self.checks_executed,
            "trace": [
                {
                    "step_index": s.step_index,
                    "check_name": s.check_name,
                    "expression": s.expression,
                    "observed_value": s.observed_value,
                    "passed": s.passed,
                }
                for s in self.trace
            ],
            "eval_time_ms": round(self.eval_time_ms, 4),
        }


@dataclass(frozen=True)
class _DecisionNode:
    """Internal binary decision node or terminal leaf diagnosis."""

    check_name: Optional[str] = None
    expression: Optional[str] = None
    metric_key: Optional[str] = None
    predicate: Optional[Callable[[Dict[str, Any]], bool]] = None
    on_true: Optional["_DecisionNode"] = None
    on_false: Optional["_DecisionNode"] = None
    # Leaf attributes
    root_cause_code: Optional[str] = None
    recommended_action: Optional[str] = None
    severity: str = "SEV-2"
    requires_human_approval: bool = False

    @property
    def is_leaf(self) -> bool:
        return self.root_cause_code is not None


def _leaf(
    code: str,
    action: str,
    severity: str = "SEV-2",
    approval: bool = False,
) -> _DecisionNode:
    return _DecisionNode(
        root_cause_code=code,
        recommended_action=action,
        severity=severity,
        requires_human_approval=approval,
    )


def _branch(
    check_name: str,
    expression: str,
    metric_key: str,
    predicate: Callable[[Dict[str, Any]], bool],
    on_true: _DecisionNode,
    on_false: _DecisionNode,
) -> _DecisionNode:
    return _DecisionNode(
        check_name=check_name,
        expression=expression,
        metric_key=metric_key,
        predicate=predicate,
        on_true=on_true,
        on_false=on_false,
    )


def _build_workflow_trees() -> Dict[str, _DecisionNode]:
    """Build <= 3-depth decision trees for all 5 NOC & fulfillment alarm classes."""
    trees: Dict[str, _DecisionNode] = {}

    # 1. GROUND_GATEWAY_SNR_DROP (max depth = 3)
    trees["GROUND_GATEWAY_SNR_DROP"] = _branch(
        check_name="rf_lna_current_health",
        expression="rf_lna_bias_ma < 85.0",
        metric_key="rf_lna_bias_ma",
        predicate=lambda m: float(m.get("rf_lna_bias_ma", 120.0)) < 85.0,
        on_true=_branch(
            check_name="standby_rf_chain_ready",
            expression="standby_rf_chain_ready == True",
            metric_key="standby_rf_chain_ready",
            predicate=lambda m: bool(m.get("standby_rf_chain_ready", False)),
            on_true=_leaf(
                "RF_LNA_FAULT_STANDBY_FAILOVER",
                "SWITCH_TO_STANDBY_RF_CHAIN_AND_OPEN_RMA_TICKET",
                severity="SEV-2",
                approval=False,
            ),
            on_false=_leaf(
                "RF_LNA_FAULT_NO_STANDBY",
                "DRAIN_GATEWAY_TO_ADJACENT_POP_AND_PAGE_FIELD_OPS",
                severity="SEV-1",
                approval=True,
            ),
        ),
        on_false=_branch(
            check_name="ka_rain_attenuation",
            expression="rain_rate_mm_hr >= 18.0",
            metric_key="rain_rate_mm_hr",
            predicate=lambda m: float(m.get("rain_rate_mm_hr", 0.0)) >= 18.0,
            on_true=_branch(
                check_name="diversity_gateway_link_margin",
                expression="diversity_gw_snr_db >= 14.0",
                metric_key="diversity_gw_snr_db",
                predicate=lambda m: float(m.get("diversity_gw_snr_db", 0.0)) >= 14.0,
                on_true=_leaf(
                    "KA_RAIN_FADE_DIVERSITY_REROUTE",
                    "SHIFT_KA_FEEDER_FLOWS_TO_DIVERSITY_GATEWAY",
                    severity="SEV-3",
                    approval=False,
                ),
                on_false=_leaf(
                    "KA_RAIN_FADE_REGIONAL_ACM_STEPDOWN",
                    "STEP_DOWN_MODCOD_TO_QPSK_AND_THROTTLE_BULK_INGRESS",
                    severity="SEV-2",
                    approval=False,
                ),
            ),
            on_false=_leaf(
                "GATEWAY_GIMBAL_POINTING_DRIFT",
                "RECALIBRATE_AZIMUTH_ELEVATION_TRACKING_SERVO",
                severity="SEV-2",
                approval=True,
            ),
        ),
    )

    # 2. SATELLITE_HANDOFF_PACKET_LOSS (max depth = 3)
    trees["SATELLITE_HANDOFF_PACKET_LOSS"] = _branch(
        check_name="beam_handoff_clock_skew",
        expression="ephemeris_skew_ms > 4.5",
        metric_key="ephemeris_skew_ms",
        predicate=lambda m: float(m.get("ephemeris_skew_ms", 0.0)) > 4.5,
        on_true=_branch(
            check_name="ptp_grandmaster_lock",
            expression="ptp_locked == False",
            metric_key="ptp_locked",
            predicate=lambda m: not bool(m.get("ptp_locked", True)),
            on_true=_leaf(
                "HANDOFF_PTP_GRANDMASTER_UNLOCK",
                "FAILOVER_PTP_TIMING_SOURCE_AND_RESYNC_BEAM_SLOTS",
                severity="SEV-1",
                approval=False,
            ),
            on_false=_leaf(
                "STALE_ORBITAL_EPHEMERIS_SCHEDULE",
                "PUSH_SIGNED_EPHEMERIS_UPDATE_TO_PLANE_CONTROLLERS",
                severity="SEV-2",
                approval=False,
            ),
        ),
        on_false=_branch(
            check_name="target_satellite_queue_saturation",
            expression="target_isl_utilization >= 0.90",
            metric_key="target_isl_utilization",
            predicate=lambda m: float(m.get("target_isl_utilization", 0.0)) >= 0.90,
            on_true=_branch(
                check_name="adjacent_orbital_plane_headroom",
                expression="adjacent_plane_residual_mbps >= 150.0",
                metric_key="adjacent_plane_residual_mbps",
                predicate=lambda m: float(m.get("adjacent_plane_residual_mbps", 0.0)) >= 150.0,
                on_true=_leaf(
                    "HANDOFF_TARGET_ISL_CONGESTION",
                    "STEER_OVERFLOW_DEMANDS_VIA_ADJACENT_ORBITAL_PLANE",
                    severity="SEV-2",
                    approval=False,
                ),
                on_false=_leaf(
                    "REGIONAL_CONSTELLATION_CAPACITY_EXHAUSTION",
                    "APPLY_PRIORITY_DSCP_SHAPING_AND_DEFER_BULK_TRANSFERS",
                    severity="SEV-2",
                    approval=True,
                ),
            ),
            on_false=_leaf(
                "DOPPLER_CARRIER_PLL_SLIP",
                "WIDEN_TERMINAL_PLL_LOOP_BANDWIDTH_PROFILE",
                severity="SEV-3",
                approval=False,
            ),
        ),
    )

    # 3. ISP_BILLING_RECONCILIATION_MISMATCH (max depth = 3)
    trees["ISP_BILLING_RECONCILIATION_MISMATCH"] = _branch(
        check_name="unkeyed_duplicate_retry_anomaly",
        expression="unkeyed_retry_events > 0",
        metric_key="unkeyed_retry_events",
        predicate=lambda m: int(m.get("unkeyed_retry_events", 0)) > 0,
        on_true=_leaf(
            "UPSTREAM_MISSING_IDEMPOTENCY_KEY_RETRY",
            "QUARANTINE_UNKEYED_USAGE_BATCH_AND_REGENERATE_DETERMINISTIC_KEYS",
            severity="SEV-1",
            approval=True,
        ),
        on_false=_branch(
            check_name="gateway_metering_watermark_lag",
            expression="watermark_lag_sec > 300",
            metric_key="watermark_lag_sec",
            predicate=lambda m: int(m.get("watermark_lag_sec", 0)) > 300,
            on_true=_branch(
                check_name="store_and_forward_queue_depth",
                expression="delayed_gateway_batches > 0",
                metric_key="delayed_gateway_batches",
                predicate=lambda m: int(m.get("delayed_gateway_batches", 0)) > 0,
                on_true=_leaf(
                    "LATE_GATEWAY_BATCH_WATERMARK_LAG",
                    "DEFER_INVOICE_CLOSE_UNTIL_GATEWAY_WATERMARK_CONVERGES",
                    severity="SEV-3",
                    approval=False,
                ),
                on_false=_leaf(
                    "METERING_CONSUMER_PARTITION_STALL",
                    "RESET_STALLED_CONSUMER_GROUP_WITH_IDEMPOTENT_REPLAY",
                    severity="SEV-2",
                    approval=False,
                ),
            ),
            on_false=_leaf(
                "REGIONAL_TAX_SCHEDULE_VERSION_SKEW",
                "ALIGN_TAX_BPS_CATALOG_AND_POST_BALANCED_ADJUSTMENT_ENTRY",
                severity="SEV-2",
                approval=True,
            ),
        ),
    )

    # 4. FULFILLMENT_CONVEYOR_BUFFER_JAM (max depth = 3)
    trees["FULFILLMENT_CONVEYOR_BUFFER_JAM"] = _branch(
        check_name="vfd_drive_thermal_state",
        expression="vfd_motor_temp_c >= 88.0",
        metric_key="vfd_motor_temp_c",
        predicate=lambda m: float(m.get("vfd_motor_temp_c", 50.0)) >= 88.0,
        on_true=_branch(
            check_name="motor_overcurrent_draw",
            expression="motor_current_amps >= 14.5",
            metric_key="motor_current_amps",
            predicate=lambda m: float(m.get("motor_current_amps", 8.0)) >= 14.5,
            on_true=_leaf(
                "CONVEYOR_MECHANICAL_BIND_THERMAL_TRIP",
                "LOCKOUT_CONVEYOR_ZONE_AND_DISPATCH_RME_TECHNICIAN",
                severity="SEV-1",
                approval=True,
            ),
            on_false=_leaf(
                "VFD_CABINET_COOLING_FAN_FAILURE",
                "THROTTLE_BELT_SPEED_50_PCT_AND_REPLACE_CABINET_BLOWER",
                severity="SEV-2",
                approval=False,
            ),
        ),
        on_false=_branch(
            check_name="downstream_sort_chute_occupancy",
            expression="downstream_chute_fill_pct >= 94.0",
            metric_key="downstream_chute_fill_pct",
            predicate=lambda m: float(m.get("downstream_chute_fill_pct", 40.0)) >= 94.0,
            on_true=_branch(
                check_name="secondary_divert_lane_capacity",
                expression="secondary_lane_available == True",
                metric_key="secondary_lane_available",
                predicate=lambda m: bool(m.get("secondary_lane_available", False)),
                on_true=_leaf(
                    "SORT_CHUTE_FULL_DIVERT_ELIGIBLE",
                    "ACTIVATE_SHOE_SORTER_DIVERT_TO_SECONDARY_CHUTE",
                    severity="SEV-3",
                    approval=False,
                ),
                on_false=_leaf(
                    "PACK_WALL_DOWNSTREAM_BACKPRESSURE",
                    "PACE_UPSTREAM_SINGULATOR_INDUCT_RATE_MINUS_30_PCT",
                    severity="SEV-2",
                    approval=False,
                ),
            ),
            on_false=_leaf(
                "PHOTO_EYE_REFLECTOR_OCCLUSION",
                "TRIGGER_AIR_BLAST_CLEAN_AND_VERIFY_PHOTO_EYE_STATE",
                severity="SEV-3",
                approval=False,
            ),
        ),
    )

    # 5. INVENTORY_BIN_COUNT_DRIFT (max depth = 3)
    trees["INVENTORY_BIN_COUNT_DRIFT"] = _branch(
        check_name="inflight_scanner_transaction_lag",
        expression="inflight_scans > 0 and oldest_scan_age_sec < 120",
        metric_key="inflight_scans",
        predicate=lambda m: (
            int(m.get("inflight_scans", 0)) > 0
            and int(m.get("oldest_scan_age_sec", 999)) < 120
        ),
        on_true=_leaf(
            "TRANSIENT_INFLIGHT_PICK_STOW_LAG",
            "RECHECK_BIN_PARITY_AFTER_INFLIGHT_TXN_COMMIT",
            severity="SEV-4",
            approval=False,
        ),
        on_false=_branch(
            check_name="multi_unit_physical_variance",
            expression="abs(unit_variance) >= 3",
            metric_key="unit_variance",
            predicate=lambda m: abs(int(m.get("unit_variance", 0))) >= 3,
            on_true=_branch(
                check_name="open_customer_shipments_at_risk",
                expression="allocated_open_orders > 0",
                metric_key="allocated_open_orders",
                predicate=lambda m: int(m.get("allocated_open_orders", 0)) > 0,
                on_true=_leaf(
                    "PHYSICAL_BIN_SHORTAGE_OPEN_ORDER_RISK",
                    "FREEZE_BIN_RESERVATIONS_REALLOCATE_PEER_FC_AND_COUNT",
                    severity="SEV-1",
                    approval=True,
                ),
                on_false=_leaf(
                    "BULK_STOW_LOT_QUANTITY_MISMATCH",
                    "PLACE_QUALITY_HOLD_ON_BIN_AND_QUEUE_ICQA_CYCLE_COUNT",
                    severity="SEV-2",
                    approval=False,
                ),
            ),
            on_false=_leaf(
                "ADJACENT_BIN_SINGLE_UNIT_MISSTOW",
                "AUDIT_NEIGHBORING_CUBBY_BARCODES_NEXT_POD_ARRIVAL",
                severity="SEV-3",
                approval=False,
            ),
        ),
    )

    return trees


class OperationalWorkflowEngine:
    """Bounded (`<= 3` step) NOC & fulfillment diagnostic decision-tree runner."""

    MAX_ALLOWED_CHECKS = 3

    def __init__(self) -> None:
        self._trees = _build_workflow_trees()

    @property
    def supported_alarms(self) -> Tuple[str, ...]:
        return SUPPORTED_ALARM_TYPES

    def diagnose(self, snapshot: IncidentTelemetrySnapshot) -> DiagnosticResult:
        """Evaluate the incident's decision tree in at most 3 deterministic checks."""
        if snapshot.alarm_type not in self._trees:
            raise ValueError(
                f"Unsupported alarm_type '{snapshot.alarm_type}'. Expected one of {SUPPORTED_ALARM_TYPES}."
            )

        t0 = time.perf_counter()
        node = self._trees[snapshot.alarm_type]
        trace: List[CheckEvaluationStep] = []
        step_idx = 0

        while not node.is_leaf:
            step_idx += 1
            if step_idx > self.MAX_ALLOWED_CHECKS:
                raise RuntimeError(
                    f"Workflow '{snapshot.alarm_type}' exceeded {self.MAX_ALLOWED_CHECKS}-check bound."
                )
            assert node.predicate is not None
            assert node.check_name is not None
            assert node.expression is not None
            observed = snapshot.metrics.get(node.metric_key or "")
            passed = bool(node.predicate(snapshot.metrics))
            trace.append(
                CheckEvaluationStep(
                    step_index=step_idx,
                    check_name=node.check_name,
                    expression=node.expression,
                    observed_value=observed,
                    passed=passed,
                )
            )
            nxt = node.on_true if passed else node.on_false
            assert nxt is not None
            node = nxt

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert node.root_cause_code is not None
        assert node.recommended_action is not None
        return DiagnosticResult(
            incident_id=snapshot.incident_id,
            alarm_type=snapshot.alarm_type,
            root_cause_code=node.root_cause_code,
            recommended_action=node.recommended_action,
            severity=node.severity,
            requires_human_approval=node.requires_human_approval,
            checks_executed=len(trace),
            trace=trace,
            eval_time_ms=elapsed_ms,
        )


class UnstructuredLogScannerBaseline:
    """Baseline sequential regex/keyword log scanner over raw multi-daemon logs.

    Scans log lines sequentially from oldest to newest and triggers on the first high-priority
    error token or fallback symptom pattern — susceptible to cascading sympathetic alerts
    (e.g., ARQ retransmissions or queue backpressure logged ahead of root telemetry).
    """

    KEYWORD_RULES: List[Tuple[str, str]] = [
        ("LNA_BIAS_UNDERVOLT_NO_STBY", "RF_LNA_FAULT_NO_STANDBY"),
        ("LNA_BIAS_UNDERVOLT", "RF_LNA_FAULT_STANDBY_FAILOVER"),
        ("RAIN_ATTEN_DIVERSITY_OK", "KA_RAIN_FADE_DIVERSITY_REROUTE"),
        ("RAIN_ATTEN_SEVERE", "KA_RAIN_FADE_REGIONAL_ACM_STEPDOWN"),
        ("GIMBAL_SERVO_ERR", "GATEWAY_GIMBAL_POINTING_DRIFT"),
        ("PTP_GM_UNLOCKED", "HANDOFF_PTP_GRANDMASTER_UNLOCK"),
        ("EPHEMERIS_CRC_STALE", "STALE_ORBITAL_EPHEMERIS_SCHEDULE"),
        ("ISL_EGRESS_QUEUE_HIGH", "HANDOFF_TARGET_ISL_CONGESTION"),
        ("CONSTELLATION_CAP_LIMIT", "REGIONAL_CONSTELLATION_CAPACITY_EXHAUSTION"),
        ("PLL_CARRIER_SLIP", "DOPPLER_CARRIER_PLL_SLIP"),
        ("IDEMPOTENCY_KEY_NULL", "UPSTREAM_MISSING_IDEMPOTENCY_KEY_RETRY"),
        ("WATERMARK_DELAYED_BATCH", "LATE_GATEWAY_BATCH_WATERMARK_LAG"),
        ("CONSUMER_OFFSET_STALLED", "METERING_CONSUMER_PARTITION_STALL"),
        ("TAX_CATALOG_MISMATCH", "REGIONAL_TAX_SCHEDULE_VERSION_SKEW"),
        ("VFD_OVERCURRENT_TRIP", "CONVEYOR_MECHANICAL_BIND_THERMAL_TRIP"),
        ("VFD_BLOWER_RPM_LOW", "VFD_CABINET_COOLING_FAN_FAILURE"),
        ("CHUTE_FULL_ALT_OPEN", "SORT_CHUTE_FULL_DIVERT_ELIGIBLE"),
        ("PACK_WALL_CONGESTED", "PACK_WALL_DOWNSTREAM_BACKPRESSURE"),
        ("PHOTO_EYE_BLOCKED", "PHOTO_EYE_REFLECTOR_OCCLUSION"),
        ("INFLIGHT_SCAN_PENDING", "TRANSIENT_INFLIGHT_PICK_STOW_LAG"),
        ("BIN_SHORTAGE_ORDER_RISK", "PHYSICAL_BIN_SHORTAGE_OPEN_ORDER_RISK"),
        ("BULK_LOT_COUNT_DIFF", "BULK_STOW_LOT_QUANTITY_MISMATCH"),
        ("NEIGHBOR_CUBBY_SCAN", "ADJACENT_BIN_SINGLE_UNIT_MISSTOW"),
    ]

    def scan(self, snapshot: IncidentTelemetrySnapshot) -> Tuple[str, int, float]:
        """Return `(predicted_root_cause, log_lines_scanned, scan_time_ms)`."""
        t0 = time.perf_counter()
        lines_scanned = 0
        predicted = "UNKNOWN_INCIDENT_CAUSE"

        for line in snapshot.raw_logs:
            lines_scanned += 1
            if "ERROR" not in line and "WARN" not in line:
                continue
            matched = False
            for token, root_cause in self.KEYWORD_RULES:
                if token in line:
                    predicted = root_cause
                    matched = True
                    break
            if matched:
                break

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return predicted, lines_scanned, elapsed_ms


def generate_incident_workload(
    num_incidents: int = 150,
    seed: int = 42,
) -> List[IncidentTelemetrySnapshot]:
    """Generate `num_incidents` deterministic telemetry + 60-line operational log snapshots."""
    rng = random.Random(seed)
    regions = ["us-west-2", "us-east-1", "eu-central-1", "ap-northeast-1", "sa-east-1"]

    # Scenario templates: (alarm_type, expected_code, metrics, root_token, sympathetic_early_token)
    templates: List[Tuple[str, str, Dict[str, Any], str, Optional[str]]] = [
        (
            "GROUND_GATEWAY_SNR_DROP",
            "RF_LNA_FAULT_STANDBY_FAILOVER",
            {"rf_lna_bias_ma": 42.0, "standby_rf_chain_ready": True, "rain_rate_mm_hr": 2.0, "diversity_gw_snr_db": 17.5},
            "LNA_BIAS_UNDERVOLT",
            "PLL_CARRIER_SLIP",
        ),
        (
            "GROUND_GATEWAY_SNR_DROP",
            "RF_LNA_FAULT_NO_STANDBY",
            {"rf_lna_bias_ma": 31.0, "standby_rf_chain_ready": False, "rain_rate_mm_hr": 4.0, "diversity_gw_snr_db": 15.0},
            "LNA_BIAS_UNDERVOLT_NO_STBY",
            "LNA_BIAS_UNDERVOLT",
        ),
        (
            "GROUND_GATEWAY_SNR_DROP",
            "KA_RAIN_FADE_DIVERSITY_REROUTE",
            {"rf_lna_bias_ma": 118.0, "standby_rf_chain_ready": True, "rain_rate_mm_hr": 34.5, "diversity_gw_snr_db": 16.8},
            "RAIN_ATTEN_DIVERSITY_OK",
            None,
        ),
        (
            "GROUND_GATEWAY_SNR_DROP",
            "KA_RAIN_FADE_REGIONAL_ACM_STEPDOWN",
            {"rf_lna_bias_ma": 116.0, "standby_rf_chain_ready": True, "rain_rate_mm_hr": 41.0, "diversity_gw_snr_db": 9.2},
            "RAIN_ATTEN_SEVERE",
            "ISL_EGRESS_QUEUE_HIGH",
        ),
        (
            "GROUND_GATEWAY_SNR_DROP",
            "GATEWAY_GIMBAL_POINTING_DRIFT",
            {"rf_lna_bias_ma": 119.0, "standby_rf_chain_ready": True, "rain_rate_mm_hr": 1.5, "diversity_gw_snr_db": 18.0},
            "GIMBAL_SERVO_ERR",
            None,
        ),
        (
            "SATELLITE_HANDOFF_PACKET_LOSS",
            "HANDOFF_PTP_GRANDMASTER_UNLOCK",
            {"ephemeris_skew_ms": 9.4, "ptp_locked": False, "target_isl_utilization": 0.62, "adjacent_plane_residual_mbps": 310.0},
            "PTP_GM_UNLOCKED",
            "PLL_CARRIER_SLIP",
        ),
        (
            "SATELLITE_HANDOFF_PACKET_LOSS",
            "STALE_ORBITAL_EPHEMERIS_SCHEDULE",
            {"ephemeris_skew_ms": 6.8, "ptp_locked": True, "target_isl_utilization": 0.58, "adjacent_plane_residual_mbps": 290.0},
            "EPHEMERIS_CRC_STALE",
            None,
        ),
        (
            "SATELLITE_HANDOFF_PACKET_LOSS",
            "HANDOFF_TARGET_ISL_CONGESTION",
            {"ephemeris_skew_ms": 0.8, "ptp_locked": True, "target_isl_utilization": 0.95, "adjacent_plane_residual_mbps": 240.0},
            "ISL_EGRESS_QUEUE_HIGH",
            None,
        ),
        (
            "SATELLITE_HANDOFF_PACKET_LOSS",
            "REGIONAL_CONSTELLATION_CAPACITY_EXHAUSTION",
            {"ephemeris_skew_ms": 1.1, "ptp_locked": True, "target_isl_utilization": 0.97, "adjacent_plane_residual_mbps": 65.0},
            "CONSTELLATION_CAP_LIMIT",
            "ISL_EGRESS_QUEUE_HIGH",
        ),
        (
            "ISP_BILLING_RECONCILIATION_MISMATCH",
            "UPSTREAM_MISSING_IDEMPOTENCY_KEY_RETRY",
            {"unkeyed_retry_events": 14, "watermark_lag_sec": 45, "delayed_gateway_batches": 0},
            "IDEMPOTENCY_KEY_NULL",
            None,
        ),
        (
            "ISP_BILLING_RECONCILIATION_MISMATCH",
            "LATE_GATEWAY_BATCH_WATERMARK_LAG",
            {"unkeyed_retry_events": 0, "watermark_lag_sec": 640, "delayed_gateway_batches": 3},
            "WATERMARK_DELAYED_BATCH",
            None,
        ),
        (
            "ISP_BILLING_RECONCILIATION_MISMATCH",
            "METERING_CONSUMER_PARTITION_STALL",
            {"unkeyed_retry_events": 0, "watermark_lag_sec": 820, "delayed_gateway_batches": 0},
            "CONSUMER_OFFSET_STALLED",
            "WATERMARK_DELAYED_BATCH",
        ),
        (
            "ISP_BILLING_RECONCILIATION_MISMATCH",
            "REGIONAL_TAX_SCHEDULE_VERSION_SKEW",
            {"unkeyed_retry_events": 0, "watermark_lag_sec": 35, "delayed_gateway_batches": 0},
            "TAX_CATALOG_MISMATCH",
            None,
        ),
        (
            "FULFILLMENT_CONVEYOR_BUFFER_JAM",
            "CONVEYOR_MECHANICAL_BIND_THERMAL_TRIP",
            {"vfd_motor_temp_c": 94.5, "motor_current_amps": 17.2, "downstream_chute_fill_pct": 52.0, "secondary_lane_available": True},
            "VFD_OVERCURRENT_TRIP",
            "PHOTO_EYE_BLOCKED",
        ),
        (
            "FULFILLMENT_CONVEYOR_BUFFER_JAM",
            "SORT_CHUTE_FULL_DIVERT_ELIGIBLE",
            {"vfd_motor_temp_c": 61.0, "motor_current_amps": 9.4, "downstream_chute_fill_pct": 97.0, "secondary_lane_available": True},
            "CHUTE_FULL_ALT_OPEN",
            None,
        ),
        (
            "FULFILLMENT_CONVEYOR_BUFFER_JAM",
            "PACK_WALL_DOWNSTREAM_BACKPRESSURE",
            {"vfd_motor_temp_c": 64.0, "motor_current_amps": 10.1, "downstream_chute_fill_pct": 98.5, "secondary_lane_available": False},
            "PACK_WALL_CONGESTED",
            "PHOTO_EYE_BLOCKED",
        ),
        (
            "INVENTORY_BIN_COUNT_DRIFT",
            "TRANSIENT_INFLIGHT_PICK_STOW_LAG",
            {"inflight_scans": 2, "oldest_scan_age_sec": 38, "unit_variance": -2, "allocated_open_orders": 1},
            "INFLIGHT_SCAN_PENDING",
            None,
        ),
        (
            "INVENTORY_BIN_COUNT_DRIFT",
            "PHYSICAL_BIN_SHORTAGE_OPEN_ORDER_RISK",
            {"inflight_scans": 0, "oldest_scan_age_sec": 0, "unit_variance": -5, "allocated_open_orders": 4},
            "BIN_SHORTAGE_ORDER_RISK",
            "INFLIGHT_SCAN_PENDING",
        ),
        (
            "INVENTORY_BIN_COUNT_DRIFT",
            "BULK_STOW_LOT_QUANTITY_MISMATCH",
            {"inflight_scans": 0, "oldest_scan_age_sec": 0, "unit_variance": 6, "allocated_open_orders": 0},
            "BULK_LOT_COUNT_DIFF",
            None,
        ),
    ]

    snapshots: List[IncidentTelemetrySnapshot] = []
    for idx in range(num_incidents):
        alarm_type, expected_code, base_metrics, root_tok, symp_tok = templates[idx % len(templates)]
        reg = regions[idx % len(regions)]
        res_id = f"{alarm_type.split('_')[0].lower()}-{reg}-{(idx % 12) + 1:02d}"

        # Build 60-line operational log bundle with routine heartbeats + cascading noise
        logs: List[str] = []
        root_line_idx = rng.randint(36, 54)
        symp_line_idx = rng.randint(14, 28) if symp_tok is not None else -1

        for line_i in range(60):
            ts = f"2026-11-23T08:{(idx % 60):02d}:{line_i:02d}Z"
            if line_i == symp_line_idx and symp_tok is not None and rng.random() < 0.72:
                logs.append(f"{ts} WARN [{res_id}] cascading symptom detected code={symp_tok}")
            elif line_i == root_line_idx:
                logs.append(f"{ts} ERROR [{res_id}] primary diagnostic event code={root_tok}")
            else:
                logs.append(f"{ts} INFO [{res_id}] telemetry_heartbeat seq={line_i} status=NOMINAL")

        snapshots.append(
            IncidentTelemetrySnapshot(
                incident_id=f"INC-{idx + 1:04d}",
                alarm_type=alarm_type,
                region=reg,
                resource_id=res_id,
                metrics=dict(base_metrics),
                raw_logs=logs,
                expected_root_cause=expected_code,
            )
        )
    return snapshots


def run_workflow_comparison(
    num_incidents: int = 150,
    seed: int = 42,
) -> Dict[str, Any]:
    """Compare `OperationalWorkflowEngine` vs. `UnstructuredLogScannerBaseline`."""
    snapshots = generate_incident_workload(num_incidents=num_incidents, seed=seed)
    engine = OperationalWorkflowEngine()
    scanner = UnstructuredLogScannerBaseline()

    wf_results: List[DiagnosticResult] = []
    wf_correct = 0

    scan_steps: List[int] = []
    scan_times: List[float] = []
    scan_correct = 0

    for snap in snapshots:
        res = engine.diagnose(snap)
        wf_results.append(res)
        if res.root_cause_code == snap.expected_root_cause:
            wf_correct += 1

        pred, lines_read, scan_ms = scanner.scan(snap)
        scan_steps.append(lines_read)
        scan_times.append(scan_ms)
        if pred == snap.expected_root_cause:
            scan_correct += 1

    wf_checks = [r.checks_executed for r in wf_results]
    wf_times = [r.eval_time_ms for r in wf_results]

    mean_wf_checks = statistics.mean(wf_checks)
    mean_scan_lines = statistics.mean(scan_steps)
    step_reduction_pct = (
        ((mean_scan_lines - mean_wf_checks) / mean_scan_lines) * 100.0
        if mean_scan_lines > 0
        else 0.0
    )

    return {
        "workload": {
            "incident_count": len(snapshots),
            "alarm_classes": list(SUPPORTED_ALARM_TYPES),
            "log_lines_per_incident": 60,
        },
        "structured_workflow_engine": {
            "diagnostic_accuracy_pct": round((wf_correct / len(snapshots)) * 100.0, 2),
            "correct_diagnoses": wf_correct,
            "max_checks_executed": max(wf_checks),
            "mean_checks_executed": round(mean_wf_checks, 2),
            "total_eval_time_ms": round(sum(wf_times), 3),
            "mean_eval_time_ms": round(statistics.mean(wf_times), 4),
        },
        "unstructured_log_scanner_baseline": {
            "diagnostic_accuracy_pct": round((scan_correct / len(snapshots)) * 100.0, 2),
            "correct_diagnoses": scan_correct,
            "mean_lines_scanned": round(mean_scan_lines, 2),
            "max_lines_scanned": max(scan_steps),
            "total_scan_time_ms": round(sum(scan_times), 3),
        },
        "improvements": {
            "accuracy_gain_pct_points": round(
                ((wf_correct - scan_correct) / len(snapshots)) * 100.0, 2
            ),
            "diagnostic_step_reduction_pct": round(step_reduction_pct, 2),
            "bounded_within_3_checks": max(wf_checks) <= 3,
        },
        "sample_diagnoses": [wf_results[i].to_dict() for i in range(min(5, len(wf_results)))],
    }
