"""Unit and invariant test suite (`python3 -m unittest discover -s tests -v`).

Covers:
  - 28-node LEO/gateway mesh routing, outage rerouting, SNR avoidance, capacity limits,
    flow expiration, and 2-way residual flow splitting (`route_ledger.network_router`)
  - 5x idempotent retry deduplication, payload hash conflict detection, multi-region tax math,
    out-of-order timestamps, credit/inventory rejections, and strict `SUM(debits) == SUM(credits)`
    double-entry conservation (`route_ledger.double_entry_ledger`)
  - Bounded (`<= 3` checks) root-cause isolation across all 5 NOC & fulfillment alarm classes
    (`route_ledger.noc_workflow`)
  - CLI modes and AWS Lambda Function URL handler (`route_ledger.cli`, `route_ledger.aws_lambda_handler`)
"""

from __future__ import annotations

import base64
import io
import json
import unittest
from contextlib import redirect_stdout

from route_ledger.aws_lambda_handler import handler
from route_ledger.cli import execute_cli_mode, main as cli_main
from route_ledger.double_entry_ledger import (
    IdempotencyConflictError,
    IdempotentSettlementLedger,
    UsageOrderEvent,
    compute_regional_charge_cents,
    run_ledger_comparison,
)
from route_ledger.network_router import (
    CapacityAwareRouter,
    LinkDegradationEvent,
    LinkState,
    StaticShortestPathRouter,
    TrafficDemand,
    build_default_28_node_mesh,
    run_router_comparison,
)
from route_ledger.noc_workflow import (
    SUPPORTED_ALARM_TYPES,
    IncidentTelemetrySnapshot,
    OperationalWorkflowEngine,
    run_workflow_comparison,
)


class TestCapacityAwareRouter(unittest.TestCase):
    """Tests for `CapacityAwareRouter` and `StaticShortestPathRouter`."""

    def test_01_default_28_node_mesh_structure(self) -> None:
        nodes, links = build_default_28_node_mesh()
        self.assertEqual(len(nodes), 28)
        self.assertEqual(len(set(nodes)), 28)
        self.assertGreaterEqual(len(links), 100)
        pairs = {(lnk.src, lnk.dst) for lnk in links}
        for lnk in links:
            self.assertIn((lnk.dst, lnk.src), pairs)

    def test_02_reroutes_around_hard_link_outage(self) -> None:
        nodes = ["A", "B", "C", "D"]
        links = [
            LinkState("L1", "A", "B", "KA_FEEDER", 500.0, 5.0, snr_db=22.0),
            LinkState("L2", "B", "D", "OPTICAL_ISL", 500.0, 5.0, snr_db=25.0),
            LinkState("L3", "A", "C", "KA_FEEDER", 500.0, 8.0, snr_db=21.0),
            LinkState("L4", "C", "D", "OPTICAL_ISL", 500.0, 8.0, snr_db=25.0),
        ]
        outage = LinkDegradationEvent(
            event_id="OUT-1",
            step=0,
            src="B",
            dst="D",
            capacity_factor=0.0,
            snr_db=0.0,
            noise_penalty_ms=0.0,
            outage=True,
            reason="OPTICAL_FAULT",
            bidirectional=False,
        )
        static_r = StaticShortestPathRouter(nodes, links)
        aware_r = CapacityAwareRouter(nodes, links)
        static_r.apply_degradation_event(outage)
        aware_r.apply_degradation_event(outage)

        dmd = TrafficDemand("D1", 1, 10, "A", "D", 100.0, sla_max_latency_ms=60.0, max_hops=4)
        s_alloc = static_r.route_demand(dmd)
        a_alloc = aware_r.route_demand(dmd)

        self.assertTrue(s_alloc.sla_violated)
        self.assertAlmostEqual(s_alloc.dropped_mbps, 100.0)
        self.assertFalse(a_alloc.sla_violated)
        self.assertEqual(a_alloc.subflows[0].nodes, ["A", "C", "D"])
        self.assertAlmostEqual(a_alloc.dropped_mbps, 0.0)

    def test_03_avoids_low_snr_rain_fade_link(self) -> None:
        nodes, links = build_default_28_node_mesh()
        router = CapacityAwareRouter(nodes, links, min_snr_db=8.0)
        router.apply_degradation_event(
            LinkDegradationEvent(
                event_id="RAIN-SEA",
                step=0,
                src="GW-US-WEST",
                dst="SAT-P0-S0",
                capacity_factor=0.25,
                snr_db=5.5,
                noise_penalty_ms=30.0,
                outage=False,
                reason="HEAVY_RAIN_FADE",
            )
        )
        dmd = TrafficDemand("D-SEA-NRT", 1, 15, "POP-SEA", "POP-NRT", 90.0, 95.0, max_hops=7)
        alloc = router.route_demand(dmd)
        self.assertTrue(alloc.routed)
        self.assertFalse(alloc.sla_violated)
        for sf in alloc.subflows:
            path_edges = list(zip(sf.nodes, sf.nodes[1:]))
            self.assertNotIn(("GW-US-WEST", "SAT-P0-S0"), path_edges)

    def test_04_enforces_link_capacity_and_steers_secondary_path(self) -> None:
        nodes = ["S", "M1", "M2", "T"]
        links = [
            LinkState("L-SM1", "S", "M1", "KA_FEEDER", 150.0, 4.0, snr_db=22.0),
            LinkState("L-M1T", "M1", "T", "OPTICAL_ISL", 150.0, 4.0, snr_db=25.0),
            LinkState("L-SM2", "S", "M2", "KA_FEEDER", 150.0, 7.0, snr_db=22.0),
            LinkState("L-M2T", "M2", "T", "OPTICAL_ISL", 150.0, 7.0, snr_db=25.0),
        ]
        router = CapacityAwareRouter(nodes, links)
        a1 = router.route_demand(TrafficDemand("F1", 0, 20, "S", "T", 110.0, 60.0, max_hops=4))
        a2 = router.route_demand(TrafficDemand("F2", 1, 20, "S", "T", 110.0, 60.0, max_hops=4))

        self.assertEqual(a1.subflows[0].nodes, ["S", "M1", "T"])
        self.assertEqual(a2.subflows[0].nodes, ["S", "M2", "T"])
        self.assertFalse(a2.sla_violated)

    def test_05_two_way_residual_flow_splitting_for_elephant_demand(self) -> None:
        nodes = ["S", "P1", "P2", "T"]
        links = [
            LinkState("E1", "S", "P1", "KA_FEEDER", 200.0, 6.0, snr_db=22.0),
            LinkState("E2", "P1", "T", "OPTICAL_ISL", 200.0, 6.0, snr_db=25.0),
            LinkState("E3", "S", "P2", "KA_FEEDER", 200.0, 7.0, snr_db=22.0),
            LinkState("E4", "P2", "T", "OPTICAL_ISL", 200.0, 7.0, snr_db=25.0),
        ]
        router = CapacityAwareRouter(nodes, links, allow_flow_splitting=True)
        # 280 Mbps exceeds any single 200 Mbps path, requiring a 2-way residual split
        dmd = TrafficDemand("ELEPHANT-1", 0, 10, "S", "T", 280.0, sla_max_latency_ms=80.0, max_hops=4)
        alloc = router.route_demand(dmd)

        self.assertTrue(alloc.routed)
        self.assertEqual(alloc.split_count, 2)
        self.assertAlmostEqual(sum(sf.allocated_mbps for sf in alloc.subflows), 280.0)
        self.assertAlmostEqual(alloc.dropped_mbps, 0.0)
        self.assertFalse(alloc.sla_violated)

    def test_06_flow_expiration_releases_reserved_bandwidth(self) -> None:
        nodes = ["S", "T"]
        links = [LinkState("DIRECT", "S", "T", "ACCESS_FIBER", 100.0, 3.0, snr_db=28.0)]
        router = CapacityAwareRouter(nodes, links)

        alloc1 = router.route_demand(TrafficDemand("D1", 0, 5, "S", "T", 80.0, 40.0, max_hops=2))
        self.assertTrue(alloc1.routed)
        self.assertAlmostEqual(router.links_by_id["DIRECT"].residual_mbps, 20.0)

        # At step 5, D1 expires so another 80 Mbps demand fits cleanly
        alloc2 = router.route_demand(TrafficDemand("D2", 5, 5, "S", "T", 80.0, 40.0, max_hops=2))
        self.assertTrue(alloc2.routed)
        self.assertAlmostEqual(router.links_by_id["DIRECT"].allocated_mbps, 80.0)

    def test_07_unroutable_when_cutset_exceeded_without_splitting(self) -> None:
        nodes = ["S", "T"]
        links = [LinkState("SMALL", "S", "T", "KA_FEEDER", 50.0, 5.0, snr_db=20.0)]
        router = CapacityAwareRouter(nodes, links, allow_flow_splitting=False)
        alloc = router.route_demand(TrafficDemand("BIG", 0, 5, "S", "T", 120.0, 50.0, max_hops=2))
        self.assertFalse(alloc.routed)
        self.assertTrue(alloc.sla_violated)
        self.assertIn("NO_FEASIBLE_PATH", alloc.violation_reasons)

    def test_08_router_benchmark_determinism_and_sla_gains(self) -> None:
        run1 = run_router_comparison(num_demands=120, seed=42)
        run2 = run_router_comparison(num_demands=120, seed=42)
        self.assertEqual(
            run1["capacity_aware_router"]["sla_violations"],
            run2["capacity_aware_router"]["sla_violations"],
        )
        self.assertGreater(run1["improvements"]["p99_latency_reduction_pct"], 50.0)
        self.assertGreater(run1["improvements"]["sla_violation_reduction_pct"], 80.0)


class TestIdempotentSettlementLedger(unittest.TestCase):
    """Tests for SQLite ACID double-entry settlement, idempotency, taxes, and reconciliation."""

    def setUp(self) -> None:
        self.ledger = IdempotentSettlementLedger(":memory:")
        self.ledger.seed_account("ACCT-US", "US-WA", credit_limit_cents=100_000)
        self.ledger.seed_account("ACCT-DE", "DE", credit_limit_cents=100_000)
        self.ledger.seed_account("ACCT-JP", "JP", credit_limit_cents=100_000)
        self.ledger.seed_account("ACCT-BR", "BR", credit_limit_cents=100_000)
        self.ledger.seed_inventory_sku("SKU-KIT", "LEO Standard Dish", on_hand_qty=3)

    def tearDown(self) -> None:
        self.ledger.close()

    def test_09_multi_region_tax_quantization_rules(self) -> None:
        # US-WA: 10.25% on 10,000 cents -> 1,025 tax -> 11,025 total
        self.assertEqual(compute_regional_charge_cents("1", 10_000, "US-WA"), (10_000, 1_025, 11_025))
        # DE: 19.00% on 10,000 cents -> 1,900 tax -> 11,900 total
        self.assertEqual(compute_regional_charge_cents("1", 10_000, "DE"), (10_000, 1_900, 11_900))
        # JP: 10.00% on 10,000 cents -> 1,000 tax -> 11,000 total
        self.assertEqual(compute_regional_charge_cents("1", 10_000, "JP"), (10_000, 1_000, 11_000))
        # BR: 26.50% on 10,000 cents -> 2,650 tax -> 12,650 total
        self.assertEqual(compute_regional_charge_cents("1", 10_000, "BR"), (10_000, 2_650, 12_650))

    def test_10_idempotency_under_5x_duplicate_retries(self) -> None:
        ev = UsageOrderEvent(
            event_id="E-100",
            idempotency_key="IDEM-5X",
            account_id="ACCT-US",
            region="US-WA",
            event_type="TERMINAL_KIT_ORDER",
            quantity="1",
            unit_price_cents=20_000,
            event_timestamp=1_700_000_100,
            sku_id="SKU-KIT",
            sku_qty=1,
        )
        receipts = [self.ledger.process_event(ev) for _ in range(5)]
        self.assertFalse(receipts[0].is_duplicate)
        for retry_receipt in receipts[1:]:
            self.assertTrue(retry_receipt.is_duplicate)
            self.assertEqual(retry_receipt.status, "SETTLED")
            self.assertEqual(retry_receipt.total_cents, receipts[0].total_cents)

        # Charged once (20,000 + 2,050 = 22,050 cents) and reserved 1 kit out of 3
        self.assertEqual(self.ledger.get_account_balance_cents("ACCT-US"), 22_050)
        self.assertEqual(self.ledger.get_sku_availability("SKU-KIT"), 2)
        debits, credits, balanced = self.ledger.verify_trial_balance()
        self.assertTrue(balanced)
        self.assertEqual(debits, 22_050)
        self.assertEqual(credits, 22_050)

    def test_11_idempotency_conflict_raises_on_mutated_payload(self) -> None:
        ev1 = UsageOrderEvent(
            event_id="E-1",
            idempotency_key="IDEM-CONFLICT",
            account_id="ACCT-DE",
            region="DE",
            event_type="METERED_BANDWIDTH_GB",
            quantity="10.500",
            unit_price_cents=200,
            event_timestamp=1_700_000_200,
        )
        ev2_mutated = UsageOrderEvent(
            event_id="E-1-MUT",
            idempotency_key="IDEM-CONFLICT",
            account_id="ACCT-DE",
            region="DE",
            event_type="METERED_BANDWIDTH_GB",
            quantity="19.500",  # Mutated quantity under same key
            unit_price_cents=200,
            event_timestamp=1_700_000_200,
        )
        self.ledger.process_event(ev1)
        with self.assertRaises(IdempotencyConflictError):
            self.ledger.process_event(ev2_mutated)

    def test_12_insufficient_credit_rejection_preserves_trial_balance(self) -> None:
        # 95,000 subtotal + 9,738 tax = 104,738 > 100,000 credit limit
        over_limit = UsageOrderEvent(
            event_id="E-OVER",
            idempotency_key="IDEM-OVER",
            account_id="ACCT-US",
            region="US-WA",
            event_type="MONTHLY_SUBSCRIPTION",
            quantity="1",
            unit_price_cents=95_000,
            event_timestamp=1_700_000_300,
        )
        rec = self.ledger.process_event(over_limit)
        self.assertEqual(rec.status, "REJECTED")
        self.assertEqual(rec.rejection_reason, "INSUFFICIENT_CREDIT")
        self.assertEqual(self.ledger.get_account_balance_cents("ACCT-US"), 0)

        debits, credits, balanced = self.ledger.verify_trial_balance()
        self.assertTrue(balanced)
        self.assertEqual(debits, 0)
        self.assertEqual(credits, 0)

        # Retry of rejected event returns cached idempotent rejection
        rec_retry = self.ledger.process_event(over_limit)
        self.assertTrue(rec_retry.is_duplicate)
        self.assertEqual(rec_retry.status, "REJECTED")

    def test_13_insufficient_inventory_rejection_does_not_charge_customer(self) -> None:
        # Request 4 kits when only 3 are in stock
        over_sku = UsageOrderEvent(
            event_id="E-SKU-OVER",
            idempotency_key="IDEM-SKU-OVER",
            account_id="ACCT-JP",
            region="JP",
            event_type="TERMINAL_KIT_ORDER",
            quantity="4",
            unit_price_cents=5_000,
            event_timestamp=1_700_000_400,
            sku_id="SKU-KIT",
            sku_qty=4,
        )
        rec = self.ledger.process_event(over_sku)
        self.assertEqual(rec.status, "REJECTED")
        self.assertEqual(rec.rejection_reason, "INSUFFICIENT_INVENTORY")
        self.assertEqual(self.ledger.get_account_balance_cents("ACCT-JP"), 0)
        self.assertEqual(self.ledger.get_sku_availability("SKU-KIT"), 3)
        _, _, balanced = self.ledger.verify_trial_balance()
        self.assertTrue(balanced)

    def test_14_out_of_order_timestamp_watermark_and_reconciliation(self) -> None:
        ev_newer = UsageOrderEvent(
            event_id="E-NEW",
            idempotency_key="IDEM-NEW",
            account_id="ACCT-BR",
            region="BR",
            event_type="METERED_BANDWIDTH_GB",
            quantity="5.250",
            unit_price_cents=200,
            event_timestamp=1_700_001_000,
        )
        ev_older_late = UsageOrderEvent(
            event_id="E-LATE",
            idempotency_key="IDEM-LATE",
            account_id="ACCT-BR",
            region="BR",
            event_type="METERED_BANDWIDTH_GB",
            quantity="3.125",
            unit_price_cents=200,
            event_timestamp=1_700_000_500,
        )
        r1 = self.ledger.process_event(ev_newer)
        r2 = self.ledger.process_event(ev_older_late)
        self.assertFalse(r1.is_out_of_order)
        self.assertTrue(r2.is_out_of_order)

        recon = self.ledger.reconcile_against_metering_log([ev_newer, ev_older_late, ev_newer])
        self.assertEqual(recon.duplicate_retries_blocked, 1)
        self.assertEqual(recon.out_of_order_events_handled, 1)
        self.assertEqual(recon.reconciliation_diff_cents, 0)
        self.assertTrue(recon.is_double_entry_balanced)
        self.assertEqual(recon.unmatched_keys, [])

    def test_15_full_2000_event_ledger_comparison_invariants(self) -> None:
        res = run_ledger_comparison(total_events=2000, duplicate_rate=0.15, seed=42)
        idem = res["idempotent_double_entry_ledger"]
        naive = res["naive_float_baseline"]

        self.assertEqual(res["workload"]["events_processed"], 2000)
        self.assertEqual(res["workload"]["injected_duplicate_retries"], 300)
        self.assertEqual(idem["double_charges_applied"], 0)
        self.assertEqual(idem["duplicate_charge_elimination_pct"], 100.0)
        self.assertEqual(idem["rounding_drift_cents"], 0)
        self.assertTrue(idem["double_entry_balanced"])
        self.assertEqual(idem["total_debits_cents"], idem["total_credits_cents"])
        self.assertGreater(naive["duplicate_charges_applied"], 200)
        self.assertGreater(naive["float_rounding_drift_on_settled_set_cents"], 0)


class TestOperationalWorkflowEngine(unittest.TestCase):
    """Tests for bounded `<= 3` step NOC and fulfillment floor decision trees."""

    def setUp(self) -> None:
        self.engine = OperationalWorkflowEngine()

    def test_16_gateway_snr_drop_rain_fade_vs_lna_hardware_fault(self) -> None:
        rain_snap = IncidentTelemetrySnapshot(
            incident_id="INC-GW-1",
            alarm_type="GROUND_GATEWAY_SNR_DROP",
            region="us-west-2",
            resource_id="gw-sea-01",
            metrics={"rf_lna_bias_ma": 118.0, "rain_rate_mm_hr": 38.0, "diversity_gw_snr_db": 16.2},
            raw_logs=[],
        )
        lna_snap = IncidentTelemetrySnapshot(
            incident_id="INC-GW-2",
            alarm_type="GROUND_GATEWAY_SNR_DROP",
            region="us-west-2",
            resource_id="gw-sea-02",
            metrics={"rf_lna_bias_ma": 24.0, "standby_rf_chain_ready": False},
            raw_logs=[],
        )
        r_rain = self.engine.diagnose(rain_snap)
        r_lna = self.engine.diagnose(lna_snap)

        self.assertEqual(r_rain.root_cause_code, "KA_RAIN_FADE_DIVERSITY_REROUTE")
        self.assertLessEqual(r_rain.checks_executed, 3)
        self.assertFalse(r_rain.requires_human_approval)

        self.assertEqual(r_lna.root_cause_code, "RF_LNA_FAULT_NO_STANDBY")
        self.assertEqual(r_lna.checks_executed, 2)
        self.assertTrue(r_lna.requires_human_approval)

    def test_17_satellite_handoff_packet_loss_branches(self) -> None:
        ptp_snap = IncidentTelemetrySnapshot(
            incident_id="INC-SAT-1",
            alarm_type="SATELLITE_HANDOFF_PACKET_LOSS",
            region="eu-central-1",
            resource_id="sat-p1-s2",
            metrics={"ephemeris_skew_ms": 8.2, "ptp_locked": False},
            raw_logs=[],
        )
        isl_snap = IncidentTelemetrySnapshot(
            incident_id="INC-SAT-2",
            alarm_type="SATELLITE_HANDOFF_PACKET_LOSS",
            region="eu-central-1",
            resource_id="sat-p1-s3",
            metrics={
                "ephemeris_skew_ms": 0.9,
                "ptp_locked": True,
                "target_isl_utilization": 0.96,
                "adjacent_plane_residual_mbps": 260.0,
            },
            raw_logs=[],
        )
        self.assertEqual(self.engine.diagnose(ptp_snap).root_cause_code, "HANDOFF_PTP_GRANDMASTER_UNLOCK")
        res_isl = self.engine.diagnose(isl_snap)
        self.assertEqual(res_isl.root_cause_code, "HANDOFF_TARGET_ISL_CONGESTION")
        self.assertEqual(res_isl.checks_executed, 3)

    def test_18_isp_billing_reconciliation_mismatch_branches(self) -> None:
        unkeyed = IncidentTelemetrySnapshot(
            incident_id="INC-BIL-1",
            alarm_type="ISP_BILLING_RECONCILIATION_MISMATCH",
            region="us-east-1",
            resource_id="regulus-batch-07",
            metrics={"unkeyed_retry_events": 9, "watermark_lag_sec": 20},
            raw_logs=[],
        )
        tax_skew = IncidentTelemetrySnapshot(
            incident_id="INC-BIL-2",
            alarm_type="ISP_BILLING_RECONCILIATION_MISMATCH",
            region="sa-east-1",
            resource_id="regulus-br-02",
            metrics={"unkeyed_retry_events": 0, "watermark_lag_sec": 40, "delayed_gateway_batches": 0},
            raw_logs=[],
        )
        r1 = self.engine.diagnose(unkeyed)
        r2 = self.engine.diagnose(tax_skew)
        self.assertEqual(r1.root_cause_code, "UPSTREAM_MISSING_IDEMPOTENCY_KEY_RETRY")
        self.assertEqual(r1.checks_executed, 1)
        self.assertEqual(r2.root_cause_code, "REGIONAL_TAX_SCHEDULE_VERSION_SKEW")
        self.assertLessEqual(r2.checks_executed, 3)

    def test_19_fulfillment_conveyor_jam_and_inventory_drift_branches(self) -> None:
        jam_snap = IncidentTelemetrySnapshot(
            incident_id="INC-AFT-1",
            alarm_type="FULFILLMENT_CONVEYOR_BUFFER_JAM",
            region="us-west-2",
            resource_id="fc-bfi4-conv-12",
            metrics={"vfd_motor_temp_c": 93.0, "motor_current_amps": 16.8},
            raw_logs=[],
        )
        bin_snap = IncidentTelemetrySnapshot(
            incident_id="INC-IMS-1",
            alarm_type="INVENTORY_BIN_COUNT_DRIFT",
            region="us-west-2",
            resource_id="bin-p1-a204",
            metrics={
                "inflight_scans": 0,
                "oldest_scan_age_sec": 0,
                "unit_variance": -4,
                "allocated_open_orders": 3,
            },
            raw_logs=[],
        )
        r_jam = self.engine.diagnose(jam_snap)
        r_bin = self.engine.diagnose(bin_snap)

        self.assertEqual(r_jam.root_cause_code, "CONVEYOR_MECHANICAL_BIND_THERMAL_TRIP")
        self.assertTrue(r_jam.requires_human_approval)
        self.assertLessEqual(r_jam.checks_executed, 3)

        self.assertEqual(r_bin.root_cause_code, "PHYSICAL_BIN_SHORTAGE_OPEN_ORDER_RISK")
        self.assertTrue(r_bin.requires_human_approval)
        self.assertEqual(r_bin.checks_executed, 3)

    def test_20_rejects_unknown_alarm_type_and_verifies_150_incident_suite(self) -> None:
        self.assertEqual(len(SUPPORTED_ALARM_TYPES), 5)
        bad = IncidentTelemetrySnapshot("X", "UNREGISTERED_ALARM", "us-west-2", "r", {}, [])
        with self.assertRaises(ValueError):
            self.engine.diagnose(bad)

        suite = run_workflow_comparison(num_incidents=150, seed=42)
        self.assertEqual(suite["structured_workflow_engine"]["diagnostic_accuracy_pct"], 100.0)
        self.assertLessEqual(suite["structured_workflow_engine"]["max_checks_executed"], 3)
        self.assertGreater(suite["improvements"]["diagnostic_step_reduction_pct"], 85.0)


class TestCLIAndLambdaHandler(unittest.TestCase):
    """Tests for `route_ledger.cli` and `route_ledger.aws_lambda_handler`."""

    def test_21_cli_modes_return_valid_json_telemetry(self) -> None:
        for mode in ("router", "ledger", "workflow"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main(["--mode", mode, "--demands", "40", "--events", "120", "--incidents", "30"])
            self.assertEqual(rc, 0)
            parsed = json.loads(buf.getvalue())
            self.assertEqual(parsed["mode"], mode)
            self.assertIn(mode, parsed)

    def test_22_cli_demo_all_and_invalid_mode_rejection(self) -> None:
        out = execute_cli_mode("demo-all", demands=30, events=100, incidents=20)
        self.assertIn("router", out)
        self.assertIn("ledger", out)
        self.assertIn("workflow", out)
        with self.assertRaises(ValueError):
            execute_cli_mode("nonexistent-mode")

    def test_23_lambda_health_and_root_html_endpoints(self) -> None:
        health = handler({"rawPath": "/api/health", "requestContext": {"http": {"method": "GET"}}})
        self.assertEqual(health["statusCode"], 200)
        body = json.loads(health["body"])
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["service"], "RouteLedger")

        root = handler({"rawPath": "/", "requestContext": {"http": {"method": "GET"}}})
        self.assertEqual(root["statusCode"], 200)
        self.assertIn("text/html", root["headers"]["Content-Type"])

    def test_24_lambda_post_run_and_base64_diagnose(self) -> None:
        raw_payload = json.dumps(
            {
                "mode": "diagnose",
                "alarm_type": "GROUND_GATEWAY_SNR_DROP",
                "metrics": {"rf_lna_bias_ma": 115.0, "rain_rate_mm_hr": 29.0, "diversity_gw_snr_db": 15.8},
            }
        )
        encoded = base64.b64encode(raw_payload.encode("utf-8")).decode("utf-8")
        resp = handler(
            {
                "rawPath": "/api/run",
                "isBase64Encoded": True,
                "body": encoded,
                "requestContext": {"http": {"method": "POST"}},
            }
        )
        self.assertEqual(resp["statusCode"], 200)
        parsed = json.loads(resp["body"])
        self.assertEqual(parsed["diagnosis"]["root_cause_code"], "KA_RAIN_FADE_DIVERSITY_REROUTE")

        bad_resp = handler(
            {
                "rawPath": "/api/run",
                "body": json.dumps({"mode": "bad-mode"}),
                "requestContext": {"http": {"method": "POST"}},
            }
        )
        self.assertEqual(bad_resp["statusCode"], 400)

        not_found = handler({"rawPath": "/unknown", "requestContext": {"http": {"method": "GET"}}})
        self.assertEqual(not_found["statusCode"], 404)


if __name__ == "__main__":
    unittest.main()
