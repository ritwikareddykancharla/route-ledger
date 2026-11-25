"""Unit tests for SCOT-FO ATROPS Multi-Modal Pareto Solver & LEO 5-Subsystem MCP / Flight C++ Gate."""

from __future__ import annotations

import json
import os
import unittest

from route_ledger.atrops_pareto import (
    CartItemSpec,
    CounterfactualScheduleValidator,
    MultiItemCartSourcingOptimizer,
    PRESET_SCHEDULE_PATCHES,
    ShipmentWaveDemand,
    TimeExpandedParetoSolver,
    build_scot_multimodal_topology,
    run_atrops_benchmark,
    solve_atrops_pareto_workbench,
)
from route_ledger.aws_lambda_handler import handler as standalone_lambda_handler
from route_ledger.leo_flight_mcp import (
    JplPowerOfTenVerifier,
    LeoDiagnosticMCPRegistry,
    MCP_SUBSYSTEM_TOOLS,
    PRESET_FLIGHT_CPP_SNIPPETS,
    PRESET_LEO_INCIDENTS,
    run_leo_diagnose_workbench,
    run_leo_mcp_benchmark,
)
from route_ledger.web_api import lambda_handler, route_api_request


class TestAtropsMultiModalPareto(unittest.TestCase):
    """Tests for time-expanded CPT Pareto solver, split-box cart optimizer, and LLM config gate."""

    def test_01_pareto_routes_are_strictly_non_dominated_and_respect_cpt(self) -> None:
        facs, arcs = build_scot_multimodal_topology("nominal")
        solver = TimeExpandedParetoSolver(facs, arcs)
        routes = solver.enumerate_pareto_routes(
            origin_fc="SEA1", dest_station="EWR7", ready_hr=1.0, sla_deadline_hr=36.0
        )
        self.assertGreaterEqual(len(routes), 4)

        # Verify no returned route Pareto-dominates any other returned route
        for i, r1 in enumerate(routes):
            for j, r2 in enumerate(routes):
                if i == j:
                    continue
                dom = (
                    r1.arrival_hr <= r2.arrival_hr + 1e-6
                    and r1.cost_per_pkg_usd <= r2.cost_per_pkg_usd + 1e-6
                    and r1.co2e_kg_per_pkg <= r2.co2e_kg_per_pkg + 1e-6
                    and (
                        r1.arrival_hr < r2.arrival_hr - 1e-6
                        or r1.cost_per_pkg_usd < r2.cost_per_pkg_usd - 1e-6
                        or r1.co2e_kg_per_pkg < r2.co2e_kg_per_pkg - 1e-6
                    )
                )
                self.assertFalse(dom, f"{r1.plan_id} unexpectedly dominated {r2.plan_id}")

        # Fastest air route should arrive earlier than lowest-carbon Rail/EV route, which has lower CO2e
        fastest = min(routes, key=lambda r: r.arrival_hr)
        greenest = min(routes, key=lambda r: r.co2e_kg_per_pkg)
        self.assertLess(fastest.arrival_hr, greenest.arrival_hr)
        self.assertLess(greenest.co2e_kg_per_pkg, fastest.co2e_kg_per_pkg)

    def test_02_cpt_cutoff_rollover_adds_24h_when_wave_misses_pull(self) -> None:
        dep_ok, rolls_ok = TimeExpandedParetoSolver.compute_next_cpt_departure(1.8, 2.0)
        self.assertEqual(dep_ok, 2.0)
        self.assertEqual(rolls_ok, 0)

        dep_late, rolls_late = TimeExpandedParetoSolver.compute_next_cpt_departure(2.4, 2.0)
        self.assertEqual(dep_late, 26.0)
        self.assertEqual(rolls_late, 1)

    def test_03_multi_item_cart_single_box_consolidation_vs_tight_sla_split(self) -> None:
        opt = MultiItemCartSourcingOptimizer()
        cart = [
            CartItemSpec("SKU-ANC-HEADPHONES", "Headphones", 1, 0.65),
            CartItemSpec("SKU-USBC-DOCK", "USB-C Dock", 1, 0.90),
            CartItemSpec("SKU-KINDLE-FOLIO", "Kindle Folio", 1, 0.35),
        ]
        # At 20h SLA to NY, CVG3 holds all 3 items at 16.5h transit -> 1 consolidated box beats greedy 2-box split!
        res_20h = opt.optimize_cart(cart, dest_region="US_EAST_NY", sla_deadline_hr=20.0)
        self.assertEqual(res_20h["recommended_mode"], "SINGLE_BOX_CONSOLIDATION")
        self.assertEqual(res_20h["atrops_optimal"]["box_count"], 1)
        self.assertTrue(res_20h["atrops_optimal"]["sla_met"])
        self.assertGreater(res_20h["greedy_nearest_fc_baseline"]["box_count"], 1)
        self.assertGreater(res_20h["savings_vs_greedy"]["cost_saved_usd"], 0.0)

        # Under an ultra-tight 12h Same-Day/Overnight SLA to NY, CVG3 (16.5h) is too slow,
        # so the optimizer switches to Split-FC Dispatch across EWR7 (6.5h) + BWI2 (11.0h)!
        res_12h = opt.optimize_cart(cart, dest_region="US_EAST_NY", sla_deadline_hr=12.0)
        self.assertEqual(res_12h["recommended_mode"], "SPLIT_FC_DISPATCH")
        self.assertEqual(res_12h["atrops_optimal"]["box_count"], 2)
        self.assertTrue(res_12h["atrops_optimal"]["sla_met"])

    def test_04_counterfactual_schedule_validator_approves_safe_and_blocks_overflow(self) -> None:
        validator = CounterfactualScheduleValidator()
        wave = ShipmentWaveDemand("W1", "SEA1", "EWR7", 1.4, 34.0, 3600, "ONE_DAY")

        ok = validator.validate_patch(
            "cvg3_weather_ground_stop",
            patch=PRESET_SCHEDULE_PATCHES["approved_modal_shift_and_cpt_push"],
            wave=wave,
        )
        self.assertTrue(ok["approved"])
        self.assertEqual(ok["verdict"], "APPROVED_FOR_COMMIT")
        self.assertEqual(ok["violations"], [])

        bad = validator.validate_patch(
            "cvg3_weather_ground_stop",
            patch=PRESET_SCHEDULE_PATCHES["rejected_naive_llm_sort_cap_overflow"],
            wave=wave,
        )
        self.assertFalse(bad["approved"])
        self.assertEqual(bad["verdict"], "REJECTED_BY_SOLVER_GATE")
        self.assertGreaterEqual(len(bad["violations"]), 1)

    def test_05_atrops_benchmark_invariants(self) -> None:
        bm = run_atrops_benchmark(num_waves=60, num_carts=40, seed=42)
        self.assertGreater(bm["pareto_improvements"]["co2e_per_pkg_reduction_pct"], 25.0)
        self.assertGreater(bm["pareto_improvements"]["cost_per_pkg_reduction_pct"], 20.0)
        self.assertEqual(bm["schedule_patch_gate"]["unsafe_patch_rejection_rate_pct"], 100.0)


class TestLeoFlightMcpAndCppGate(unittest.TestCase):
    """Tests for 5-subsystem LEO MCP diagnostics and JPL Power-of-10 static C/C++ verifier."""

    def test_06_all_five_mcp_tools_isolate_every_subsystem_fault(self) -> None:
        reg = LeoDiagnosticMCPRegistry(enabled_tools=MCP_SUBSYSTEM_TOOLS)
        self.assertEqual(len(reg.list_registered_tools()), 5)
        for key, inc in PRESET_LEO_INCIDENTS.items():
            diag = reg.diagnose_incident(inc)
            self.assertTrue(diag["accurate"], f"Failed on {key}: {diag}")
            self.assertEqual(diag["predicted_root_cause"], inc.expected_root_cause)

    def test_07_missing_handover_mcp_tool_exposes_blind_spot_fixed_by_onboarding(self) -> None:
        inc = PRESET_LEO_INCIDENTS["beam_handover_ephemeris_slip"]
        reg_4 = LeoDiagnosticMCPRegistry(
            enabled_tools=("rf_link_budget", "optical_isl_ber", "terminal_modem_pll", "gateway_bgp_flap")
        )
        res_blind = reg_4.diagnose_incident(inc)
        self.assertFalse(res_blind["accurate"])
        self.assertEqual(res_blind["subsystem_coverage_pct"], 80.0)

        reg_5 = LeoDiagnosticMCPRegistry(enabled_tools=MCP_SUBSYSTEM_TOOLS)
        res_fixed = reg_5.diagnose_incident(inc)
        self.assertTrue(res_fixed["accurate"])
        self.assertEqual(res_fixed["subsystem_coverage_pct"], 100.0)

    def test_08_jpl_power_of_10_static_cpp_gate_rules(self) -> None:
        verifier = JplPowerOfTenVerifier()

        # 1. Compliant zero-heap bounded handler passes
        ok_res = verifier.verify_source(PRESET_FLIGHT_CPP_SNIPPETS["compliant_ccsds_framer"]["code"])
        self.assertTrue(ok_res["approved"])
        self.assertEqual(ok_res["violation_count"], 0)

        # 2. Comments mentioning malloc / while(true) must NOT trigger false positives
        comment_only = """
        // Do not call malloc(64) or new uint8_t[16] or while(true) in flight ISR!
        int32_t read_reg(uint32_t v) {
            uint32_t acc = 0U;
            for (uint32_t i = 0U; i < 8U; ++i) { acc += v; }
            return static_cast<int32_t>(acc);
        }
        """
        self.assertTrue(verifier.verify_source(comment_only)["approved"])

        # 3. Heap (`malloc`/`new`) + unguarded `while` rejected
        bad_heap = verifier.verify_source(
            PRESET_FLIGHT_CPP_SNIPPETS["faulty_heap_and_unbounded_loop"]["code"]
        )
        self.assertFalse(bad_heap["approved"])
        rule_ids = {v["rule_id"] for v in bad_heap["violations"]}
        self.assertIn("P10-03", rule_ids)
        self.assertIn("P10-02", rule_ids)

        # 4. Mutual call-graph recursion rejected
        bad_rec = verifier.verify_source(
            PRESET_FLIGHT_CPP_SNIPPETS["faulty_mutual_recursion_fdir"]["code"]
        )
        self.assertFalse(bad_rec["approved"])
        self.assertTrue(any(v["rule_id"] == "P10-01" for v in bad_rec["violations"]))

    def test_09_api_atrops_pareto_and_leo_diagnose_get_and_post(self) -> None:
        os.environ["ROUTELEDGER_OFFLINE_LLM"] = "1"

        # GET /api/atrops/pareto
        st_get, body_get = route_api_request("GET", "/api/atrops/pareto", {}, {})
        self.assertEqual(st_get, 200)
        self.assertGreaterEqual(len(body_get["pareto_frontier"]), 2)
        self.assertIn("cart_sourcing", body_get)
        self.assertIn("schedule_patch_validation", body_get)

        # POST /api/atrops/pareto via Lambda handler
        ev_post = {
            "rawPath": "/api/atrops/pareto",
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps(
                {
                    "disruption": "cvg3_weather_ground_stop",
                    "volume_pkgs": 4200,
                    "w_speed": 0.25,
                    "w_cost": 0.25,
                    "w_carbon": 0.50,
                    "patch_preset": "rejected_naive_llm_sort_cap_overflow",
                    "invoke_bedrock": True,
                }
            ),
        }
        resp_p = lambda_handler(ev_post)
        self.assertEqual(resp_p["statusCode"], 200)
        parsed_p = json.loads(resp_p["body"])
        self.assertEqual(
            parsed_p["schedule_patch_validation"]["verdict"], "REJECTED_BY_SOLVER_GATE"
        )

        # POST /api/leo/diagnose with custom C++ code
        st_leo, body_leo = route_api_request(
            "POST",
            "/api/leo/diagnose",
            {},
            {
                "scenario_key": "optical_isl_ber_spike",
                "custom_cpp_code": "int bad_rec(int x) { void* p = malloc(16); while(true){} return bad_rec(x-1); }",
                "invoke_bedrock": True,
            },
        )
        self.assertEqual(st_leo, 200)
        self.assertEqual(
            body_leo["mcp_diagnosis"]["predicted_root_cause"],
            "OPTICAL_ISL_FPA_JITTER_BER_SPIKE",
        )
        self.assertEqual(body_leo["flight_cpp_gate"]["verdict"], "FLIGHT_GATE_REJECTED")
        self.assertGreaterEqual(body_leo["flight_cpp_gate"]["violation_count"], 3)

        # Standalone aws_lambda_handler parity check
        s_resp = standalone_lambda_handler(
            {"rawPath": "/api/atrops/pareto", "requestContext": {"http": {"method": "GET"}}}
        )
        self.assertEqual(s_resp["statusCode"], 200)

        bm_leo = run_leo_mcp_benchmark(num_incidents=60, seed=42)
        self.assertEqual(bm_leo["mcp_suite_v2_full_5_subsystem"]["diagnostic_accuracy_pct"], 100.0)
        self.assertEqual(bm_leo["flight_cpp_power_of_10_gate"]["unsafe_violation_recall_pct"], 100.0)
        self.assertEqual(bm_leo["flight_cpp_power_of_10_gate"]["false_positive_rate_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
