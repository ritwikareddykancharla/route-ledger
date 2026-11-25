# RouteLedger v2: Multi-Modal ATROPS Pareto Solver, LEO 5-Subsystem MCP Diagnostics, Flight C++ Gate & ACID Double-Entry Ledger

**Live Production App (Amazon CloudFront + Private S3 OAC + Bedrock Nova):** https://d2x9ti2h7j9apb.cloudfront.net  
**CloudFront Distribution ID:** `E35WFLOHTA485K` (`us-west-2` `/api/*` Origin + Private S3 OAC `routeledger-web-796330847946-us-west-2`)  
**Live Endpoints:** `https://d2x9ti2h7j9apb.cloudfront.net/api/health` · `https://d2x9ti2h7j9apb.cloudfront.net/api/atrops/pareto` · `https://d2x9ti2h7j9apb.cloudfront.net/api/leo/diagnose`

**RouteLedger v2** is a routing, fulfillment optimization, satellite diagnostics, flight-safety static verification, and transactional settlement platform uniting five deterministic engines paired with live **Amazon Bedrock (`us.amazon.nova-lite-v1:0`)** synthesis:

1. **SCOT-FO Time-Expanded Multi-Modal Pareto Solver & Split-Box Sourcing Optimizer (`route_ledger/atrops_pareto.py`)**: Computes exact non-dominated 3D Pareto frontiers across **Prime Delivery Speed (`99.65%` SLA vs. `90.11%` Air-priority baseline, `+9.54 pp`)**, **Fulfillment Cost (`$3.024/pkg` vs. `$5.542/pkg`, `-45.44%`)**, and **Carbon Footprint (`1.323 kg CO2e/pkg` vs. `3.232 kg CO2e/pkg`, `-59.05%`)** over scheduled `AIR_CARGO`, `LINEHAUL_TRUCK`, `INTERMODAL_RAIL`, and `EV_MIDDLE_MILE` arcs governed by Critical Pull Time (`CPT`) cutoffs in **`0.0939 ms` p50 / `0.2169 ms` p99**. Includes combinatorial multi-item cart sourcing (`$8.61/order` and `6.88%` split rate vs. `$10.95/order` and `55.0%` splits on greedy nearest-FC, **`-21.42%` cost**) plus a counterfactual schedule-patch gate blocking **`12/12` (`100.0%`)** unsafe LLM station-overflow and CPT-causality violations before commit.
2. **5-Subsystem LEO Connectivity MCP Registry & JPL Power-of-10 Flight C++ Gate (`route_ledger/leo_flight_mcp.py`)**: Wraps `rf_link_budget`, `orbital_beam_handover`, `optical_isl_ber`, `terminal_modem_pll`, and `gateway_bgp_flap` as schema-validated read-only MCP tools, lifting launch-gate diagnostic accuracy from `83.33%` (`100/120`, 4-tool blind-spot suite) to **`100.0%` (`120/120`, full 5-tool suite)** in `0.0076 ms` mean dispatch. Pairs telemetry triage with a deterministic NASA/JPL Power-of-10 static C/C++ source verifier rejecting `malloc`/`new`, unbounded `while` loops, and direct or mutual call-graph recursion at **`100.0%` recall (`20/20` unsafe rejected)** and **`0.0%` false positives (`20/20` compliant RTOS handlers passed)**.
3. **Capacity- & SNR-Threshold Multi-Path Graph Solver (`route_ledger/network_router.py`)**: Models a `28`-node / `120`-directed-link multi-hop constellation and middle-mile mesh with per-edge capacity limits, convex M/M/1 queuing latency, `snr_db >= 8.0` pruning, and 2-way residual flow splitting, cutting p99 path latency from `526.47 ms` to **`132.80 ms` (`-74.78%`)** and SLA violations from `35.67%` (`107/300`) to **`1.67%` (`5/300`, `-95.33%`)** in **`0.0609 ms` p50 / `0.2680 ms` p99** compute time.
4. **Idempotent SQLite (`BEGIN IMMEDIATE` / WAL) ACID Double-Entry Ledger (`route_ledger/double_entry_ledger.py`)**: Settles at-least-once carrier and bandwidth metering streams (`2,000`-event benchmark at **`67,291.9 events/sec`** plus a 60-way concurrent SQLite WAL `ThreadPoolExecutor` retry storm) using canonical `SHA-256` payload seals and `Decimal(ROUND_HALF_UP)` minor-unit tax rules (`US-WA`, `DE`, `JP`, `BR`) with **`0` duplicate charges** and **`$0.00` (`0¢`) trial-balance drift (`19,376,379¢ Dr == Cr`)**.
5. **Bounded `<= 3`-Check Operational Fault-Tree Engine (`route_ledger/noc_workflow.py`)**: Isolates cascading hub and gateway alarms across `150` incidents (`5` alarm classes) at **`100.0%` (`150/150`) accuracy** in **`2.41` mean checks** (`max = 3`, `-93.47%` steps vs. `36.95` log lines scanned at `64.0%` baseline accuracy).

![RouteLedger CloudFront Architecture](docs/architecture.svg)

---

## Live Interactive Capabilities

1. **Tab 01: ATROPS Multi-Modal Pareto, Split-Box Cart Sourcing & LEO 5-Subsystem MCP / Flight C++ Gate (`/api/atrops/pareto` & `/api/leo/diagnose`)**:
   - Slide objective weights across **Prime Delivery Speed**, **Fulfillment Cost (`$/pkg`)**, and **Carbon (`kg CO2e`)**, inject a **`CVG3` Ice Ground-Stop** or **`ORD5` Yard Congestion**, compare **`Single-Box Consolidation`** vs. **`Split-FC Dispatch`** across `12h` Same-Day and `20h` 1-Day Prime carts, and test safe vs. unsafe LLM network configuration patches in simulation before commit.
   - Run the **5-Subsystem LEO MCP Registry** (`rf_link_budget`, `orbital_beam_handover`, `optical_isl_ber`, `terminal_modem_pll`, `gateway_bgp_flap`), toggle individual MCP tools off/on to reproduce missing-tool launch-gate blind spots, and validate C/C++ telemetry handlers against the **JPL Power-of-10 Zero-Heap & Zero-Recursion Static Gate**.
2. **Tab 02: Middle-Mile Fulfillment Corridor Graph (`SEA1 -> SLC2 -> DEN4 -> ORD5 -> CVG3 -> EWR7`)**:
   - Inject an **`ORD5` Blizzard Closure** or **`DEN4` Linehaul Outage** across up to `5,200 units/hr` to watch augmenting paths split freight onto `SLC2 -> CVG3` and `DFW6` bypasses within a `24.0h` CPT ceiling.
3. **Tab 03: SNR-Threshold Multi-Hop Relay Graph (`Gateway-Redmond <-> Plane-1 / Plane-2 <-> Gateway-Frankfurt`)**:
   - Inject **Ka-Band Rain Attenuation (`5.4 dB < 8.0 dB` threshold) & Laser ISL Flap** to divert up to `64.0 Gbps` across diversity relays with `0.0 Gbps` loss.
4. **Tab 04: 60-Delivery SQS Retry Storm & Tamper-Evident `SHA-256` ACID Double-Entry Ledger**:
   - Replay `60` at-least-once SQS deliveries (`12` unique events x `5x` replay) and trigger the **`IdempotencyConflictError` SQLite Tamper Trap** to verify `0` duplicate charges and `100%` balanced `SUM(Debits) == SUM(Credits)`.

---

## Measured Benchmark Results (`benchmarks/latest_results.json`, `seed=42`, `260.11 ms` total wall time)

| Subsystem & Metric | Measured Result (`RouteLedger v2`) | Baseline Comparison (`run_benchmarks.py`) |
| :--- | :---: | :---: |
| **ATROPS Multi-Modal Pareto (`240` waves, `668,723` pkgs)** | **`99.65%` SLA · `$3.024/pkg` (`-45.44%`) · `1.323 kg CO2e` (`-59.05%`)** | `90.11%` SLA · `$5.542/pkg` · `3.232 kg CO2e` Air-priority (`0.0939 ms` p50 / `0.2169 ms` p99) |
| **Multi-Item Cart Sourcing (`160` Prime carts, `2-4` SKUs)** | **`$8.61/order` (`-21.42%`) · `6.88%` split rate · `91.25%` SLA** | `$10.95/order` · `55.0%` splits on Greedy Nearest-FC (`84.38%` SLA on Single-Box-Only) |
| **Counterfactual LLM Schedule Patch Gate (`24` patches)** | **`12/12` valid approved · `12/12` (`100.0%`) unsafe blocked** | Catches downstream station sort-cap overflows & backward CPT rollovers |
| **LEO 5-Subsystem MCP Triage (`120` incidents, `6` classes)** | **`100.0%` (`120/120`) accuracy · `100.0%` coverage (Launch Gate Pass)** | `83.33%` (`100/120`, Launch Gate Fail) on 4-tool suite missing `orbital_beam_handover` |
| **Flight C/C++ Power-of-10 Static Verifier (`40` handlers)** | **`100.0%` (`20/20`) unsafe blocked · `0.0%` (`0/20`) false positives** | Deterministically rejects `malloc`/`new`, unbounded `while`, and mutual recursion (`0.133 ms`) |
| **Graph Solver p99 Path Latency (`300` demands, `28` nodes)** | **`132.80 ms` (`-74.78%`) · `5/300` (`1.67%`, `-95.33%`) SLA breaches** | `526.47 ms` p99 · `107/300` (`35.67%`) breaches on static shortest-path (`0.0609 ms` p50) |
| **Ledger ACID Settlement (`2,000` events, `15%` retries)** | **`0` double-charges · `0¢` drift (`19,376,379¢ Dr == Cr`) · `67,291.9 ev/s`** | `245` duplicate charges (`+$17,792.88` overbilled) & `60¢` float drift |
| **Fault-Tree Root-Cause Accuracy (`150` incidents, `5` classes)** | **`100.0%` (`150/150`) accuracy · `2.41` mean checks (`max = 3`)** | `64.0%` (`96/150`) across `36.95` scanned log lines (`-93.47%` steps) |

---

## Run Locally & Deploy to Amazon CloudFront

```bash
# 1. Run all 38 unit, ATROPS Pareto, LEO MCP, Power-of-10 C++ gate, and SQLite WAL storm tests
python3 -m unittest discover -s tests -v

# 2. Reproduce all benchmark metrics in benchmarks/latest_results.json (260.11 ms)
python3 benchmarks/run_benchmarks.py

# 3. Start the interactive RouteLedger v2 workbench locally
python3 -m route_ledger.web_api
# -> Open http://localhost:3097

# 4. Deploy to Amazon CloudFront + Private S3 (OAC) + Lambda /api/* + Amazon Bedrock (us-west-2)
chmod +x infra/deploy_cloudfront.sh
./infra/deploy_cloudfront.sh
```
