# RouteLedger — Capacity-Aware Network Router, Double-Entry Ledger & NOC Fault Engine

> **Live Demo:** [https://prlngxiwuhv32dkej5xvzg7i2e0xqjfq.lambda-url.us-west-2.on.aws/](https://prlngxiwuhv32dkej5xvzg7i2e0xqjfq.lambda-url.us-west-2.on.aws/)  
> **Health Check:** `https://prlngxiwuhv32dkej5xvzg7i2e0xqjfq.lambda-url.us-west-2.on.aws/api/health`  
> **Live API Call:** `curl -s -X POST https://prlngxiwuhv32dkej5xvzg7i2e0xqjfq.lambda-url.us-west-2.on.aws/api/run -H 'Content-Type: application/json' -d '{}'`

**RouteLedger** addresses three operational failure modes across satellite/ISP networks and regional fulfillment centers: rerouting traffic around congested or rain-faded links before latency SLAs break, settling out-of-order usage and inventory events in a double-entry ledger without duplicate retry charges or rounding drift, and isolating cascading gateway and conveyor alarms in at most three telemetry checks.

---

## What Problem This Solves

- **Static shortest-path routing drops packets and blows latency SLAs during rain fade and link cuts:** Routing solely by nominal propagation latency keeps forwarding traffic onto oversubscribed feeders, rain-faded Ka-band uplinks (`< 8 dB` SNR), and failed optical ISLs. Pruning degraded/outage links, penalizing convex M/M/1 queue utilization, and splitting elephant trunk bursts across parallel planes cuts p99 latency by **74.78%** (`526.47 ms` to `132.80 ms`), SLA violations by **95.33%** (`107` to `5` of `300`), and dropped bandwidth by **91.29%** at **0.12 ms p50** reroute compute time.
- **At-least-once metering streams and binary floats corrupt usage billing and SKU stock:** Network retries, out-of-order gateway store-and-forward batches, and `float` tax math cause customer overbilling and accounting drift. Enforcing SHA-256 payload-verified `idempotency_key` deduplication, exact `Decimal` minor-unit (`cents`) quantization across regional tax rules (`US-WA`, `DE`, `JP`, `BR`), and `BEGIN IMMEDIATE` double-entry checks eliminates **100.0%** of duplicate charges (`0` vs `245` double-charges / `$17,792.88` overbilled in the naive processor) with **0 cents** rounding drift (`SUM(debits) == SUM(credits)`) at **32,704.9 events/sec**.
- **Unstructured log scanning misdiagnoses cascading NOC and fulfillment alarms:** Rain fade, PTP clock unlock, conveyor motor overload, and IMS bin shortages trigger upstream/downstream warning noise before the primary fault appears in raw daemon logs. Executing deterministic telemetry decision trees isolates the exact root cause and safe runbook action in at most **3 checks** (**2.41 mean checks** vs **36.95 log lines**, **93.47% fewer steps**), raising root-cause accuracy from **64.00%** to **100.00%**.

---

## System Architecture

> **Excalidraw Source:** [`docs/architecture.excalidraw`](docs/architecture.excalidraw)

![RouteLedger Architecture Diagram](docs/architecture.svg)

---

## How It Works

1. **Capacity-Aware Multi-Hop Link & Fulfillment Router (`route_ledger/network_router.py`)**  
   Models a 28-node, 120-directed-link topology (`6` edge POPs / sortation terminals, `6` regional ground gateways, and `16` LEO satellites across `4` orbital planes with optical ISLs, diversity Ka-band feeders, and terrestrial fiber backhaul) under `6` mid-stream weather attenuation and link outage events:
   - **Constrained Dijkstra (`heapq`):** Prunes links in outage, below `min_snr_db` (`8.0 dB`), or lacking `residual_mbps >= demand_mbps`, while scoring projected propagation + ARQ noise + convex M/M/1 queuing delay within `max_hops` and `sla_max_latency_ms`.
   - **2-Way Residual Flow Splitting:** Decomposes peak trunk bursts (`135–198 Mbps`) across two augmenting paths on the residual capacity graph when no single feeder/ISL bottleneck fits the demand.
   - **Reservation Lifecycle:** Releases bandwidth cleanly as active flow windows expire (`release_expired_flows`).

2. **Idempotent Double-Entry Usage & Subscription Settlement Ledger (`route_ledger/double_entry_ledger.py`)**  
   SQLite-backed (`BEGIN IMMEDIATE`) transactional ledger processing metered bandwidth (`GB`), recurring ISP subscriptions, and hardware terminal kit orders (`SKU` reservations):
   - **Idempotency & Payload Integrity:** Deduplicates retries by `idempotency_key` and raises `IdempotencyConflictError` if a caller reuses an existing key with a mutated SHA-256 canonical payload.
   - **Regional Minor-Unit Tax Rules:** Computes `subtotal_cents` and `tax_cents` via `decimal.Decimal` (`ROUND_HALF_UP`) for `US-WA` (`10.25%`), `DE` (`19.00%`), `JP` (`10.00%`), and `BR` (`26.50%`).
   - **Atomic Rejections & Double-Entry Conservation:** Rejects events exceeding customer `credit_limit_cents` (`INSUFFICIENT_CREDIT`) or warehouse `on_hand_qty - reserved_qty` (`INSUFFICIENT_INVENTORY`) with zero orphan postings, enforcing `SUM(debits) == SUM(credits)` on every settled transaction and auditing against raw metering logs (`reconcile_against_metering_log`).

3. **NOC & Floor Troubleshooting Decision-Tree Runner (`route_ledger/noc_workflow.py`)**  
   Evaluates structured telemetry snapshots in `<= 3` deterministic checks across `GROUND_GATEWAY_SNR_DROP`, `SATELLITE_HANDOFF_PACKET_LOSS`, `ISP_BILLING_RECONCILIATION_MISMATCH`, `FULFILLMENT_CONVEYOR_BUFFER_JAM`, and `INVENTORY_BIN_COUNT_DRIFT`, emitting an auditable predicate trace, severity tier, and `requires_human_approval` gate for high-blast-radius interventions (such as gateway drain, conveyor zone lockout, or SKU reservation freeze).

4. **CLI & AWS Lambda Function URL Adapter (`route_ledger/cli.py`, `route_ledger/aws_lambda_handler.py`)**  
   Pure Python stdlib CLI and AWS Lambda Function URL (`us-west-2`) HTTP handler exposing `/api/health` and `/api/run` with zero third-party wheels required.

---

## Benchmark Results

Reproducible on Python 3.10+ (stdlib only, `random.Random(42)`) via `python3 benchmarks/run_benchmarks.py` (saved in [`benchmarks/latest_results.json`](benchmarks/latest_results.json)):

| Subsystem & Metric | Measured Result (`RouteLedger`) | Baseline Comparison |
| :--- | :---: | :---: |
| **Router p99 End-to-End Latency (`300` demands, `28` nodes)** | **132.80 ms** (**-74.78%**) | `526.47 ms` static shortest-path |
| **Router SLA Violations (`6` degradation/outage events)** | **5 / 300 (`1.67%`; -95.33%)** | `107 / 300` (`35.67%`) static shortest-path |
| **Router Dropped Bandwidth** | **372.40 Mbps (`1.57%`; -91.29%)** | `4,273.71 Mbps` (`17.99%`) static shortest-path |
| **Router Per-Demand Reroute Compute (`p50` / `p99`)** | **0.1211 ms p50** / **0.4648 ms p99** | Includes `6` 2-way residual split flows |
| **Ledger Duplicate-Charge Elimination (`2,000` events, `15%` retries)** | **100.0% (`0` double-charges)** | `245` double-charges (`$17,792.88` overbilled) naive |
| **Ledger Minor-Unit Rounding Drift & Trial Balance** | **0 cents drift** (`$193,763.79 == $193,763.79`) | `60 cents` IEEE-754 float drift on settled set |
| **Ledger SQLite ACID Throughput & Recon Audit** | **32,704.9 events/sec** (`61.15 ms`) | Full `2,000`-event recon audit in `5.82 ms` (`72` OOO) |
| **NOC & Floor Root-Cause Diagnostic Accuracy (`150` incidents)** | **100.00% (`150 / 150`; +36.00 pp)** | `64.00%` (`96 / 150`) sequential log scanner |
| **NOC & Floor Diagnostic Steps Executed** | **2.41 mean checks** (`max = 3`; **-93.47%**) | `36.95` mean log lines scanned (`max = 55`) |

---

## Quickstart

Requires Python 3.10+ with zero external package dependencies:

```bash
# 1. Run all 24 unit, invariant, double-entry, and workflow tests
python3 -m unittest discover -s tests -v

# 2. Reproduce all benchmark metrics in benchmarks/latest_results.json
python3 benchmarks/run_benchmarks.py

# 3. Run JSON telemetry CLI modes
python3 -m route_ledger.cli --mode demo-all
python3 -m route_ledger.cli --mode router
python3 -m route_ledger.cli --mode ledger
python3 -m route_ledger.cli --mode workflow
```
