# RouteLedger — Capacity-Constrained Multi-Path Graph Solver, Bounded NOC Triage & ACID Double-Entry Ledger

**Live Production App (Amazon CloudFront + Private S3 OAC + Bedrock Nova):** https://d2x9ti2h7j9apb.cloudfront.net  
**CloudFront Distribution ID:** `E35WFLOHTA485K` (`us-west-2` `/api/*` Origin + Private S3 OAC `routeledger-web-796330847946-us-west-2`)  
**Live Health Check:** `https://d2x9ti2h7j9apb.cloudfront.net/api/health`

**RouteLedger** is a Python routing, incident triage, and transactional financial settlement engine uniting three deterministic subsystems paired with live **Amazon Bedrock (`us.amazon.nova-lite-v1:0`)** advisory synthesis:
1. **Capacity- & SNR-Threshold Multi-Path Graph Solver (`route_ledger/network_router.py`)**: Models a `28`-node / `120`-directed-link multi-hop backbone and middle-mile hub graph with per-edge capacity limits, convex M/M/1-style queuing latency, `snr_db >= 8.0` link-quality threshold pruning, and 2-way residual flow splitting—cutting p99 path latency from `526.47 ms` to **`132.80 ms` (`-74.78%`)** and SLA violations from `35.67%` (`107/300`) to **`1.67%` (`5/300`, `-95.33%`)** in **`0.1211 ms` p50 / `0.4648 ms` p99** compute time.
2. **Idempotent SQLite (`BEGIN IMMEDIATE` / WAL) ACID Double-Entry Ledger (`route_ledger/double_entry_ledger.py`)**: Settles at-least-once carrier and bandwidth metering streams (`2,000`-event benchmark at **`32,704.9 events/sec`** plus a 60-way concurrent SQLite WAL `ThreadPoolExecutor` retry storm) using canonical `SHA-256` payload seals and `Decimal(ROUND_HALF_UP)` minor-unit tax rules (`US-WA`, `DE`, `JP`, `BR`) with **`0` duplicate charges** and **`$0.00` (`0¢`) trial-balance drift (`SUM(Debits) == SUM(Credits)`)**.
3. **Bounded `<= 3`-Check Operational Fault-Tree Engine (`route_ledger/noc_workflow.py`)**: Isolates cascading hub and gateway alarms across `150` incidents (`5` alarm classes) at **`100.0%` (`150/150`) accuracy** in **`2.41` mean checks** (`max = 3`, `-93.47%` steps vs `36.95` log lines scanned at `64.0%` baseline accuracy) before invoking live Amazon Bedrock Nova for operator runbooks.

![RouteLedger CloudFront Architecture](docs/architecture.svg)

---

## Live Interactive Capabilities

1. **Sandbox 01 — Middle-Mile Fulfillment Corridor Graph (`SEA1 -> SLC2 -> DEN4 -> ORD5 -> CVG3 -> EWR7`)**:
   - Interactive blueprint SVG topology showing parcel flow (`units/hr`), arc capacity utilization (`%`), Critical Pull Time (`<= 24.0h` CPT) SLA compliance, and `$/unit` freight cost.
   - Inject an **`ORD5` Hub Closure** or **`DEN4` Thermal Throttle** (or drag demand up to `5,200 units/hr`) to watch successive shortest augmenting paths split flow onto `SEA1 -> SLC2 -> CVG3 -> EWR7` and `DFW6` bypasses in `< 0.5 ms`.
2. **Sandbox 02 — SNR-Threshold Multi-Hop Backbone Graph (`Gateway-Redmond <-> Plane-1 / Plane-2 Relay Nodes <-> Gateway-Frankfurt`)**:
   - Inject **Link Attenuation (`5.4 dB < 8.0 dB` threshold) & Hard Link Flap** on the primary relay path to watch constrained Dijkstra + 2-way residual flow splitting divert Gbps trunks across diversity relays while **Amazon Bedrock (`us.amazon.nova-lite-v1:0`)** synthesizes a grounded operator runbook.
3. **Sandbox 03 — 60-Delivery SQS Retry Storm & Tamper-Evident `SHA-256` ACID Double-Entry Ledger**:
   - Replay `60` at-least-once SQS deliveries (`12` unique events × `5x` replay with out-of-order timestamps across `US-WA`, `DE`, `JP`, and `BR`) and trigger the **`IdempotencyConflictError` SQLite Tamper Trap** to verify `0` duplicate charges and `100%` balanced `SUM(Debits) == SUM(Credits)` (`$0.00` drift).

---

## Measured Benchmark Results (`benchmarks/latest_results.json`)

| Subsystem & Metric | Measured Result (`RouteLedger`) | Baseline Comparison (`run_benchmarks.py`) |
| :--- | :---: | :---: |
| **Graph Solver p99 Path Latency (`300` demands, `28` nodes, `120` links)** | **132.80 ms (-74.78%)** | `526.47 ms` static shortest-path (`45.00 ms -> 30.48 ms` p50) |
| **Graph Solver SLA Violations (`6` link outage/attenuation events)** | **5 / 300 (1.67%; -95.33%)** | `107 / 300` (`35.67%`) static shortest-path |
| **Graph Solver Dropped Bandwidth (`23,041.4 Mbps` requested)** | **372.40 Mbps (1.62%; -91.29%)** | `4,273.71 Mbps` (`18.55%`) static shortest-path |
| **Graph Solver Compute Time (`p50` / `p99`)** | **0.1211 ms p50** / **0.4648 ms p99** | Includes 2-way residual flow splitting (`6` split flows) |
| **Ledger Duplicate-Charge Elimination (`2,000` events, `15%` retries)** | **100.0% (0 double-charges)** | `245` duplicate charges (`$17,792.88` overbilled in float baseline) |
| **Ledger Minor-Unit Rounding Drift & Trial Balance** | **0¢ drift (`19,376,379¢ Dr == Cr`)** | `60¢` IEEE-754 float drift |
| **Ledger SQLite ACID Throughput & Reconciliation** | **32,704.9 events/sec** | `61.15 ms` settlement wall time · `5.823 ms` full recon audit |
| **Fault-Tree Root-Cause Accuracy (`150` incidents, `5` classes)** | **100.0% (`150/150`, `2.41` mean checks)** | `64.0%` (`96/150`, `36.95` log lines scanned; `-93.47%` steps) |

---

## Run Locally & Deploy to Amazon CloudFront

```bash
# 1. Run all 29 unit, concurrent SQLite WAL storm, workflow, and API tests
python3 -m unittest discover -s tests -v

# 2. Reproduce all benchmark metrics in benchmarks/latest_results.json
python3 benchmarks/run_benchmarks.py

# 3. Start the interactive Topology & Ledger server locally
python3 -m route_ledger.web_api
# -> Open http://localhost:3097

# 4. Deploy to Amazon CloudFront + Private S3 (OAC) + /api/* + Amazon Bedrock (us-west-2)
chmod +x infra/deploy_cloudfront.sh
./infra/deploy_cloudfront.sh
```
