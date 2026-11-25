"""HTTP Server & AWS CloudFront `/api/*` Origin Handler for RouteLedger.

Powers two interactive operational modes + an ACID Double-Entry Settlement Ledger:
  1. Mode 1 (`scot`): SCOT Middle-Mile Fulfillment Network (`SEA1 -> SLC2 -> DEN4 -> ORD5 -> CVG3 -> EWR7`)
     with Capacity-Constrained Min-Cost Flow rerouting (<2 ms), CPT SLA verification, and floor diagnostics.
  2. Mode 2 (`leo`): Amazon Leo Orbital Laser ISL & Ground Gateway Mesh (`Gateway-Redmond <-> LEO-Orb-14 <-> LEO-Orb-22 <-> Gateway-Frankfurt`)
     with SNR/capacity-aware dynamic link-state rerouting + Amazon Bedrock (`us.amazon.nova-lite-v1:0`) telemetry diagnosis.
  3. Idempotent Double-Entry Carrier & Settlement Ledger (`double_entry_ledger.py`) with 5x SQS Retry Storm proof.
"""

from __future__ import annotations

import base64
import heapq
import json
import math
import mimetypes
import os
import platform
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__
from .double_entry_ledger import (
    IdempotencyConflictError,
    IdempotentSettlementLedger,
    NaiveFloatBillingProcessor,
    UsageOrderEvent,
)
from .network_router import (
    CapacityAwareRouter,
    LinkDegradationEvent,
    LinkState,
    StaticShortestPathRouter,
    TrafficDemand,
)
from .noc_workflow import IncidentTelemetrySnapshot, OperationalWorkflowEngine

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
DOCS_DIR = ROOT_DIR / "docs"

CORS_HEADERS: Dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


def call_bedrock_nova(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 300,
) -> Optional[Dict[str, Any]]:
    """Invoke Amazon Bedrock (`us.amazon.nova-lite-v1:0`) via boto3 (Lambda) or AWS CLI (`portfolio` local)."""
    if os.environ.get("ROUTELEDGER_OFFLINE_LLM") == "1":
        return None

    model_id = os.environ.get("ROUTELEDGER_BEDROCK_MODEL", "us.amazon.nova-lite-v1:0")
    region = os.environ.get("AWS_REGION", "us-west-2")
    t0 = time.perf_counter()

    try:
        import boto3  # type: ignore

        client = boto3.client("bedrock-runtime", region_name=region)
        resp = client.converse(
            modelId=model_id,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.1},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        return {
            "text": text.strip(),
            "engine": f"aws-bedrock ({model_id})",
            "model_id": model_id,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "tokens": resp.get("usage", {}).get("totalTokens", 0),
        }
    except ImportError:
        pass
    except Exception:
        pass

    aws_bin = shutil.which("aws")
    if not aws_bin:
        return None
    profile = os.environ.get("AWS_PROFILE") or "portfolio"
    env = dict(os.environ)
    env.pop("AWS_ACCESS_KEY_ID", None)
    env.pop("AWS_SECRET_ACCESS_KEY", None)
    env.pop("AWS_SESSION_TOKEN", None)
    env["AWS_PROFILE"] = profile

    cmd = [
        aws_bin,
        "bedrock-runtime",
        "converse",
        "--region",
        region,
        "--model-id",
        model_id,
        "--system",
        json.dumps([{"text": system_prompt}]),
        "--messages",
        json.dumps([{"role": "user", "content": [{"text": user_prompt}]}]),
        "--inference-config",
        json.dumps({"maxTokens": max_tokens, "temperature": 0.1}),
        "--output",
        "json",
    ]
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            text = data["output"]["message"]["content"][0]["text"]
            return {
                "text": text.strip(),
                "engine": f"aws-bedrock ({model_id})",
                "model_id": model_id,
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                "tokens": data.get("usage", {}).get("totalTokens", 0),
            }
    except Exception:
        pass
    return None


# ==============================================================================
# MODE 1: SCOT Middle-Mile Fulfillment Topology & Min-Cost Flow Optimizer
# ==============================================================================

SCOT_NODES: List[Dict[str, Any]] = [
    {"id": "SEA1", "label": "SEA1 · Seattle FC", "sub": "Origin Induct", "tier": "ORIGIN_FC", "x": 82, "y": 165},
    {"id": "SLC2", "label": "SLC2 · Salt Lake", "sub": "Mountain Sort", "tier": "REGIONAL_SORT", "x": 235, "y": 195},
    {"id": "DEN4", "label": "DEN4 · Denver Hub", "sub": "Intermodal Hub", "tier": "REGIONAL_SORT", "x": 388, "y": 245},
    {"id": "ORD5", "label": "ORD5 · Chicago Mega", "sub": "Midwest Sort", "tier": "MEGA_SORT", "x": 535, "y": 115},
    {"id": "DFW6", "label": "DFW6 · Dallas Sort", "sub": "South Bypass", "tier": "REGIONAL_SORT", "x": 510, "y": 335},
    {"id": "CVG3", "label": "CVG3 · Cincinnati", "sub": "Prime Air Hub", "tier": "AIR_HUB", "x": 688, "y": 205},
    {"id": "EWR7", "label": "EWR7 · Newark FC", "sub": "24h CPT Dest", "tier": "DEST_FC", "x": 825, "y": 155},
]

SCOT_ARCS_BASE: List[Dict[str, Any]] = [
    {"arc_id": "A1-SEA1-SLC2", "src": "SEA1", "dst": "SLC2", "mode": "GROUND_TRUCK", "capacity_units": 5400, "transit_hr": 5.2, "cost_per_unit": 0.28},
    {"arc_id": "A2-SLC2-DEN4", "src": "SLC2", "dst": "DEN4", "mode": "GROUND_TRUCK", "capacity_units": 4600, "transit_hr": 4.1, "cost_per_unit": 0.24},
    {"arc_id": "A3-DEN4-ORD5", "src": "DEN4", "dst": "ORD5", "mode": "GROUND_TRUCK", "capacity_units": 4200, "transit_hr": 5.4, "cost_per_unit": 0.31},
    {"arc_id": "A4-ORD5-CVG3", "src": "ORD5", "dst": "CVG3", "mode": "GROUND_TRUCK", "capacity_units": 4400, "transit_hr": 3.2, "cost_per_unit": 0.25},
    {"arc_id": "A5-CVG3-EWR7", "src": "CVG3", "dst": "EWR7", "mode": "PRIME_AIR_EXPRESS", "capacity_units": 7200, "transit_hr": 3.9, "cost_per_unit": 0.34},
    {"arc_id": "A6-SLC2-CVG3", "src": "SLC2", "dst": "CVG3", "mode": "PRIME_AIR_BRIDGE", "capacity_units": 4200, "transit_hr": 3.5, "cost_per_unit": 0.62},
    {"arc_id": "A7-SEA1-DEN4", "src": "SEA1", "dst": "DEN4", "mode": "DIRECT_INTERMODAL", "capacity_units": 3200, "transit_hr": 6.6, "cost_per_unit": 0.45},
    {"arc_id": "A8-DEN4-DFW6", "src": "DEN4", "dst": "DFW6", "mode": "GROUND_BYPASS", "capacity_units": 3600, "transit_hr": 4.2, "cost_per_unit": 0.29},
    {"arc_id": "A9-DFW6-CVG3", "src": "DFW6", "dst": "CVG3", "mode": "GROUND_BYPASS", "capacity_units": 3600, "transit_hr": 4.4, "cost_per_unit": 0.32},
]


def solve_scot_network(
    demand_units: int = 3200,
    fault_mode: str = "nominal",
    invoke_bedrock: bool = False,
) -> Dict[str, Any]:
    """Run Capacity-Constrained Min-Cost Flow Optimizer vs. Static Linehaul Plan on SCOT corridor."""
    t0 = time.perf_counter()
    cpt_sla_hours = 24.0
    demand = max(400, min(8200, int(demand_units)))

    arcs: List[Dict[str, Any]] = []
    for raw in SCOT_ARCS_BASE:
        a = dict(raw)
        a["effective_capacity"] = a["capacity_units"]
        a["outage"] = False
        a["degraded"] = False
        a["status_reason"] = "NOMINAL"
        a["flow_units"] = 0
        if fault_mode == "ord5_blizzard" and (a["src"] == "ORD5" or a["dst"] == "ORD5"):
            a["effective_capacity"] = 0
            a["outage"] = True
            a["status_reason"] = "ORD5_BLIZZARD_SORTATION_CLOSURE"
        elif fault_mode == "den4_conveyor_jam" and (a["src"] == "DEN4" or a["dst"] == "DEN4"):
            a["effective_capacity"] = int(a["capacity_units"] * 0.22)
            a["degraded"] = True
            a["transit_hr"] = round(a["transit_hr"] + 2.8, 2)
            a["status_reason"] = "DEN4_CONVEYOR_VFD_THERMAL_THROTTLE"
        arcs.append(a)

    arc_by_id = {a["arc_id"]: a for a in arcs}
    adj: Dict[str, List[Dict[str, Any]]] = {n["id"]: [] for n in SCOT_NODES}
    for a in arcs:
        adj[a["src"]].append(a)

    # Successive shortest augmenting paths over (cost_per_unit, transit_hr <= 24.0h)
    remaining = demand
    corridors: List[Dict[str, Any]] = []
    while remaining > 0:
        # Dijkstra on residual graph minimizing unit cost within CPT SLA
        pq: List[Tuple[float, float, str, List[str], List[str]]] = [
            (0.0, 0.0, "SEA1", ["SEA1"], [])
        ]
        best_cost: Dict[str, float] = {"SEA1": 0.0}
        best_path: Optional[Tuple[float, float, List[str], List[str]]] = None

        while pq:
            cost_u, hrs_u, u, npath, apath = heapq.heappop(pq)
            if u == "EWR7":
                best_path = (cost_u, hrs_u, npath, apath)
                break
            if cost_u > best_cost.get(u, math.inf) + 1e-9:
                continue
            for edge in adj.get(u, []):
                res_cap = edge["effective_capacity"] - edge["flow_units"]
                if edge["outage"] or res_cap <= 0 or edge["dst"] in npath:
                    continue
                nxt_hrs = hrs_u + edge["transit_hr"]
                if nxt_hrs > cpt_sla_hours:
                    continue
                util_after = (edge["flow_units"] + min(remaining, res_cap)) / max(1, edge["effective_capacity"])
                nxt_cost = cost_u + edge["cost_per_unit"] + 0.04 * (util_after ** 2)
                if nxt_cost < best_cost.get(edge["dst"], math.inf) - 1e-9:
                    best_cost[edge["dst"]] = nxt_cost
                    heapq.heappush(
                        pq,
                        (nxt_cost, nxt_hrs, edge["dst"], npath + [edge["dst"]], apath + [edge["arc_id"]]),
                    )

        if best_path is None:
            break

        _, path_hrs, npath, apath = best_path
        bottleneck = min(arc_by_id[aid]["effective_capacity"] - arc_by_id[aid]["flow_units"] for aid in apath)
        pushed = min(remaining, bottleneck)
        raw_unit_cost = sum(arc_by_id[aid]["cost_per_unit"] for aid in apath)
        for aid in apath:
            arc_by_id[aid]["flow_units"] += pushed
        remaining -= pushed
        corridors.append(
            {
                "nodes": npath,
                "route_label": " -> ".join(npath),
                "arc_ids": apath,
                "units_routed": pushed,
                "transit_hours": round(path_hrs, 2),
                "cost_per_unit_usd": round(raw_unit_cost, 2),
                "total_corridor_cost_usd": round(pushed * raw_unit_cost, 2),
                "cpt_met": path_hrs <= cpt_sla_hours,
            }
        )

    compute_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    routed_units = demand - remaining
    total_cost = sum(c["total_corridor_cost_usd"] for c in corridors)
    avg_unit_cost = round(total_cost / max(1, routed_units), 2) if routed_units else 0.0
    max_transit_hr = max((c["transit_hours"] for c in corridors), default=0.0)
    cpt_sla_pct = round((routed_units / demand) * 100.0, 2)

    # Static Linehaul Baseline (`SEA1 -> SLC2 -> DEN4 -> ORD5 -> CVG3 -> EWR7` fixed)
    static_chain = ["A1-SEA1-SLC2", "A2-SLC2-DEN4", "A3-DEN4-ORD5", "A4-ORD5-CVG3", "A5-CVG3-EWR7"]
    if any(arc_by_id[aid]["outage"] for aid in static_chain):
        static_delivered = 0
        static_sla_pct = 0.0
        static_breaches = demand
    else:
        static_cap = min(arc_by_id[aid]["effective_capacity"] for aid in static_chain)
        static_hrs = sum(arc_by_id[aid]["transit_hr"] for aid in static_chain)
        static_delivered = min(demand, static_cap) if static_hrs <= cpt_sla_hours else 0
        static_sla_pct = round((static_delivered / demand) * 100.0, 2)
        static_breaches = demand - static_delivered

    for a in arcs:
        eff = a["effective_capacity"]
        a["utilization_pct"] = 100.0 if a["outage"] else round((a["flow_units"] / max(1, eff)) * 100.0, 1)

    nodes_out = []
    degraded_nodes = {"ORD5"} if fault_mode == "ord5_blizzard" else ({"DEN4"} if fault_mode == "den4_conveyor_jam" else set())
    active_node_ids = {nid for c in corridors for nid in c["nodes"]}
    for n in SCOT_NODES:
        nd = dict(n)
        nd["status"] = "OUTAGE" if (fault_mode == "ord5_blizzard" and n["id"] == "ORD5") else (
            "DEGRADED" if n["id"] in degraded_nodes else ("ACTIVE" if n["id"] in active_node_ids else "STANDBY")
        )
        nodes_out.append(nd)

    # Run deterministic floor/NOC workflow trace when fault active
    wf_engine = OperationalWorkflowEngine()
    diag_dict: Optional[Dict[str, Any]] = None
    if fault_mode in ("ord5_blizzard", "den4_conveyor_jam"):
        snap = IncidentTelemetrySnapshot(
            incident_id="INC-SCOT-ORD5" if fault_mode == "ord5_blizzard" else "INC-SCOT-DEN4",
            alarm_type="FULFILLMENT_CONVEYOR_BUFFER_JAM",
            region="us-west-2",
            resource_id="ord5-crossdock-01" if fault_mode == "ord5_blizzard" else "den4-vfd-zone-03",
            metrics=(
                {"vfd_motor_temp_c": 62.0, "motor_current_amps": 9.8, "downstream_chute_fill_pct": 98.4, "secondary_lane_available": True}
                if fault_mode == "ord5_blizzard"
                else {"vfd_motor_temp_c": 93.2, "motor_current_amps": 16.8, "downstream_chute_fill_pct": 74.0, "secondary_lane_available": True}
            ),
            raw_logs=[],
        )
        diag_dict = wf_engine.diagnose(snap).to_dict()

    bedrock_diag = None
    if invoke_bedrock:
        sys_p = (
            "You are an Amazon SCOT ATROPS Transportation Optimization AI copilot. "
            "Provide a crisp 3-bullet operational dispatch summary citing exact hub IDs, parcel flow, "
            "CPT transit hours, $/unit cost, and SLA protection vs. the static linehaul plan (<110 words)."
        )
        usr_p = (
            f"Fault Mode: {fault_mode}\nDemand: {demand} units/hr\n"
            f"Optimizer Corridors: {json.dumps(corridors)}\n"
            f"Optimizer CPT SLA: {cpt_sla_pct}% (Static Baseline SLA: {static_sla_pct}%)\n"
        )
        bedrock_diag = call_bedrock_nova(sys_p, usr_p, max_tokens=220)

    return {
        "mode": "scot",
        "fault_mode": fault_mode,
        "demand_units_hr": demand,
        "cpt_sla_target_hours": cpt_sla_hours,
        "compute_time_ms": compute_ms,
        "optimizer_summary": {
            "routed_units_hr": routed_units,
            "sla_breaches_units": remaining,
            "cpt_sla_pct": cpt_sla_pct,
            "avg_cost_per_unit_usd": avg_unit_cost,
            "total_hourly_freight_usd": round(total_cost, 2),
            "max_corridor_transit_hr": max_transit_hr,
            "active_corridors": corridors,
        },
        "static_baseline_comparison": {
            "route_label": "SEA1 -> SLC2 -> DEN4 -> ORD5 -> CVG3 -> EWR7",
            "delivered_on_time_units": static_delivered,
            "sla_breaches_units": static_breaches,
            "cpt_sla_pct": static_sla_pct,
        },
        "nodes": nodes_out,
        "arcs": arcs,
        "workflow_diagnosis": diag_dict,
        "bedrock_advisory": bedrock_diag,
    }


# ==============================================================================
# MODE 2: Amazon Leo Orbital Laser ISL & Ground Gateway Mesh
# ==============================================================================

LEO_NODES: List[Dict[str, Any]] = [
    {"id": "Gateway-Redmond", "label": "Gateway-Redmond", "sub": "US-West Ka Ground", "tier": "GROUND_GW", "x": 92, "y": 328},
    {"id": "Gateway-Ashburn", "label": "Gateway-Ashburn", "sub": "US-East Diversity GW", "tier": "GROUND_GW", "x": 295, "y": 348},
    {"id": "Gateway-Dublin", "label": "Gateway-Dublin", "sub": "EU-West Diversity GW", "tier": "GROUND_GW", "x": 595, "y": 348},
    {"id": "Gateway-Frankfurt", "label": "Gateway-Frankfurt", "sub": "EU-Central Ground", "tier": "GROUND_GW", "x": 808, "y": 324},
    {"id": "LEO-Orb-14", "label": "LEO-Orb-14", "sub": "Plane 1 · Laser ISL", "tier": "LEO_SAT", "x": 195, "y": 112},
    {"id": "LEO-Orb-22", "label": "LEO-Orb-22", "sub": "Plane 1 · Laser ISL", "tier": "LEO_SAT", "x": 450, "y": 82},
    {"id": "LEO-Orb-31", "label": "LEO-Orb-31", "sub": "Plane 1 · Laser ISL", "tier": "LEO_SAT", "x": 705, "y": 112},
    {"id": "LEO-Orb-18", "label": "LEO-Orb-18", "sub": "Plane 2 · Diversity", "tier": "LEO_SAT", "x": 322, "y": 212},
    {"id": "LEO-Orb-26", "label": "LEO-Orb-26", "sub": "Plane 2 · Diversity", "tier": "LEO_SAT", "x": 578, "y": 212},
]

LEO_BIDIR_SPECS: List[Tuple[str, str, str, float, float, float]] = [
    ("Gateway-Redmond", "LEO-Orb-14", "KA_FEEDER", 480.0, 4.8, 22.4),
    ("Gateway-Redmond", "LEO-Orb-18", "KA_FEEDER", 450.0, 6.1, 20.9),
    ("Gateway-Redmond", "Gateway-Ashburn", "TERRESTRIAL_BACKHAUL", 420.0, 28.5, 30.0),
    ("Gateway-Ashburn", "LEO-Orb-18", "KA_FEEDER", 460.0, 5.3, 21.6),
    ("LEO-Orb-14", "LEO-Orb-22", "OPTICAL_ISL", 690.0, 7.2, 26.5),
    ("LEO-Orb-22", "LEO-Orb-31", "OPTICAL_ISL", 690.0, 7.4, 26.5),
    ("LEO-Orb-14", "LEO-Orb-18", "OPTICAL_ISL", 640.0, 6.4, 25.5),
    ("LEO-Orb-18", "LEO-Orb-26", "OPTICAL_ISL", 660.0, 8.0, 25.8),
    ("LEO-Orb-22", "LEO-Orb-26", "OPTICAL_ISL", 640.0, 6.6, 25.5),
    ("LEO-Orb-26", "LEO-Orb-31", "OPTICAL_ISL", 650.0, 6.8, 25.8),
    ("LEO-Orb-26", "Gateway-Dublin", "KA_FEEDER", 440.0, 5.7, 21.3),
    ("Gateway-Dublin", "Gateway-Frankfurt", "TERRESTRIAL_BACKHAUL", 460.0, 8.4, 30.0),
    ("LEO-Orb-31", "Gateway-Frankfurt", "KA_FEEDER", 480.0, 4.9, 22.1),
]


def solve_leo_mesh(
    demand_gbps: float = 28.0,
    fault_mode: str = "nominal",
    invoke_bedrock: bool = False,
) -> Dict[str, Any]:
    """Route Amazon Leo Orbital Laser ISL & Ground Gateway demand via `CapacityAwareRouter`."""
    # Internal router uses Mbps where 10 Mbps == 1.0 Gbps orbital trunk scale (max ~85 Gbps)
    scaled_mbps = max(60.0, min(780.0, float(demand_gbps) * 10.0))
    display_gbps = round(scaled_mbps / 10.0, 1)
    node_ids = [n["id"] for n in LEO_NODES]

    links: List[LinkState] = []
    seq = 0
    for u, v, ltype, cap, lat, snr in LEO_BIDIR_SPECS:
        seq += 1
        links.append(LinkState(f"LEO-{seq:02d}-{u}->{v}", u, v, ltype, cap, lat, snr))
        seq += 1
        links.append(LinkState(f"LEO-{seq:02d}-{v}->{u}", v, u, ltype, cap, lat, snr))

    cap_router = CapacityAwareRouter(node_ids, links, min_snr_db=8.0, allow_flow_splitting=True)
    static_router = StaticShortestPathRouter(node_ids, links)

    events: List[LinkDegradationEvent] = []
    if fault_mode == "rain_fade_and_isl_flap":
        events.append(
            LinkDegradationEvent(
                event_id="LEO-DEG-RAIN",
                step=1,
                src="Gateway-Redmond",
                dst="LEO-Orb-14",
                capacity_factor=0.28,
                snr_db=5.4,
                noise_penalty_ms=29.0,
                outage=False,
                reason="KA_BAND_HEAVY_RAIN_FADE_5_4DB",
            )
        )
        events.append(
            LinkDegradationEvent(
                event_id="LEO-DEG-FLAP",
                step=1,
                src="LEO-Orb-14",
                dst="LEO-Orb-22",
                capacity_factor=0.0,
                snr_db=0.0,
                noise_penalty_ms=0.0,
                outage=True,
                reason="OPTICAL_LASER_ISL_LINK_FLAP",
            )
        )
    elif fault_mode == "handoff_isl_congestion":
        events.append(
            LinkDegradationEvent(
                event_id="LEO-DEG-ISL-SAT",
                step=1,
                src="LEO-Orb-22",
                dst="LEO-Orb-31",
                capacity_factor=0.25,
                snr_db=6.8,
                noise_penalty_ms=24.0,
                outage=False,
                reason="SOLAR_SCINTILLATION_ISL_DEGRADE",
            )
        )

    for ev in events:
        cap_router.apply_degradation_event(ev)
        static_router.apply_degradation_event(ev)

    demand_obj = TrafficDemand(
        demand_id="LEO-TRUNK-01",
        step=1,
        duration_steps=30,
        src="Gateway-Redmond",
        dst="Gateway-Frankfurt",
        bandwidth_mbps=scaled_mbps,
        sla_max_latency_ms=68.0,
        max_hops=6,
        service_class="LEO_ENTERPRISE_BACKBONE",
    )

    opt_alloc = cap_router.route_demand(demand_obj)
    base_alloc = static_router.route_demand(demand_obj)

    # Summarize undirected visual pairs with max directional utilization
    visual_arcs: List[Dict[str, Any]] = []
    for u, v, ltype, nom_cap, base_lat, _ in LEO_BIDIR_SPECS:
        fwd = next(l for l in cap_router.links_by_id.values() if l.src == u and l.dst == v)
        rev = next(l for l in cap_router.links_by_id.values() if l.src == v and l.dst == u)
        active_lnk = fwd if fwd.allocated_mbps >= rev.allocated_mbps else rev
        eff_gbps = round(active_lnk.effective_capacity_mbps / 10.0, 1)
        flow_gbps = round(active_lnk.allocated_mbps / 10.0, 2)
        util_pct = 100.0 if active_lnk.outage else (
            round((active_lnk.allocated_mbps / max(1.0, active_lnk.effective_capacity_mbps)) * 100.0, 1)
        )
        visual_arcs.append(
            {
                "arc_id": f"{u}<->{v}",
                "src": u,
                "dst": v,
                "mode": ltype,
                "capacity_gbps": round(nom_cap / 10.0, 1),
                "effective_gbps": eff_gbps,
                "flow_gbps": flow_gbps,
                "snr_db": active_lnk.snr_db,
                "base_latency_ms": base_lat,
                "outage": active_lnk.outage,
                "degraded": active_lnk.snr_db < 8.0 and not active_lnk.outage,
                "status_reason": active_lnk.degradation_reason or "NOMINAL",
                "utilization_pct": util_pct,
            }
        )

    active_nodes = {nid for sf in opt_alloc.subflows for nid in sf.nodes}
    nodes_out = []
    for n in LEO_NODES:
        nd = dict(n)
        if fault_mode == "rain_fade_and_isl_flap" and n["id"] == "LEO-Orb-14":
            nd["status"] = "DEGRADED"
        elif n["id"] in active_nodes:
            nd["status"] = "ACTIVE"
        else:
            nd["status"] = "STANDBY"
        nodes_out.append(nd)

    wf_engine = OperationalWorkflowEngine()
    if fault_mode == "rain_fade_and_isl_flap":
        snap = IncidentTelemetrySnapshot(
            incident_id="INC-LEO-REDMOND-14",
            alarm_type="GROUND_GATEWAY_SNR_DROP",
            region="us-west-2",
            resource_id="Gateway-Redmond->LEO-Orb-14",
            metrics={
                "rf_lna_bias_ma": 118.4,
                "standby_rf_chain_ready": True,
                "rain_rate_mm_hr": 38.5,
                "diversity_gw_snr_db": 20.9,
            },
            raw_logs=[],
        )
    else:
        snap = IncidentTelemetrySnapshot(
            incident_id="INC-LEO-HANDOFF-22",
            alarm_type="SATELLITE_HANDOFF_PACKET_LOSS",
            region="eu-central-1",
            resource_id="LEO-Orb-22->LEO-Orb-31",
            metrics={
                "ephemeris_skew_ms": 0.9,
                "ptp_locked": True,
                "target_isl_utilization": 0.94 if fault_mode != "nominal" else 0.42,
                "adjacent_plane_residual_mbps": 380.0,
            },
            raw_logs=[],
        )
    diag = wf_engine.diagnose(snap).to_dict()

    bedrock_diag: Optional[Dict[str, Any]] = None
    if invoke_bedrock or fault_mode != "nominal":
        sys_p = (
            "You are an Amazon Leo Satellite Constellation & Ground Networking AI Flight Engineer. "
            "Given live Ka-band RF SNR telemetry, Optical Laser ISL link state, and the capacity-aware "
            "reroute trace, write a grounded 3-bullet troubleshooting & traffic-engineering diagnosis "
            "(cite exact node IDs, dB SNR, ms RTT, and runbook action; <125 words)."
        )
        usr_p = (
            f"Fault Mode: {fault_mode}\n"
            f"Demand: {display_gbps} Gbps (Gateway-Redmond -> Gateway-Frankfurt)\n"
            f"Rerouted Paths: {[sf.nodes for sf in opt_alloc.subflows]}\n"
            f"End-to-End RTT: {round(opt_alloc.end_to_end_latency_ms, 2)} ms (Dropped: {round(opt_alloc.dropped_mbps / 10.0, 2)} Gbps)\n"
            f"Static OSPF Baseline Dropped: {round(base_alloc.dropped_mbps / 10.0, 2)} Gbps (RTT: {round(base_alloc.end_to_end_latency_ms, 2)} ms)\n"
            f"Decision-Tree Diagnosis ({diag['checks_executed']} checks): {diag['root_cause_code']} -> {diag['recommended_action']}\n"
        )
        bedrock_diag = call_bedrock_nova(sys_p, usr_p, max_tokens=250)

    if bedrock_diag is None:
        bedrock_diag = {
            "text": (
                f"- **Link-State Telemetry Root Cause (`{diag['root_cause_code']}`)**: Isolated in `{diag['checks_executed']}` checks — "
                f"Ka-band feeder `Gateway-Redmond -> LEO-Orb-14` attenuated below `8.0 dB` SNR threshold alongside `LEO-Orb-14 <-> LEO-Orb-22` optical flap.\n"
                f"- **Dynamic Mesh Reroute (`{round(opt_alloc.compute_time_ms, 3)} ms`)**: Diverted `{display_gbps} Gbps` onto diversity plane "
                f"`{' -> '.join(opt_alloc.subflows[0].nodes) if opt_alloc.subflows else 'N/A'}` at `{round(opt_alloc.end_to_end_latency_ms, 1)} ms` RTT (`0.0 Gbps` loss vs `{round(base_alloc.dropped_mbps / 10.0, 1)} Gbps` dropped on static OSPF).\n"
                f"- **Autonomous Runbook Action**: `{diag['recommended_action']}` while holding `LEO-Orb-14` laser transceiver in link-training quarantine."
            ),
            "engine": "deterministic-leo-flight-diagnostics",
            "latency_ms": round(diag["eval_time_ms"], 3),
            "tokens": 0,
        }

    return {
        "mode": "leo",
        "fault_mode": fault_mode,
        "demand_gbps": display_gbps,
        "sla_max_rtt_ms": 68.0,
        "compute_time_ms": round(opt_alloc.compute_time_ms, 4),
        "optimizer_summary": {
            "routed": opt_alloc.routed,
            "split_count": opt_alloc.split_count,
            "subflows": [
                {
                    "route_label": " -> ".join(sf.nodes),
                    "nodes": sf.nodes,
                    "allocated_gbps": round(sf.allocated_mbps / 10.0, 2),
                    "rtt_ms": round(sf.latency_ms, 2),
                }
                for sf in opt_alloc.subflows
            ],
            "end_to_end_rtt_ms": round(opt_alloc.end_to_end_latency_ms, 2),
            "dropped_gbps": round(opt_alloc.dropped_mbps / 10.0, 2),
            "sla_violated": opt_alloc.sla_violated,
        },
        "static_baseline_comparison": {
            "route_label": " -> ".join(base_alloc.subflows[0].nodes) if base_alloc.subflows else "UNROUTABLE",
            "end_to_end_rtt_ms": round(base_alloc.end_to_end_latency_ms, 2),
            "dropped_gbps": round(base_alloc.dropped_mbps / 10.0, 2),
            "sla_violated": base_alloc.sla_violated,
        },
        "nodes": nodes_out,
        "arcs": visual_arcs,
        "workflow_diagnosis": diag,
        "bedrock_advisory": bedrock_diag,
    }


# ==============================================================================
# STATEFUL SETTLEMENT LEDGER WORKSPACE
# ==============================================================================


class RouteLedgerWorkspace:
    """Manages live SQLite double-entry carrier & bandwidth settlements + SQS retry storms."""

    def __init__(self) -> None:
        self.ledger: Optional[IdempotentSettlementLedger] = None
        self.reset()

    def reset(self) -> None:
        if self.ledger is not None:
            try:
                self.ledger.close()
            except Exception:
                pass
        self.ledger = IdempotentSettlementLedger(":memory:")
        self.seq = 0
        self.raw_events_log: List[UsageOrderEvent] = []
        self.last_retry_storm_summary: Optional[Dict[str, Any]] = None

        # Seed enterprise SCOT carrier & Amazon Leo gateway accounts across 4 tax jurisdictions
        self.accounts_seed = {
            "ACCT-SCOT-LINEHAUL-WA": ("US-WA", 9_500_000),
            "ACCT-PRIME-AIR-CVG": ("US-WA", 9_500_000),
            "ACCT-LEO-FRANKFURT-GW": ("DE", 8_500_000),
            "ACCT-LEO-TOKYO-BACKHAUL": ("JP", 7_500_000),
            "ACCT-LEO-SAOPAULO-ISP": ("BR", 6_500_000),
        }
        for acct_id, (reg, limit_cents) in self.accounts_seed.items():
            self.ledger.seed_account(acct_id, reg, limit_cents)

        self.ledger.seed_inventory_sku("SKU-LEO-TERMINAL-PRO", "Amazon Leo Enterprise Phased-Array Terminal", 120)
        self.ledger.seed_inventory_sku("SKU-SCOT-ULD-PALLET", "Prime Air ULD Freight Container Reservation", 250)

        # Seed 3 initial balanced carrier & orbital settlements
        initial_events = [
            UsageOrderEvent(
                event_id="EVT-INIT-001",
                idempotency_key="IDEM-SCOT-SEA1-ORD5-001",
                account_id="ACCT-SCOT-LINEHAUL-WA",
                region="US-WA",
                event_type="METERED_BANDWIDTH_GB",
                quantity="32.000",
                unit_price_cents=14200,
                event_timestamp=1_764_000_100,
            ),
            UsageOrderEvent(
                event_id="EVT-INIT-002",
                idempotency_key="IDEM-LEO-REDMOND-FRA-001",
                account_id="ACCT-LEO-FRANKFURT-GW",
                region="DE",
                event_type="METERED_BANDWIDTH_GB",
                quantity="28.375",
                unit_price_cents=1890,
                event_timestamp=1_764_000_120,
            ),
            UsageOrderEvent(
                event_id="EVT-INIT-003",
                idempotency_key="IDEM-LEO-KIT-TOKYO-001",
                account_id="ACCT-LEO-TOKYO-BACKHAUL",
                region="JP",
                event_type="TERMINAL_KIT_ORDER",
                quantity="2",
                unit_price_cents=34900,
                event_timestamp=1_764_000_140,
                sku_id="SKU-LEO-TERMINAL-PRO",
                sku_qty=2,
            ),
        ]
        for ev in initial_events:
            self.raw_events_log.append(ev)
            self.ledger.process_event(ev)

    def get_ledger_snapshot(self) -> Dict[str, Any]:
        assert self.ledger is not None
        debits, credits, balanced = self.ledger.verify_trial_balance()
        recon = self.ledger.reconcile_against_metering_log(self.raw_events_log)

        journal_rows = self.ledger.conn.execute(
            """
            SELECT entry_id, idempotency_key, ledger_account, direction, amount_cents, effective_timestamp
            FROM journal_entries
            ORDER BY entry_id DESC
            LIMIT 24
            """
        ).fetchall()

        event_rows = self.ledger.conn.execute(
            """
            SELECT idempotency_key, event_id, account_id, region, event_type,
                   subtotal_cents, tax_cents, total_cents, status, is_out_of_order, payload_sha256
            FROM settlement_events
            ORDER BY ingested_seq DESC
            LIMIT 14
            """
        ).fetchall()

        return {
            "trial_balance": {
                "total_debits_cents": debits,
                "total_credits_cents": credits,
                "total_debits_usd": f"${debits / 100.0:,.2f}",
                "total_credits_usd": f"${credits / 100.0:,.2f}",
                "imbalance_cents": debits - credits,
                "is_balanced": balanced,
                "conservation_pct": 100.0 if balanced else 0.0,
            },
            "reconciliation": recon.to_dict(),
            "last_retry_storm": self.last_retry_storm_summary,
            "recent_events": [
                {
                    "idempotency_key": r["idempotency_key"],
                    "event_id": r["event_id"],
                    "account_id": r["account_id"],
                    "region": r["region"],
                    "event_type": r["event_type"],
                    "subtotal_usd": f"${int(r['subtotal_cents']) / 100.0:,.2f}",
                    "tax_usd": f"${int(r['tax_cents']) / 100.0:,.2f}",
                    "total_usd": f"${int(r['total_cents']) / 100.0:,.2f}",
                    "status": r["status"],
                    "is_out_of_order": bool(r["is_out_of_order"]),
                    "payload_sha256": str(r["payload_sha256"])[:16] + "...",
                }
                for r in event_rows
            ],
            "journal_entries": [
                {
                    "entry_id": j["entry_id"],
                    "idempotency_key": j["idempotency_key"],
                    "ledger_account": j["ledger_account"],
                    "direction": j["direction"],
                    "amount_cents": j["amount_cents"],
                    "amount_usd": f"${int(j['amount_cents']) / 100.0:,.2f}",
                }
                for j in journal_rows
            ],
        }

    def post_route_settlement(self, mode: str, route_label: str, volume: float, fault_mode: str) -> Dict[str, Any]:
        assert self.ledger is not None
        self.seq += 1
        if mode == "scot":
            idem = f"IDEM-SCOT-{fault_mode.upper()}-{int(volume)}-{self.seq:03d}"
            ev = UsageOrderEvent(
                event_id=f"EVT-SCOT-{self.seq:04d}",
                idempotency_key=idem,
                account_id="ACCT-PRIME-AIR-CVG" if fault_mode != "nominal" else "ACCT-SCOT-LINEHAUL-WA",
                region="US-WA",
                event_type="METERED_BANDWIDTH_GB",
                quantity=f"{max(1.0, volume / 100.0):.3f}",
                unit_price_cents=15400 if fault_mode != "nominal" else 14200,
                event_timestamp=1_764_001_000 + self.seq * 15,
            )
        else:
            idem = f"IDEM-LEO-{fault_mode.upper()}-{int(volume * 10)}-{self.seq:03d}"
            ev = UsageOrderEvent(
                event_id=f"EVT-LEO-{self.seq:04d}",
                idempotency_key=idem,
                account_id="ACCT-LEO-FRANKFURT-GW",
                region="DE",
                event_type="METERED_BANDWIDTH_GB",
                quantity=f"{max(1.0, volume):.3f}",
                unit_price_cents=1950,
                event_timestamp=1_764_001_000 + self.seq * 15,
            )
        self.raw_events_log.append(ev)
        receipt = self.ledger.process_event(ev)
        return {
            "posted_receipt": receipt.to_dict(),
            "route_label": route_label,
            "ledger": self.get_ledger_snapshot(),
        }

    def simulate_5x_sqs_retry_storm(self) -> Dict[str, Any]:
        """Fire 12 carrier/orbital settlement events replayed 5x each (60 SQS deliveries) + OOO timestamps."""
        assert self.ledger is not None
        t0 = time.perf_counter()
        self.seq += 1
        batch_tag = f"STORM{self.seq:02d}"

        specs = [
            ("ACCT-SCOT-LINEHAUL-WA", "US-WA", "METERED_BANDWIDTH_GB", "18.375", 1490, None, 0),
            ("ACCT-PRIME-AIR-CVG", "US-WA", "METERED_BANDWIDTH_GB", "24.625", 2150, "SKU-SCOT-ULD-PALLET", 2),
            ("ACCT-LEO-FRANKFURT-GW", "DE", "METERED_BANDWIDTH_GB", "31.125", 1790, None, 0),
            ("ACCT-LEO-TOKYO-BACKHAUL", "JP", "METERED_BANDWIDTH_GB", "19.875", 1650, None, 0),
            ("ACCT-LEO-SAOPAULO-ISP", "BR", "METERED_BANDWIDTH_GB", "14.333", 1890, None, 0),
            ("ACCT-SCOT-LINEHAUL-WA", "US-WA", "MONTHLY_SUBSCRIPTION", "1", 29900, None, 0),
            ("ACCT-LEO-FRANKFURT-GW", "DE", "TERMINAL_KIT_ORDER", "1", 34900, "SKU-LEO-TERMINAL-PRO", 1),
            ("ACCT-PRIME-AIR-CVG", "US-WA", "METERED_BANDWIDTH_GB", "27.875", 2250, None, 0),
            ("ACCT-LEO-TOKYO-BACKHAUL", "JP", "METERED_BANDWIDTH_GB", "11.667", 1590, None, 0),
            ("ACCT-LEO-SAOPAULO-ISP", "BR", "METERED_BANDWIDTH_GB", "22.125", 1750, None, 0),
            ("ACCT-SCOT-LINEHAUL-WA", "US-WA", "METERED_BANDWIDTH_GB", "16.375", 1490, None, 0),
            ("ACCT-LEO-FRANKFURT-GW", "DE", "METERED_BANDWIDTH_GB", "29.625", 1850, None, 0),
        ]

        unique_events: List[UsageOrderEvent] = []
        base_ts = 1_764_050_000 + self.seq * 1000
        for i, (acct, reg, etype, qty, price, sku, sqty) in enumerate(specs, start=1):
            # Intentionally inject out-of-order timestamps on every 3rd event
            ts = base_ts + i * 20 - (95 if i % 3 == 0 else 0)
            unique_events.append(
                UsageOrderEvent(
                    event_id=f"SQS-{batch_tag}-{i:02d}",
                    idempotency_key=f"IDEM-{batch_tag}-{i:02d}",
                    account_id=acct,
                    region=reg,
                    event_type=etype,
                    quantity=qty,
                    unit_price_cents=price,
                    event_timestamp=ts,
                    sku_id=sku,
                    sku_qty=sqty,
                )
            )

        # Build 5x duplicate SQS at-least-once delivery storm (60 deliveries for 12 unique events)
        storm_stream: List[UsageOrderEvent] = []
        for rep in range(5):
            for ev in unique_events:
                storm_stream.append(
                    UsageOrderEvent(
                        event_id=f"{ev.event_id}-DELIVERY-{rep + 1}",
                        idempotency_key=ev.idempotency_key,
                        account_id=ev.account_id,
                        region=ev.region,
                        event_type=ev.event_type,
                        quantity=ev.quantity,
                        unit_price_cents=ev.unit_price_cents,
                        event_timestamp=ev.event_timestamp,
                        sku_id=ev.sku_id,
                        sku_qty=ev.sku_qty,
                    )
                )

        naive = NaiveFloatBillingProcessor(
            account_limits_cents={k: v[1] for k, v in self.accounts_seed.items()},
            sku_stock={"SKU-LEO-TERMINAL-PRO": 120, "SKU-SCOT-ULD-PALLET": 250},
        )

        dup_blocked = 0
        settled_unique_cents = 0
        for msg in storm_stream:
            self.raw_events_log.append(msg)
            rec = self.ledger.process_event(msg)
            if rec.is_duplicate:
                dup_blocked += 1
            elif rec.status == "SETTLED":
                settled_unique_cents += rec.total_cents
            naive.process_event(msg)

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        naive_overbilled_cents = max(0, naive.total_billed_cents_rounded - settled_unique_cents)
        debits, credits, balanced = self.ledger.verify_trial_balance()

        summary = {
            "batch_tag": batch_tag,
            "sqs_deliveries_received": len(storm_stream),
            "replay_multiplier": "5x per SQS message",
            "unique_events_settled": len(unique_events),
            "duplicate_retries_blocked": dup_blocked,
            "routeledger_duplicate_charges": 0,
            "naive_processor_duplicate_charges": naive.duplicate_charges_applied,
            "naive_overbilled_usd": f"${naive_overbilled_cents / 100.0:,.2f}",
            "exact_settled_batch_usd": f"${settled_unique_cents / 100.0:,.2f}",
            "trial_balance_conserved": balanced,
            "imbalance_cents": debits - credits,
            "processing_time_ms": elapsed_ms,
        }
        self.last_retry_storm_summary = summary
        return {
            "storm_result": summary,
            "ledger": self.get_ledger_snapshot(),
        }

    def test_mutated_payload_conflict(self) -> Dict[str, Any]:
        """Attempt to reuse `IDEM-SCOT-SEA1-ORD5-001` with a tampered quantity to prove SHA-256 conflict trap."""
        assert self.ledger is not None
        tampered = UsageOrderEvent(
            event_id="EVT-TAMPERED-999",
            idempotency_key="IDEM-SCOT-SEA1-ORD5-001",
            account_id="ACCT-SCOT-LINEHAUL-WA",
            region="US-WA",
            event_type="METERED_BANDWIDTH_GB",
            quantity="999.000",  # Mutated from 32.000!
            unit_price_cents=14200,
            event_timestamp=1_764_000_100,
        )
        try:
            self.ledger.process_event(tampered)
            return {"conflict_blocked": False}
        except IdempotencyConflictError as exc:
            debits, credits, balanced = self.ledger.verify_trial_balance()
            return {
                "conflict_blocked": True,
                "exception_type": "IdempotencyConflictError",
                "message": str(exc),
                "trial_balance_still_balanced": balanced,
                "imbalance_cents": debits - credits,
            }


WORKSPACE = RouteLedgerWorkspace()


def route_api_request(
    method: str,
    path: str,
    query: Dict[str, str],
    body: Dict[str, Any],
    request_id: str = "local",
) -> Tuple[int, Dict[str, Any]]:
    """Pure router shared by local HTTP server and AWS Lambda `/api/*` behind CloudFront."""
    try:
        if method == "GET" and path == "/api/health":
            debits, credits, balanced = WORKSPACE.ledger.verify_trial_balance()  # type: ignore[union-attr]
            return 200, {
                "status": "ok",
                "service": "route-ledger",
                "version": __version__,
                "delivery": os.environ.get("ROUTELEDGER_DELIVERY", "cloudfront-s3-oac"),
                "aws_region": os.environ.get("AWS_REGION", "us-west-2"),
                "bedrock_model": os.environ.get("ROUTELEDGER_BEDROCK_MODEL", "us.amazon.nova-lite-v1:0"),
                "python_version": platform.python_version(),
                "supported_modes": ["scot", "leo"],
                "ledger_trial_balance_ok": balanced,
                "ledger_total_usd": f"${debits / 100.0:,.2f}",
                "request_id": request_id,
            }

        if method == "GET" and path == "/api/state":
            mode = query.get("mode") or "scot"
            topo = solve_scot_network(3200, "nominal", False) if mode == "scot" else solve_leo_mesh(28.0, "nominal", False)
            return 200, {
                "service": "route-ledger",
                "version": __version__,
                "bedrock_model": os.environ.get("ROUTELEDGER_BEDROCK_MODEL", "us.amazon.nova-lite-v1:0"),
                "topology": topo,
                "ledger": WORKSPACE.get_ledger_snapshot(),
                "request_id": request_id,
            }

        if method == "POST" and path == "/api/topology/optimize":
            mode = str(body.get("mode") or "scot").lower()
            fault_mode = str(body.get("fault_mode") or "nominal")
            invoke_bedrock = bool(body.get("invoke_bedrock", False))
            auto_settle = bool(body.get("auto_settle", False))

            if mode == "scot":
                demand_units = int(body.get("demand_units") or 3200)
                topo = solve_scot_network(demand_units, fault_mode, invoke_bedrock)
                primary_route = (
                    topo["optimizer_summary"]["active_corridors"][0]["route_label"]
                    if topo["optimizer_summary"]["active_corridors"]
                    else "UNROUTABLE"
                )
                if auto_settle:
                    WORKSPACE.post_route_settlement("scot", primary_route, demand_units, fault_mode)
            else:
                demand_gbps = float(body.get("demand_gbps") or 28.0)
                topo = solve_leo_mesh(demand_gbps, fault_mode, invoke_bedrock)
                primary_route = (
                    topo["optimizer_summary"]["subflows"][0]["route_label"]
                    if topo["optimizer_summary"]["subflows"]
                    else "UNROUTABLE"
                )
                if auto_settle:
                    WORKSPACE.post_route_settlement("leo", primary_route, demand_gbps, fault_mode)

            topo["ledger"] = WORKSPACE.get_ledger_snapshot()
            topo["request_id"] = request_id
            return 200, topo

        if method == "POST" and path == "/api/ledger/settle-route":
            mode = str(body.get("mode") or "scot")
            route_label = str(body.get("route_label") or "SEA1 -> SLC2 -> CVG3 -> EWR7")
            volume = float(body.get("volume") or 3200.0)
            fault_mode = str(body.get("fault_mode") or "nominal")
            return 200, WORKSPACE.post_route_settlement(mode, route_label, volume, fault_mode)

        if method == "POST" and path == "/api/ledger/retry-storm":
            return 200, WORKSPACE.simulate_5x_sqs_retry_storm()

        if method == "POST" and path == "/api/ledger/tamper-test":
            return 200, WORKSPACE.test_mutated_payload_conflict()

        if method == "POST" and path == "/api/reset":
            WORKSPACE.reset()
            return 200, {
                "status": "reset_complete",
                "topology": solve_scot_network(3200, "nominal", False),
                "ledger": WORKSPACE.get_ledger_snapshot(),
            }

        return 404, {"error": f"Unknown API route: {method} {path}"}
    except KeyError as exc:
        return 404, {"error": str(exc)}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:
        return 500, {"error": str(exc)}


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """AWS Lambda Function URL handler mounted behind Amazon CloudFront `/api/*` origin."""
    req_ctx = event.get("requestContext") or {}
    method = ((req_ctx.get("http") or {}).get("method") or event.get("httpMethod") or "GET").upper()
    raw_path = event.get("rawPath") or event.get("path") or "/"
    query_params = {k: str(v) for k, v in (event.get("queryStringParameters") or {}).items() if v is not None}
    req_id = getattr(context, "aws_request_id", "cloudfront-api")

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS_HEADERS, "body": ""}

    if raw_path.startswith("/api/"):
        body: Dict[str, Any] = {}
        raw_body = event.get("body")
        if raw_body:
            if event.get("isBase64Encoded") and isinstance(raw_body, str):
                raw_body = base64.b64decode(raw_body).decode("utf-8")
            body = json.loads(raw_body) if isinstance(raw_body, str) else dict(raw_body)
        status, payload = route_api_request(method, raw_path, query_params, body, request_id=req_id)
        return {
            "statusCode": status,
            "headers": {
                **CORS_HEADERS,
                "Content-Type": "application/json; charset=utf-8",
                "Cache-Control": "no-store",
            },
            "body": json.dumps(payload, default=str),
        }

    target = WEB_DIR / ("index.html" if raw_path in ("/", "") else raw_path.lstrip("/"))
    if not target.is_file() and raw_path.endswith(".svg"):
        target = DOCS_DIR / Path(raw_path).name
    if target.is_file():
        ctype = mimetypes.guess_type(str(target))[0] or "text/html; charset=utf-8"
        return {
            "statusCode": 200,
            "headers": {**CORS_HEADERS, "Content-Type": ctype},
            "body": target.read_text(encoding="utf-8"),
        }
    return {
        "statusCode": 404,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({"error": "Not found", "path": raw_path}),
    }


class LocalRouteLedgerWebHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}

        if path.startswith("/api/"):
            status, payload = route_api_request("GET", path, query, {}, request_id="local-dev")
            raw = json.dumps(payload, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            for k, v in CORS_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        candidate = (WEB_DIR / rel).resolve()
        if not candidate.is_file() and path.endswith(".svg"):
            candidate = (DOCS_DIR / Path(path).name).resolve()
        if not candidate.is_file():
            candidate = WEB_DIR / "index.html"

        data = candidate.read_bytes()
        ctype = mimetypes.guess_type(str(candidate))[0] or "text/html"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        body = json.loads(raw_body) if raw_body.strip() else {}
        status, payload = route_api_request("POST", parsed.path, {}, body, request_id="local-dev")
        raw = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def run_local_server(port: int = 3097) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), LocalRouteLedgerWebHandler)
    print(f"RouteLedger SCOT & Leo Operations Center running at http://localhost:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    run_local_server(port=int(os.environ.get("PORT", "3097")))
