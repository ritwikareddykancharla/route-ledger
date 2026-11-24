"""Command-line entrypoint (`python3 -m route_ledger.cli`).

Supports deterministic JSON telemetry output across:
  - `--mode demo-all`
  - `--mode router`
  - `--mode ledger`
  - `--mode workflow`
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict, Optional, Sequence, Tuple

from . import __version__
from .double_entry_ledger import run_ledger_comparison
from .network_router import run_router_comparison
from .noc_workflow import run_workflow_comparison


SUPPORTED_MODES: Tuple[str, ...] = ("demo-all", "router", "ledger", "workflow")


def execute_cli_mode(
    mode: str = "demo-all",
    *,
    seed: int = 42,
    demands: int = 300,
    events: int = 2000,
    incidents: int = 150,
) -> Dict[str, Any]:
    """Execute the requested `RouteLedger` subsystem(s) and return structured JSON telemetry."""
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported mode '{mode}'. Choose from {SUPPORTED_MODES}.")

    t0 = time.perf_counter()
    payload: Dict[str, Any] = {
        "service": "RouteLedger",
        "version": __version__,
        "mode": mode,
        "seed": seed,
    }

    if mode in ("demo-all", "router"):
        payload["router"] = run_router_comparison(num_demands=demands, seed=seed)

    if mode in ("demo-all", "ledger"):
        payload["ledger"] = run_ledger_comparison(total_events=events, seed=seed)

    if mode in ("demo-all", "workflow"):
        payload["workflow"] = run_workflow_comparison(num_incidents=incidents, seed=seed)

    payload["wall_time_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="route-ledger",
        description="RouteLedger CLI — Capacity-Aware Router, Double-Entry Ledger & NOC Workflow Engine",
    )
    parser.add_argument(
        "--mode",
        choices=SUPPORTED_MODES,
        default="demo-all",
        help="Subsystem to execute (default: demo-all).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic PRNG seed (default: 42).",
    )
    parser.add_argument(
        "--demands",
        type=int,
        default=300,
        help="Traffic/fulfillment demands for router mode (default: 300).",
    )
    parser.add_argument(
        "--events",
        type=int,
        default=2000,
        help="Metering/order events for ledger mode (default: 2000).",
    )
    parser.add_argument(
        "--incidents",
        type=int,
        default=150,
        help="Telemetry snapshots for workflow mode (default: 150).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = execute_cli_mode(
        args.mode,
        seed=args.seed,
        demands=args.demands,
        events=args.events,
        incidents=args.incidents,
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
