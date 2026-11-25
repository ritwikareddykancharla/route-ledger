/* RouteLedger Blueprint Interactive Topology, Bedrock Diagnostics & Double-Entry Ledger */
(function () {
  "use strict";

  const root = document.documentElement;
  root.classList.add("js");

  /* ---------- Light / Dark Theme Toggle (dataset.theme + URL hash) ---------- */
  const themeBtn = document.querySelector(".theme");
  if (themeBtn) {
    themeBtn.addEventListener("click", () => {
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      const baseHash = (location.hash || "").replace(/(&|\?)?theme=(dark|light)/g, "").replace(/^#&/, "#");
      const suffix = "theme=" + next;
      const newHash = !baseHash || baseHash === "#" ? "#" + suffix : baseHash + "&" + suffix;
      history.replaceState(null, "", newHash);
      if (STATE.lastTopo) renderSvgTopology(STATE.lastTopo);
    });
  }

  /* ---------- Contents Sidebar Progress, Active Marker & Back-to-Top Ring ---------- */
  const HEADER = 92;
  const side = document.querySelector(".side");
  const postBody = document.querySelector(".post-body");
  const sideLinks = side ? [...side.querySelectorAll(".side-list a")] : [];
  const sideTargets = sideLinks.map((a) => document.getElementById(a.hash.slice(1)));
  const sideInd = side && side.querySelector(".side-ind");
  const sideBar = side && side.querySelector(".side-bar i");
  const sidePct = side && side.querySelector(".side-pct");
  const toTop = document.querySelector(".totop");
  const ring = toTop && toTop.querySelector(".ring");
  const RING = 2 * Math.PI * 21;
  if (ring) ring.style.strokeDasharray = String(RING);

  function updateScrollChrome() {
    const y = window.scrollY;
    const max = root.scrollHeight - window.innerHeight;
    const whole = max > 0 ? y / max : 0;
    if (toTop && ring) {
      toTop.classList.toggle("show", y > 520);
      ring.style.strokeDashoffset = String(RING * (1 - whole));
    }
    if (side && postBody && sideBar && sidePct) {
      const r = postBody.getBoundingClientRect();
      const read = Math.min(1, Math.max(0, (window.innerHeight * 0.45 - r.top) / Math.max(1, r.height)));
      sideBar.style.transform = `scaleX(${read})`;
      sidePct.textContent = Math.round(read * 100) + "%";
      let active = sideLinks[0] || null;
      sideTargets.forEach((t, i) => {
        if (t && t.getBoundingClientRect().top <= HEADER + 80) active = sideLinks[i];
      });
      sideLinks.forEach((a) => a.classList.toggle("on", a === active));
      if (sideInd && active) {
        const listRect = side.querySelector(".side-list").getBoundingClientRect();
        const aRect = active.getBoundingClientRect();
        sideInd.style.opacity = "1";
        sideInd.style.transform = `translateY(${aRect.top - listRect.top}px)`;
        sideInd.style.height = aRect.height + "px";
      }
    }
  }
  window.addEventListener("scroll", updateScrollChrome, { passive: true });
  window.addEventListener("resize", updateScrollChrome);

  /* ---------- RouteLedger State ---------- */
  const STATE = {
    mode: "scot",
    faultMode: "nominal",
    scotDemand: 3200,
    leoDemandGbps: 28.0,
    lastTopo: null,
    sliderTimer: null,
  };

  function el(id) {
    return document.getElementById(id);
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function apiFetch(path, options) {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...(options || {}),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "API error " + resp.status);
    }
    return data;
  }

  function syncSliderForMode() {
    const slider = el("demand-slider");
    if (STATE.mode === "scot") {
      slider.min = "1200";
      slider.max = "7400";
      slider.step = "200";
      slider.value = String(STATE.scotDemand);
      el("slider-title").textContent = "SCOT Parcel Demand:";
      el("slider-readout").textContent = `${STATE.scotDemand.toLocaleString()} units/hr`;
    } else {
      slider.min = "10";
      slider.max = "74";
      slider.step = "2";
      slider.value = String(STATE.leoDemandGbps);
      el("slider-title").textContent = "Orbital Trunk Demand:";
      el("slider-readout").textContent = `${STATE.leoDemandGbps.toFixed(1)} Gbps`;
    }
  }

  function renderSvgTopology(topo) {
    const svg = el("topology-svg");
    const stage = el("svg-canvas-wrap");
    const isLeo = topo.mode === "leo";
    stage.classList.toggle("leo-dark", isLeo);

    const nodesById = {};
    (topo.nodes || []).forEach((n) => {
      nodesById[n.id] = n;
    });

    const isDarkTheme = root.dataset.theme === "dark";
    const bgGridColor = isLeo
      ? "rgba(143,176,230,0.12)"
      : isDarkTheme
      ? "rgba(255,255,255,0.06)"
      : "rgba(53,82,126,0.11)";

    let parts = [];
    parts.push(`
      <defs>
        <pattern id="bp-grid" width="28" height="28" patternUnits="userSpaceOnUse">
          <path d="M 28 0 L 0 0 0 28" fill="none" stroke="${bgGridColor}" stroke-width="0.8"/>
        </pattern>
        <marker id="flow-ok" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M0 0L10 5L0 10z" fill="#0c8a4d"/>
        </marker>
        <marker id="flow-cyan" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M0 0L10 5L0 10z" fill="#38bdf8"/>
        </marker>
      </defs>
      <rect width="920" height="400" fill="url(#bp-grid)"/>
    `);

    if (isLeo) {
      // Subtle orbital plane guide lines & Earth ground horizon band
      parts.push(`
        <line x1="40" y1="100" x2="880" y2="100" stroke="rgba(143,176,230,0.22)" stroke-dasharray="4 6"/>
        <text x="48" y="92" font-family="Martian Mono, monospace" font-size="9.5" fill="#8fb0e6">ORBITAL PLANE 01 · LASER ISL RING (590 KM)</text>
        <line x1="40" y1="212" x2="880" y2="212" stroke="rgba(143,176,230,0.18)" stroke-dasharray="4 6"/>
        <text x="48" y="204" font-family="Martian Mono, monospace" font-size="9.5" fill="#8fb0e6">ORBITAL PLANE 02 · DIVERSITY SHELL (610 KM)</text>
        <path d="M 0 368 Q 460 302 920 368 L 920 400 L 0 400 Z" fill="rgba(18,115,122,0.22)" stroke="#12737a" stroke-width="1.2"/>
      `);
    } else {
      parts.push(`
        <text x="24" y="28" font-family="Martian Mono, monospace" font-size="10" fill="${isDarkTheme ? "#8b8b8b" : "#6b7a92"}">SCOT MIDDLE-MILE FULFILLMENT GRAPH · CPT SLA &le; 24.0H · MIN-COST AUGMENTING FLOW</text>
      `);
    }

    // Draw Arcs
    (topo.arcs || []).forEach((arc) => {
      const u = nodesById[arc.src];
      const v = nodesById[arc.dst];
      if (!u || !v) return;

      const activeFlow = isLeo ? arc.flow_gbps > 0 : arc.flow_units > 0;
      const isOutage = Boolean(arc.outage);
      const isDegraded = Boolean(arc.degraded);

      let stroke = isLeo ? "rgba(148,163,184,0.35)" : "#94a3b8";
      let width = 1.6;
      let dash = "3 4";
      let animClass = "";

      if (isOutage) {
        stroke = "#d4301a";
        width = 2.4;
        dash = "5 4";
      } else if (isDegraded) {
        stroke = "#ff6433";
        width = 2.4;
        dash = "6 4";
      } else if (activeFlow) {
        stroke = isLeo ? "#38bdf8" : "#0c8a4d";
        width = 3.4;
        dash = "";
        animClass = "animated-flow";
      }

      // Base line
      parts.push(
        `<line x1="${u.x}" y1="${u.y}" x2="${v.x}" y2="${v.y}" stroke="${stroke}" stroke-width="${width}" ${dash ? `stroke-dasharray="${dash}"` : ""} ${activeFlow && !isLeo ? 'marker-end="url(#flow-ok)"' : ""}/>`
      );
      if (activeFlow && !isOutage) {
        parts.push(
          `<line x1="${u.x}" y1="${u.y}" x2="${v.x}" y2="${v.y}" stroke="${isLeo ? "#a5f3fc" : "#34d399"}" stroke-width="2.2" class="${animClass}"/>`
        );
      }

      // Midpoint telemetry tag
      const mx = (u.x + v.x) / 2;
      const my = (u.y + v.y) / 2;
      let labelTxt = "";
      if (isOutage) {
        labelTxt = isLeo ? "FLAP / OUTAGE" : "BLIZZARD CLOSED";
      } else if (isDegraded) {
        labelTxt = isLeo ? `${arc.snr_db} dB RAIN FADE` : `THROTTLED (${arc.utilization_pct}%)`;
      } else if (activeFlow) {
        labelTxt = isLeo
          ? `${arc.flow_gbps}G · ${arc.snr_db}dB (${arc.utilization_pct}%)`
          : `${arc.flow_units}u/h · $${arc.cost_per_unit.toFixed(2)} (${arc.utilization_pct}%)`;
      } else {
        labelTxt = isLeo ? `${arc.snr_db}dB · idle` : `$${arc.cost_per_unit.toFixed(2)}/u`;
      }

      const pillW = Math.max(78, labelTxt.length * 6.1 + 10);
      const pillBg = isOutage
        ? "#d4301a"
        : isDegraded
        ? "#ff6433"
        : activeFlow
        ? isLeo
          ? "#0e1a2b"
          : "#0e1a2b"
        : isLeo
        ? "rgba(15,23,42,0.82)"
        : isDarkTheme
        ? "#262626"
        : "#ffffff";
      const pillInk =
        isOutage || isDegraded || activeFlow
          ? "#ffffff"
          : isLeo
          ? "#94a3b8"
          : isDarkTheme
          ? "#bdbdbd"
          : "#475569";
      const pillStroke = isOutage ? "#d4301a" : activeFlow ? stroke : "#64748b";

      parts.push(`
        <g transform="translate(${mx - pillW / 2}, ${my - 10})">
          <rect width="${pillW}" height="18" fill="${pillBg}" stroke="${pillStroke}" stroke-width="1"/>
          <text x="${pillW / 2}" y="12.5" text-anchor="middle" font-family="Martian Mono, monospace" font-size="9" fill="${pillInk}">${escapeHtml(labelTxt)}</text>
        </g>
      `);
    });

    // Draw Nodes (sharp rectangular blueprint blocks)
    (topo.nodes || []).forEach((n) => {
      const w = isLeo ? 136 : 122;
      const h = 44;
      const nx = n.x - w / 2;
      const ny = n.y - h / 2;

      let fill = isLeo ? "#102136" : isDarkTheme ? "#1d1d1d" : "#ffffff";
      let border = isLeo ? "#8fb0e6" : "#0e1a2b";
      let titleColor = isLeo ? "#f8fafc" : isDarkTheme ? "#f1f1f1" : "#0e1a2b";
      let subColor = isLeo ? "#93c5fd" : "#6b7a92";

      if (n.status === "OUTAGE") {
        fill = "#d4301a";
        border = "#0e1a2b";
        titleColor = "#ffffff";
        subColor = "#fee2e2";
      } else if (n.status === "DEGRADED") {
        fill = "#ff6433";
        border = "#0e1a2b";
        titleColor = "#ffffff";
        subColor = "#fff3ee";
      } else if (n.status === "ACTIVE") {
        border = isLeo ? "#38bdf8" : "#0c8a4d";
      }

      parts.push(`
        <g transform="translate(${nx}, ${ny})">
          <rect width="${w}" height="${h}" fill="${fill}" stroke="${border}" stroke-width="2"/>
          <text x="${w / 2}" y="18" text-anchor="middle" font-family="Schibsted Grotesk, sans-serif" font-size="11.5" font-weight="800" fill="${titleColor}">${escapeHtml(n.id)}</text>
          <text x="${w / 2}" y="33" text-anchor="middle" font-family="Martian Mono, monospace" font-size="8.8" fill="${subColor}">${escapeHtml(n.sub)}</text>
        </g>
      `);
    });

    svg.innerHTML = parts.join("");
  }

  function renderTopologyTelemetry(topo) {
    STATE.lastTopo = topo;
    renderSvgTopology(topo);

    const opt = topo.optimizer_summary || {};
    const base = topo.static_baseline_comparison || {};

    el("kpi-4-val").textContent = `${topo.compute_time_ms} ms`;

    if (topo.mode === "scot") {
      el("kpi-1-label").textContent = "SCOT Parcel Volume";
      el("kpi-1-val").textContent = `${topo.demand_units_hr.toLocaleString()} u/hr`;
      el("kpi-1-sub").textContent = `Fault: ${topo.fault_mode}`;

      el("kpi-2-label").textContent = "CPT SLA Compliance";
      el("kpi-2-val").textContent = `${opt.cpt_sla_pct}% (${opt.sla_breaches_units} breach)`;
      el("kpi-2-val").style.color = opt.sla_breaches_units === 0 ? "var(--ok)" : "var(--bad)";
      el("kpi-2-sub").textContent = `Static Plan: ${base.cpt_sla_pct}% SLA`;

      el("kpi-3-label").textContent = "Blended Freight / Unit";
      el("kpi-3-val").textContent = `$${Number(opt.avg_cost_per_unit_usd || 0).toFixed(2)} / unit`;
      el("kpi-3-sub").textContent = `Max Transit: ${opt.max_corridor_transit_hr}h (&le;24h CPT)`;

      el("optimizer-vs-static").innerHTML =
        `<strong>MIN-COST FLOW OPTIMIZER (${topo.compute_time_ms} ms):</strong> Routed <strong>${opt.routed_units_hr.toLocaleString()}/${topo.demand_units_hr.toLocaleString()} units/hr</strong> within 24.0h CPT SLA (<strong>${opt.sla_breaches_units} breaches</strong>) &middot; Static <code>SEA1&rarr;ORD5&rarr;EWR7</code> plan delivered <strong>${base.delivered_on_time_units.toLocaleString()} units/hr</strong> (${base.sla_breaches_units.toLocaleString()} stalled/breached).`;

      const cors = opt.active_corridors || [];
      el("corridor-split-badge").textContent = `${cors.length} Active Corridor(s)`;
      el("corridors-list").innerHTML = cors
        .map(
          (c, i) => `
          <div class="corridor-row">
            <div style="display:flex;justify-content:space-between;font-weight:500;color:var(--ink);">
              <span>#${i + 1} &middot; ${escapeHtml(c.route_label)}</span>
              <span style="color:var(--ok);">${c.units_routed.toLocaleString()} u/hr</span>
            </div>
            <div style="color:var(--soft);">transit=${c.transit_hours}h &middot; unit_cost=$${c.cost_per_unit_usd.toFixed(2)} &middot; total=$${c.total_corridor_cost_usd.toLocaleString()}</div>
          </div>
        `
        )
        .join("");
    } else {
      el("kpi-1-label").textContent = "Orbital Trunk Demand";
      el("kpi-1-val").textContent = `${topo.demand_gbps} Gbps`;
      el("kpi-1-sub").textContent = `Fault: ${topo.fault_mode}`;

      el("kpi-2-label").textContent = "Packet Delivery / SLA";
      el("kpi-2-val").textContent = opt.sla_violated
        ? `DEGRADED (${opt.dropped_gbps}G drop)`
        : `100% (0.0 Gbps drop)`;
      el("kpi-2-val").style.color = opt.sla_violated ? "var(--bad)" : "var(--ok)";
      el("kpi-2-sub").textContent = `Static OSPF Drop: ${base.dropped_gbps} Gbps`;

      el("kpi-3-label").textContent = "End-to-End Orbital RTT";
      el("kpi-3-val").textContent = `${opt.end_to_end_rtt_ms} ms`;
      el("kpi-3-sub").textContent = `Static OSPF RTT: ${base.end_to_end_rtt_ms} ms`;

      el("optimizer-vs-static").innerHTML =
        `<strong>DYNAMIC LINK-STATE REROUTER (${topo.compute_time_ms} ms):</strong> Delivered <strong>${topo.demand_gbps} Gbps</strong> at <strong>${opt.end_to_end_rtt_ms} ms RTT</strong> (${opt.dropped_gbps} Gbps dropped) &middot; Static OSPF baseline dropped <strong>${base.dropped_gbps} Gbps</strong> (${base.end_to_end_rtt_ms} ms RTT).`;

      const sfs = opt.subflows || [];
      el("corridor-split-badge").textContent = `${sfs.length} Orbital Subflow(s)`;
      el("corridors-list").innerHTML = sfs
        .map(
          (sf, i) => `
          <div class="corridor-row" style="border-left-color:var(--cobalt);">
            <div style="display:flex;justify-content:space-between;font-weight:500;color:var(--ink);">
              <span>Plane Subflow #${i + 1}</span>
              <span style="color:var(--ok);">${sf.allocated_gbps} Gbps &middot; ${sf.rtt_ms} ms</span>
            </div>
            <div style="color:var(--body);">${escapeHtml(sf.route_label)}</div>
          </div>
        `
        )
        .join("");
    }

    // Render Workflow Decision Tree + Amazon Bedrock Diagnosis
    const wf = topo.workflow_diagnosis;
    const bd = topo.bedrock_advisory;
    let diagHtml = "";
    if (wf) {
      diagHtml += `
        <div>
          <span class="mono-chip bad">${escapeHtml(wf.severity)} &middot; ${escapeHtml(wf.root_cause_code)}</span>
          <span class="mono-chip ok">${wf.checks_executed}/3 checks (${wf.eval_time_ms} ms)</span>
        </div>
        <div style="font:400 11px/1.55 var(--mono);color:var(--body);margin:4px 0 8px;">
          ${(wf.trace || [])
            .map(
              (s) =>
                `<div>[${s.step_index}] <code>${escapeHtml(s.expression)}</code> &rarr; observed=<strong>${escapeHtml(s.observed_value)}</strong> (${s.passed ? "TRUE" : "FALSE"})</div>`
            )
            .join("")}
          <div style="color:var(--ok);margin-top:4px;">Action: <code>${escapeHtml(wf.recommended_action)}</code></div>
        </div>
      `;
    } else {
      diagHtml += `<div style="font:400 11px var(--mono);color:var(--soft);">All nodes nominal. Inject a blizzard outage or Ka-band rain fade / optical flap (or click the Bedrock button) for live troubleshooting synthesis.</div>`;
    }

    if (bd && bd.text) {
      diagHtml += `
        <div style="margin-top:6px;">
          <span class="mono-chip ok">${escapeHtml(bd.engine || "aws-bedrock")}</span>
          ${bd.latency_ms ? `<span class="mono-chip">${bd.latency_ms} ms</span>` : ""}
        </div>
        <div class="prose-callout">${escapeHtml(bd.text)}</div>
      `;
    }
    el("bedrock-diagnosis-panel").innerHTML = diagHtml;

    if (topo.ledger) {
      renderLedgerSnapshot(topo.ledger);
    }
  }

  function renderLedgerSnapshot(ledger, conflictNotice) {
    const tb = ledger.trial_balance || {};
    const recon = ledger.reconciliation || {};
    const storm = ledger.last_retry_storm;

    el("kpi-5-val").textContent = tb.is_balanced
      ? `100% (${tb.total_debits_usd})`
      : "IMBALANCED";
    el("kpi-5-sub").textContent = `${recon.duplicate_retries_blocked || 0} SQS retries deduplicated`;

    const banner = el("ledger-proof-banner");
    if (conflictNotice) {
      banner.style.borderLeftColor = "var(--signal)";
      banner.innerHTML =
        `<strong>SHA-256 PAYLOAD INTEGRITY VERIFIED:</strong> Reused key <code>IDEM-SCOT-SEA1-ORD5-001</code> with mutated quantity rejected via <code>${escapeHtml(conflictNotice.exception_type)}</code> &mdash; Trial Balance remains <strong>${tb.total_debits_usd} == ${tb.total_credits_usd}</strong> (${tb.imbalance_cents}c drift).`;
    } else if (storm) {
      banner.style.borderLeftColor = "var(--ok)";
      banner.innerHTML =
        `<strong>5x SQS RETRY STORM PROOF (${storm.sqs_deliveries_received} deliveries / ${storm.unique_events_settled} unique):</strong> Blocked <strong>${storm.duplicate_retries_blocked}</strong> duplicate retries &rarr; <strong>0 duplicate charges</strong> in RouteLedger (vs. <strong>${storm.naive_processor_duplicate_charges} double-charges / ${escapeHtml(storm.naive_overbilled_usd)} overbilled</strong> in naive float processor). Trial Balance: <strong>${tb.total_debits_usd} DEBITS == ${tb.total_credits_usd} CREDITS</strong> (0c drift).`;
    } else {
      banner.style.borderLeftColor = "var(--ok)";
      banner.innerHTML =
        `<strong>DOUBLE-ENTRY CONSERVATION VERIFIED:</strong> <code>SUM(DEBITS) = ${escapeHtml(tb.total_debits_usd)}</code> == <code>SUM(CREDITS) = ${escapeHtml(tb.total_credits_usd)}</code> &middot; Imbalance: <strong>0 cents</strong> &middot; Out-of-Order Watermarks Handled: <strong>${recon.out_of_order_events_handled || 0}</strong>.`;
    }

    el("settlement-events-tbody").innerHTML = (ledger.recent_events || [])
      .map(
        (r) => `
        <tr>
          <td><code>${escapeHtml(r.idempotency_key)}</code></td>
          <td>${escapeHtml(r.account_id)}</td>
          <td><code>${escapeHtml(r.region)}</code></td>
          <td class="r"><strong>${escapeHtml(r.total_usd)}</strong></td>
          <td style="font:400 10.5px var(--mono);">
            ${r.is_out_of_order ? '<span style="color:var(--signal);font-weight:500;">[OOO]</span> ' : ""}
            sha256:${escapeHtml(r.payload_sha256)}
          </td>
        </tr>
      `
      )
      .join("");

    el("journal-entries-tbody").innerHTML = (ledger.journal_entries || [])
      .map(
        (j) => `
        <tr>
          <td style="font-family:var(--mono);">#${j.entry_id}</td>
          <td><code>${escapeHtml(j.idempotency_key)}</code></td>
          <td><code>${escapeHtml(j.ledger_account)}</code></td>
          <td class="dir-${escapeHtml(j.direction)}">${escapeHtml(j.direction)}</td>
          <td class="r">${escapeHtml(j.amount_usd)}</td>
        </tr>
      `
      )
      .join("");
  }

  async function runOptimize(opts) {
    const options = opts || {};
    const payload = {
      mode: STATE.mode,
      fault_mode: STATE.faultMode,
      demand_units: STATE.scotDemand,
      demand_gbps: STATE.leoDemandGbps,
      invoke_bedrock: Boolean(options.invokeBedrock),
      auto_settle: Boolean(options.autoSettle),
    };
    const res = await apiFetch("/api/topology/optimize", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderTopologyTelemetry(res);
  }

  function switchMode(newMode) {
    STATE.mode = newMode;
    STATE.faultMode = "nominal";
    el("mode-btn-scot").classList.toggle("active", newMode === "scot");
    el("mode-btn-leo").classList.toggle("active", newMode === "leo");
    el("toolbar-scot").classList.toggle("hidden", newMode !== "scot");
    el("toolbar-leo").classList.toggle("hidden", newMode !== "leo");
    syncSliderForMode();
    runOptimize({ invokeBedrock: false });
  }

  async function bootstrap() {
    updateScrollChrome();
    syncSliderForMode();

    const [health, stateSnap] = await Promise.all([
      apiFetch("/api/health"),
      apiFetch("/api/state?mode=scot"),
    ]);
    el("health-status-label").textContent =
      `Live: ${health.delivery} · ${health.bedrock_model} (${health.aws_region})`;

    renderTopologyTelemetry(stateSnap.topology);
    renderLedgerSnapshot(stateSnap.ledger);

    el("mode-btn-scot").addEventListener("click", () => switchMode("scot"));
    el("mode-btn-leo").addEventListener("click", () => switchMode("leo"));

    el("demand-slider").addEventListener("input", (ev) => {
      const val = Number(ev.target.value);
      if (STATE.mode === "scot") {
        STATE.scotDemand = val;
        el("slider-readout").textContent = `${val.toLocaleString()} units/hr`;
      } else {
        STATE.leoDemandGbps = val;
        el("slider-readout").textContent = `${val.toFixed(1)} Gbps`;
      }
      clearTimeout(STATE.sliderTimer);
      STATE.sliderTimer = setTimeout(() => runOptimize({ invokeBedrock: false }), 90);
    });

    // SCOT Fault Buttons
    el("btn-scot-blizzard").addEventListener("click", () => {
      STATE.faultMode = "ord5_blizzard";
      runOptimize({ invokeBedrock: false, autoSettle: true });
    });
    el("btn-scot-den4").addEventListener("click", () => {
      STATE.faultMode = "den4_conveyor_jam";
      runOptimize({ invokeBedrock: false, autoSettle: true });
    });
    el("btn-scot-nominal").addEventListener("click", () => {
      STATE.faultMode = "nominal";
      runOptimize({ invokeBedrock: false });
    });
    el("btn-scot-bedrock").addEventListener("click", async () => {
      const btn = el("btn-scot-bedrock");
      const orig = btn.innerHTML;
      btn.textContent = "Synthesizing Bedrock Dispatch...";
      btn.disabled = true;
      try {
        await runOptimize({ invokeBedrock: true });
      } finally {
        btn.innerHTML = orig;
        btn.disabled = false;
      }
    });

    // Leo Fault Buttons
    el("btn-leo-rainflap").addEventListener("click", async () => {
      STATE.faultMode = "rain_fade_and_isl_flap";
      const btn = el("btn-leo-rainflap");
      const orig = btn.innerHTML;
      btn.textContent = "Rerouting Mesh & Querying Bedrock Nova...";
      btn.disabled = true;
      try {
        await runOptimize({ invokeBedrock: true, autoSettle: true });
      } finally {
        btn.innerHTML = orig;
        btn.disabled = false;
      }
    });
    el("btn-leo-scint").addEventListener("click", () => {
      STATE.faultMode = "handoff_isl_congestion";
      runOptimize({ invokeBedrock: true });
    });
    el("btn-leo-nominal").addEventListener("click", () => {
      STATE.faultMode = "nominal";
      runOptimize({ invokeBedrock: false });
    });
    el("btn-leo-bedrock").addEventListener("click", async () => {
      const btn = el("btn-leo-bedrock");
      const orig = btn.innerHTML;
      btn.textContent = "Querying Bedrock Nova...";
      btn.disabled = true;
      try {
        await runOptimize({ invokeBedrock: true });
      } finally {
        btn.innerHTML = orig;
        btn.disabled = false;
      }
    });

    // Ledger Actions
    el("btn-settle-active-route").addEventListener("click", async () => {
      const res = await apiFetch("/api/ledger/settle-route", {
        method: "POST",
        body: JSON.stringify({
          mode: STATE.mode,
          volume: STATE.mode === "scot" ? STATE.scotDemand : STATE.leoDemandGbps,
          fault_mode: STATE.faultMode,
        }),
      });
      renderLedgerSnapshot(res.ledger);
    });

    el("btn-retry-storm").addEventListener("click", async () => {
      const btn = el("btn-retry-storm");
      const orig = btn.innerHTML;
      btn.textContent = "Replaying 60 SQS Messages (5x Duplicate Storm)...";
      btn.disabled = true;
      try {
        const res = await apiFetch("/api/ledger/retry-storm", {
          method: "POST",
          body: "{}",
        });
        renderLedgerSnapshot(res.ledger);
      } finally {
        btn.innerHTML = orig;
        btn.disabled = false;
      }
    });

    el("btn-tamper-payload").addEventListener("click", async () => {
      const conflict = await apiFetch("/api/ledger/tamper-test", {
        method: "POST",
        body: "{}",
      });
      const snap = await apiFetch("/api/state?mode=" + STATE.mode);
      renderLedgerSnapshot(snap.ledger, conflict);
    });

    el("btn-reset-all").addEventListener("click", async () => {
      const r = await apiFetch("/api/reset", { method: "POST", body: "{}" });
      STATE.mode = "scot";
      STATE.faultMode = "nominal";
      STATE.scotDemand = 3200;
      syncSliderForMode();
      el("mode-btn-scot").classList.add("active");
      el("mode-btn-leo").classList.remove("active");
      el("toolbar-scot").classList.remove("hidden");
      el("toolbar-leo").classList.add("hidden");
      renderTopologyTelemetry(r.topology);
      renderLedgerSnapshot(r.ledger);
    });
  }

  bootstrap().catch((err) => {
    el("health-status-label").textContent = "API Error: " + err.message;
  });
})();
