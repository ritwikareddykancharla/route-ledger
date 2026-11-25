"""5-Subsystem LEO Connectivity MCP Diagnostic Registry & Embedded Zero-Heap Code Gate (`route_ledger/leo_flight_mcp.py`).

Targets Amazon Leo Networking AI Foundations (`10601502`) and Flight Software AI (`10568209`):
  1. `LeoDiagnosticMCPRegistry`:
     Exposes five schema-validated, read-only MCP diagnostic tools across LEO satellite,
     optical laser ISL, customer phased-array terminal, and ground-station subsystems:
       - `rf_link_budget`
       - `orbital_beam_handover`
       - `optical_isl_ber`
       - `terminal_modem_pll`
       - `gateway_bgp_flap`
     Supports launch-gating tool-coverage evaluation (comparing 4-tool blind-spot suite vs.
     full 5-tool registry) and grounded Amazon Bedrock (`us.amazon.nova-lite-v1:0`) RCA synthesis.
  2. `JplPowerOfTenVerifier`:
     Deterministic static source analyzer enforcing NASA/JPL Power-of-10 flight-software safety
     invariants on C/C++ RTOS telemetry handlers and device drivers:
       - `P10-03`: Zero dynamic heap allocation (`malloc`/`calloc`/`realloc`/`free`/`new`/`delete`)
       - `P10-02`: Statically bounded loops (rejects `while(true)`, `for(;;)`, and unguarded MMIO polls)
       - `P10-01`: Acyclic call graph (rejects direct and mutual function recursion + `goto`)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import random
import re
import statistics
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple


MCP_SUBSYSTEM_TOOLS: Tuple[str, ...] = (
    "rf_link_budget",
    "orbital_beam_handover",
    "optical_isl_ber",
    "terminal_modem_pll",
    "gateway_bgp_flap",
)

LEO_FAILURE_CLASSES: Tuple[str, ...] = (
    "KA_RAIN_FADE_ACM_MODCOD_DROP",
    "ORBITAL_BEAM_HANDOVER_EPHEMERIS_SLIP",
    "OPTICAL_ISL_FPA_JITTER_BER_SPIKE",
    "TERMINAL_PHASED_ARRAY_THERMAL_PLL_SLIP",
    "GATEWAY_BACKHAUL_BGP_HOLD_TIMER_FLAP",
    "COMPOUND_RAIN_FADE_AND_GATEWAY_BGP_FLAP",
)


@dataclass(frozen=True)
class MCPToolSpec:
    """Reusable LEO subsystem MCP tool definition with JSON Schema contract."""

    name: str
    subsystem: str
    owner_team: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]

    def metadata_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "subsystem": self.subsystem,
            "owner_team": self.owner_team,
            "read_only": True,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


@dataclass(frozen=True)
class MCPToolCallTrace:
    """Single audited MCP tool invocation with measured latency and anomaly flag."""

    step: int
    tool_name: str
    subsystem: str
    arguments: Dict[str, Any]
    anomaly_detected: bool
    evidence_citation: str
    output: Dict[str, Any]
    latency_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "tool_name": self.tool_name,
            "subsystem": self.subsystem,
            "arguments": dict(self.arguments),
            "anomaly_detected": self.anomaly_detected,
            "evidence_citation": self.evidence_citation,
            "output": dict(self.output),
            "latency_ms": round(self.latency_ms, 4),
        }


@dataclass(frozen=True)
class LeoOutageIncident:
    """Ground-truth LEO satellite-to-ground connectivity incident snapshot."""

    incident_id: str
    scenario_key: str
    customer_terminal_id: str
    serving_satellite_id: str
    gateway_id: str
    symptom_summary: str
    telemetry: Dict[str, Any]
    expected_root_cause: str


PRESET_LEO_INCIDENTS: Dict[str, LeoOutageIncident] = {
    "beam_handover_ephemeris_slip": LeoOutageIncident(
        incident_id="LEO-INC-401",
        scenario_key="beam_handover_ephemeris_slip",
        customer_terminal_id="UT-USW-98109-44",
        serving_satellite_id="LEO-Orb-14",
        gateway_id="Gateway-Redmond",
        symptom_summary="Periodic 1.8 s packet-loss burst every 94 seconds during LEO pass transition; clear sky at Redmond.",
        telemetry={
            "es_n0_db": 15.8,
            "qpsk_lock_floor_db": 6.5,
            "rain_rate_mm_hr": 0.4,
            "acm_modcod": "32APSK_4_5",
            "lna_bias_ma": 119.0,
            "elevation_deg": 38.5,
            "ephemeris_skew_ms": 7.9,
            "ptp_gm_locked": False,
            "beam_switch_drop_pkts": 1420,
            "handover_interval_sec": 94,
            "isl_post_fec_ber": 1.2e-11,
            "isl_uncorrected_cw": 0,
            "fpa_pointing_jitter_urad": 0.9,
            "terminal_pll_slips": 1,
            "terminal_rfic_temp_c": 58.0,
            "terminal_obstruction_pct": 0.8,
            "poe_input_volts": 53.6,
            "bgp_state": "ESTABLISHED",
            "bgp_flaps_5m": 0,
            "cgnat_pool_pct": 61.0,
        },
        expected_root_cause="ORBITAL_BEAM_HANDOVER_EPHEMERIS_SLIP",
    ),
    "optical_isl_ber_spike": LeoOutageIncident(
        incident_id="LEO-INC-402",
        scenario_key="optical_isl_ber_spike",
        customer_terminal_id="UT-EUC-60311-19",
        serving_satellite_id="LEO-Orb-22",
        gateway_id="Gateway-Frankfurt",
        symptom_summary="Trans-Atlantic RTT spiked from 24.3 ms to 69.8 ms with 4.2% TCP retransmissions on Plane-1 trunk.",
        telemetry={
            "es_n0_db": 18.4,
            "qpsk_lock_floor_db": 6.5,
            "rain_rate_mm_hr": 1.2,
            "acm_modcod": "64APSK_5_6",
            "lna_bias_ma": 118.2,
            "elevation_deg": 51.0,
            "ephemeris_skew_ms": 0.6,
            "ptp_gm_locked": True,
            "beam_switch_drop_pkts": 0,
            "handover_interval_sec": 108,
            "isl_post_fec_ber": 4.8e-4,
            "isl_uncorrected_cw": 3890,
            "fpa_pointing_jitter_urad": 9.6,
            "terminal_pll_slips": 0,
            "terminal_rfic_temp_c": 54.0,
            "terminal_obstruction_pct": 0.0,
            "poe_input_volts": 54.1,
            "bgp_state": "ESTABLISHED",
            "bgp_flaps_5m": 0,
            "cgnat_pool_pct": 58.0,
        },
        expected_root_cause="OPTICAL_ISL_FPA_JITTER_BER_SPIKE",
    ),
    "ka_rain_fade_and_bgp_flap": LeoOutageIncident(
        incident_id="LEO-INC-403",
        scenario_key="ka_rain_fade_and_bgp_flap",
        customer_terminal_id="UT-USE-20147-08",
        serving_satellite_id="LEO-Orb-18",
        gateway_id="Gateway-Ashburn",
        symptom_summary="Compound convective storm over Ashburn gateway with simultaneous core transit prefix instability.",
        telemetry={
            "es_n0_db": 4.9,
            "qpsk_lock_floor_db": 6.5,
            "rain_rate_mm_hr": 44.0,
            "acm_modcod": "QPSK_1_4_UNLOCK",
            "lna_bias_ma": 117.5,
            "elevation_deg": 42.0,
            "ephemeris_skew_ms": 0.8,
            "ptp_gm_locked": True,
            "beam_switch_drop_pkts": 12,
            "handover_interval_sec": 102,
            "isl_post_fec_ber": 2.0e-11,
            "isl_uncorrected_cw": 0,
            "fpa_pointing_jitter_urad": 1.1,
            "terminal_pll_slips": 2,
            "terminal_rfic_temp_c": 52.0,
            "terminal_obstruction_pct": 1.5,
            "poe_input_volts": 53.8,
            "bgp_state": "ACTIVE_RETRY",
            "bgp_flaps_5m": 7,
            "cgnat_pool_pct": 74.0,
        },
        expected_root_cause="COMPOUND_RAIN_FADE_AND_GATEWAY_BGP_FLAP",
    ),
    "terminal_modem_pll_thermal": LeoOutageIncident(
        incident_id="LEO-INC-404",
        scenario_key="terminal_modem_pll_thermal",
        customer_terminal_id="UT-USS-75201-73",
        serving_satellite_id="LEO-Orb-26",
        gateway_id="Gateway-Redmond",
        symptom_summary="Single enterprise rooftop terminal dropping uplink carrier lock while neighbor cells on same beam remain green.",
        telemetry={
            "es_n0_db": 16.9,
            "qpsk_lock_floor_db": 6.5,
            "rain_rate_mm_hr": 0.0,
            "acm_modcod": "32APSK_4_5",
            "lna_bias_ma": 120.1,
            "elevation_deg": 49.0,
            "ephemeris_skew_ms": 0.5,
            "ptp_gm_locked": True,
            "beam_switch_drop_pkts": 0,
            "handover_interval_sec": 110,
            "isl_post_fec_ber": 1.0e-11,
            "isl_uncorrected_cw": 0,
            "fpa_pointing_jitter_urad": 0.8,
            "terminal_pll_slips": 47,
            "terminal_rfic_temp_c": 96.4,
            "terminal_obstruction_pct": 3.0,
            "poe_input_volts": 44.2,
            "bgp_state": "ESTABLISHED",
            "bgp_flaps_5m": 0,
            "cgnat_pool_pct": 63.0,
        },
        expected_root_cause="TERMINAL_PHASED_ARRAY_THERMAL_PLL_SLIP",
    ),
    "gateway_bgp_flap_only": LeoOutageIncident(
        incident_id="LEO-INC-405",
        scenario_key="gateway_bgp_flap_only",
        customer_terminal_id="UT-EUW-00114-52",
        serving_satellite_id="LEO-Orb-31",
        gateway_id="Gateway-Dublin",
        symptom_summary="RF and ISL counters nominal across Plane-1, but all Dublin gateway subscriber sessions fail DNS/HTTPS reachability.",
        telemetry={
            "es_n0_db": 19.2,
            "qpsk_lock_floor_db": 6.5,
            "rain_rate_mm_hr": 2.1,
            "acm_modcod": "64APSK_5_6",
            "lna_bias_ma": 119.5,
            "elevation_deg": 54.0,
            "ephemeris_skew_ms": 0.4,
            "ptp_gm_locked": True,
            "beam_switch_drop_pkts": 0,
            "handover_interval_sec": 112,
            "isl_post_fec_ber": 1.5e-11,
            "isl_uncorrected_cw": 0,
            "fpa_pointing_jitter_urad": 0.7,
            "terminal_pll_slips": 0,
            "terminal_rfic_temp_c": 51.0,
            "terminal_obstruction_pct": 0.0,
            "poe_input_volts": 54.0,
            "bgp_state": "IDLE_HOLD_EXPIRED",
            "bgp_flaps_5m": 9,
            "cgnat_pool_pct": 94.5,
        },
        expected_root_cause="GATEWAY_BACKHAUL_BGP_HOLD_TIMER_FLAP",
    ),
}


class LeoDiagnosticMCPRegistry:
    """Typed 5-subsystem MCP tool registry and deterministic root-cause triage engine."""

    def __init__(self, enabled_tools: Optional[Iterable[str]] = None) -> None:
        allowed = set(enabled_tools) if enabled_tools is not None else set(MCP_SUBSYSTEM_TOOLS)
        self._all_tools = self._build_tools()
        self.enabled_tools: Tuple[str, ...] = tuple(
            t for t in MCP_SUBSYSTEM_TOOLS if t in allowed
        )

    @staticmethod
    def _build_tools() -> Dict[str, MCPToolSpec]:
        def _rf_link_budget(args: Dict[str, Any], tel: Dict[str, Any]) -> Dict[str, Any]:
            es_n0 = float(tel["es_n0_db"])
            floor_db = float(tel.get("qpsk_lock_floor_db", 6.5))
            margin_db = round(es_n0 - floor_db, 2)
            rain_mm = float(tel["rain_rate_mm_hr"])
            anomaly = margin_db < 1.5 or rain_mm >= 25.0
            return {
                "gateway_id": args.get("gateway_id"),
                "satellite_id": args.get("satellite_id"),
                "es_n0_db": es_n0,
                "link_margin_above_qpsk_db": margin_db,
                "rain_rate_mm_hr": rain_mm,
                "acm_modcod": tel["acm_modcod"],
                "lna_bias_ma": tel["lna_bias_ma"],
                "anomaly": anomaly,
                "citation": f"Es/N0={es_n0:.1f} dB (margin {margin_db:+.1f} dB), rain={rain_mm:.1f} mm/hr, modcod={tel['acm_modcod']}",
            }

        def _orbital_beam_handover(args: Dict[str, Any], tel: Dict[str, Any]) -> Dict[str, Any]:
            skew_ms = float(tel["ephemeris_skew_ms"])
            ptp_ok = bool(tel["ptp_gm_locked"])
            drops = int(tel["beam_switch_drop_pkts"])
            anomaly = skew_ms > 4.0 or (not ptp_ok) or drops >= 250
            return {
                "satellite_id": args.get("satellite_id"),
                "elevation_deg": tel["elevation_deg"],
                "ephemeris_skew_ms": skew_ms,
                "ptp_gm_locked": ptp_ok,
                "beam_switch_drop_pkts": drops,
                "handover_interval_sec": tel["handover_interval_sec"],
                "anomaly": anomaly,
                "citation": f"ephemeris_skew={skew_ms:.1f} ms, ptp_locked={ptp_ok}, handover_drops={drops} pkts",
            }

        def _optical_isl_ber(args: Dict[str, Any], tel: Dict[str, Any]) -> Dict[str, Any]:
            ber = float(tel["isl_post_fec_ber"])
            uncorr = int(tel["isl_uncorrected_cw"])
            jitter = float(tel["fpa_pointing_jitter_urad"])
            anomaly = ber >= 1e-6 or uncorr > 50 or jitter >= 5.0
            return {
                "satellite_id": args.get("satellite_id"),
                "post_fec_ber": ber,
                "uncorrected_codewords": uncorr,
                "fpa_pointing_jitter_urad": jitter,
                "anomaly": anomaly,
                "citation": f"post_fec_ber={ber:.2e}, uncorrected_cw={uncorr}, fpa_jitter={jitter:.1f} urad",
            }

        def _terminal_modem_pll(args: Dict[str, Any], tel: Dict[str, Any]) -> Dict[str, Any]:
            slips = int(tel["terminal_pll_slips"])
            temp_c = float(tel["terminal_rfic_temp_c"])
            poe_v = float(tel["poe_input_volts"])
            anomaly = slips >= 15 or temp_c >= 88.0 or poe_v < 46.0
            return {
                "terminal_id": args.get("terminal_id"),
                "carrier_pll_slips": slips,
                "rfic_die_temp_c": temp_c,
                "poe_input_volts": poe_v,
                "sky_obstruction_pct": tel["terminal_obstruction_pct"],
                "anomaly": anomaly,
                "citation": f"pll_slips={slips}, rfic_temp={temp_c:.1f}C, poe={poe_v:.1f}V",
            }

        def _gateway_bgp_flap(args: Dict[str, Any], tel: Dict[str, Any]) -> Dict[str, Any]:
            state = str(tel["bgp_state"])
            flaps = int(tel["bgp_flaps_5m"])
            anomaly = state != "ESTABLISHED" or flaps >= 3
            return {
                "gateway_id": args.get("gateway_id"),
                "bgp_session_state": state,
                "prefix_flaps_5m": flaps,
                "cgnat_pool_utilization_pct": tel["cgnat_pool_pct"],
                "anomaly": anomaly,
                "citation": f"bgp_state={state}, flaps_5m={flaps}, cgnat_util={tel['cgnat_pool_pct']}%",
            }

        return {
            "rf_link_budget": MCPToolSpec(
                name="rf_link_budget",
                subsystem="KA_RF_FEEDER_AND_USER_BEAM",
                owner_team="LEO RF Systems",
                description="Inspect Ka/Ku-band Es/N0 link margin, ACM ModCod state, LNA current, and rain radar.",
                input_schema={
                    "type": "object",
                    "required": ["gateway_id", "satellite_id"],
                    "properties": {
                        "gateway_id": {"type": "string"},
                        "satellite_id": {"type": "string"},
                    },
                },
                handler=_rf_link_budget,
            ),
            "orbital_beam_handover": MCPToolSpec(
                name="orbital_beam_handover",
                subsystem="ORBITAL_PASS_AND_BEAM_SCHEDULER",
                owner_team="LEO Constellation Timing",
                description="Inspect orbital ephemeris skew, PTP grandmaster lock, and beam-switch packet drops.",
                input_schema={
                    "type": "object",
                    "required": ["satellite_id", "terminal_id"],
                    "properties": {
                        "satellite_id": {"type": "string"},
                        "terminal_id": {"type": "string"},
                    },
                },
                handler=_orbital_beam_handover,
            ),
            "optical_isl_ber": MCPToolSpec(
                name="optical_isl_ber",
                subsystem="OPTICAL_INTER_SATELLITE_LASER_LINK",
                owner_team="LEO Optical Payload",
                description="Read 1550nm laser ISL post-FEC BER, uncorrected LDPC codewords, and FPA pointing jitter.",
                input_schema={
                    "type": "object",
                    "required": ["satellite_id"],
                    "properties": {"satellite_id": {"type": "string"}},
                },
                handler=_optical_isl_ber,
            ),
            "terminal_modem_pll": MCPToolSpec(
                name="terminal_modem_pll",
                subsystem="CUSTOMER_PHASED_ARRAY_TERMINAL",
                owner_team="LEO Customer Terminal Silicon",
                description="Query customer dish carrier PLL slips, RFIC thermal derating, PoE voltage, and sky obstruction.",
                input_schema={
                    "type": "object",
                    "required": ["terminal_id"],
                    "properties": {"terminal_id": {"type": "string"}},
                },
                handler=_terminal_modem_pll,
            ),
            "gateway_bgp_flap": MCPToolSpec(
                name="gateway_bgp_flap",
                subsystem="GROUND_GATEWAY_CORE_ROUTING",
                owner_team="LEO Core Backbone",
                description="Check ground gateway BGP peer session state, 5-minute route flaps, and CGNAT pool pressure.",
                input_schema={
                    "type": "object",
                    "required": ["gateway_id"],
                    "properties": {"gateway_id": {"type": "string"}},
                },
                handler=_gateway_bgp_flap,
            ),
        }

    def list_registered_tools(self) -> List[Dict[str, Any]]:
        return [self._all_tools[name].metadata_dict() for name in self.enabled_tools]

    def call_mcp_tool(
        self,
        step: int,
        tool_name: str,
        arguments: Dict[str, Any],
        telemetry: Dict[str, Any],
    ) -> MCPToolCallTrace:
        if tool_name not in self.enabled_tools:
            raise KeyError(f"MCP tool '{tool_name}' is not registered in active suite.")
        spec = self._all_tools[tool_name]
        for req_key in spec.input_schema.get("required", []):
            if not arguments.get(req_key):
                raise ValueError(f"MCP tool '{tool_name}' missing required field '{req_key}'.")

        t0 = time.perf_counter()
        out = spec.handler(arguments, telemetry)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return MCPToolCallTrace(
            step=step,
            tool_name=tool_name,
            subsystem=spec.subsystem,
            arguments=arguments,
            anomaly_detected=bool(out.get("anomaly")),
            evidence_citation=str(out.get("citation", "")),
            output=out,
            latency_ms=elapsed_ms,
        )

    def diagnose_incident(self, incident: LeoOutageIncident) -> Dict[str, Any]:
        """Execute enabled subsystem MCP tools, isolate root cause, and list ruled-out hypotheses."""
        t0 = time.perf_counter()
        traces: List[MCPToolCallTrace] = []
        anomalies: Dict[str, MCPToolCallTrace] = {}
        ruled_out: List[str] = []

        arg_map = {
            "rf_link_budget": {"gateway_id": incident.gateway_id, "satellite_id": incident.serving_satellite_id},
            "gateway_bgp_flap": {"gateway_id": incident.gateway_id},
            "orbital_beam_handover": {"satellite_id": incident.serving_satellite_id, "terminal_id": incident.customer_terminal_id},
            "optical_isl_ber": {"satellite_id": incident.serving_satellite_id},
            "terminal_modem_pll": {"terminal_id": incident.customer_terminal_id},
        }

        step = 0
        for tool_name in self.enabled_tools:
            step += 1
            tr = self.call_mcp_tool(step, tool_name, arg_map[tool_name], incident.telemetry)
            traces.append(tr)
            if tr.anomaly_detected:
                anomalies[tool_name] = tr
            else:
                ruled_out.append(f"{tool_name} nominal ({tr.evidence_citation})")

        # Classify root cause strictly from observed MCP subsystem evidence
        if "rf_link_budget" in anomalies and "gateway_bgp_flap" in anomalies:
            root_cause = "COMPOUND_RAIN_FADE_AND_GATEWAY_BGP_FLAP"
            remediation = (
                f"Fail over {incident.gateway_id} Ka-band feeder + BGP transit to diversity ground station "
                f"and drain flapped peer ({anomalies['gateway_bgp_flap'].output['prefix_flaps_5m']} flaps/5m)."
            )
        elif "rf_link_budget" in anomalies:
            root_cause = "KA_RAIN_FADE_ACM_MODCOD_DROP"
            remediation = (
                f"Step down ACM ModCod to QPSK 1/4 and steer {incident.gateway_id} traffic to diversity gateway."
            )
        elif "orbital_beam_handover" in anomalies:
            root_cause = "ORBITAL_BEAM_HANDOVER_EPHEMERIS_SLIP"
            remediation = (
                f"Fail over PTP grandmaster timing source on {incident.serving_satellite_id} plane controller "
                f"and push signed orbital ephemeris slot table."
            )
        elif "optical_isl_ber" in anomalies:
            root_cause = "OPTICAL_ISL_FPA_JITTER_BER_SPIKE"
            remediation = (
                f"Quarantine {incident.serving_satellite_id} optical laser ISL for FPA closed-loop recalibration "
                f"and reroute trunk onto Plane-2 diversity ISL."
            )
        elif "terminal_modem_pll" in anomalies:
            root_cause = "TERMINAL_PHASED_ARRAY_THERMAL_PLL_SLIP"
            remediation = (
                f"Apply duty-cycle thermal derate on customer terminal {incident.customer_terminal_id}, widen PLL "
                f"loop bandwidth, and flag undervolt PoE injector RMA."
            )
        elif "gateway_bgp_flap" in anomalies:
            root_cause = "GATEWAY_BACKHAUL_BGP_HOLD_TIMER_FLAP"
            remediation = (
                f"Withdraw {incident.gateway_id} ingress prefixes via route-map graceful-shutdown and shift "
                f"NAT/backhaul flows to regional peer POP."
            )
        else:
            # Blind-spot fallback when the failing subsystem's MCP tool is missing from the registry!
            # Mirrors 10601502 "A day in the life": missing orbital_beam_handover causes periodic drops
            # to be misattributed to transient Ka-band RF fade.
            root_cause = "KA_RAIN_FADE_ACM_MODCOD_DROP"
            remediation = (
                "Blind-spot fallback: no registered MCP tool surfaced an active counter breach; "
                "onboard missing subsystem tool (`orbital_beam_handover`) to close diagnostic gap."
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 4)
        return {
            "incident_id": incident.incident_id,
            "symptom_summary": incident.symptom_summary,
            "enabled_mcp_tools": list(self.enabled_tools),
            "subsystem_coverage_pct": round((len(self.enabled_tools) / len(MCP_SUBSYSTEM_TOOLS)) * 100.0, 1),
            "predicted_root_cause": root_cause,
            "expected_root_cause": incident.expected_root_cause,
            "accurate": root_cause == incident.expected_root_cause,
            "recommended_remediation": remediation,
            "tool_calls_executed": len(traces),
            "anomalous_tools": list(anomalies.keys()),
            "ruled_out_hypotheses": ruled_out,
            "mcp_traces": [tr.to_dict() for tr in traces],
            "diagnosis_time_ms": elapsed_ms,
        }


# ==============================================================================
# 2. EMBEDDED FLIGHT SOFTWARE C/C++ ZERO-HEAP & RECURSION STATIC VERIFIER
# ==============================================================================


@dataclass(frozen=True)
class StaticSafetyViolation:
    """Single NASA/JPL Power-of-10 flight-software static rule violation."""

    rule_id: str  # P10-01 | P10-02 | P10-03
    rule_name: str
    line: int
    symbol: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "line": self.line,
            "symbol": self.symbol,
            "detail": self.detail,
        }


PRESET_FLIGHT_CPP_SNIPPETS: Dict[str, Dict[str, str]] = {
    "compliant_ccsds_framer": {
        "title": "Compliant Zero-Heap CCSDS Telemetry Handler (C++20 RTOS)",
        "expected": "APPROVED",
        "code": """#include <stdint.h>
#include <stddef.h>

static constexpr uint32_t MAX_POLL_TICKS = 64U;
static constexpr size_t CCSDS_MAX_BYTES = 256U;
static uint8_t g_tx_frame_buf[CCSDS_MAX_BYTES];

uint16_t compute_crc16_ccitt(const uint8_t* data, size_t len) {
    uint16_t crc = 0xFFFFU;
    for (size_t i = 0U; i < len && i < CCSDS_MAX_BYTES; ++i) {
        crc ^= static_cast<uint16_t>(data[i]) << 8U;
        for (uint8_t bit = 0U; bit < 8U; ++bit) {
            crc = (crc & 0x8000U) ? static_cast<uint16_t>((crc << 1U) ^ 0x1021U)
                                  : static_cast<uint16_t>(crc << 1U);
        }
    }
    return crc;
}

int32_t flush_mmio_telemetry_fifo(volatile uint32_t* mmio_status, size_t payload_len) {
    if (payload_len > CCSDS_MAX_BYTES - 2U) {
        return -1;
    }
    uint32_t tick = 0U;
    while (((*mmio_status & 0x01U) != 0U) && (tick < MAX_POLL_TICKS)) {
        ++tick;
    }
    if (tick >= MAX_POLL_TICKS) {
        return -2;
    }
    uint16_t crc = compute_crc16_ccitt(g_tx_frame_buf, payload_len);
    g_tx_frame_buf[payload_len] = static_cast<uint8_t>(crc >> 8U);
    g_tx_frame_buf[payload_len + 1U] = static_cast<uint8_t>(crc & 0xFFU);
    return 0;
}
""",
    },
    "faulty_heap_and_unbounded_loop": {
        "title": "Unsafe LLM Draft: Dynamic Heap (`malloc`/`new`) + Unguarded MMIO `while`",
        "expected": "REJECTED",
        "code": """#include <stdint.h>
extern "C" { void* malloc(unsigned long); void free(void*); }

int32_t handle_star_tracker_irq(volatile uint32_t* spi_status, uint32_t pkt_bytes) {
    uint8_t* scratch = static_cast<uint8_t*>(malloc(pkt_bytes));
    uint32_t* seq_table = new uint32_t[32];

    while ((*spi_status & 0x01U) != 0U) {
        // Hardware brownout or stuck-busy bit hangs RTOS tick indefinitely!
    }

    free(scratch);
    delete[] seq_table;
    return 0;
}
""",
    },
    "faulty_mutual_recursion_fdir": {
        "title": "Unsafe LLM Draft: Mutual Call-Graph Recursion in FDIR Decoder",
        "expected": "REJECTED",
        "code": """#include <stdint.h>

int32_t recover_subframe(const uint8_t* buf, uint32_t depth);

int32_t dispatch_fdir_telemetry(const uint8_t* buf, uint32_t depth) {
    if (buf[0] == 0x7EU) {
        return recover_subframe(buf + 1, depth + 1U);
    }
    return 0;
}

int32_t recover_subframe(const uint8_t* buf, uint32_t depth) {
    if (depth < 8U) {
        return dispatch_fdir_telemetry(buf, depth + 1U);
    }
    return 1;
}
""",
    },
}


class JplPowerOfTenVerifier:
    """Deterministic static source gate enforcing zero-heap, bounded loops, and zero recursion."""

    BANNED_HEAP_PATTERNS: Tuple[Tuple[re.Pattern[str], str, str], ...] = (
        (re.compile(r"\bmalloc\s*\("), "malloc", "Dynamic C heap allocation (`malloc`) forbidden after RTOS init."),
        (re.compile(r"\bcalloc\s*\("), "calloc", "Dynamic C heap allocation (`calloc`) forbidden after RTOS init."),
        (re.compile(r"\brealloc\s*\("), "realloc", "Dynamic C heap reallocation (`realloc`) forbidden in flight code."),
        (re.compile(r"\bfree\s*\("), "free", "Dynamic C heap deallocation (`free`) forbidden in flight code."),
        (re.compile(r"\baligned_alloc\s*\("), "aligned_alloc", "Dynamic aligned heap allocation forbidden."),
        (re.compile(r"\bstrdup\s*\("), "strdup", "Heap-allocating `strdup` forbidden in flight code."),
        (re.compile(r"\bnew\s+[A-Za-z_][A-Za-z0-9_:<>]*"), "operator new", "C++ `new` heap allocation forbidden."),
        (re.compile(r"\bdelete\s*(\[\s*\])?\s*[A-Za-z_*]"), "operator delete", "C++ `delete` heap deallocation forbidden."),
        (re.compile(r"\bstd::(vector|string|map|unordered_map|shared_ptr|unique_ptr|make_shared|make_unique)\b"), "std::heap_container", "Dynamic-allocator STL container/smart-pointer forbidden."),
    )

    C_CONTROL_KEYWORDS: Set[str] = {
        "if", "for", "while", "switch", "catch", "return", "sizeof", "static_cast",
        "reinterpret_cast", "const_cast", "dynamic_cast", "alignof", "decltype",
    }

    @staticmethod
    def _strip_comments_and_strings_preserve_lines(source: str) -> str:
        """Replace C/C++ comments and string/char literals with spaces while preserving `\\n`."""
        out: List[str] = []
        i = 0
        n = len(source)
        while i < n:
            ch = source[i]
            nxt = source[i + 1] if i + 1 < n else ""
            if ch == "/" and nxt == "/":
                while i < n and source[i] != "\n":
                    out.append(" ")
                    i += 1
            elif ch == "/" and nxt == "*":
                out.append("  ")
                i += 2
                while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                    out.append("\n" if source[i] == "\n" else " ")
                    i += 1
                if i < n:
                    out.append("  ")
                    i += 2
            elif ch in ('"', "'"):
                quote = ch
                out.append(" ")
                i += 1
                while i < n and source[i] != quote:
                    if source[i] == "\\" and i + 1 < n:
                        out.append("  ")
                        i += 2
                        continue
                    out.append("\n" if source[i] == "\n" else " ")
                    i += 1
                if i < n:
                    out.append(" ")
                    i += 1
            else:
                out.append(ch)
                i += 1
        return "".join(out)

    @staticmethod
    def _line_of_offset(text: str, offset: int) -> int:
        return text.count("\n", 0, max(0, offset)) + 1

    @staticmethod
    def _extract_balanced(text: str, open_pos: int, open_ch: str, close_ch: str) -> Tuple[str, int]:
        depth = 0
        i = open_pos
        n = len(text)
        while i < n:
            if text[i] == open_ch:
                depth += 1
            elif text[i] == close_ch:
                depth -= 1
                if depth == 0:
                    return text[open_pos + 1 : i], i
            i += 1
        return text[open_pos + 1 :], n - 1

    def verify_source(self, source_code: str) -> Dict[str, Any]:
        """Run deterministic JPL Power-of-10 static checks over C/C++ telemetry handler code."""
        t0 = time.perf_counter()
        cleaned = self._strip_comments_and_strings_preserve_lines(source_code)
        violations: List[StaticSafetyViolation] = []

        # 1. Rule P10-03: Zero Dynamic Heap Allocation
        for idx, line_txt in enumerate(cleaned.splitlines(), start=1):
            for regex, sym, detail in self.BANNED_HEAP_PATTERNS:
                if regex.search(line_txt):
                    violations.append(
                        StaticSafetyViolation(
                            rule_id="P10-03",
                            rule_name="ZERO_DYNAMIC_HEAP_ALLOCATION",
                            line=idx,
                            symbol=sym,
                            detail=detail,
                        )
                    )

            # Check banned `goto` (Rule P10-01a: structured control flow)
            if re.search(r"\bgoto\s+[A-Za-z_]", line_txt):
                violations.append(
                    StaticSafetyViolation(
                        rule_id="P10-01",
                        rule_name="NO_GOTO_OR_RECURSION",
                        line=idx,
                        symbol="goto",
                        detail="Unstructured `goto` jump violates JPL Power-of-10 Rule 1.",
                    )
                )

        # 2. Rule P10-02: Statically Bounded Loops (`while` and `for`)
        for m in re.finditer(r"\b(while|for)\s*\(", cleaned):
            kw = m.group(1)
            open_paren = cleaned.find("(", m.start())
            cond, close_paren = self._extract_balanced(cleaned, open_paren, "(", ")")
            line_no = self._line_of_offset(cleaned, m.start())

            # Locate loop body if `{ ... }` follows; ignore `do { ... } while (...);` trailing semicolon
            tail = cleaned[close_paren + 1 :].lstrip()
            body = ""
            if tail.startswith("{"):
                brace_pos = cleaned.find("{", close_paren + 1)
                body, _ = self._extract_balanced(cleaned, brace_pos, "{", "}")

            cond_compact = " ".join(cond.split())
            if kw == "while":
                if cond_compact in ("1", "true", "TRUE") or cond_compact == "":
                    violations.append(
                        StaticSafetyViolation(
                            rule_id="P10-02",
                            rule_name="FIXED_LOOP_UPPER_BOUND",
                            line=line_no,
                            symbol=f"while({cond_compact})",
                            detail="Infinite `while` loop lacks a compile-time/static iteration bound.",
                        )
                    )
                else:
                    # Require explicit relational bound `<`, `<=`, `>`, `>=` on a monotonic counter variable
                    bound_match = re.search(
                        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:<|<=|>|>=)\s*([A-Za-z0-9_]+)",
                        cond_compact,
                    )
                    has_counter_step = False
                    if bound_match:
                        counter_var = bound_match.group(1)
                        step_re = re.compile(
                            rf"(\+\+\s*{re.escape(counter_var)}\b|\b{re.escape(counter_var)}\s*\+\+|\b{re.escape(counter_var)}\s*\+=|--\s*{re.escape(counter_var)}\b|\b{re.escape(counter_var)}\s*--)"
                        )
                        has_counter_step = bool(step_re.search(body) or step_re.search(cond_compact))
                    if not (bound_match and has_counter_step):
                        violations.append(
                            StaticSafetyViolation(
                                rule_id="P10-02",
                                rule_name="FIXED_LOOP_UPPER_BOUND",
                                line=line_no,
                                symbol=f"while({cond_compact[:38]})",
                                detail="Hardware/status `while` loop lacks an explicit monotonic max-tick guard.",
                            )
                        )
            elif kw == "for":
                parts = cond_compact.split(";")
                mid_cond = parts[1].strip() if len(parts) >= 2 else ""
                if not mid_cond or not re.search(r"(<|<=|>|>=)", mid_cond):
                    violations.append(
                        StaticSafetyViolation(
                            rule_id="P10-02",
                            rule_name="FIXED_LOOP_UPPER_BOUND",
                            line=line_no,
                            symbol=f"for({cond_compact[:38]})",
                            detail="`for` loop condition lacks an explicit relational upper bound.",
                        )
                    )

        # 3. Rule P10-01: Direct & Mutual Call-Graph Recursion Detection
        func_def_re = re.compile(
            r"(?:^|\n)\s*(?:[A-Za-z_][A-Za-z0-9_:<>\s\*&]*?)\b([A-Za-z_][A-Za-z0-9_]*)\s*\(([^;{}]*)\)\s*\{",
        )
        func_bodies: Dict[str, Tuple[int, str]] = {}
        pos = 0
        while True:
            fm = func_def_re.search(cleaned, pos)
            if not fm:
                break
            fname = fm.group(1)
            brace_idx = cleaned.find("{", fm.end() - 1)
            body_str, end_brace = self._extract_balanced(cleaned, brace_idx, "{", "}")
            if fname not in self.C_CONTROL_KEYWORDS:
                func_bodies[fname] = (self._line_of_offset(cleaned, fm.start(1)), body_str)
            pos = max(fm.end(), end_brace + 1)

        call_graph: Dict[str, Set[str]] = {fn: set() for fn in func_bodies}
        call_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        for fn, (_, fbody) in func_bodies.items():
            for cm in call_re.finditer(fbody):
                callee = cm.group(1)
                if callee in func_bodies and callee not in self.C_CONTROL_KEYWORDS:
                    call_graph[fn].add(callee)

        # Detect all distinct elementary cycles in `call_graph`
        reported_cycles: Set[Tuple[str, ...]] = set()
        for start_fn in sorted(call_graph):
            stack: List[Tuple[str, Tuple[str, ...]]] = [(start_fn, (start_fn,))]
            while stack:
                curr, path = stack.pop()
                for nxt in sorted(call_graph.get(curr, ())):
                    if nxt == start_fn:
                        cycle_ring = path + (start_fn,)
                        # Canonicalize rotation to avoid duplicate cycle reports
                        ring_nodes = path
                        min_idx = min(range(len(ring_nodes)), key=lambda idx: ring_nodes[idx])
                        canon = ring_nodes[min_idx:] + ring_nodes[:min_idx]
                        if canon not in reported_cycles:
                            reported_cycles.add(canon)
                            kind = "Direct recursion" if len(canon) == 1 else "Mutual call-graph recursion"
                            violations.append(
                                StaticSafetyViolation(
                                    rule_id="P10-01",
                                    rule_name="ACYCLIC_CALL_GRAPH_NO_RECURSION",
                                    line=func_bodies[canon[0]][0],
                                    symbol=" -> ".join(cycle_ring),
                                    detail=f"{kind} cycle detected: {' -> '.join(cycle_ring)}.",
                                )
                            )
                    elif nxt not in path:
                        stack.append((nxt, path + (nxt,)))

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 4)
        approved = len(violations) == 0
        return {
            "verdict": "FLIGHT_GATE_PASSED" if approved else "FLIGHT_GATE_REJECTED",
            "approved": approved,
            "functions_analyzed": sorted(func_bodies.keys()),
            "call_graph": {k: sorted(v) for k, v in call_graph.items()},
            "violation_count": len(violations),
            "violations": [v.to_dict() for v in violations],
            "rules_checked": [
                "P10-01: Zero Direct/Mutual Call-Graph Recursion & No goto",
                "P10-02: Statically Bounded Loops (Explicit Monotonic Upper Bound)",
                "P10-03: Zero Dynamic Heap Allocation (No malloc/free/new/delete)",
            ],
            "analysis_time_ms": elapsed_ms,
        }


# ==============================================================================
# 3. UNIFIED LEO MCP + FLIGHT CODE GATE ENTRYPOINT & BENCHMARK HARNESS
# ==============================================================================


def run_leo_diagnose_workbench(
    scenario_key: str = "beam_handover_ephemeris_slip",
    enabled_tools: Optional[Sequence[str]] = None,
    cpp_preset: str = "compliant_ccsds_framer",
    custom_cpp_code: Optional[str] = None,
    bedrock_callable: Optional[Callable[[str, str, int], Optional[Dict[str, Any]]]] = None,
    invoke_bedrock: bool = False,
) -> Dict[str, Any]:
    """Run 5-subsystem LEO MCP triage + JPL Power-of-10 flight C/C++ static verification."""
    incident = PRESET_LEO_INCIDENTS.get(
        scenario_key, PRESET_LEO_INCIDENTS["beam_handover_ephemeris_slip"]
    )
    registry = LeoDiagnosticMCPRegistry(enabled_tools=enabled_tools)
    diagnosis = registry.diagnose_incident(incident)

    snippet_meta = PRESET_FLIGHT_CPP_SNIPPETS.get(
        cpp_preset, PRESET_FLIGHT_CPP_SNIPPETS["compliant_ccsds_framer"]
    )
    code_to_verify = custom_cpp_code if (custom_cpp_code and custom_cpp_code.strip()) else snippet_meta["code"]
    verifier = JplPowerOfTenVerifier()
    code_gate = verifier.verify_source(code_to_verify)

    bedrock_rca: Optional[Dict[str, Any]] = None
    if invoke_bedrock and bedrock_callable is not None:
        sys_p = (
            "You are an Amazon Leo Networking & Flight Software AI Staff Engineer. "
            "Given verified MCP subsystem tool telemetry and the JPL Power-of-10 C++ static gate output, "
            "provide a concise 3-bullet root-cause & flight-safety summary citing exact dB/BER/ms metrics "
            "and rule IDs (<125 words)."
        )
        usr_p = (
            f"Incident: {incident.incident_id} ({incident.symptom_summary})\n"
            f"MCP Root Cause: {diagnosis['predicted_root_cause']}\n"
            f"Anomalous Tools: {diagnosis['anomalous_tools']}\n"
            f"Citations: {[t['evidence_citation'] for t in diagnosis['mcp_traces']]}\n"
            f"C++ Gate Verdict: {code_gate['verdict']} (violations={code_gate['violations']})\n"
        )
        bedrock_rca = bedrock_callable(sys_p, usr_p, 240)

    if bedrock_rca is None:
        bedrock_rca = {
            "engine": "deterministic-leo-mcp-synthesizer",
            "latency_ms": diagnosis["diagnosis_time_ms"],
            "tokens": 0,
            "text": (
                f"• MCP Subsystem Root Cause ({diagnosis['predicted_root_cause']}): Isolated across "
                f"{diagnosis['tool_calls_executed']} read-only MCP tool calls "
                f"({diagnosis['subsystem_coverage_pct']}% constellation coverage).\n"
                f"• Grounded Remediation: {diagnosis['recommended_remediation']}\n"
                f"• Flight C/C++ Static Gate ({code_gate['verdict']}): Analyzed {len(code_gate['functions_analyzed'])} "
                f"RTOS function(s) in {code_gate['analysis_time_ms']} ms with {code_gate['violation_count']} "
                f"Power-of-10 violation(s)."
            ),
        }

    return {
        "service": "route-ledger",
        "subsystem": "leo_flight_mcp",
        "registered_tools": registry.list_registered_tools(),
        "incident": {
            "incident_id": incident.incident_id,
            "scenario_key": incident.scenario_key,
            "customer_terminal_id": incident.customer_terminal_id,
            "serving_satellite_id": incident.serving_satellite_id,
            "gateway_id": incident.gateway_id,
            "symptom_summary": incident.symptom_summary,
        },
        "mcp_diagnosis": diagnosis,
        "flight_cpp_gate": {
            "preset": cpp_preset,
            "title": "Custom C/C++ Telemetry Handler" if custom_cpp_code else snippet_meta["title"],
            "source_code": code_to_verify,
            **code_gate,
        },
        "bedrock_advisory": bedrock_rca,
    }


def run_leo_mcp_benchmark(
    num_incidents: int = 120,
    seed: int = 42,
) -> Dict[str, Any]:
    """Benchmark 5-subsystem LEO MCP launch-gate accuracy/coverage + Power-of-10 C++ verifier."""
    rng = random.Random(seed)
    base_scenarios = list(PRESET_LEO_INCIDENTS.values())
    # Add pure Ka-band rain-fade scenario so all 6 failure classes are balanced (20 each in 120)
    rain_only = LeoOutageIncident(
        incident_id="LEO-INC-406",
        scenario_key="ka_rain_fade_only",
        customer_terminal_id="UT-USW-98052-11",
        serving_satellite_id="LEO-Orb-14",
        gateway_id="Gateway-Redmond",
        symptom_summary="Heavy Pacific squall attenuating Ka-band feeder below QPSK threshold while BGP stays up.",
        telemetry={
            **PRESET_LEO_INCIDENTS["ka_rain_fade_and_bgp_flap"].telemetry,
            "bgp_state": "ESTABLISHED",
            "bgp_flaps_5m": 0,
        },
        expected_root_cause="KA_RAIN_FADE_ACM_MODCOD_DROP",
    )
    templates = base_scenarios + [rain_only]

    workload: List[LeoOutageIncident] = []
    for i in range(num_incidents):
        tmpl = templates[i % len(templates)]
        tel = dict(tmpl.telemetry)
        # Deterministic micro-jitter keeping physical thresholds intact
        tel["es_n0_db"] = round(float(tel["es_n0_db"]) + rng.uniform(-0.25, 0.25), 2)
        workload.append(
            LeoOutageIncident(
                incident_id=f"BM-LEO-{i + 1:04d}",
                scenario_key=tmpl.scenario_key,
                customer_terminal_id=tmpl.customer_terminal_id,
                serving_satellite_id=tmpl.serving_satellite_id,
                gateway_id=tmpl.gateway_id,
                symptom_summary=tmpl.symptom_summary,
                telemetry=tel,
                expected_root_cause=tmpl.expected_root_cause,
            )
        )

    # Suite v1: 4 tools (missing `orbital_beam_handover`) -> fails launch gate on beam-handover class
    suite_v1 = LeoDiagnosticMCPRegistry(
        enabled_tools=("rf_link_budget", "optical_isl_ber", "terminal_modem_pll", "gateway_bgp_flap")
    )
    # Suite v2: Full 5-subsystem MCP tool registry
    suite_v2 = LeoDiagnosticMCPRegistry(enabled_tools=MCP_SUBSYSTEM_TOOLS)

    v1_correct = 0
    v2_correct = 0
    v2_times: List[float] = []
    v2_calls: List[int] = []

    for inc in workload:
        r1 = suite_v1.diagnose_incident(inc)
        if r1["accurate"]:
            v1_correct += 1
        r2 = suite_v2.diagnose_incident(inc)
        if r2["accurate"]:
            v2_correct += 1
        v2_times.append(r2["diagnosis_time_ms"])
        v2_calls.append(r2["tool_calls_executed"])

    # Flight C/C++ Power-of-10 Static Verifier Corpus (40 handlers: 20 compliant, 20 unsafe)
    verifier = JplPowerOfTenVerifier()
    compliant_passed = 0
    unsafe_blocked = 0
    gate_times: List[float] = []

    base_ok = PRESET_FLIGHT_CPP_SNIPPETS["compliant_ccsds_framer"]["code"]
    unsafe_templates = [
        PRESET_FLIGHT_CPP_SNIPPETS["faulty_heap_and_unbounded_loop"]["code"],
        PRESET_FLIGHT_CPP_SNIPPETS["faulty_mutual_recursion_fdir"]["code"],
        "void poll_imu(volatile int* r) { while (true) { if (*r == 99) break; } }",
        "int self_rec(int n) { return n <= 0 ? 0 : self_rec(n - 1); }",
        "void alloc_pkt(unsigned n) { char* p = (char*)calloc(n, 1); free(p); }",
    ]

    for k in range(20):
        ok_variant = base_ok.replace("MAX_POLL_TICKS = 64U", f"MAX_POLL_TICKS = {32 + k * 4}U")
        res_ok = verifier.verify_source(ok_variant)
        gate_times.append(res_ok["analysis_time_ms"])
        if res_ok["approved"]:
            compliant_passed += 1

        bad_src = unsafe_templates[k % len(unsafe_templates)]
        res_bad = verifier.verify_source(bad_src)
        gate_times.append(res_bad["analysis_time_ms"])
        if not res_bad["approved"]:
            unsafe_blocked += 1

    v1_acc = round((v1_correct / num_incidents) * 100.0, 2)
    v2_acc = round((v2_correct / num_incidents) * 100.0, 2)

    return {
        "workload": {
            "incident_count": num_incidents,
            "failure_classes": list(LEO_FAILURE_CLASSES),
            "mcp_tools_total": len(MCP_SUBSYSTEM_TOOLS),
            "flight_cpp_handlers_tested": 40,
        },
        "mcp_suite_v1_missing_handover_tool": {
            "enabled_tool_count": 4,
            "subsystem_coverage_pct": 80.0,
            "diagnostic_accuracy_pct": v1_acc,
            "correct_diagnoses": v1_correct,
            "launch_gate_passed": False,
        },
        "mcp_suite_v2_full_5_subsystem": {
            "enabled_tool_count": 5,
            "subsystem_coverage_pct": 100.0,
            "diagnostic_accuracy_pct": v2_acc,
            "correct_diagnoses": v2_correct,
            "mean_tool_calls": round(statistics.mean(v2_calls), 2),
            "mean_diagnosis_ms": round(statistics.mean(v2_times), 4),
            "launch_gate_passed": v2_acc >= 95.0,
        },
        "flight_cpp_power_of_10_gate": {
            "compliant_handlers_approved": compliant_passed,
            "compliant_handlers_total": 20,
            "false_positive_rate_pct": round(((20 - compliant_passed) / 20) * 100.0, 2),
            "unsafe_handlers_rejected": unsafe_blocked,
            "unsafe_handlers_total": 20,
            "unsafe_violation_recall_pct": round((unsafe_blocked / 20) * 100.0, 2),
            "mean_static_check_ms": round(statistics.mean(gate_times), 4),
        },
    }
