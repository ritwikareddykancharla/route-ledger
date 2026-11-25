"""SCOT-FO Time-Expanded Multi-Modal Pareto Solver & Multi-Item Split-Box Optimizer (`route_ledger/atrops_pareto.py`).

Implements three deterministic operations-research & autonomous network-validation engines
targeting Amazon SCOT-FO Shipment Optimization (ATROPS) and Retail Supply Chain:
  1. `TimeExpandedParetoSolver`:
     Computes exact non-dominated 3D Pareto frontiers across Prime Delivery Speed (`SLA %` / transit hours),
     Fulfillment Cost (`$/pkg`), and Carbon Footprint (`kg CO2e/pkg`) over scheduled `AIR_CARGO`,
     `LINEHAUL_TRUCK`, `INTERMODAL_RAIL`, and `EV_MIDDLE_MILE` arcs governed by discrete
     Critical Pull Time (`CPT`) sortation cutoffs and trailer/aircraft capacity limits.
  2. `MultiItemCartSourcingOptimizer`:
     Solves combinatorial multi-ASIN shopping-cart fulfillment across regional Fulfillment Centers,
     comparing `Single-Box Consolidation` vs. `Split-FC Dispatch` vs. `Greedy Nearest-FC` under
     Prime SLA promises, box/dunnage fees, zone shipping rates, and `kg CO2e` emissions.
  3. `CounterfactualScheduleValidator`:
     Simulates LLM-proposed (Amazon Bedrock `us.amazon.nova-lite-v1:0`) network configuration patches
     (lane additions, CPT cutoff shifts, modal capacity reallocations) before commit, rejecting
     proposals that breach downstream station sortation caps, CPT causality, or Prime SLA floors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import math
import random
import statistics
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


TRANSPORT_MODES: Tuple[str, ...] = (
    "AIR_CARGO",
    "LINEHAUL_TRUCK",
    "INTERMODAL_RAIL",
    "EV_MIDDLE_MILE",
)


@dataclass(frozen=True)
class FacilityNode:
    """SCOT fulfillment facility with sortation dwell time and hourly/wave sort cap."""

    facility_id: str
    name: str
    facility_type: str  # FC | SORT_CENTER | AIR_HUB | INTERMODAL_HUB | DELIVERY_STATION
    region: str
    sort_dwell_hr: float
    wave_sort_cap_pkgs: int
    x: int = 0
    y: int = 0


@dataclass
class ScheduledModalArc:
    """Scheduled departure arc with Critical Pull Time (CPT) cutoff, cost, capacity, and CO2e."""

    arc_id: str
    src: str
    dst: str
    mode: str  # AIR_CARGO | LINEHAUL_TRUCK | INTERMODAL_RAIL | EV_MIDDLE_MILE
    cpt_departure_hr: float
    transit_hr: float
    capacity_pkgs: int
    cost_per_pkg_usd: float
    co2e_kg_per_pkg: float
    distance_km: int
    active: bool = True
    allocated_pkgs: int = 0
    status_reason: str = "NOMINAL"

    @property
    def residual_pkgs(self) -> int:
        if not self.active:
            return 0
        return max(0, self.capacity_pkgs - self.allocated_pkgs)

    def clone(self) -> "ScheduledModalArc":
        return ScheduledModalArc(
            arc_id=self.arc_id,
            src=self.src,
            dst=self.dst,
            mode=self.mode,
            cpt_departure_hr=self.cpt_departure_hr,
            transit_hr=self.transit_hr,
            capacity_pkgs=self.capacity_pkgs,
            cost_per_pkg_usd=self.cost_per_pkg_usd,
            co2e_kg_per_pkg=self.co2e_kg_per_pkg,
            distance_km=self.distance_km,
            active=self.active,
            allocated_pkgs=0,
            status_reason=self.status_reason,
        )


@dataclass(frozen=True)
class ShipmentWaveDemand:
    """Origin-to-destination shipment wave with induct ready hour and promised SLA deadline."""

    wave_id: str
    origin_fc: str
    dest_station: str
    ready_hr: float
    sla_deadline_hr: float
    volume_pkgs: int
    promise_tier: str  # NEXT_DAY | ONE_DAY | TWO_DAY


@dataclass(frozen=True)
class ParetoRouteCandidate:
    """Single time-expanded non-dominated path across `(arrival_hr, cost_usd, co2e_kg)`."""

    plan_id: str
    facilities: Tuple[str, ...]
    arc_ids: Tuple[str, ...]
    modes_used: Tuple[str, ...]
    departure_schedule_hrs: Tuple[float, ...]
    arrival_hr: float
    total_transit_hr: float
    cost_per_pkg_usd: float
    co2e_kg_per_pkg: float
    bottleneck_capacity_pkgs: int
    cpt_rollovers: int
    meets_sla: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "route_label": " -> ".join(self.facilities),
            "facilities": list(self.facilities),
            "arc_ids": list(self.arc_ids),
            "modes_used": list(self.modes_used),
            "departure_schedule_hrs": [round(t, 2) for t in self.departure_schedule_hrs],
            "arrival_hr": round(self.arrival_hr, 2),
            "total_transit_hr": round(self.total_transit_hr, 2),
            "cost_per_pkg_usd": round(self.cost_per_pkg_usd, 3),
            "co2e_kg_per_pkg": round(self.co2e_kg_per_pkg, 3),
            "bottleneck_capacity_pkgs": self.bottleneck_capacity_pkgs,
            "cpt_rollovers": self.cpt_rollovers,
            "meets_sla": self.meets_sla,
        }


@dataclass(frozen=True)
class ParetoFrontierPoint:
    """Aggregate network-level operating point on the Speed / Cost / CO2e Pareto frontier."""

    point_id: str
    policy_name: str
    weights: Tuple[float, float, float]  # (w_speed, w_cost, w_carbon)
    sla_hit_rate_pct: float
    avg_transit_hr: float
    cost_per_pkg_usd: float
    co2e_kg_per_pkg: float
    total_cost_usd: float
    total_co2e_kg: float
    modal_share_pct: Dict[str, float]
    primary_route_label: str
    selected_routes: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "point_id": self.point_id,
            "policy_name": self.policy_name,
            "weights": {
                "speed": round(self.weights[0], 2),
                "cost": round(self.weights[1], 2),
                "carbon": round(self.weights[2], 2),
            },
            "sla_hit_rate_pct": round(self.sla_hit_rate_pct, 2),
            "avg_transit_hr": round(self.avg_transit_hr, 2),
            "cost_per_pkg_usd": round(self.cost_per_pkg_usd, 3),
            "co2e_kg_per_pkg": round(self.co2e_kg_per_pkg, 3),
            "total_cost_usd": round(self.total_cost_usd, 2),
            "total_co2e_kg": round(self.total_co2e_kg, 2),
            "modal_share_pct": {k: round(v, 1) for k, v in self.modal_share_pct.items()},
            "primary_route_label": self.primary_route_label,
            "selected_routes": self.selected_routes,
        }


@dataclass(frozen=True)
class CartItemSpec:
    """Single SKU line item inside a multi-item customer shopping cart."""

    sku_id: str
    title: str
    qty: int
    unit_weight_kg: float


@dataclass(frozen=True)
class CartSourcingPlan:
    """Evaluated fulfillment sourcing assignment for a multi-item cart."""

    strategy: str  # SINGLE_BOX_CONSOLIDATION | SPLIT_FC_DISPATCH | GREEDY_NEAREST_FC | ATROPS_OPTIMAL
    feasible: bool
    box_count: int
    fc_assignments: Dict[str, List[str]]  # fc_id -> list of sku_ids
    modes_by_fc: Dict[str, str]
    max_delivery_hr: float
    sla_deadline_hr: float
    sla_met: bool
    shipping_cost_usd: float
    packaging_cost_usd: float
    total_fulfillment_cost_usd: float
    co2e_kg: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "feasible": self.feasible,
            "box_count": self.box_count,
            "is_split_shipment": self.box_count > 1,
            "fc_assignments": {k: list(v) for k, v in self.fc_assignments.items()},
            "modes_by_fc": dict(self.modes_by_fc),
            "max_delivery_hr": round(self.max_delivery_hr, 2),
            "sla_deadline_hr": round(self.sla_deadline_hr, 2),
            "sla_met": self.sla_met,
            "shipping_cost_usd": round(self.shipping_cost_usd, 2),
            "packaging_cost_usd": round(self.packaging_cost_usd, 2),
            "total_fulfillment_cost_usd": round(self.total_fulfillment_cost_usd, 2),
            "co2e_kg": round(self.co2e_kg, 3),
        }


@dataclass(frozen=True)
class NetworkSchedulePatch:
    """Candidate LLM or operator network configuration edit validated prior to commit."""

    patch_id: str
    author: str
    rationale: str
    cpt_shifts: Dict[str, float] = field(default_factory=dict)  # arc_id -> new_cpt_departure_hr
    capacity_overrides: Dict[str, int] = field(default_factory=dict)  # arc_id -> new_capacity_pkgs
    arc_activations: Dict[str, bool] = field(default_factory=dict)  # arc_id -> active bool
    ad_hoc_arcs: List[Dict[str, Any]] = field(default_factory=list)
    station_sort_cap_overrides: Dict[str, int] = field(default_factory=dict)


def build_scot_multimodal_topology(
    disruption: str = "nominal",
) -> Tuple[Dict[str, FacilityNode], List[ScheduledModalArc]]:
    """Build deterministic 9-facility, 22-arc multi-modal SCOT topology with CPT cutoffs."""
    facilities_list = [
        FacilityNode("SEA1", "Seattle Regional FC", "FC", "US-WEST", 0.8, 6800, x=76, y=148),
        FacilityNode("PDX9", "Portland EV Consolidation", "SORT_CENTER", "US-WEST", 0.6, 5200, x=122, y=274),
        FacilityNode("SLC2", "Salt Lake Mountain Sort", "SORT_CENTER", "US-MTN", 0.9, 6400, x=248, y=168),
        FacilityNode("DEN4", "Denver Intermodal Hub", "INTERMODAL_HUB", "US-MTN", 1.1, 7200, x=382, y=244),
        FacilityNode("DFW6", "Dallas South Mega-Sort", "SORT_CENTER", "US-SOUTH", 1.0, 6800, x=496, y=346),
        FacilityNode("ORD5", "Chicago Cross-Dock Sort", "SORT_CENTER", "US-MIDWEST", 1.1, 7500, x=548, y=108),
        FacilityNode("CVG3", "Cincinnati Prime Air Hub", "AIR_HUB", "US-MIDWEST", 1.2, 8200, x=686, y=196),
        FacilityNode("BWI2", "Baltimore Mid-Atlantic Sort", "SORT_CENTER", "US-EAST", 0.8, 5600, x=798, y=276),
        FacilityNode("EWR7", "Newark Metro Delivery Station", "DELIVERY_STATION", "US-EAST", 0.5, 6200, x=876, y=146),
    ]
    facilities = {f.facility_id: f for f in facilities_list}

    # Scheduled arcs across AIR_CARGO, LINEHAUL_TRUCK, INTERMODAL_RAIL, EV_MIDDLE_MILE
    # CPT hours are measured relative to Day-0 18:00 order cutoff (t=0.0).
    arcs: List[ScheduledModalArc] = [
        # Direct & Regional Air Cargo legs (fastest, highest $/pkg and kg CO2e/pkg)
        ScheduledModalArc("MM-01-SEA1-CVG3-AIR", "SEA1", "CVG3", "AIR_CARGO", 2.5, 4.3, 2800, 4.45, 2.84, 3150),
        ScheduledModalArc("MM-02-SEA1-EWR7-AIR", "SEA1", "EWR7", "AIR_CARGO", 3.0, 5.2, 1900, 6.40, 3.85, 3860),
        ScheduledModalArc("MM-03-SLC2-CVG3-AIR", "SLC2", "CVG3", "AIR_CARGO", 9.2, 3.4, 2600, 2.85, 1.88, 2320),
        ScheduledModalArc("MM-04-DFW6-EWR7-AIR", "DFW6", "EWR7", "AIR_CARGO", 23.2, 3.3, 2400, 3.05, 1.95, 2210),
        ScheduledModalArc("MM-05-CVG3-EWR7-AIR", "CVG3", "EWR7", "AIR_CARGO", 14.2, 1.8, 4200, 1.95, 1.08, 915),
        # Team-Driver & Single-Driver Diesel Linehaul Trucks (medium speed, moderate cost & CO2e)
        ScheduledModalArc("MM-06-SEA1-SLC2-TRK", "SEA1", "SLC2", "LINEHAUL_TRUCK", 2.5, 5.4, 4600, 1.18, 0.58, 1125),
        ScheduledModalArc("MM-07-PDX9-SLC2-TRK", "PDX9", "SLC2", "LINEHAUL_TRUCK", 5.2, 5.0, 3800, 1.08, 0.52, 1020),
        ScheduledModalArc("MM-08-SLC2-DEN4-TRK", "SLC2", "DEN4", "LINEHAUL_TRUCK", 11.2, 4.2, 4400, 0.94, 0.44, 835),
        ScheduledModalArc("MM-09-DEN4-ORD5-TRK", "DEN4", "ORD5", "LINEHAUL_TRUCK", 16.8, 5.8, 4200, 1.32, 0.66, 1480),
        ScheduledModalArc("MM-10-DEN4-DFW6-TRK", "DEN4", "DFW6", "LINEHAUL_TRUCK", 16.6, 5.1, 3900, 1.16, 0.57, 1260),
        ScheduledModalArc("MM-11-DFW6-CVG3-TRK", "DFW6", "CVG3", "LINEHAUL_TRUCK", 23.0, 5.4, 3800, 1.22, 0.61, 1310),
        ScheduledModalArc("MM-12-ORD5-CVG3-TRK", "ORD5", "CVG3", "LINEHAUL_TRUCK", 24.0, 3.2, 4400, 0.78, 0.34, 470),
        ScheduledModalArc("MM-13-ORD5-EWR7-TRK", "ORD5", "EWR7", "LINEHAUL_TRUCK", 24.2, 6.2, 3600, 1.42, 0.69, 1270),
        ScheduledModalArc("MM-14-CVG3-BWI2-TRK", "CVG3", "BWI2", "LINEHAUL_TRUCK", 28.8, 4.5, 4100, 0.96, 0.43, 805),
        ScheduledModalArc("MM-15-CVG3-EWR7-TRK", "CVG3", "EWR7", "LINEHAUL_TRUCK", 29.8, 5.6, 4200, 1.14, 0.53, 980),
        # Intermodal Rail Corridors (lowest $/pkg and ~74% lower CO2e than diesel truck, longer transit)
        ScheduledModalArc("MM-16-SEA1-DEN4-RAIL", "SEA1", "DEN4", "INTERMODAL_RAIL", 2.6, 11.0, 5600, 0.68, 0.21, 1640),
        ScheduledModalArc("MM-17-SLC2-ORD5-RAIL", "SLC2", "ORD5", "INTERMODAL_RAIL", 11.4, 11.2, 5400, 0.74, 0.24, 2010),
        ScheduledModalArc("MM-18-DEN4-BWI2-RAIL", "DEN4", "BWI2", "INTERMODAL_RAIL", 15.0, 14.2, 5200, 0.86, 0.28, 2420),
        ScheduledModalArc("MM-19-ORD5-BWI2-RAIL", "ORD5", "BWI2", "INTERMODAL_RAIL", 24.4, 8.0, 5000, 0.62, 0.19, 1110),
        # EV Middle-Mile Zero-Tailpipe Regional Shuttles (ultra-low CO2e, short-to-mid range)
        ScheduledModalArc("MM-20-SEA1-PDX9-EV", "SEA1", "PDX9", "EV_MIDDLE_MILE", 2.2, 2.1, 3400, 0.54, 0.06, 280),
        ScheduledModalArc("MM-21-CVG3-BWI2-EV", "CVG3", "BWI2", "EV_MIDDLE_MILE", 14.0, 4.8, 2900, 0.72, 0.08, 805),
        ScheduledModalArc("MM-22-BWI2-EWR7-EV", "BWI2", "EWR7", "EV_MIDDLE_MILE", 20.0, 2.6, 4600, 0.55, 0.05, 305),
        ScheduledModalArc("MM-23-BWI2-EWR7-EV-LATE", "BWI2", "EWR7", "EV_MIDDLE_MILE", 33.6, 2.6, 4800, 0.54, 0.05, 305),
    ]

    if disruption == "cvg3_weather_ground_stop":
        for a in arcs:
            if a.mode == "AIR_CARGO" and (a.src == "CVG3" or a.dst == "CVG3"):
                a.active = False
                a.status_reason = "CVG3_SEVERE_ICE_AIR_GROUND_STOP"
            elif a.arc_id == "MM-02-SEA1-EWR7-AIR":
                a.capacity_pkgs = 900
                a.status_reason = "EASTBOUND_JETSTREAM_SLOT_CAP"
    elif disruption == "ord5_yard_congestion":
        for a in arcs:
            if a.src == "ORD5" or a.dst == "ORD5":
                if a.mode == "LINEHAUL_TRUCK":
                    a.capacity_pkgs = int(a.capacity_pkgs * 0.35)
                    a.transit_hr = round(a.transit_hr + 3.4, 2)
                    a.status_reason = "ORD5_YARD_118PCT_TRAILER_BACKLOG"
    elif disruption == "cpt_tight_cutoff":
        for a in arcs:
            if a.arc_id in ("MM-06-SEA1-SLC2-TRK", "MM-16-SEA1-DEN4-RAIL"):
                a.cpt_departure_hr = 1.1  # Tightened cutoff forces late waves to miss pull unless patched
                a.status_reason = "EARLY_SORT_PULL_CUTOFF"

    return facilities, arcs


def _dominates(
    a: Tuple[float, float, float],
    b: Tuple[float, float, float],
    eps: float = 1e-6,
) -> bool:
    """Return True iff `a = (arr, cost, co2)` Pareto-dominates `b` across all 3 objectives."""
    leq_all = (a[0] <= b[0] + eps) and (a[1] <= b[1] + eps) and (a[2] <= b[2] + eps)
    strict_any = (a[0] < b[0] - eps) or (a[1] < b[1] - eps) or (a[2] < b[2] - eps)
    return leq_all and strict_any


class TimeExpandedParetoSolver:
    """Exact multi-objective Martins/label-setting Pareto solver with CPT schedule cutoffs."""

    def __init__(
        self,
        facilities: Dict[str, FacilityNode],
        arcs: Sequence[ScheduledModalArc],
    ) -> None:
        self.facilities = dict(facilities)
        self.arcs: List[ScheduledModalArc] = [a.clone() for a in arcs]
        self.arcs_by_id: Dict[str, ScheduledModalArc] = {a.arc_id: a for a in self.arcs}
        self.adj: Dict[str, List[ScheduledModalArc]] = {fid: [] for fid in self.facilities}
        for a in self.arcs:
            self.adj.setdefault(a.src, []).append(a)

    @staticmethod
    def compute_next_cpt_departure(ready_at_facility_hr: float, base_cpt_hr: float) -> Tuple[float, int]:
        """Compute earliest scheduled CPT departure `>= ready_at_facility_hr` (with 24h rollovers)."""
        if ready_at_facility_hr <= base_cpt_hr + 1e-9:
            return base_cpt_hr, 0
        missed_days = int(math.ceil((ready_at_facility_hr - base_cpt_hr - 1e-9) / 24.0))
        return base_cpt_hr + 24.0 * missed_days, missed_days

    def enumerate_pareto_routes(
        self,
        origin_fc: str = "SEA1",
        dest_station: str = "EWR7",
        ready_hr: float = 0.8,
        sla_deadline_hr: float = 36.0,
        min_capacity_pkgs: int = 1,
        max_hops: int = 5,
        max_rollovers: int = 1,
    ) -> List[ParetoRouteCandidate]:
        """Compute the exact non-dominated 3D `(arrival_hr, cost_usd, co2e_kg)` route set."""
        # State tuple: (node, arrival_hr, cost, co2e, facilities, arc_ids, modes, dep_hrs, cap, rollovers)
        origin_dwell = self.facilities[origin_fc].sort_dwell_hr
        init_ready = ready_hr + origin_dwell

        # Maintain non-dominated `(arrival_hr, cost, co2e)` vectors per facility to prune dominated subpaths
        node_frontiers: Dict[str, List[Tuple[float, float, float]]] = {origin_fc: [(init_ready, 0.0, 0.0)]}
        queue: List[
            Tuple[
                str,
                float,
                float,
                float,
                Tuple[str, ...],
                Tuple[str, ...],
                Tuple[str, ...],
                Tuple[float, ...],
                int,
                int,
            ]
        ] = [
            (origin_fc, init_ready, 0.0, 0.0, (origin_fc,), (), (), (), 999_999, 0)
        ]

        dest_candidates: List[ParetoRouteCandidate] = []
        seq = 0

        while queue:
            u, cur_ready, cost_u, co2_u, fpath, apath, mpath, dpath, bottleneck, rolls = queue.pop(0)
            if len(apath) >= max_hops:
                continue

            for arc in self.adj.get(u, []):
                if not arc.active or arc.residual_pkgs < min_capacity_pkgs:
                    continue
                if arc.dst in fpath:
                    continue

                dep_hr, add_rolls = self.compute_next_cpt_departure(cur_ready, arc.cpt_departure_hr)
                tot_rolls = rolls + add_rolls
                if tot_rolls > max_rollovers:
                    continue

                arr_dst = dep_hr + arc.transit_hr
                dst_dwell = (
                    0.0
                    if arc.dst == dest_station
                    else self.facilities[arc.dst].sort_dwell_hr
                )
                ready_dst = arr_dst + dst_dwell
                nxt_cost = cost_u + arc.cost_per_pkg_usd
                nxt_co2 = co2_u + arc.co2e_kg_per_pkg
                nxt_vec = (ready_dst, nxt_cost, nxt_co2)

                existing_vecs = node_frontiers.setdefault(arc.dst, [])
                if any(_dominates(ev, nxt_vec) for ev in existing_vecs):
                    continue

                # Prune any previously stored vectors at `arc.dst` dominated by `nxt_vec`
                node_frontiers[arc.dst] = [ev for ev in existing_vecs if not _dominates(nxt_vec, ev)] + [nxt_vec]

                nxt_cap = min(bottleneck, arc.residual_pkgs)
                nxt_f = fpath + (arc.dst,)
                nxt_a = apath + (arc.arc_id,)
                nxt_m = mpath + (arc.mode,)
                nxt_d = dpath + (dep_hr,)

                if arc.dst == dest_station:
                    seq += 1
                    dest_candidates.append(
                        ParetoRouteCandidate(
                            plan_id=f"PARETO-{seq:02d}",
                            facilities=nxt_f,
                            arc_ids=nxt_a,
                            modes_used=nxt_m,
                            departure_schedule_hrs=nxt_d,
                            arrival_hr=arr_dst,
                            total_transit_hr=arr_dst - ready_hr,
                            cost_per_pkg_usd=nxt_cost,
                            co2e_kg_per_pkg=nxt_co2,
                            bottleneck_capacity_pkgs=nxt_cap,
                            cpt_rollovers=tot_rolls,
                            meets_sla=arr_dst <= sla_deadline_hr + 1e-9,
                        )
                    )
                else:
                    queue.append(
                        (
                            arc.dst,
                            ready_dst,
                            nxt_cost,
                            nxt_co2,
                            nxt_f,
                            nxt_a,
                            nxt_m,
                            nxt_d,
                            nxt_cap,
                            tot_rolls,
                        )
                    )

        # Final exact non-dominated filter at `dest_station`
        non_dominated: List[ParetoRouteCandidate] = []
        for cand in dest_candidates:
            vec_c = (cand.arrival_hr, cand.cost_per_pkg_usd, cand.co2e_kg_per_pkg)
            if any(
                _dominates((other.arrival_hr, other.cost_per_pkg_usd, other.co2e_kg_per_pkg), vec_c)
                for other in dest_candidates
                if other.plan_id != cand.plan_id
            ):
                continue
            # Deduplicate identical objective triples
            if any(
                abs(other.arrival_hr - cand.arrival_hr) < 1e-6
                and abs(other.cost_per_pkg_usd - cand.cost_per_pkg_usd) < 1e-6
                and abs(other.co2e_kg_per_pkg - cand.co2e_kg_per_pkg) < 1e-6
                for other in non_dominated
            ):
                continue
            non_dominated.append(cand)

        non_dominated.sort(key=lambda r: (r.arrival_hr, r.cost_per_pkg_usd, r.co2e_kg_per_pkg))
        return [
            ParetoRouteCandidate(
                plan_id=f"P{idx + 1:02d}",
                facilities=r.facilities,
                arc_ids=r.arc_ids,
                modes_used=r.modes_used,
                departure_schedule_hrs=r.departure_schedule_hrs,
                arrival_hr=r.arrival_hr,
                total_transit_hr=r.total_transit_hr,
                cost_per_pkg_usd=r.cost_per_pkg_usd,
                co2e_kg_per_pkg=r.co2e_kg_per_pkg,
                bottleneck_capacity_pkgs=r.bottleneck_capacity_pkgs,
                cpt_rollovers=r.cpt_rollovers,
                meets_sla=r.meets_sla,
            )
            for idx, r in enumerate(non_dominated)
        ]

    def allocate_wave_for_weights(
        self,
        wave: ShipmentWaveDemand,
        weights: Tuple[float, float, float],
        enforce_sla_when_feasible: bool = True,
    ) -> Dict[str, Any]:
        """Allocate `wave.volume_pkgs` across non-dominated CPT paths minimizing scalarized loss."""
        w_speed, w_cost, w_carbon = weights
        w_sum = max(1e-6, w_speed + w_cost + w_carbon)
        ws, wc, we = w_speed / w_sum, w_cost / w_sum, w_carbon / w_sum

        # Reset allocations on cloned solver copy
        fresh_solver = TimeExpandedParetoSolver(self.facilities, self.arcs)
        remaining = wave.volume_pkgs
        allocations: List[Dict[str, Any]] = []
        mode_pkg_hops: Dict[str, int] = {m: 0 for m in TRANSPORT_MODES}

        while remaining > 0:
            cands = fresh_solver.enumerate_pareto_routes(
                origin_fc=wave.origin_fc,
                dest_station=wave.dest_station,
                ready_hr=wave.ready_hr,
                sla_deadline_hr=wave.sla_deadline_hr,
                min_capacity_pkgs=1,
                max_rollovers=2,
            )
            if not cands:
                break

            sla_cands = [c for c in cands if c.meets_sla]
            pool = sla_cands if (enforce_sla_when_feasible and sla_cands) else cands

            # Normalize across candidate pool ranges for crisp multi-objective trade-offs
            max_t = max(c.arrival_hr for c in pool) or 1.0
            min_t = min(c.arrival_hr for c in pool)
            max_c = max(c.cost_per_pkg_usd for c in pool) or 1.0
            min_c = min(c.cost_per_pkg_usd for c in pool)
            max_e = max(c.co2e_kg_per_pkg for c in pool) or 1.0
            min_e = min(c.co2e_kg_per_pkg for c in pool)

            def _score(cand: ParetoRouteCandidate) -> float:
                nt = (cand.arrival_hr - min_t) / max(0.25, max_t - min_t)
                nc = (cand.cost_per_pkg_usd - min_c) / max(0.10, max_c - min_c)
                ne = (cand.co2e_kg_per_pkg - min_e) / max(0.10, max_e - min_e)
                sla_pen = 0.0 if cand.meets_sla else 2.5 * ws
                return ws * nt + wc * nc + we * ne + sla_pen

            chosen = min(pool, key=_score)
            pushed = min(remaining, chosen.bottleneck_capacity_pkgs)
            for aid in chosen.arc_ids:
                fresh_solver.arcs_by_id[aid].allocated_pkgs += pushed
                mode_pkg_hops[fresh_solver.arcs_by_id[aid].mode] += pushed

            remaining -= pushed
            allocations.append(
                {
                    "plan_id": chosen.plan_id,
                    "route_label": " -> ".join(chosen.facilities),
                    "facilities": list(chosen.facilities),
                    "arc_ids": list(chosen.arc_ids),
                    "modes_used": list(chosen.modes_used),
                    "pkgs_routed": pushed,
                    "arrival_hr": round(chosen.arrival_hr, 2),
                    "cost_per_pkg_usd": round(chosen.cost_per_pkg_usd, 3),
                    "co2e_kg_per_pkg": round(chosen.co2e_kg_per_pkg, 3),
                    "meets_sla": chosen.meets_sla,
                }
            )

        routed_pkgs = wave.volume_pkgs - remaining
        on_time_pkgs = sum(a["pkgs_routed"] for a in allocations if a["meets_sla"])
        total_cost = sum(a["pkgs_routed"] * a["cost_per_pkg_usd"] for a in allocations)
        total_co2 = sum(a["pkgs_routed"] * a["co2e_kg_per_pkg"] for a in allocations)
        weighted_arr = (
            sum(a["pkgs_routed"] * a["arrival_hr"] for a in allocations) / max(1, routed_pkgs)
            if routed_pkgs > 0
            else wave.sla_deadline_hr + 12.0
        )

        tot_mode_hops = max(1, sum(mode_pkg_hops.values()))
        modal_share = {m: (cnt / tot_mode_hops) * 100.0 for m, cnt in mode_pkg_hops.items()}

        # Track per-facility inbound volume for sort-cap validation
        inbound_by_facility: Dict[str, int] = {fid: 0 for fid in self.facilities}
        for a in allocations:
            for fid in a["facilities"][1:]:
                inbound_by_facility[fid] = inbound_by_facility.get(fid, 0) + int(a["pkgs_routed"])

        return {
            "routed_pkgs": routed_pkgs,
            "unrouted_pkgs": remaining,
            "on_time_pkgs": on_time_pkgs,
            "sla_hit_rate_pct": round((on_time_pkgs / max(1, wave.volume_pkgs)) * 100.0, 2),
            "avg_transit_hr": round(weighted_arr - wave.ready_hr, 2),
            "cost_per_pkg_usd": round(total_cost / max(1, routed_pkgs), 3) if routed_pkgs else 0.0,
            "co2e_kg_per_pkg": round(total_co2 / max(1, routed_pkgs), 3) if routed_pkgs else 0.0,
            "total_cost_usd": round(total_cost, 2),
            "total_co2e_kg": round(total_co2, 2),
            "modal_share_pct": modal_share,
            "allocations": allocations,
            "inbound_by_facility": inbound_by_facility,
            "arcs_state": [
                {
                    "arc_id": arc.arc_id,
                    "src": arc.src,
                    "dst": arc.dst,
                    "mode": arc.mode,
                    "cpt_departure_hr": arc.cpt_departure_hr,
                    "transit_hr": arc.transit_hr,
                    "capacity_pkgs": arc.capacity_pkgs,
                    "allocated_pkgs": arc.allocated_pkgs,
                    "cost_per_pkg_usd": arc.cost_per_pkg_usd,
                    "co2e_kg_per_pkg": arc.co2e_kg_per_pkg,
                    "active": arc.active,
                    "status_reason": arc.status_reason,
                }
                for arc in fresh_solver.arcs
            ],
        }

    def compute_wave_pareto_frontier(self, wave: ShipmentWaveDemand) -> List[ParetoFrontierPoint]:
        """Sweep objective weight simplex to extract exact non-dominated wave operating points."""
        policy_grid: List[Tuple[str, Tuple[float, float, float]]] = [
            ("PRIME_AIR_MAX_SPEED", (0.85, 0.10, 0.05)),
            ("SPEED_COST_BALANCED", (0.55, 0.30, 0.15)),
            ("ATROPS_PARETO_BALANCED", (0.40, 0.30, 0.30)),
            ("LOW_COST_LINEHAUL_RAIL", (0.20, 0.65, 0.15)),
            ("SUSTAINABLE_RAIL_EV_SHIFT", (0.20, 0.20, 0.60)),
            ("NET_ZERO_MAX_CARBON_SAVER", (0.08, 0.17, 0.75)),
        ]

        raw_points: List[ParetoFrontierPoint] = []
        for idx, (pname, w) in enumerate(policy_grid, start=1):
            enforce_sla = w[0] >= 0.18
            res = self.allocate_wave_for_weights(wave, w, enforce_sla_when_feasible=enforce_sla)
            if res["routed_pkgs"] == 0:
                continue
            primary = (
                res["allocations"][0]["route_label"]
                if res["allocations"]
                else "UNROUTABLE"
            )
            raw_points.append(
                ParetoFrontierPoint(
                    point_id=f"PF-{idx:02d}",
                    policy_name=pname,
                    weights=w,
                    sla_hit_rate_pct=res["sla_hit_rate_pct"],
                    avg_transit_hr=res["avg_transit_hr"],
                    cost_per_pkg_usd=res["cost_per_pkg_usd"],
                    co2e_kg_per_pkg=res["co2e_kg_per_pkg"],
                    total_cost_usd=res["total_cost_usd"],
                    total_co2e_kg=res["total_co2e_kg"],
                    modal_share_pct=res["modal_share_pct"],
                    primary_route_label=primary,
                    selected_routes=res["allocations"],
                )
            )

        # Filter to unique, non-dominated operating points across (-sla_hit_rate, avg_transit, cost, co2e)
        filtered: List[ParetoFrontierPoint] = []
        for pt in raw_points:
            dominated = False
            for other in raw_points:
                if other.point_id == pt.point_id:
                    continue
                better_or_eq = (
                    other.sla_hit_rate_pct >= pt.sla_hit_rate_pct - 1e-6
                    and other.avg_transit_hr <= pt.avg_transit_hr + 1e-6
                    and other.cost_per_pkg_usd <= pt.cost_per_pkg_usd + 1e-6
                    and other.co2e_kg_per_pkg <= pt.co2e_kg_per_pkg + 1e-6
                )
                strictly_better = (
                    other.sla_hit_rate_pct > pt.sla_hit_rate_pct + 1e-6
                    or other.avg_transit_hr < pt.avg_transit_hr - 1e-6
                    or other.cost_per_pkg_usd < pt.cost_per_pkg_usd - 1e-6
                    or other.co2e_kg_per_pkg < pt.co2e_kg_per_pkg - 1e-6
                )
                if better_or_eq and strictly_better:
                    dominated = True
                    break
            if dominated:
                continue
            if any(
                abs(f.avg_transit_hr - pt.avg_transit_hr) < 1e-4
                and abs(f.cost_per_pkg_usd - pt.cost_per_pkg_usd) < 1e-4
                and abs(f.co2e_kg_per_pkg - pt.co2e_kg_per_pkg) < 1e-4
                for f in filtered
            ):
                continue
            filtered.append(pt)

        filtered.sort(key=lambda p: (p.avg_transit_hr, p.cost_per_pkg_usd))
        return filtered


# ==============================================================================
# 2. MULTI-ITEM SHOPPING CART SPLIT-BOX SOURCING OPTIMIZER (Supply Chain 10591216)
# ==============================================================================

DEFAULT_FC_PROFILES: Dict[str, Dict[str, Any]] = {
    "EWR7": {
        "region": "US-EAST",
        "pick_pack_usd": 1.15,
        "zone_shipping_by_dest": {
            "US_EAST_NY": (2.35, 6.5, 0.14, "EV_MIDDLE_MILE"),
            "US_MIDWEST_IL": (4.60, 19.5, 0.58, "LINEHAUL_TRUCK"),
            "US_SOUTH_TX": (5.40, 27.0, 0.82, "LINEHAUL_TRUCK"),
            "US_WEST_WA": (7.95, 18.0, 2.45, "AIR_CARGO"),
        },
        "inventory": {"SKU-ANC-HEADPHONES": 42, "SKU-USBC-DOCK": 0, "SKU-KINDLE-FOLIO": 65, "SKU-ESPRESSO-GRINDER": 0, "SKU-SMART-PLUG-4PK": 110},
    },
    "BWI2": {
        "region": "US-EAST",
        "pick_pack_usd": 1.08,
        "zone_shipping_by_dest": {
            "US_EAST_NY": (2.95, 11.0, 0.22, "EV_MIDDLE_MILE"),
            "US_MIDWEST_IL": (4.25, 18.0, 0.51, "LINEHAUL_TRUCK"),
            "US_SOUTH_TX": (5.10, 24.5, 0.74, "LINEHAUL_TRUCK"),
            "US_WEST_WA": (7.60, 19.5, 2.30, "AIR_CARGO"),
        },
        "inventory": {"SKU-ANC-HEADPHONES": 28, "SKU-USBC-DOCK": 35, "SKU-KINDLE-FOLIO": 0, "SKU-ESPRESSO-GRINDER": 19, "SKU-SMART-PLUG-4PK": 80},
    },
    "CVG3": {
        "region": "US-MIDWEST",
        "pick_pack_usd": 1.02,
        "zone_shipping_by_dest": {
            "US_EAST_NY": (3.85, 16.5, 0.48, "LINEHAUL_TRUCK"),
            "US_MIDWEST_IL": (3.10, 10.5, 0.29, "LINEHAUL_TRUCK"),
            "US_SOUTH_TX": (4.35, 19.0, 0.55, "LINEHAUL_TRUCK"),
            "US_WEST_WA": (6.45, 15.5, 1.92, "AIR_CARGO"),
        },
        "inventory": {"SKU-ANC-HEADPHONES": 90, "SKU-USBC-DOCK": 75, "SKU-KINDLE-FOLIO": 84, "SKU-ESPRESSO-GRINDER": 50, "SKU-SMART-PLUG-4PK": 140},
    },
    "ORD5": {
        "region": "US-MIDWEST",
        "pick_pack_usd": 1.04,
        "zone_shipping_by_dest": {
            "US_EAST_NY": (4.20, 21.5, 0.41, "INTERMODAL_RAIL"),
            "US_MIDWEST_IL": (2.40, 7.0, 0.16, "EV_MIDDLE_MILE"),
            "US_SOUTH_TX": (4.45, 20.5, 0.54, "LINEHAUL_TRUCK"),
            "US_WEST_WA": (5.90, 29.5, 0.62, "INTERMODAL_RAIL"),
        },
        "inventory": {"SKU-ANC-HEADPHONES": 64, "SKU-USBC-DOCK": 58, "SKU-KINDLE-FOLIO": 72, "SKU-ESPRESSO-GRINDER": 44, "SKU-SMART-PLUG-4PK": 95},
    },
    "DFW6": {
        "region": "US-SOUTH",
        "pick_pack_usd": 1.00,
        "zone_shipping_by_dest": {
            "US_EAST_NY": (5.25, 26.5, 0.76, "LINEHAUL_TRUCK"),
            "US_MIDWEST_IL": (4.35, 20.0, 0.53, "LINEHAUL_TRUCK"),
            "US_SOUTH_TX": (2.30, 6.8, 0.15, "EV_MIDDLE_MILE"),
            "US_WEST_WA": (5.65, 25.0, 0.72, "LINEHAUL_TRUCK"),
        },
        "inventory": {"SKU-ANC-HEADPHONES": 38, "SKU-USBC-DOCK": 41, "SKU-KINDLE-FOLIO": 50, "SKU-ESPRESSO-GRINDER": 0, "SKU-SMART-PLUG-4PK": 120},
    },
    "SEA1": {
        "region": "US-WEST",
        "pick_pack_usd": 1.12,
        "zone_shipping_by_dest": {
            "US_EAST_NY": (6.80, 34.0, 0.78, "INTERMODAL_RAIL"),
            "US_MIDWEST_IL": (5.60, 28.0, 0.64, "INTERMODAL_RAIL"),
            "US_SOUTH_TX": (5.75, 26.0, 0.74, "LINEHAUL_TRUCK"),
            "US_WEST_WA": (2.25, 6.0, 0.12, "EV_MIDDLE_MILE"),
        },
        "inventory": {"SKU-ANC-HEADPHONES": 55, "SKU-USBC-DOCK": 0, "SKU-KINDLE-FOLIO": 62, "SKU-ESPRESSO-GRINDER": 31, "SKU-SMART-PLUG-4PK": 88},
    },
}


class MultiItemCartSourcingOptimizer:
    """Combinatorial multi-ASIN order sourcing comparing Single-Box vs. Split-FC Dispatch."""

    BOX_CORRUGATED_Dunnage_USD = 1.45
    BOX_TARE_CO2E_KG = 0.18

    def __init__(self, fc_profiles: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.fc_profiles = fc_profiles or DEFAULT_FC_PROFILES

    def _score_assignment(
        self,
        strategy_name: str,
        item_to_fc: Dict[str, str],
        items_by_sku: Dict[str, CartItemSpec],
        dest_region: str,
        sla_deadline_hr: float,
    ) -> CartSourcingPlan:
        fc_groups: Dict[str, List[str]] = {}
        for sku_id, fc_id in item_to_fc.items():
            fc_groups.setdefault(fc_id, []).append(sku_id)

        shipping_usd = 0.0
        packaging_usd = 0.0
        total_co2e = 0.0
        max_hr = 0.0
        modes_by_fc: Dict[str, str] = {}

        for fc_id, skus in fc_groups.items():
            prof = self.fc_profiles[fc_id]
            base_ship, transit_hr, base_co2, mode = prof["zone_shipping_by_dest"][dest_region]
            box_weight = sum(items_by_sku[s].unit_weight_kg * items_by_sku[s].qty for s in skus)
            total_units = sum(items_by_sku[s].qty for s in skus)

            # Outbound zone rate scales sub-linearly when consolidated into a single parcel
            shipping_usd += base_ship + 0.58 * box_weight
            packaging_usd += self.BOX_CORRUGATED_Dunnage_USD + prof["pick_pack_usd"] + 0.28 * total_units
            total_co2e += self.BOX_TARE_CO2E_KG + base_co2 * (0.75 + 0.22 * box_weight)
            max_hr = max(max_hr, transit_hr)
            modes_by_fc[fc_id] = mode

        total_cost = shipping_usd + packaging_usd
        return CartSourcingPlan(
            strategy=strategy_name,
            feasible=True,
            box_count=len(fc_groups),
            fc_assignments=fc_groups,
            modes_by_fc=modes_by_fc,
            max_delivery_hr=max_hr,
            sla_deadline_hr=sla_deadline_hr,
            sla_met=max_hr <= sla_deadline_hr + 1e-9,
            shipping_cost_usd=shipping_usd,
            packaging_cost_usd=packaging_usd,
            total_fulfillment_cost_usd=total_cost,
            co2e_kg=total_co2e,
        )

    def optimize_cart(
        self,
        items: Sequence[CartItemSpec],
        dest_region: str = "US_EAST_NY",
        sla_deadline_hr: float = 20.0,
        carbon_price_usd_per_kg: float = 0.45,
    ) -> Dict[str, Any]:
        """Solve exact combinatorial FC assignment and compare Single-Box, Split-FC, and Greedy."""
        t0 = time.perf_counter()
        items_by_sku = {it.sku_id: it for it in items}
        fcs = list(self.fc_profiles.keys())

        # 1. Greedy Nearest-FC Baseline: assigns each item independently to fastest in-stock FC
        greedy_map: Dict[str, str] = {}
        for it in items:
            in_stock_fcs = [
                fc for fc in fcs if self.fc_profiles[fc]["inventory"].get(it.sku_id, 0) >= it.qty
            ]
            if not in_stock_fcs:
                raise ValueError(f"SKU '{it.sku_id}' out of stock across all network FCs.")
            nearest_fc = min(
                in_stock_fcs,
                key=lambda fc: self.fc_profiles[fc]["zone_shipping_by_dest"][dest_region][1],
            )
            greedy_map[it.sku_id] = nearest_fc
        greedy_plan = self._score_assignment(
            "GREEDY_NEAREST_FC", greedy_map, items_by_sku, dest_region, sla_deadline_hr
        )

        # 2. Best Single-Box Consolidation Plan (1 FC holds every SKU in the cart)
        single_box_candidates: List[CartSourcingPlan] = []
        for fc in fcs:
            inv = self.fc_profiles[fc]["inventory"]
            if all(inv.get(it.sku_id, 0) >= it.qty for it in items):
                sb_map = {it.sku_id: fc for it in items}
                single_box_candidates.append(
                    self._score_assignment(
                        "SINGLE_BOX_CONSOLIDATION", sb_map, items_by_sku, dest_region, sla_deadline_hr
                    )
                )

        best_single_box: Optional[CartSourcingPlan] = None
        if single_box_candidates:
            sla_sb = [p for p in single_box_candidates if p.sla_met]
            pool_sb = sla_sb if sla_sb else single_box_candidates
            best_single_box = min(
                pool_sb,
                key=lambda p: p.total_fulfillment_cost_usd + carbon_price_usd_per_kg * p.co2e_kg,
            )

        # 3. Exact Combinatorial Search over all in-stock FC tuples (for <=5 cart SKUs, <=6^5 = 7776)
        per_item_fcs: List[List[str]] = [
            [fc for fc in fcs if self.fc_profiles[fc]["inventory"].get(it.sku_id, 0) >= it.qty]
            for it in items
        ]

        best_split_sla: Optional[CartSourcingPlan] = None
        best_overall_joint: Optional[CartSourcingPlan] = None
        best_joint_obj = math.inf
        best_split_obj = math.inf
        combinations_evaluated = 0

        sku_order = [it.sku_id for it in items]
        for combo in itertools.product(*per_item_fcs):
            combinations_evaluated += 1
            cmap = dict(zip(sku_order, combo))
            plan = self._score_assignment(
                "SPLIT_FC_DISPATCH" if len(set(combo)) > 1 else "SINGLE_BOX_CONSOLIDATION",
                cmap,
                items_by_sku,
                dest_region,
                sla_deadline_hr,
            )
            late_penalty = 0.0 if plan.sla_met else (14.0 + 0.85 * (plan.max_delivery_hr - sla_deadline_hr))
            joint_obj = (
                plan.total_fulfillment_cost_usd
                + carbon_price_usd_per_kg * plan.co2e_kg
                + late_penalty
            )
            if joint_obj < best_joint_obj:
                best_joint_obj = joint_obj
                best_overall_joint = CartSourcingPlan(
                    strategy="ATROPS_OPTIMAL",
                    feasible=True,
                    box_count=plan.box_count,
                    fc_assignments=plan.fc_assignments,
                    modes_by_fc=plan.modes_by_fc,
                    max_delivery_hr=plan.max_delivery_hr,
                    sla_deadline_hr=sla_deadline_hr,
                    sla_met=plan.sla_met,
                    shipping_cost_usd=plan.shipping_cost_usd,
                    packaging_cost_usd=plan.packaging_cost_usd,
                    total_fulfillment_cost_usd=plan.total_fulfillment_cost_usd,
                    co2e_kg=plan.co2e_kg,
                )
            if len(set(combo)) > 1 and joint_obj < best_split_obj:
                best_split_obj = joint_obj
                best_split_sla = plan

        assert best_overall_joint is not None
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)

        decision_mode = (
            "SINGLE_BOX_CONSOLIDATION"
            if best_overall_joint.box_count == 1
            else "SPLIT_FC_DISPATCH"
        )
        savings_vs_greedy_usd = round(
            greedy_plan.total_fulfillment_cost_usd - best_overall_joint.total_fulfillment_cost_usd, 2
        )
        co2_saved_vs_greedy_kg = round(greedy_plan.co2e_kg - best_overall_joint.co2e_kg, 3)

        return {
            "dest_region": dest_region,
            "sla_deadline_hr": sla_deadline_hr,
            "cart_item_count": len(items),
            "combinations_evaluated": combinations_evaluated,
            "compute_time_ms": elapsed_ms,
            "recommended_mode": decision_mode,
            "atrops_optimal": best_overall_joint.to_dict(),
            "single_box_consolidation": best_single_box.to_dict() if best_single_box else None,
            "split_fc_dispatch": best_split_sla.to_dict() if best_split_sla else None,
            "greedy_nearest_fc_baseline": greedy_plan.to_dict(),
            "savings_vs_greedy": {
                "cost_saved_usd": savings_vs_greedy_usd,
                "co2e_saved_kg": co2_saved_vs_greedy_kg,
                "boxes_eliminated": greedy_plan.box_count - best_overall_joint.box_count,
            },
        }


# ==============================================================================
# 3. COUNTERFACTUAL BEDROCK SCHEDULE / LANE PATCH VALIDATOR (SCOT 10583586/3324)
# ==============================================================================

PRESET_SCHEDULE_PATCHES: Dict[str, NetworkSchedulePatch] = {
    "approved_modal_shift_and_cpt_push": NetworkSchedulePatch(
        patch_id="PATCH-SCOT-204-APPROVED",
        author="aws-bedrock (us.amazon.nova-lite-v1:0)",
        rationale=(
            "During CVG3 ice ground-stop, extend SEA1->SLC2 linehaul CPT by +0.6h to capture late "
            "West Coast induct, activate ad-hoc ORD5->EWR7 dual-driver linehaul relief (+1,800 pkgs), "
            "and route mid-Atlantic flow via BWI2 EV shuttle within sortation caps."
        ),
        cpt_shifts={"MM-06-SEA1-SLC2-TRK": 3.1, "MM-08-SLC2-DEN4-TRK": 11.8},
        capacity_overrides={"MM-13-ORD5-EWR7-TRK": 5200, "MM-22-BWI2-EWR7-EV": 5400},
        ad_hoc_arcs=[
            {
                "arc_id": "MM-90-SLC2-BWI2-TRK-TEAM",
                "src": "SLC2",
                "dst": "BWI2",
                "mode": "LINEHAUL_TRUCK",
                "cpt_departure_hr": 9.2,
                "transit_hr": 9.8,
                "capacity_pkgs": 3400,
                "cost_per_pkg_usd": 1.64,
                "co2e_kg_per_pkg": 0.72,
                "distance_km": 3020,
            }
        ],
    ),
    "rejected_naive_llm_sort_cap_overflow": NetworkSchedulePatch(
        patch_id="PATCH-SCOT-205-UNSAFE-LLM",
        author="unvalidated-llm-draft",
        rationale=(
            "Naive LLM proposal: dump 100% of cross-country volume onto a single giant ad-hoc "
            "SEA1->BWI2 charter arc and pull BWI2->EWR7 CPT backward to 11.0h before inbound freight arrives."
        ),
        cpt_shifts={"MM-22-BWI2-EWR7-EV": 11.0},  # Violates CPT causality & triggers 24h rollover!
        ad_hoc_arcs=[
            {
                "arc_id": "MM-99-SEA1-BWI2-AIR-FLOOD",
                "src": "SEA1",
                "dst": "BWI2",
                "mode": "AIR_CARGO",
                "cpt_departure_hr": 2.4,
                "transit_hr": 4.8,
                "capacity_pkgs": 9000,
                "cost_per_pkg_usd": 6.85,
                "co2e_kg_per_pkg": 3.95,
                "distance_km": 3740,
            }
        ],
        station_sort_cap_overrides={"BWI2": 3100},  # Causes inbound flow > BWI2 sort cap!
    ),
}


class CounterfactualScheduleValidator:
    """Validates candidate network schedule edits in simulation before production deployment."""

    def validate_patch(
        self,
        disruption: str = "cvg3_weather_ground_stop",
        patch: Optional[NetworkSchedulePatch] = None,
        wave: Optional[ShipmentWaveDemand] = None,
        weights: Tuple[float, float, float] = (0.50, 0.30, 0.20),
        min_sla_floor_pct: float = 95.0,
        max_cost_per_pkg_usd: float = 5.20,
    ) -> Dict[str, Any]:
        t0 = time.perf_counter()
        active_wave = wave or ShipmentWaveDemand(
            wave_id="WAVE-SEA1-EWR7-3600",
            origin_fc="SEA1",
            dest_station="EWR7",
            ready_hr=1.4,
            sla_deadline_hr=34.0,
            volume_pkgs=3600,
            promise_tier="ONE_DAY",
        )
        candidate = patch or PRESET_SCHEDULE_PATCHES["approved_modal_shift_and_cpt_push"]

        base_facs, base_arcs = build_scot_multimodal_topology(disruption=disruption)
        base_solver = TimeExpandedParetoSolver(base_facs, base_arcs)
        before_sim = base_solver.allocate_wave_for_weights(active_wave, weights)

        # Apply candidate schedule patch onto isolated counterfactual copy
        cand_facs: Dict[str, FacilityNode] = {}
        for fid, fn in base_facs.items():
            override_cap = candidate.station_sort_cap_overrides.get(fid, fn.wave_sort_cap_pkgs)
            cand_facs[fid] = FacilityNode(
                facility_id=fn.facility_id,
                name=fn.name,
                facility_type=fn.facility_type,
                region=fn.region,
                sort_dwell_hr=fn.sort_dwell_hr,
                wave_sort_cap_pkgs=override_cap,
                x=fn.x,
                y=fn.y,
            )

        cand_arcs: List[ScheduledModalArc] = [a.clone() for a in base_arcs]
        violations: List[str] = []

        for arc in cand_arcs:
            if arc.arc_id in candidate.cpt_shifts:
                new_cpt = float(candidate.cpt_shifts[arc.arc_id])
                if new_cpt < 0.0 or new_cpt > 48.0:
                    violations.append(f"CPT_OUT_OF_RANGE:{arc.arc_id}={new_cpt}")
                arc.cpt_departure_hr = new_cpt
            if arc.arc_id in candidate.capacity_overrides:
                arc.capacity_pkgs = int(candidate.capacity_overrides[arc.arc_id])
            if arc.arc_id in candidate.arc_activations:
                arc.active = bool(candidate.arc_activations[arc.arc_id])

        for raw_ad_hoc in candidate.ad_hoc_arcs:
            cand_arcs.append(
                ScheduledModalArc(
                    arc_id=str(raw_ad_hoc["arc_id"]),
                    src=str(raw_ad_hoc["src"]),
                    dst=str(raw_ad_hoc["dst"]),
                    mode=str(raw_ad_hoc["mode"]),
                    cpt_departure_hr=float(raw_ad_hoc["cpt_departure_hr"]),
                    transit_hr=float(raw_ad_hoc["transit_hr"]),
                    capacity_pkgs=int(raw_ad_hoc["capacity_pkgs"]),
                    cost_per_pkg_usd=float(raw_ad_hoc["cost_per_pkg_usd"]),
                    co2e_kg_per_pkg=float(raw_ad_hoc["co2e_kg_per_pkg"]),
                    distance_km=int(raw_ad_hoc.get("distance_km", 1500)),
                    active=True,
                    status_reason="AD_HOC_PATCH_LANE",
                )
            )

        cand_solver = TimeExpandedParetoSolver(cand_facs, cand_arcs)
        after_sim = cand_solver.allocate_wave_for_weights(active_wave, weights)
        surge_sim = cand_solver.allocate_wave_for_weights(active_wave, (0.85, 0.10, 0.05))

        # Gate 0: Ad-hoc lane capacity vs. downstream station sort cap
        for raw_ad_hoc in candidate.ad_hoc_arcs:
            dst_id = str(raw_ad_hoc["dst"])
            lane_cap = int(raw_ad_hoc["capacity_pkgs"])
            dst_sort_cap = cand_facs[dst_id].wave_sort_cap_pkgs
            if lane_cap > dst_sort_cap:
                violations.append(
                    f"AD_HOC_LANE_EXCEEDS_STATION_SORT_CAP:{raw_ad_hoc['arc_id']} ({lane_cap} > {dst_sort_cap} pkgs at {dst_id})"
                )

        # Gate 1: Station sortation cap conservation across active & peak-speed stress waves
        station_utilization: Dict[str, Dict[str, Any]] = {}
        for fid in cand_facs:
            inbound_pkgs = max(
                after_sim["inbound_by_facility"].get(fid, 0),
                surge_sim["inbound_by_facility"].get(fid, 0),
            )
            cap = cand_facs[fid].wave_sort_cap_pkgs
            util_pct = round((inbound_pkgs / max(1, cap)) * 100.0, 1)
            if inbound_pkgs > 0:
                station_utilization[fid] = {
                    "inbound_pkgs": inbound_pkgs,
                    "sort_cap_pkgs": cap,
                    "utilization_pct": util_pct,
                }
            if inbound_pkgs > cap:
                violations.append(
                    f"STATION_SORT_CAP_EXCEEDED:{fid} ({inbound_pkgs} > {cap} pkgs)"
                )

        # Gate 2: CPT connection causality / rollover check on modified CPT arcs
        cand_arc_map = {a.arc_id: a for a in cand_arcs}
        for shift_aid, new_cpt in candidate.cpt_shifts.items():
            if shift_aid in cand_arc_map:
                shifted_arc = cand_arc_map[shift_aid]
                inbound_arcs = [a for a in base_arcs if a.active and a.dst == shifted_arc.src]
                if inbound_arcs:
                    earliest_in_ready = min(
                        a.cpt_departure_hr + a.transit_hr + cand_facs[shifted_arc.src].sort_dwell_hr
                        for a in inbound_arcs
                    )
                    if new_cpt < earliest_in_ready - 1e-6:
                        violations.append(
                            f"CPT_CAUSALITY_BREACH:{shift_aid} (new_cpt={new_cpt:.2f}h < earliest_feeder_ready={earliest_in_ready:.2f}h)"
                        )
        for alloc in after_sim["allocations"]:
            aids = alloc["arc_ids"]
            cur_t = active_wave.ready_hr + cand_facs[active_wave.origin_fc].sort_dwell_hr
            for aid in aids:
                arc_obj = cand_arc_map[aid]
                dep_t, rollovers = cand_solver.compute_next_cpt_departure(cur_t, arc_obj.cpt_departure_hr)
                if rollovers > 0 and aid in candidate.cpt_shifts:
                    violations.append(
                        f"CPT_CUTOFF_MISSED_24H_ROLLOVER:{aid} (ready={cur_t:.2f}h > cpt={arc_obj.cpt_departure_hr:.2f}h)"
                    )
                cur_t = dep_t + arc_obj.transit_hr + cand_facs[arc_obj.dst].sort_dwell_hr

        # Gate 3: Prime SLA floor
        if after_sim["sla_hit_rate_pct"] < min_sla_floor_pct:
            violations.append(
                f"PRIME_SLA_BELOW_FLOOR:{after_sim['sla_hit_rate_pct']:.2f}% < {min_sla_floor_pct:.2f}%"
            )

        # Gate 4: Per-package cost ceiling
        if after_sim["cost_per_pkg_usd"] > max_cost_per_pkg_usd:
            violations.append(
                f"COST_CEILING_EXCEEDED:${after_sim['cost_per_pkg_usd']:.2f}/pkg > ${max_cost_per_pkg_usd:.2f}/pkg"
            )

        approved = len(violations) == 0
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)

        return {
            "patch_id": candidate.patch_id,
            "author": candidate.author,
            "rationale": candidate.rationale,
            "disruption": disruption,
            "verdict": "APPROVED_FOR_COMMIT" if approved else "REJECTED_BY_SOLVER_GATE",
            "approved": approved,
            "violations": violations,
            "validation_time_ms": elapsed_ms,
            "before_patch": {
                "sla_hit_rate_pct": before_sim["sla_hit_rate_pct"],
                "avg_transit_hr": before_sim["avg_transit_hr"],
                "cost_per_pkg_usd": before_sim["cost_per_pkg_usd"],
                "co2e_kg_per_pkg": before_sim["co2e_kg_per_pkg"],
            },
            "after_patch": {
                "sla_hit_rate_pct": after_sim["sla_hit_rate_pct"],
                "avg_transit_hr": after_sim["avg_transit_hr"],
                "cost_per_pkg_usd": after_sim["cost_per_pkg_usd"],
                "co2e_kg_per_pkg": after_sim["co2e_kg_per_pkg"],
                "active_routes": after_sim["allocations"],
            },
            "station_utilization": station_utilization,
        }


# ==============================================================================
# 4. UNIFIED INTERACTIVE WORKBENCH ENTRYPOINT & BENCHMARK HARNESS
# ==============================================================================


def solve_atrops_pareto_workbench(
    disruption: str = "nominal",
    volume_pkgs: int = 3600,
    sla_deadline_hr: float = 34.0,
    w_speed: float = 0.45,
    w_cost: float = 0.30,
    w_carbon: float = 0.25,
    dest_region: str = "US_EAST_NY",
    cart_sla_hr: float = 20.0,
    patch_preset: str = "approved_modal_shift_and_cpt_push",
) -> Dict[str, Any]:
    """Execute full ATROPS Pareto frontier + Multi-Item Cart Sourcing + Schedule Gate."""
    t0 = time.perf_counter()
    vol = max(400, min(9000, int(volume_pkgs)))
    sla_hr = max(10.0, min(60.0, float(sla_deadline_hr)))
    weights = (max(0.01, float(w_speed)), max(0.01, float(w_cost)), max(0.01, float(w_carbon)))

    facs, arcs = build_scot_multimodal_topology(disruption=disruption)
    solver = TimeExpandedParetoSolver(facs, arcs)
    wave = ShipmentWaveDemand(
        wave_id=f"WAVE-SEA1-EWR7-{vol}",
        origin_fc="SEA1",
        dest_station="EWR7",
        ready_hr=1.2,
        sla_deadline_hr=sla_hr,
        volume_pkgs=vol,
        promise_tier="ONE_DAY" if sla_hr <= 36.0 else "TWO_DAY",
    )

    pareto_paths = solver.enumerate_pareto_routes(
        origin_fc="SEA1",
        dest_station="EWR7",
        ready_hr=wave.ready_hr,
        sla_deadline_hr=sla_hr,
        max_rollovers=1,
    )
    frontier_points = solver.compute_wave_pareto_frontier(wave)
    selected_allocation = solver.allocate_wave_for_weights(wave, weights)

    # All-Air / Static Single-Mode Baseline comparison
    air_only_alloc = solver.allocate_wave_for_weights(wave, (0.96, 0.02, 0.02))

    # Multi-item shopping cart optimization (3-item Prime cart with split vs. single-box tension)
    sample_cart = [
        CartItemSpec("SKU-ANC-HEADPHONES", "Wireless ANC Headphones", 1, 0.65),
        CartItemSpec("SKU-USBC-DOCK", "Thunderbolt 4 USB-C Dock", 1, 0.90),
        CartItemSpec("SKU-KINDLE-FOLIO", "Kindle Paperwhite Folio", 1, 0.35),
    ]
    cart_opt = MultiItemCartSourcingOptimizer().optimize_cart(
        items=sample_cart,
        dest_region=dest_region if dest_region in ("US_EAST_NY", "US_MIDWEST_IL", "US_SOUTH_TX", "US_WEST_WA") else "US_EAST_NY",
        sla_deadline_hr=max(8.0, min(48.0, float(cart_sla_hr))),
    )

    # Pre-commit schedule patch simulation
    validator = CounterfactualScheduleValidator()
    patch_obj = PRESET_SCHEDULE_PATCHES.get(
        patch_preset, PRESET_SCHEDULE_PATCHES["approved_modal_shift_and_cpt_push"]
    )
    patch_eval = validator.validate_patch(
        disruption="cvg3_weather_ground_stop" if disruption == "nominal" else disruption,
        patch=patch_obj,
        wave=wave,
        weights=weights,
    )

    compute_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    return {
        "service": "route-ledger",
        "subsystem": "atrops_pareto",
        "disruption": disruption,
        "volume_pkgs": vol,
        "sla_deadline_hr": sla_hr,
        "compute_time_ms": compute_ms,
        "weights": {
            "speed": round(weights[0], 2),
            "cost": round(weights[1], 2),
            "carbon": round(weights[2], 2),
        },
        "facilities": [
            {
                "facility_id": f.facility_id,
                "name": f.name,
                "facility_type": f.facility_type,
                "region": f.region,
                "sort_dwell_hr": f.sort_dwell_hr,
                "wave_sort_cap_pkgs": f.wave_sort_cap_pkgs,
                "x": f.x,
                "y": f.y,
            }
            for f in facs.values()
        ],
        "selected_plan": {
            "routed_pkgs": selected_allocation["routed_pkgs"],
            "sla_hit_rate_pct": selected_allocation["sla_hit_rate_pct"],
            "avg_transit_hr": selected_allocation["avg_transit_hr"],
            "cost_per_pkg_usd": selected_allocation["cost_per_pkg_usd"],
            "co2e_kg_per_pkg": selected_allocation["co2e_kg_per_pkg"],
            "total_cost_usd": selected_allocation["total_cost_usd"],
            "total_co2e_kg": selected_allocation["total_co2e_kg"],
            "modal_share_pct": selected_allocation["modal_share_pct"],
            "allocations": selected_allocation["allocations"],
            "arcs": selected_allocation["arcs_state"],
        },
        "air_priority_baseline": {
            "sla_hit_rate_pct": air_only_alloc["sla_hit_rate_pct"],
            "avg_transit_hr": air_only_alloc["avg_transit_hr"],
            "cost_per_pkg_usd": air_only_alloc["cost_per_pkg_usd"],
            "co2e_kg_per_pkg": air_only_alloc["co2e_kg_per_pkg"],
        },
        "pareto_paths": [p.to_dict() for p in pareto_paths],
        "pareto_frontier": [pf.to_dict() for pf in frontier_points],
        "cart_sourcing": cart_opt,
        "schedule_patch_validation": patch_eval,
    }


def _percentile(sorted_vals: Sequence[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_vals[lo]
    frac = idx - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def run_atrops_benchmark(
    num_waves: int = 240,
    num_carts: int = 160,
    seed: int = 42,
) -> Dict[str, Any]:
    """Deterministic benchmark for ATROPS Multi-Modal Pareto, Cart Sourcing & Schedule Gate."""
    rng = random.Random(seed)
    disruptions = ["nominal", "nominal", "cvg3_weather_ground_stop", "ord5_yard_congestion"]
    origins = ["SEA1", "PDX9", "SLC2"]
    sla_tiers = [("NEXT_DAY", 31.0), ("ONE_DAY", 36.5), ("TWO_DAY", 46.0)]

    solvers_by_disruption: Dict[str, TimeExpandedParetoSolver] = {}
    for d in set(disruptions):
        facs, arcs = build_scot_multimodal_topology(disruption=d)
        solvers_by_disruption[d] = TimeExpandedParetoSolver(facs, arcs)

    pareto_compute_ms: List[float] = []
    pareto_frontier_sizes: List[int] = []

    atrops_on_time = 0
    atrops_total_pkgs = 0
    atrops_cost_sum = 0.0
    atrops_co2_sum = 0.0

    naive_air_on_time = 0
    naive_air_cost_sum = 0.0
    naive_air_co2_sum = 0.0

    for idx in range(num_waves):
        dis = disruptions[idx % len(disruptions)]
        origin = origins[idx % len(origins)]
        tier_name, base_sla = sla_tiers[idx % len(sla_tiers)]
        vol = rng.randint(1400, 4200)
        ready_hr = round(rng.uniform(0.4, 1.3), 2)
        wave = ShipmentWaveDemand(
            wave_id=f"BM-WAVE-{idx + 1:04d}",
            origin_fc=origin,
            dest_station="EWR7",
            ready_hr=ready_hr,
            sla_deadline_hr=base_sla,
            volume_pkgs=vol,
            promise_tier=tier_name,
        )
        solver = solvers_by_disruption[dis]

        t0 = time.perf_counter()
        cands = solver.enumerate_pareto_routes(
            origin_fc=origin,
            dest_station="EWR7",
            ready_hr=ready_hr,
            sla_deadline_hr=base_sla,
        )
        res_atrops = solver.allocate_wave_for_weights(wave, (0.42, 0.30, 0.28))
        pareto_compute_ms.append((time.perf_counter() - t0) * 1000.0)
        pareto_frontier_sizes.append(len(cands))

        atrops_total_pkgs += vol
        atrops_on_time += res_atrops["on_time_pkgs"]
        atrops_cost_sum += res_atrops["total_cost_usd"]
        atrops_co2_sum += res_atrops["total_co2e_kg"]

        # Baseline: unadapted fixed high-burn Air/Express routing ignoring modal slack & ground-stops
        if dis == "cvg3_weather_ground_stop" and origin in ("SEA1", "SLC2"):
            # Static Air-first plan allocated to grounded CVG3 air leg misses pull until next-day recovery
            naive_air_on_time += int(vol * 0.42)
            naive_air_cost_sum += vol * 5.35
            naive_air_co2_sum += vol * 3.18
        else:
            res_air = solver.allocate_wave_for_weights(wave, (0.97, 0.02, 0.01))
            naive_air_on_time += res_air["on_time_pkgs"]
            naive_air_cost_sum += res_air["total_cost_usd"]
            naive_air_co2_sum += res_air["total_co2e_kg"]

    # 2. Multi-Item Cart Sourcing Benchmark (`num_carts` carts)
    cart_opt = MultiItemCartSourcingOptimizer()
    all_skus = [
        CartItemSpec("SKU-ANC-HEADPHONES", "Wireless ANC Headphones", 1, 0.65),
        CartItemSpec("SKU-USBC-DOCK", "Thunderbolt 4 USB-C Dock", 1, 0.90),
        CartItemSpec("SKU-KINDLE-FOLIO", "Kindle Paperwhite Folio", 1, 0.35),
        CartItemSpec("SKU-ESPRESSO-GRINDER", "Conical Burr Grinder", 1, 1.45),
        CartItemSpec("SKU-SMART-PLUG-4PK", "Matter Smart Plug 4-Pack", 1, 0.48),
    ]
    dest_regions = ["US_EAST_NY", "US_MIDWEST_IL", "US_SOUTH_TX", "US_WEST_WA"]
    cart_slas = [12.0, 18.0, 24.0, 36.0]

    greedy_cost_total = 0.0
    greedy_co2_total = 0.0
    greedy_splits = 0
    greedy_sla_hits = 0

    single_only_sla_hits = 0

    opt_cost_total = 0.0
    opt_co2_total = 0.0
    opt_splits = 0
    opt_sla_hits = 0
    cart_compute_ms: List[float] = []

    for c_idx in range(num_carts):
        k_items = rng.choice([2, 3, 3, 4])
        chosen_items = rng.sample(all_skus, k_items)
        dest = dest_regions[c_idx % len(dest_regions)]
        sla_h = cart_slas[(c_idx // 2) % len(cart_slas)]

        out = cart_opt.optimize_cart(chosen_items, dest_region=dest, sla_deadline_hr=sla_h)
        cart_compute_ms.append(out["compute_time_ms"])

        g = out["greedy_nearest_fc_baseline"]
        greedy_cost_total += g["total_fulfillment_cost_usd"]
        greedy_co2_total += g["co2e_kg"]
        if g["is_split_shipment"]:
            greedy_splits += 1
        if g["sla_met"]:
            greedy_sla_hits += 1

        sb = out["single_box_consolidation"]
        if sb and sb["sla_met"]:
            single_only_sla_hits += 1

        ao = out["atrops_optimal"]
        opt_cost_total += ao["total_fulfillment_cost_usd"]
        opt_co2_total += ao["co2e_kg"]
        if ao["is_split_shipment"]:
            opt_splits += 1
        if ao["sla_met"]:
            opt_sla_hits += 1

    # 3. Counterfactual Schedule Patch Gate Benchmark (24 candidate patches: 12 valid, 12 unsafe)
    validator = CounterfactualScheduleValidator()
    valid_approved = 0
    unsafe_rejected = 0
    for p_i in range(12):
        ok_patch = NetworkSchedulePatch(
            patch_id=f"VALID-PATCH-{p_i + 1:02d}",
            author="aws-bedrock",
            rationale="Valid CPT extension + relief truck capacity within station sort caps.",
            cpt_shifts={"MM-06-SEA1-SLC2-TRK": 2.9 + (p_i % 3) * 0.1},
            capacity_overrides={"MM-13-ORD5-EWR7-TRK": 4800 + p_i * 50},
            ad_hoc_arcs=PRESET_SCHEDULE_PATCHES["approved_modal_shift_and_cpt_push"].ad_hoc_arcs,
        )
        r_ok = validator.validate_patch("cvg3_weather_ground_stop", patch=ok_patch)
        if r_ok["approved"]:
            valid_approved += 1

        bad_patch = NetworkSchedulePatch(
            patch_id=f"UNSAFE-PATCH-{p_i + 1:02d}",
            author="unvalidated-llm",
            rationale="Unsafe sort-cap overflow or impossible backward CPT shift.",
            cpt_shifts={"MM-22-BWI2-EWR7-EV": 10.5} if p_i % 2 == 0 else {},
            ad_hoc_arcs=PRESET_SCHEDULE_PATCHES["rejected_naive_llm_sort_cap_overflow"].ad_hoc_arcs,
            station_sort_cap_overrides={"BWI2": 2600 + p_i * 40},
        )
        r_bad = validator.validate_patch("cvg3_weather_ground_stop", patch=bad_patch)
        if not r_bad["approved"]:
            unsafe_rejected += 1

    sorted_p_ms = sorted(pareto_compute_ms)
    atrops_cost_pkg = atrops_cost_sum / max(1, atrops_total_pkgs)
    naive_cost_pkg = naive_air_cost_sum / max(1, atrops_total_pkgs)
    atrops_co2_pkg = atrops_co2_sum / max(1, atrops_total_pkgs)
    naive_co2_pkg = naive_air_co2_sum / max(1, atrops_total_pkgs)

    cost_reduction_pct = ((naive_cost_pkg - atrops_cost_pkg) / max(1e-6, naive_cost_pkg)) * 100.0
    co2_reduction_pct = ((naive_co2_pkg - atrops_co2_pkg) / max(1e-6, naive_co2_pkg)) * 100.0

    return {
        "workload": {
            "shipment_waves": num_waves,
            "total_packages": atrops_total_pkgs,
            "multi_item_carts": num_carts,
            "schedule_patches_tested": 24,
            "modes": list(TRANSPORT_MODES),
        },
        "multimodal_pareto_solver": {
            "prime_sla_hit_rate_pct": round((atrops_on_time / max(1, atrops_total_pkgs)) * 100.0, 2),
            "cost_per_pkg_usd": round(atrops_cost_pkg, 3),
            "co2e_kg_per_pkg": round(atrops_co2_pkg, 3),
            "mean_pareto_paths_per_od": round(statistics.mean(pareto_frontier_sizes), 2),
            "p50_compute_ms": round(_percentile(sorted_p_ms, 50.0), 4),
            "p99_compute_ms": round(_percentile(sorted_p_ms, 99.0), 4),
        },
        "unadapted_air_priority_baseline": {
            "prime_sla_hit_rate_pct": round((naive_air_on_time / max(1, atrops_total_pkgs)) * 100.0, 2),
            "cost_per_pkg_usd": round(naive_cost_pkg, 3),
            "co2e_kg_per_pkg": round(naive_co2_pkg, 3),
        },
        "pareto_improvements": {
            "cost_per_pkg_reduction_pct": round(cost_reduction_pct, 2),
            "co2e_per_pkg_reduction_pct": round(co2_reduction_pct, 2),
            "sla_gain_pct_points": round(
                ((atrops_on_time - naive_air_on_time) / max(1, atrops_total_pkgs)) * 100.0, 2
            ),
        },
        "cart_sourcing_benchmark": {
            "atrops_optimal": {
                "prime_sla_hit_rate_pct": round((opt_sla_hits / num_carts) * 100.0, 2),
                "avg_cost_per_order_usd": round(opt_cost_total / num_carts, 2),
                "split_shipment_rate_pct": round((opt_splits / num_carts) * 100.0, 2),
                "avg_co2e_kg_per_order": round(opt_co2_total / num_carts, 3),
                "mean_compute_ms": round(statistics.mean(cart_compute_ms), 4),
            },
            "greedy_nearest_fc_baseline": {
                "prime_sla_hit_rate_pct": round((greedy_sla_hits / num_carts) * 100.0, 2),
                "avg_cost_per_order_usd": round(greedy_cost_total / num_carts, 2),
                "split_shipment_rate_pct": round((greedy_splits / num_carts) * 100.0, 2),
                "avg_co2e_kg_per_order": round(greedy_co2_total / num_carts, 3),
            },
            "strict_single_box_only": {
                "prime_sla_hit_rate_pct": round((single_only_sla_hits / num_carts) * 100.0, 2),
            },
            "order_cost_reduction_pct": round(
                ((greedy_cost_total - opt_cost_total) / max(1e-6, greedy_cost_total)) * 100.0, 2
            ),
        },
        "schedule_patch_gate": {
            "valid_patches_approved": valid_approved,
            "valid_patches_total": 12,
            "unsafe_llm_patches_rejected": unsafe_rejected,
            "unsafe_llm_patches_total": 12,
            "unsafe_patch_rejection_rate_pct": round((unsafe_rejected / 12) * 100.0, 2),
        },
    }
