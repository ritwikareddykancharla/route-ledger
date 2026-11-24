"""Capacity-Aware Multi-Hop Link & Fulfillment Router (`route_ledger/network_router.py`).

Models a dynamic 28-node hybrid LEO satellite constellation, regional ground gateways, and
edge POP / fulfillment sortation terminals with:
  - Per-link nominal bandwidth capacity, base propagation delay, SNR (dB), and active load
  - Convex M/M/1-style congestion queuing delay and oversubscription tail-drop loss
  - Mid-stream weather attenuation (Ka-band rain fade) and hard optical/fiber link outages

Compares:
  1. `StaticShortestPathRouter`: Baseline OSPF-style static base-latency Dijkstra router that
     ignores real-time link saturation, SNR degradation, and mid-epoch outages.
  2. `CapacityAwareRouter`: Constrained Dijkstra + 2-way residual flow-splitting router that
     prunes outage/low-SNR/saturated links and routes within hop and end-to-end latency SLAs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import heapq
import math
import random
import statistics
import time
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class LinkState:
    """Directed network or fulfillment transfer link with dynamic capacity and SNR state."""

    link_id: str
    src: str
    dst: str
    link_type: str  # ACCESS_FIBER | KA_FEEDER | OPTICAL_ISL | TERRESTRIAL_BACKHAUL
    nominal_capacity_mbps: float
    base_latency_ms: float
    snr_db: float = 25.0
    capacity_factor: float = 1.0
    noise_penalty_ms: float = 0.0
    outage: bool = False
    allocated_mbps: float = 0.0
    degradation_reason: Optional[str] = None

    @property
    def effective_capacity_mbps(self) -> float:
        if self.outage or self.capacity_factor <= 0.0:
            return 0.0
        return self.nominal_capacity_mbps * self.capacity_factor

    @property
    def residual_mbps(self) -> float:
        return max(0.0, self.effective_capacity_mbps - self.allocated_mbps)

    @property
    def utilization(self) -> float:
        eff = self.effective_capacity_mbps
        if eff <= 0.0:
            return math.inf if self.allocated_mbps > 0.0 else 1.0
        return self.allocated_mbps / eff

    def projected_hop_latency_ms(self, additional_mbps: float = 0.0) -> float:
        """Compute propagation + ARQ noise + queuing delay for `allocated_mbps + additional_mbps`."""
        eff = self.effective_capacity_mbps
        if eff <= 0.0:
            return 250.0  # Hard link outage / retransmission timeout penalty

        post_load = max(0.0, self.allocated_mbps + additional_mbps)
        u = post_load / eff
        if u < 0.92:
            queue_ms = 2.4 * (u / max(0.08, 1.0 - u))
        elif u <= 1.0:
            queue_ms = 27.6 + (u - 0.92) * 380.0
        else:
            # Finite modem/router ring buffer saturates before tail-drop kicks in
            queue_ms = min(185.0, 58.0 + ((u - 1.0) ** 1.25) * 190.0)

        return self.base_latency_ms + self.noise_penalty_ms + queue_ms

    def projected_drop_fraction(self, additional_mbps: float = 0.0) -> float:
        """Fraction of traffic lost to hard outage or tail-drop oversubscription on this link."""
        eff = self.effective_capacity_mbps
        post_load = self.allocated_mbps + additional_mbps
        if post_load <= 0.0:
            return 0.0
        if eff <= 0.0:
            return 1.0
        if post_load <= eff:
            return 0.0
        return min(1.0, (post_load - eff) / post_load)

    def clone_fresh(self) -> "LinkState":
        return LinkState(
            link_id=self.link_id,
            src=self.src,
            dst=self.dst,
            link_type=self.link_type,
            nominal_capacity_mbps=self.nominal_capacity_mbps,
            base_latency_ms=self.base_latency_ms,
            snr_db=self.snr_db,
            capacity_factor=self.capacity_factor,
            noise_penalty_ms=self.noise_penalty_ms,
            outage=self.outage,
            allocated_mbps=0.0,
            degradation_reason=self.degradation_reason,
        )


@dataclass(frozen=True)
class TrafficDemand:
    """Point-to-point bandwidth or fulfillment transfer demand with hop and latency SLAs."""

    demand_id: str
    step: int
    duration_steps: int
    src: str
    dst: str
    bandwidth_mbps: float
    sla_max_latency_ms: float
    max_hops: int = 8
    service_class: str = "PRIORITY_DATA"


@dataclass(frozen=True)
class LinkDegradationEvent:
    """Scheduled weather attenuation, solar interference, or hardware link outage."""

    event_id: str
    step: int
    src: str
    dst: str
    capacity_factor: float
    snr_db: float
    noise_penalty_ms: float
    outage: bool
    reason: str
    bidirectional: bool = True


@dataclass
class SubFlowAllocation:
    """Single path or split sub-flow allocation across a sequence of directed links."""

    nodes: List[str]
    link_ids: List[str]
    allocated_mbps: float
    latency_ms: float
    drop_fraction: float


@dataclass
class RouteAllocation:
    """Outcome of routing a `TrafficDemand` on either the baseline or capacity-aware router."""

    demand_id: str
    routed: bool
    split_count: int
    subflows: List[SubFlowAllocation]
    end_to_end_latency_ms: float
    hop_count: int
    dropped_mbps: float
    sla_violated: bool
    violation_reasons: List[str]
    compute_time_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "demand_id": self.demand_id,
            "routed": self.routed,
            "split_count": self.split_count,
            "paths": [sf.nodes for sf in self.subflows],
            "end_to_end_latency_ms": round(self.end_to_end_latency_ms, 3),
            "hop_count": self.hop_count,
            "dropped_mbps": round(self.dropped_mbps, 3),
            "sla_violated": self.sla_violated,
            "violation_reasons": list(self.violation_reasons),
            "compute_time_ms": round(self.compute_time_ms, 4),
        }


@dataclass
class _ActiveReservation:
    departure_step: int
    link_ids: List[str]
    allocated_mbps: float


def build_default_28_node_mesh() -> Tuple[List[str], List[LinkState]]:
    """Construct a deterministic 28-node LEO + Gateway + Edge POP mesh topology.

    Node tiers (28 total):
      - 6 Edge POPs / Fulfillment Sortation Terminals (`POP-*`)
      - 6 Regional Ground Gateways / Hubs (`GW-*`)
      - 16 LEO Satellites across 4 orbital planes x 4 satellites (`SAT-P{0..3}-S{0..3}`)
    """
    pops = ["POP-SEA", "POP-IAD", "POP-FRA", "POP-DUB", "POP-NRT", "POP-GRU"]
    gateways = [
        "GW-US-WEST",
        "GW-US-EAST",
        "GW-EU-CENTRAL",
        "GW-EU-WEST",
        "GW-AP-TOKYO",
        "GW-SA-SAOPAULO",
    ]
    satellites = [f"SAT-P{p}-S{s}" for p in range(4) for s in range(4)]
    nodes = pops + gateways + satellites
    assert len(nodes) == 28

    links: List[LinkState] = []
    seq = 0

    def add_bidir(
        u: str,
        v: str,
        link_type: str,
        cap_mbps: float,
        lat_ms: float,
        snr_db: float,
    ) -> None:
        nonlocal seq
        seq += 1
        links.append(
            LinkState(
                link_id=f"L{seq:03d}-{u}->{v}",
                src=u,
                dst=v,
                link_type=link_type,
                nominal_capacity_mbps=cap_mbps,
                base_latency_ms=lat_ms,
                snr_db=snr_db,
            )
        )
        seq += 1
        links.append(
            LinkState(
                link_id=f"L{seq:03d}-{v}->{u}",
                src=v,
                dst=u,
                link_type=link_type,
                nominal_capacity_mbps=cap_mbps,
                base_latency_ms=lat_ms,
                snr_db=snr_db,
            )
        )

    # 1. Edge POPs -> Primary Regional Gateway + Secondary Direct LEO Terminal Uplink
    pop_connections = [
        ("POP-SEA", "GW-US-WEST", "SAT-P0-S1"),
        ("POP-IAD", "GW-US-EAST", "SAT-P1-S1"),
        ("POP-FRA", "GW-EU-CENTRAL", "SAT-P1-S3"),
        ("POP-DUB", "GW-EU-WEST", "SAT-P0-S3"),
        ("POP-NRT", "GW-AP-TOKYO", "SAT-P2-S3"),
        ("POP-GRU", "GW-SA-SAOPAULO", "SAT-P3-S2"),
    ]
    for pop, gw, backup_sat in pop_connections:
        add_bidir(pop, gw, "ACCESS_FIBER", cap_mbps=920.0, lat_ms=2.0, snr_db=29.0)
        add_bidir(pop, backup_sat, "KA_FEEDER", cap_mbps=405.0, lat_ms=7.4, snr_db=19.5)

    # 2. Regional Gateways -> Primary & Secondary Diversity LEO Feeder Links
    gw_feeders = [
        ("GW-US-WEST", "SAT-P0-S0", "SAT-P3-S0"),
        ("GW-US-EAST", "SAT-P1-S0", "SAT-P0-S2"),
        ("GW-EU-CENTRAL", "SAT-P1-S2", "SAT-P2-S0"),
        ("GW-EU-WEST", "SAT-P0-S2", "SAT-P1-S2"),
        ("GW-AP-TOKYO", "SAT-P2-S2", "SAT-P3-S1"),
        ("GW-SA-SAOPAULO", "SAT-P3-S3", "SAT-P2-S3"),
    ]
    for gw, pri_sat, sec_sat in gw_feeders:
        add_bidir(gw, pri_sat, "KA_FEEDER", cap_mbps=445.0, lat_ms=4.8, snr_db=21.5)
        add_bidir(gw, sec_sat, "KA_FEEDER", cap_mbps=425.0, lat_ms=6.6, snr_db=20.5)

    # 3. Intra-plane Optical ISLs (4 rings of 4 satellites)
    for p in range(4):
        for s in range(4):
            u = f"SAT-P{p}-S{s}"
            v = f"SAT-P{p}-S{(s + 1) % 4}"
            add_bidir(u, v, "OPTICAL_ISL", cap_mbps=690.0, lat_ms=6.8, snr_db=26.5)

    # 4. Inter-plane Optical ISLs (adjacent orbital planes + long-haul cross-links)
    for p in range(4):
        next_p = (p + 1) % 4
        for s in range(4):
            u = f"SAT-P{p}-S{s}"
            v = f"SAT-P{next_p}-S{s}"
            add_bidir(u, v, "OPTICAL_ISL", cap_mbps=650.0, lat_ms=8.2, snr_db=25.5)

    add_bidir("SAT-P0-S1", "SAT-P2-S1", "OPTICAL_ISL", cap_mbps=680.0, lat_ms=10.4, snr_db=25.0)
    add_bidir("SAT-P1-S3", "SAT-P3-S3", "OPTICAL_ISL", cap_mbps=680.0, lat_ms=10.4, snr_db=25.0)

    # 5. Regional Terrestrial Backhaul Arcs
    add_bidir("GW-US-WEST", "GW-US-EAST", "TERRESTRIAL_BACKHAUL", cap_mbps=480.0, lat_ms=28.0, snr_db=30.0)
    add_bidir("GW-EU-CENTRAL", "GW-EU-WEST", "TERRESTRIAL_BACKHAUL", cap_mbps=460.0, lat_ms=8.4, snr_db=30.0)

    return nodes, links


def build_default_degradation_events() -> List[LinkDegradationEvent]:
    """Return 6 realistic weather, interference, and hardware outage events across 300 steps."""
    return [
        LinkDegradationEvent(
            event_id="DEG-01",
            step=35,
            src="GW-US-WEST",
            dst="SAT-P0-S0",
            capacity_factor=0.30,
            snr_db=6.2,
            noise_penalty_ms=26.0,
            outage=False,
            reason="KA_RAIN_FADE_US_WEST",
        ),
        LinkDegradationEvent(
            event_id="DEG-02",
            step=80,
            src="SAT-P0-S0",
            dst="SAT-P1-S0",
            capacity_factor=0.0,
            snr_db=0.0,
            noise_penalty_ms=0.0,
            outage=True,
            reason="ISL_OPTICAL_TRANSCEIVER_OUTAGE",
        ),
        LinkDegradationEvent(
            event_id="DEG-03",
            step=125,
            src="GW-AP-TOKYO",
            dst="SAT-P2-S2",
            capacity_factor=0.28,
            snr_db=5.8,
            noise_penalty_ms=30.0,
            outage=False,
            reason="TYPHOON_KA_ATTENUATION_TOKYO",
        ),
        LinkDegradationEvent(
            event_id="DEG-04",
            step=170,
            src="SAT-P1-S1",
            dst="SAT-P2-S1",
            capacity_factor=0.25,
            snr_db=5.4,
            noise_penalty_ms=32.0,
            outage=False,
            reason="SOLAR_INTERFERENCE_ISL_PLANE1_2",
        ),
        LinkDegradationEvent(
            event_id="DEG-05",
            step=215,
            src="GW-EU-CENTRAL",
            dst="GW-EU-WEST",
            capacity_factor=0.0,
            snr_db=0.0,
            noise_penalty_ms=0.0,
            outage=True,
            reason="TERRESTRIAL_FIBER_CUT_EU",
        ),
        LinkDegradationEvent(
            event_id="DEG-06",
            step=255,
            src="GW-SA-SAOPAULO",
            dst="SAT-P3-S3",
            capacity_factor=0.32,
            snr_db=6.5,
            noise_penalty_ms=24.0,
            outage=False,
            reason="CONVECTIVE_RAIN_FADE_SAOPAULO",
        ),
    ]


def generate_traffic_workload(
    num_demands: int = 300,
    seed: int = 42,
) -> List[TrafficDemand]:
    """Generate `num_demands` deterministic POP-to-POP traffic/shipment demands."""
    rng = random.Random(seed)
    pops = ["POP-SEA", "POP-IAD", "POP-FRA", "POP-DUB", "POP-NRT", "POP-GRU"]

    # Weighted corridor pairs reflecting heavy trans-oceanic and regional gateways
    corridors = [
        ("POP-SEA", "POP-IAD", 82.0),
        ("POP-SEA", "POP-NRT", 96.0),
        ("POP-IAD", "POP-FRA", 92.0),
        ("POP-FRA", "POP-DUB", 68.0),
        ("POP-IAD", "POP-GRU", 98.0),
        ("POP-NRT", "POP-FRA", 108.0),
        ("POP-SEA", "POP-DUB", 95.0),
        ("POP-GRU", "POP-FRA", 104.0),
    ]

    demands: List[TrafficDemand] = []
    for idx in range(num_demands):
        if rng.random() < 0.78:
            src, dst, base_sla = rng.choice(corridors)
            if rng.random() < 0.45:
                src, dst = dst, src
        else:
            src, dst = rng.sample(pops, 2)
            base_sla = 102.0

        # Mix of steady enterprise flows and larger regional trunk bursts
        if rng.random() < 0.20:
            bw_mbps = round(rng.uniform(135.0, 198.0), 1)
            svc = "TRUNK_REPLENISHMENT"
        else:
            bw_mbps = round(rng.uniform(30.0, 76.0), 1)
            svc = "REALTIME_ISP_STREAM"

        duration = rng.randint(24, 46)
        demands.append(
            TrafficDemand(
                demand_id=f"DMD-{idx + 1:04d}",
                step=idx,
                duration_steps=duration,
                src=src,
                dst=dst,
                bandwidth_mbps=bw_mbps,
                sla_max_latency_ms=base_sla,
                max_hops=8,
                service_class=svc,
            )
        )
    return demands


class _BaseMeshRouter:
    """Shared graph state, flow reservation lifecycle, and degradation injection."""

    def __init__(self, nodes: List[str], links: List[LinkState]) -> None:
        self.nodes: List[str] = list(nodes)
        self.links_by_id: Dict[str, LinkState] = {lnk.link_id: lnk.clone_fresh() for lnk in links}
        self.adj: Dict[str, List[LinkState]] = {n: [] for n in self.nodes}
        self._pair_index: Dict[Tuple[str, str], List[LinkState]] = {}
        for lnk in self.links_by_id.values():
            self.adj.setdefault(lnk.src, []).append(lnk)
            self._pair_index.setdefault((lnk.src, lnk.dst), []).append(lnk)
        self._active_reservations: List[_ActiveReservation] = []

    def release_expired_flows(self, current_step: int) -> int:
        """Release bandwidth from flows whose active window ended at or before `current_step`."""
        still_active: List[_ActiveReservation] = []
        released = 0
        for res in self._active_reservations:
            if res.departure_step <= current_step:
                for lid in res.link_ids:
                    lnk = self.links_by_id[lid]
                    lnk.allocated_mbps = max(0.0, lnk.allocated_mbps - res.allocated_mbps)
                released += 1
            else:
                still_active.append(res)
        self._active_reservations = still_active
        return released

    def apply_degradation_event(self, event: LinkDegradationEvent) -> int:
        """Apply SNR / capacity degradation or hard outage to matching link(s)."""
        pairs = [(event.src, event.dst)]
        if event.bidirectional:
            pairs.append((event.dst, event.src))

        touched = 0
        for pair in pairs:
            for lnk in self._pair_index.get(pair, []):
                lnk.capacity_factor = event.capacity_factor
                lnk.snr_db = event.snr_db
                lnk.noise_penalty_ms = event.noise_penalty_ms
                lnk.outage = event.outage
                lnk.degradation_reason = event.reason
                touched += 1
        return touched

    def _commit_subflow(
        self,
        nodes_path: List[str],
        link_ids: List[str],
        mbps: float,
        departure_step: int,
    ) -> SubFlowAllocation:
        total_latency = 0.0
        survival_prob = 1.0
        for lid in link_ids:
            lnk = self.links_by_id[lid]
            hop_lat = lnk.projected_hop_latency_ms(mbps)
            hop_drop = lnk.projected_drop_fraction(mbps)
            total_latency += hop_lat
            survival_prob *= 1.0 - hop_drop
            lnk.allocated_mbps += mbps

        self._active_reservations.append(
            _ActiveReservation(
                departure_step=departure_step,
                link_ids=list(link_ids),
                allocated_mbps=mbps,
            )
        )
        drop_fraction = 1.0 - survival_prob
        return SubFlowAllocation(
            nodes=list(nodes_path),
            link_ids=list(link_ids),
            allocated_mbps=mbps,
            latency_ms=total_latency,
            drop_fraction=drop_fraction,
        )

    @staticmethod
    def _finalize_allocation(
        demand: TrafficDemand,
        subflows: List[SubFlowAllocation],
        elapsed_ms: float,
    ) -> RouteAllocation:
        if not subflows:
            return RouteAllocation(
                demand_id=demand.demand_id,
                routed=False,
                split_count=0,
                subflows=[],
                end_to_end_latency_ms=250.0,
                hop_count=0,
                dropped_mbps=demand.bandwidth_mbps,
                sla_violated=True,
                violation_reasons=["NO_FEASIBLE_PATH"],
                compute_time_ms=elapsed_ms,
            )

        e2e_latency = max(sf.latency_ms for sf in subflows)
        max_hops_used = max(len(sf.link_ids) for sf in subflows)
        dropped_mbps = sum(sf.allocated_mbps * sf.drop_fraction for sf in subflows)
        unallocated = max(0.0, demand.bandwidth_mbps - sum(sf.allocated_mbps for sf in subflows))
        dropped_mbps += unallocated

        reasons: List[str] = []
        if dropped_mbps > 1e-6:
            reasons.append("LINK_CONGESTION_OR_OUTAGE_DROP")
        if e2e_latency > demand.sla_max_latency_ms:
            reasons.append("LATENCY_SLA_EXCEEDED")
        if max_hops_used > demand.max_hops:
            reasons.append("MAX_HOPS_EXCEEDED")

        return RouteAllocation(
            demand_id=demand.demand_id,
            routed=dropped_mbps < demand.bandwidth_mbps - 1e-6,
            split_count=len(subflows),
            subflows=subflows,
            end_to_end_latency_ms=e2e_latency,
            hop_count=max_hops_used,
            dropped_mbps=dropped_mbps,
            sla_violated=bool(reasons),
            violation_reasons=reasons,
            compute_time_ms=elapsed_ms,
        )


class StaticShortestPathRouter(_BaseMeshRouter):
    """Baseline OSPF-style static router selecting shortest nominal propagation paths."""

    def route_demand(self, demand: TrafficDemand) -> RouteAllocation:
        t0 = time.perf_counter()
        self.release_expired_flows(demand.step)

        # Dijkstra strictly over static `base_latency_ms` (ignores congestion, rain fade & outages)
        pq: List[Tuple[float, int, str, List[str], List[str]]] = [
            (0.0, 0, demand.src, [demand.src], [])
        ]
        best_cost: Dict[str, float] = {demand.src: 0.0}
        chosen_nodes: Optional[List[str]] = None
        chosen_links: Optional[List[str]] = None

        while pq:
            nom_lat, hops, u, npath, lpath = heapq.heappop(pq)
            if u == demand.dst:
                chosen_nodes = npath
                chosen_links = lpath
                break
            if nom_lat > best_cost.get(u, math.inf) + 1e-9:
                continue
            for lnk in self.adj.get(u, []):
                nxt_lat = nom_lat + lnk.base_latency_ms
                if nxt_lat < best_cost.get(lnk.dst, math.inf) - 1e-9:
                    best_cost[lnk.dst] = nxt_lat
                    heapq.heappush(
                        pq,
                        (nxt_lat, hops + 1, lnk.dst, npath + [lnk.dst], lpath + [lnk.link_id]),
                    )

        if chosen_nodes is None or chosen_links is None:
            elapsed = (time.perf_counter() - t0) * 1000.0
            return self._finalize_allocation(demand, [], elapsed)

        sf = self._commit_subflow(
            chosen_nodes,
            chosen_links,
            demand.bandwidth_mbps,
            demand.step + demand.duration_steps,
        )
        elapsed = (time.perf_counter() - t0) * 1000.0
        return self._finalize_allocation(demand, [sf], elapsed)


class CapacityAwareRouter(_BaseMeshRouter):
    """Capacity & SNR-aware constrained Dijkstra router with 2-way residual flow splitting."""

    def __init__(
        self,
        nodes: List[str],
        links: List[LinkState],
        min_snr_db: float = 8.0,
        utilization_penalty_weight: float = 9.0,
        allow_flow_splitting: bool = True,
    ) -> None:
        super().__init__(nodes, links)
        self.min_snr_db = min_snr_db
        self.utilization_penalty_weight = utilization_penalty_weight
        self.allow_flow_splitting = allow_flow_splitting

    def _find_constrained_path(
        self,
        src: str,
        dst: str,
        required_mbps: float,
        max_hops: int,
        sla_max_latency_ms: Optional[float],
        min_snr_db: float,
        excluded_link_ids: Optional[set[str]] = None,
    ) -> Optional[Tuple[List[str], List[str], float]]:
        """Run resource-constrained Dijkstra over residual capacity and projected latency."""
        excluded = excluded_link_ids or set()
        # Priority queue: (routing_score, projected_latency_ms, hops, node, node_path, link_path)
        pq: List[Tuple[float, float, int, str, List[str], List[str]]] = [
            (0.0, 0.0, 0, src, [src], [])
        ]
        # Track best projected latency per (node, hops) to preserve hop-constrained feasibility
        best_state: Dict[Tuple[str, int], float] = {(src, 0): 0.0}

        while pq:
            score, proj_lat, hops, u, npath, lpath = heapq.heappop(pq)
            if u == dst:
                return npath, lpath, proj_lat

            if score > best_state.get((u, hops), math.inf) + 1e-9:
                continue
            if hops >= max_hops:
                continue

            for lnk in self.adj.get(u, []):
                if lnk.link_id in excluded or lnk.dst in npath:
                    continue
                if lnk.outage or lnk.effective_capacity_mbps <= 0.0:
                    continue
                if lnk.snr_db < min_snr_db:
                    continue
                if lnk.residual_mbps + 1e-9 < required_mbps:
                    continue

                hop_lat = lnk.projected_hop_latency_ms(required_mbps)
                nxt_lat = proj_lat + hop_lat
                if sla_max_latency_ms is not None and nxt_lat > sla_max_latency_ms:
                    continue

                post_util = (lnk.allocated_mbps + required_mbps) / lnk.effective_capacity_mbps
                congestion_bias = self.utilization_penalty_weight * (post_util ** 2)
                nxt_score = score + hop_lat + congestion_bias
                nxt_hops = hops + 1

                # Dominated check across <= nxt_hops at destination node
                dominated = False
                for h_prev in range(nxt_hops + 1):
                    if best_state.get((lnk.dst, h_prev), math.inf) <= nxt_score + 1e-9:
                        dominated = True
                        break
                if dominated:
                    continue

                best_state[(lnk.dst, nxt_hops)] = nxt_score
                heapq.heappush(
                    pq,
                    (
                        nxt_score,
                        nxt_lat,
                        nxt_hops,
                        lnk.dst,
                        npath + [lnk.dst],
                        lpath + [lnk.link_id],
                    ),
                )
        return None

    def _try_two_way_split(
        self,
        demand: TrafficDemand,
        min_snr_db: float,
        enforce_sla: bool,
    ) -> Optional[List[SubFlowAllocation]]:
        """Attempt 2-path residual flow decomposition when no single edge fits the full burst."""
        sla_cap = demand.sla_max_latency_ms if enforce_sla else None
        half_bw = demand.bandwidth_mbps * 0.5

        first = self._find_constrained_path(
            src=demand.src,
            dst=demand.dst,
            required_mbps=half_bw,
            max_hops=demand.max_hops,
            sla_max_latency_ms=sla_cap,
            min_snr_db=min_snr_db,
        )
        if first is None:
            return None

        npath1, lpath1, _ = first
        bottleneck1 = min(self.links_by_id[lid].residual_mbps for lid in lpath1)
        flow1_mbps = min(demand.bandwidth_mbps * 0.65, bottleneck1)
        flow2_mbps = demand.bandwidth_mbps - flow1_mbps
        if flow2_mbps <= 1e-6:
            return None

        # Temporarily reserve flow1 to inspect true residual graph for flow2
        for lid in lpath1:
            self.links_by_id[lid].allocated_mbps += flow1_mbps

        second = self._find_constrained_path(
            src=demand.src,
            dst=demand.dst,
            required_mbps=flow2_mbps,
            max_hops=demand.max_hops,
            sla_max_latency_ms=sla_cap,
            min_snr_db=min_snr_db,
        )

        # Roll back temporary reservation before committing atomically
        for lid in lpath1:
            self.links_by_id[lid].allocated_mbps = max(
                0.0, self.links_by_id[lid].allocated_mbps - flow1_mbps
            )

        if second is None:
            return None

        npath2, lpath2, _ = second
        dep = demand.step + demand.duration_steps
        sf1 = self._commit_subflow(npath1, lpath1, flow1_mbps, dep)
        sf2 = self._commit_subflow(npath2, lpath2, flow2_mbps, dep)
        return [sf1, sf2]

    def route_demand(self, demand: TrafficDemand) -> RouteAllocation:
        """Route `demand` around congested, degraded-SNR, and failed links within SLA bounds."""
        t0 = time.perf_counter()
        self.release_expired_flows(demand.step)
        dep = demand.step + demand.duration_steps

        # Tier 1: Single-path strict SLA + healthy SNR
        strict_path = self._find_constrained_path(
            src=demand.src,
            dst=demand.dst,
            required_mbps=demand.bandwidth_mbps,
            max_hops=demand.max_hops,
            sla_max_latency_ms=demand.sla_max_latency_ms,
            min_snr_db=self.min_snr_db,
        )
        if strict_path is not None:
            npath, lpath, _ = strict_path
            sf = self._commit_subflow(npath, lpath, demand.bandwidth_mbps, dep)
            elapsed = (time.perf_counter() - t0) * 1000.0
            return self._finalize_allocation(demand, [sf], elapsed)

        # Tier 2: 2-way residual flow split within strict SLA + healthy SNR
        if self.allow_flow_splitting:
            split_strict = self._try_two_way_split(
                demand, min_snr_db=self.min_snr_db, enforce_sla=True
            )
            if split_strict is not None:
                elapsed = (time.perf_counter() - t0) * 1000.0
                return self._finalize_allocation(demand, split_strict, elapsed)

        # Tier 3: Relax SNR floor & latency SLA (still enforcing non-outage residual capacity)
        relaxed_path = self._find_constrained_path(
            src=demand.src,
            dst=demand.dst,
            required_mbps=demand.bandwidth_mbps,
            max_hops=demand.max_hops + 2,
            sla_max_latency_ms=None,
            min_snr_db=4.0,
        )
        if relaxed_path is not None:
            npath, lpath, _ = relaxed_path
            sf = self._commit_subflow(npath, lpath, demand.bandwidth_mbps, dep)
            elapsed = (time.perf_counter() - t0) * 1000.0
            return self._finalize_allocation(demand, [sf], elapsed)

        if self.allow_flow_splitting:
            split_relaxed = self._try_two_way_split(demand, min_snr_db=4.0, enforce_sla=False)
            if split_relaxed is not None:
                elapsed = (time.perf_counter() - t0) * 1000.0
                return self._finalize_allocation(demand, split_relaxed, elapsed)

        elapsed = (time.perf_counter() - t0) * 1000.0
        return self._finalize_allocation(demand, [], elapsed)


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_vals[lo]
    frac = idx - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _summarize_allocations(allocs: List[RouteAllocation], total_mbps: float) -> Dict[str, Any]:
    latencies = sorted(a.end_to_end_latency_ms for a in allocs)
    compute_times = sorted(a.compute_time_ms for a in allocs)
    sla_violations = sum(1 for a in allocs if a.sla_violated)
    dropped_mbps = sum(a.dropped_mbps for a in allocs)
    split_flows = sum(1 for a in allocs if a.split_count > 1)

    return {
        "demands_total": len(allocs),
        "demands_routed": sum(1 for a in allocs if a.routed),
        "split_flow_count": split_flows,
        "p50_latency_ms": round(_percentile(latencies, 50.0), 2),
        "p95_latency_ms": round(_percentile(latencies, 95.0), 2),
        "p99_latency_ms": round(_percentile(latencies, 99.0), 2),
        "mean_latency_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "sla_violations": sla_violations,
        "sla_violation_rate_pct": round((sla_violations / max(1, len(allocs))) * 100.0, 2),
        "dropped_bandwidth_mbps": round(dropped_mbps, 2),
        "packet_drop_rate_pct": round((dropped_mbps / max(1e-9, total_mbps)) * 100.0, 2),
        "p50_compute_ms": round(_percentile(compute_times, 50.0), 4),
        "p99_compute_ms": round(_percentile(compute_times, 99.0), 4),
        "mean_compute_ms": round(statistics.mean(compute_times), 4) if compute_times else 0.0,
        "total_compute_ms": round(sum(compute_times), 3),
    }


def run_router_comparison(
    num_demands: int = 300,
    seed: int = 42,
) -> Dict[str, Any]:
    """Run deterministic side-by-side benchmark of Static vs. Capacity-Aware routing."""
    nodes, base_links = build_default_28_node_mesh()
    degradation_events = build_default_degradation_events()
    demands = generate_traffic_workload(num_demands=num_demands, seed=seed)

    events_by_step: Dict[int, List[LinkDegradationEvent]] = {}
    for ev in degradation_events:
        events_by_step.setdefault(ev.step, []).append(ev)

    static_router = StaticShortestPathRouter(nodes, base_links)
    aware_router = CapacityAwareRouter(nodes, base_links)

    static_allocs: List[RouteAllocation] = []
    aware_allocs: List[RouteAllocation] = []

    for dmd in demands:
        for ev in events_by_step.get(dmd.step, []):
            static_router.apply_degradation_event(ev)
            aware_router.apply_degradation_event(ev)
        static_allocs.append(static_router.route_demand(dmd))
        aware_allocs.append(aware_router.route_demand(dmd))

    total_mbps = sum(d.bandwidth_mbps for d in demands)
    s_sum = _summarize_allocations(static_allocs, total_mbps)
    a_sum = _summarize_allocations(aware_allocs, total_mbps)

    p99_reduction_pct = (
        ((s_sum["p99_latency_ms"] - a_sum["p99_latency_ms"]) / s_sum["p99_latency_ms"]) * 100.0
        if s_sum["p99_latency_ms"] > 0
        else 0.0
    )
    sla_violation_reduction_pct = (
        ((s_sum["sla_violations"] - a_sum["sla_violations"]) / s_sum["sla_violations"]) * 100.0
        if s_sum["sla_violations"] > 0
        else 0.0
    )
    drop_reduction_pct = (
        ((s_sum["dropped_bandwidth_mbps"] - a_sum["dropped_bandwidth_mbps"]) / s_sum["dropped_bandwidth_mbps"]) * 100.0
        if s_sum["dropped_bandwidth_mbps"] > 0
        else 0.0
    )

    return {
        "topology": {
            "node_count": len(nodes),
            "directed_link_count": len(base_links),
            "degradation_event_count": len(degradation_events),
            "demand_count": len(demands),
            "total_requested_mbps": round(total_mbps, 2),
        },
        "static_shortest_path": s_sum,
        "capacity_aware_router": a_sum,
        "improvements": {
            "p99_latency_reduction_pct": round(p99_reduction_pct, 2),
            "sla_violation_reduction_pct": round(sla_violation_reduction_pct, 2),
            "packet_drop_reduction_pct": round(drop_reduction_pct, 2),
            "reroute_p50_compute_ms": a_sum["p50_compute_ms"],
            "reroute_p99_compute_ms": a_sum["p99_compute_ms"],
            "reroute_mean_compute_ms": a_sum["mean_compute_ms"],
        },
        "sample_reroutes": [
            aware_allocs[i].to_dict()
            for i in (36, 82, 128, 218, 260)
            if i < len(aware_allocs)
        ],
    }
