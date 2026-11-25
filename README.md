# RouteLedger — SCOT Middle-Mile Optimizer, Amazon Leo Orbital Mesh & ACID Double-Entry Ledger

**Live Production App (Amazon CloudFront + Private S3 OAC + Bedrock Nova):** https://d2x9ti2h7j9apb.cloudfront.net  
**CloudFront Distribution ID:** `E35WFLOHTA485K` (`us-west-2` `/api/*` Origin + Private S3 OAC `routeledger-web-796330847946-us-west-2`)  
**Live Health Check:** `https://d2x9ti2h7j9apb.cloudfront.net/api/health`

**RouteLedger** addresses three operational failure modes across middle-mile fulfillment networks and LEO satellite constellations: rerouting freight and bandwidth around blizzard-closed hubs or rain-faded Ka-band links (`< 8.0 dB` SNR) in `< 1 ms`, settling at-least-once carrier/ISP metering events in a double-entry SQLite (`BEGIN IMMEDIATE`) ledger without duplicate retry charges or IEEE-754 float drift, and isolating cascading floor/gateway alarms in `<= 3` deterministic checks paired with **Amazon Bedrock (`us.amazon.nova-lite-v1:0`)** troubleshooting synthesis.

![RouteLedger CloudFront Architecture](docs/architecture.svg)

---

## Live Interactive Capabilities

1. **Mode 01 — SCOT Middle-Mile Fulfillment Network (`SEA1 -> SLC2 -> DEN4 -> ORD5 -> CVG3 -> EWR7`)**:
   - Interactive blueprint SVG topology showing live parcel volume (`units/hr`), truck/air arc capacity utilization (`%`), Critical Pull Time (CPT `<= 24.0h`) SLA compliance, and $/unit shipping cost.
   - Click **Inject Blizzard / Outage on `ORD5`** (or drag the parcel demand slider above single-corridor capacity) to watch the **Capacity-Constrained Min-Cost Flow Optimizer** reroute volume onto `SEA1 -> SLC2 -> CVG3 -> EWR7` (and `DFW6` bypasses) in `< 1 ms` with **0 SLA breaches**.
2. **Mode 02 — Amazon Leo Orbital Laser ISL & Ground Gateway Mesh (`Gateway-Redmond <-> LEO-Orb-14 <-> LEO-Orb-22 <-> Gateway-Frankfurt`)**:
   - Click **Inject Ka-Band Rain Fade (`5.4 dB` SNR) & Optical Link Flap (`LEO-Orb-14 <-> LEO-Orb-22`)** to watch dynamic link-state rerouting divert Gbps satellite traffic across `LEO-Orb-18 -> LEO-Orb-26 -> LEO-Orb-31` while **Amazon Bedrock (`us.amazon.nova-lite-v1:0`)** synthesizes a grounded link-state & RF telemetry diagnosis.
3. **Idempotent Double-Entry Carrier & Settlement Ledger**:
   - Click **Simulate 5x Duplicate SQS Retry Storm (`60` Deliveries)** to replay 12 carrier/orbital settlement events 5x each (including out-of-order timestamps across `US-WA`, `DE`, `JP`, and `BR` tax rules) and prove **`0` duplicate charges** and **`100%` balanced trial-balance conservation (`SUM(DEBITS) == SUM(CREDITS)`, `0` cents drift)**.

---

## Measured Benchmark Results (`benchmarks/latest_results.json`)

| Subsystem & Metric | Measured Result (`RouteLedger`) | Baseline Comparison |
| :--- | :---: | :---: |
| **Router p99 End-to-End Latency (`300` demands, `28` nodes)** | **132.80 ms (-74.78%)** | `526.47 ms` static shortest-path |
| **Router SLA Violations (`6` weather/outage events)** | **5 / 300 (1.67%; -95.33%)** | `107 / 300` (`35.67%`) static shortest-path |
| **Router Per-Demand Reroute Compute (`p50` / `p99`)** | **0.1211 ms p50** / **0.4648 ms p99** | Includes 2-way residual split flows |
| **Ledger Duplicate-Charge Elimination (`2,000` events, `15%` retries)** | **100.0% (0 double-charges)** | `245` double-charges (`$17,792.88` overbilled) |
| **Ledger Minor-Unit Rounding Drift & Trial Balance** | **0 cents drift (`SUM(Dr)==SUM(Cr)`)** | `60 cents` IEEE-754 float drift |
| **Ledger SQLite ACID Throughput** | **32,704.9 events/sec** | Full 2,000-event recon in `5.82 ms` |
| **NOC & Floor Root-Cause Diagnostic Accuracy (`150` incidents)** | **100.00% (2.41 mean checks)** | `64.00%` (`36.95` log lines scanned) |

---

## Run Locally & Deploy to Amazon CloudFront

```bash
# 1. Run all 28 unit, double-entry, workflow, and API tests
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
