"""Unit tests for RouteLedger `/api/*` CloudFront Origin (`route_ledger/web_api.py`)."""

from __future__ import annotations

import json
import os
import unittest

from route_ledger.web_api import lambda_handler, route_api_request


class TestRouteLedgerWebApi(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["ROUTELEDGER_OFFLINE_LLM"] = "1"
        route_api_request("POST", "/api/reset", {}, {})

    def test_01_health_and_initial_state(self) -> None:
        status, health = route_api_request("GET", "/api/health", {}, {})
        self.assertEqual(status, 200)
        self.assertTrue(health["ledger_trial_balance_ok"])

        status_s, state = route_api_request("GET", "/api/state", {"mode": "scot"}, {})
        self.assertEqual(status_s, 200)
        self.assertEqual(state["topology"]["mode"], "scot")
        self.assertTrue(state["ledger"]["trial_balance"]["is_balanced"])

    def test_02_scot_ord5_blizzard_reroutes_via_slc2_cvg3_under_2ms_zero_breaches(self) -> None:
        status, res = route_api_request(
            "POST",
            "/api/topology/optimize",
            {},
            {"mode": "scot", "demand_units": 3200, "fault_mode": "ord5_blizzard", "auto_settle": True},
        )
        self.assertEqual(status, 200)
        opt = res["optimizer_summary"]
        self.assertEqual(opt["sla_breaches_units"], 0)
        self.assertEqual(opt["cpt_sla_pct"], 100.0)
        self.assertEqual(opt["active_corridors"][0]["route_label"], "SEA1 -> SLC2 -> CVG3 -> EWR7")
        self.assertEqual(res["static_baseline_comparison"]["cpt_sla_pct"], 0.0)

        # High volume splits across 2 parallel corridors during ORD5 blizzard
        status2, split_res = route_api_request(
            "POST",
            "/api/topology/optimize",
            {},
            {"mode": "scot", "demand_units": 5800, "fault_mode": "ord5_blizzard"},
        )
        self.assertEqual(status2, 200)
        self.assertGreaterEqual(len(split_res["optimizer_summary"]["active_corridors"]), 2)
        self.assertEqual(split_res["optimizer_summary"]["cpt_sla_pct"], 100.0)

    def test_03_leo_rain_fade_and_optical_flap_reroutes_cleanly(self) -> None:
        event = {
            "rawPath": "/api/topology/optimize",
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps(
                {"mode": "leo", "demand_gbps": 28.0, "fault_mode": "rain_fade_and_isl_flap", "invoke_bedrock": True}
            ),
        }
        raw = lambda_handler(event)
        self.assertEqual(raw["statusCode"], 200)
        data = json.loads(raw["body"])
        self.assertTrue(data["optimizer_summary"]["routed"])
        self.assertEqual(data["optimizer_summary"]["dropped_gbps"], 0.0)
        self.assertFalse(data["optimizer_summary"]["sla_violated"])
        self.assertGreater(data["static_baseline_comparison"]["dropped_gbps"], 0.0)
        self.assertIn("LEO-Orb-18", data["optimizer_summary"]["subflows"][0]["route_label"])

    def test_04_sqs_5x_retry_storm_and_payload_tamper_conflict(self) -> None:
        status, storm = route_api_request("POST", "/api/ledger/retry-storm", {}, {})
        self.assertEqual(status, 200)
        sr = storm["storm_result"]
        self.assertEqual(sr["sqs_deliveries_received"], 60)
        self.assertEqual(sr["unique_events_settled"], 12)
        self.assertEqual(sr["duplicate_retries_blocked"], 48)
        self.assertEqual(sr["routeledger_duplicate_charges"], 0)
        self.assertGreater(sr["naive_processor_duplicate_charges"], 0)
        self.assertTrue(sr["trial_balance_conserved"])
        self.assertEqual(sr["imbalance_cents"], 0)

        status_t, tamper = route_api_request("POST", "/api/ledger/tamper-test", {}, {})
        self.assertEqual(status_t, 200)
        self.assertTrue(tamper["conflict_blocked"])
        self.assertTrue(tamper["trial_balance_still_balanced"])


if __name__ == "__main__":
    unittest.main()
