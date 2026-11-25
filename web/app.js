/* RouteLedger Mission Control Runtime — Radar, Laser Constellation & ACID Ledger */
(function () {
  "use strict";

  const STATE = {
    mode: "scot",
    faultMode: "nominal",
    scotDemand: 3200,
    leoDemandGbps: 28.0,
    sliderTimer: null,
  };

  function el(id) {
    return document.getElementById(id);
  }

  function esc(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(path, options) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...(options || {}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "HTTP " + r.status);
    return d;
  }

  function syncSlider() {
    const s = el("demand-slider");
    if (STATE.mode === "scot") {
      s.min = "1200";
      s.max = "7400";
      s.step = "200";
      s.value = String(STATE.scotDemand);
      el("vol-label").textContent = "SCOT PARCEL DEMAND";
      el("vol-readout").textContent = `${STATE.scotDemand.toLocaleString()} u/hr`;
      el("vol-readout").style.color = "var(--amber)";
      s.style.accentColor = "var(--amber)";
    } else {
      s.min = "10";
      s.max = "74";
      s.step = "2";
      s.value = String(STATE.leoDemandGbps);
      el("vol-label").textContent = "LEO ORBITAL TRUNK";
      el("vol-readout").textContent = `${STATE.leoDemandGbps.toFixed(1)} Gbps`;
      el("vol-readout").style.color = "var(--cyan)";
      s.style.accentColor = "var(--cyan)";
    }
  }

  function drawRadarSvg(topo) {
    const svg = el("topology-svg");
    const isLeo = topo.mode === "leo";
    const nodesById = {};
    (topo.nodes || []).forEach((n) => {
      nodesById[n.id] = n;
    });

    const p = [];
    p.push(`
      <defs>
        <pattern id="rgrid" width="32" height="32" patternUnits="userSpaceOnUse">
          <path d="M 32 0 L 0 0 0 32" fill="none" stroke="rgba(56,189,248,0.08)" stroke-width="0.8"/>
        </pattern>
        <filter id="glow-beam" x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="2.5" result="blur"/>
          <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>
      <rect width="920" height="405" fill="url(#rgrid)"/>
      <circle cx="460" cy="202" r="175" fill="none" stroke="rgba(22,48,82,0.55)" stroke-dasharray="3 5"/>
      <circle cx="460" cy="202" r="295" fill="none" stroke="rgba(22,48,82,0.4)" stroke-dasharray="3 6"/>
      <line x1="460" y1="0" x2="460" y2="405" stroke="rgba(22,48,82,0.35)"/>
      <line x1="0" y1="202" x2="920" y2="202" stroke="rgba(22,48,82,0.35)"/>
    `);

    if (isLeo) {
      p.push(`
        <line x1="36" y1="98" x2="884" y2="98" stroke="rgba(56,189,248,0.22)" stroke-dasharray="5 6"/>
        <text x="42" y="90" font-family="IBM Plex Mono, monospace" font-size="9.5" fill="#7da0c4">ORBITAL SHELL P1 // 590 KM LASER ISL</text>
        <line x1="36" y1="212" x2="884" y2="212" stroke="rgba(56,189,248,0.16)" stroke-dasharray="5 6"/>
        <text x="42" y="204" font-family="IBM Plex Mono, monospace" font-size="9.5" fill="#7da0c4">ORBITAL SHELL P2 // 610 KM DIVERSITY</text>
        <path d="M 0 370 Q 460 300 920 370 L 920 405 L 0 405 Z" fill="rgba(14,52,89,0.34)" stroke="#1f538a" stroke-width="1.4"/>
      `);
    } else {
      p.push(`
        <text x="20" y="24" font-family="IBM Plex Mono, monospace" font-size="10" fill="#7da0c4">SCOT ATROPS CORRIDOR RADAR // CPT PULL WINDOW &le; 24.0H</text>
      `);
    }

    // Arcs
    (topo.arcs || []).forEach((a) => {
      const u = nodesById[a.src];
      const v = nodesById[a.dst];
      if (!u || !v) return;

      const active = isLeo ? a.flow_gbps > 0 : a.flow_units > 0;
      const out = Boolean(a.outage);
      const deg = Boolean(a.degraded);

      let baseStroke = "rgba(125,160,196,0.28)";
      let w = 1.5;
      let dash = "3 5";

      if (out) {
        baseStroke = "#fb7185";
        w = 2.4;
        dash = "5 4";
      } else if (deg) {
        baseStroke = "#fbbf24";
        w = 2.4;
        dash = "6 4";
      } else if (active) {
        baseStroke = isLeo ? "#38bdf8" : "#fbbf24";
        w = 3.2;
        dash = "";
      }

      p.push(
        `<line x1="${u.x}" y1="${u.y}" x2="${v.x}" y2="${v.y}" stroke="${baseStroke}" stroke-width="${w}" ${dash ? `stroke-dasharray="${dash}"` : ""} ${active ? 'filter="url(#glow-beam)"' : ""}/>`
      );
      if (active && !out) {
        p.push(
          `<line x1="${u.x}" y1="${u.y}" x2="${v.x}" y2="${v.y}" stroke="${isLeo ? "#e0f2fe" : "#fef3c7"}" stroke-width="2" class="beam-flow"/>`
        );
      }

      const mx = (u.x + v.x) / 2;
      const my = (u.y + v.y) / 2;
      let tag = "";
      if (out) {
        tag = isLeo ? "ISL FLAP / DOWN" : "BLIZZARD OUTAGE";
      } else if (deg) {
        tag = isLeo ? `${a.snr_db}dB RAIN FADE` : `THROTTLED ${a.utilization_pct}%`;
      } else if (active) {
        tag = isLeo
          ? `${a.flow_gbps}G · ${a.snr_db}dB (${a.utilization_pct}%)`
          : `${a.flow_units}u/h · $${a.cost_per_unit.toFixed(2)} (${a.utilization_pct}%)`;
      } else {
        tag = isLeo ? `${a.snr_db}dB standby` : `$${a.cost_per_unit.toFixed(2)}/u`;
      }

      const tw = Math.max(76, tag.length * 6 + 12);
      const tBg = out
        ? "rgba(159,18,57,0.92)"
        : deg
        ? "rgba(146,64,14,0.92)"
        : active
        ? "#071426"
        : "rgba(5,14,28,0.82)";
      const tBorder = out
        ? "#fb7185"
        : deg
        ? "#fbbf24"
        : active
        ? isLeo
          ? "#38bdf8"
          : "#fbbf24"
        : "#163052";
      const tText = out || deg || active ? "#f8fafc" : "#7da0c4";

      p.push(`
        <g transform="translate(${mx - tw / 2}, ${my - 9})">
          <rect width="${tw}" height="18" rx="4" fill="${tBg}" stroke="${tBorder}" stroke-width="1"/>
          <text x="${tw / 2}" y="12.5" text-anchor="middle" font-family="IBM Plex Mono, monospace" font-size="9" fill="${tText}">${esc(tag)}</text>
        </g>
      `);
    });

    // Nodes
    (topo.nodes || []).forEach((n) => {
      const w = isLeo ? 136 : 120;
      const h = 42;
      const nx = n.x - w / 2;
      const ny = n.y - h / 2;

      const isFault = n.status === "OUTAGE" || n.status === "DEGRADED";
      if (isFault) {
        p.push(
          `<circle cx="${n.x}" cy="${n.y}" r="24" fill="none" stroke="#fb7185" stroke-width="2" class="outage-pulse"/>`
        );
      }

      let fill = "#08172c";
      let stroke = "#1e3f6d";
      let titleCol = "#e2f1ff";
      let subCol = "#7da0c4";

      if (n.status === "OUTAGE") {
        fill = "#4c0519";
        stroke = "#fb7185";
        titleCol = "#ffe4e6";
        subCol = "#fda4af";
      } else if (n.status === "DEGRADED") {
        fill = "#451a03";
        stroke = "#fbbf24";
        titleCol = "#fef3c7";
        subCol = "#fcd34d";
      } else if (n.status === "ACTIVE") {
        stroke = isLeo ? "#38bdf8" : "#fbbf24";
      }

      p.push(`
        <g transform="translate(${nx}, ${ny})">
          <rect width="${w}" height="${h}" rx="7" fill="${fill}" stroke="${stroke}" stroke-width="1.8"/>
          <text x="${w / 2}" y="17.5" text-anchor="middle" font-family="Barlow, sans-serif" font-size="12" font-weight="700" fill="${titleCol}">${esc(n.id)}</text>
          <text x="${w / 2}" y="31.5" text-anchor="middle" font-family="IBM Plex Mono, monospace" font-size="8.8" fill="${subCol}">${esc(n.sub)}</text>
        </g>
      `);
    });

    svg.innerHTML = p.join("");
  }

  function renderMission(topo) {
    drawRadarSvg(topo);

    const opt = topo.optimizer_summary || {};
    const base = topo.static_baseline_comparison || {};
    const hasFault = topo.fault_mode !== "nominal";

    el("mission-shell").classList.toggle("is-alert", hasFault);
    el("g-compute").textContent = `${topo.compute_time_ms} ms`;

    if (topo.mode === "scot") {
      el("map-head").innerHTML =
        `SCOT FREIGHT RADAR // SEA1 &rarr; SLC2 &rarr; DEN4 &rarr; ORD5 &rarr; CVG3 &rarr; EWR7 (${esc(topo.fault_mode.toUpperCase())})`;
      el("cost-rtt-pill").textContent = `$${Number(opt.avg_cost_per_unit_usd || 0).toFixed(2)}/unit · ${opt.max_corridor_transit_hr}h CPT`;
      el("g-sla").textContent = `${opt.cpt_sla_pct}% (${opt.sla_breaches_units} breach)`;
      el("g-sla").className = opt.sla_breaches_units === 0 ? "good" : "bad";

      el("hud-comparison").innerHTML =
        `<span class="amber">[SCOT MIN-COST SOLVER · ${topo.compute_time_ms} ms]</span> Routed <strong>${opt.routed_units_hr.toLocaleString()}/${topo.demand_units_hr.toLocaleString()} u/hr</strong> within 24.0h CPT SLA (<strong class="good">${opt.sla_breaches_units} breaches</strong>) &middot; Static <code>SEA1&rarr;ORD5&rarr;EWR7</code> plan delivered <strong>${base.delivered_on_time_units.toLocaleString()} u/hr</strong> (<span class="bad">${base.sla_breaches_units.toLocaleString()} stalled</span>).`;

      const cors = opt.active_corridors || [];
      el("subflow-badge").textContent = `${cors.length} Active Corridor(s)`;
      el("subflow-badge").className = "amber small";
      el("subflow-list").innerHTML = cors
        .map(
          (c, i) => `
          <div class="sf-card">
            <div style="display:flex;justify-content:space-between;">
              <strong>#${i + 1} ${esc(c.route_label)}</strong>
              <strong class="amber">${c.units_routed.toLocaleString()} u/h</strong>
            </div>
            <div class="dim">${c.transit_hours}h transit &middot; $${c.cost_per_unit_usd.toFixed(2)}/u &middot; $${c.total_corridor_cost_usd.toLocaleString()}/hr</div>
          </div>
        `
        )
        .join("");
    } else {
      el("map-head").innerHTML =
        `AMAZON LEO LASER ISL CONSTELLATION // REDMOND &harr; ORB-14/18/22/26/31 &harr; FRANKFURT (${esc(topo.fault_mode.toUpperCase())})`;
      el("cost-rtt-pill").textContent = `${opt.end_to_end_rtt_ms} ms RTT · ${topo.demand_gbps} Gbps`;
      el("g-sla").textContent = opt.sla_violated ? `DROP ${opt.dropped_gbps}G` : `100% (0.0G drop)`;
      el("g-sla").className = opt.sla_violated ? "bad" : "good";

      el("hud-comparison").innerHTML =
        `<span class="cyan">[LEO LINK-STATE ROUTER · ${topo.compute_time_ms} ms]</span> Delivered <strong>${topo.demand_gbps} Gbps</strong> at <strong>${opt.end_to_end_rtt_ms} ms RTT</strong> (<strong class="good">${opt.dropped_gbps} Gbps loss</strong>) &middot; Static OSPF blackholed <span class="bad">${base.dropped_gbps} Gbps</span> (${base.end_to_end_rtt_ms} ms RTT).`;

      const sfs = opt.subflows || [];
      el("subflow-badge").textContent = `${sfs.length} Laser Subflow(s)`;
      el("subflow-badge").className = "cyan small";
      el("subflow-list").innerHTML = sfs
        .map(
          (sf, i) => `
          <div class="sf-card leo-sf">
            <div style="display:flex;justify-content:space-between;">
              <strong>Subflow #${i + 1}</strong>
              <strong class="cyan">${sf.allocated_gbps} Gbps &middot; ${sf.rtt_ms} ms</strong>
            </div>
            <div class="dim">${esc(sf.route_label)}</div>
          </div>
        `
        )
        .join("");
    }

    // Fault Tree + Bedrock Flight/Network Triage
    const wf = topo.workflow_diagnosis;
    const bd = topo.bedrock_advisory;
    let html = "";
    if (wf) {
      html += `
        <div style="display:flex;justify-content:space-between;font:600 11px var(--mono);margin-bottom:5px;">
          <span class="bad">${esc(wf.severity)} // ${esc(wf.root_cause_code)}</span>
          <span class="good">${wf.checks_executed}/3 checks (${wf.eval_time_ms}ms)</span>
        </div>
        ${(wf.trace || [])
          .map(
            (s) =>
              `<div class="tree-step">[${s.step_index}] <code>${esc(s.expression)}</code> &rarr; <strong class="cyan">${esc(s.observed_value)}</strong> (${s.passed ? "TRUE" : "FALSE"})</div>`
          )
          .join("")}
        <div class="small good" style="font-family:var(--mono);margin-top:4px;">Runbook: ${esc(wf.recommended_action)}</div>
      `;
    } else {
      html += `<div class="dim small" style="font-family:var(--mono);">All links nominal. Flip any Sabotage switch above (or click Bedrock Triage) to run &le;3-check isolation + Amazon Bedrock synthesis.</div>`;
    }

    if (bd && bd.text) {
      html += `
        <div class="bedrock-box"><div class="cyan small" style="font-family:var(--mono);margin-bottom:4px;">${esc(bd.engine || "aws-bedrock")} ${bd.latency_ms ? `· ${bd.latency_ms} ms` : ""}</div>${esc(bd.text)}</div>
      `;
    }
    el("triage-console").innerHTML = html;

    if (topo.ledger) renderLedger(topo.ledger);
  }

  function renderLedger(ledger, tamperRes) {
    const tb = ledger.trial_balance || {};
    const recon = ledger.reconciliation || {};
    const storm = ledger.last_retry_storm;

    el("g-balance").textContent = tb.is_balanced ? `${tb.total_debits_usd} OK` : "DRIFT";

    const bar = el("conservation-bar");
    if (tamperRes) {
      bar.innerHTML =
        `&#128737; <strong class="amber">SHA-256 PAYLOAD TAMPER REJECTED:</strong> Reused key <code>IDEM-SCOT-SEA1-ORD5-001</code> with mutated quantity raised <code>${esc(tamperRes.exception_type)}</code> &mdash; Trial Balance intact: <strong>${esc(tb.total_debits_usd)} == ${esc(tb.total_credits_usd)}</strong> (0c drift).`;
    } else if (storm) {
      bar.innerHTML =
        `&#9889; <strong>60x SQS RETRY STORM (${storm.sqs_deliveries_received} msgs / ${storm.unique_events_settled} unique):</strong> Deduplicated <strong>${storm.duplicate_retries_blocked}</strong> retries &rarr; <strong class="good">0 duplicate charges</strong> in RouteLedger vs. <strong class="bad">${storm.naive_processor_duplicate_charges} double-charges (${esc(storm.naive_overbilled_usd)} overbilled)</strong> in naive float baseline &middot; <strong>${esc(tb.total_debits_usd)} DR == ${esc(tb.total_credits_usd)} CR</strong>.`;
    } else {
      bar.innerHTML =
        `&#10003; <strong>TRIAL BALANCE CONSERVED:</strong> <code>SUM(DEBITS) = ${esc(tb.total_debits_usd)}</code> == <code>SUM(CREDITS) = ${esc(tb.total_credits_usd)}</code> &middot; Drift: <strong>0 cents</strong> &middot; OOO Watermarks: <strong>${recon.out_of_order_events_handled || 0}</strong>.`;
    }

    el("receipts-tbody").innerHTML = (ledger.recent_events || [])
      .map(
        (r) => `
        <tr>
          <td><code>${esc(r.idempotency_key)}</code></td>
          <td>${esc(r.account_id)}</td>
          <td class="dim">${esc(r.region)}</td>
          <td class="r"><strong class="good">${esc(r.total_usd)}</strong></td>
          <td class="dim">${r.is_out_of_order ? '<span class="amber">[OOO]</span> ' : ""}sha256:${esc(r.payload_sha256)}</td>
        </tr>
      `
      )
      .join("");

    el("journal-tbody").innerHTML = (ledger.journal_entries || [])
      .map(
        (j) => `
        <tr>
          <td class="dim">#${j.entry_id}</td>
          <td><code>${esc(j.idempotency_key)}</code></td>
          <td>${esc(j.ledger_account)}</td>
          <td class="${j.direction === "DEBIT" ? "dr" : "cr"}">${esc(j.direction)}</td>
          <td class="r">${esc(j.amount_usd)}</td>
        </tr>
      `
      )
      .join("");
  }

  async function optimize(opts) {
    const o = opts || {};
    const res = await api("/api/topology/optimize", {
      method: "POST",
      body: JSON.stringify({
        mode: STATE.mode,
        fault_mode: STATE.faultMode,
        demand_units: STATE.scotDemand,
        demand_gbps: STATE.leoDemandGbps,
        invoke_bedrock: Boolean(o.invokeBedrock),
        auto_settle: Boolean(o.autoSettle),
      }),
    });
    renderMission(res);
  }

  function setMode(m) {
    STATE.mode = m;
    STATE.faultMode = "nominal";
    el("btn-mode-scot").className = m === "scot" ? "m-btn active-scot" : "m-btn";
    el("btn-mode-leo").className = m === "leo" ? "m-btn active-leo" : "m-btn";
    el("actions-scot").classList.toggle("hidden", m !== "scot");
    el("actions-leo").classList.toggle("hidden", m !== "leo");
    syncSlider();
    optimize({ invokeBedrock: false });
  }

  async function boot() {
    syncSlider();
    const [health, snap] = await Promise.all([
      api("/api/health"),
      api("/api/state?mode=scot"),
    ]);
    el("health-label").textContent =
      `CloudFront + Private S3 OAC · ${health.bedrock_model} (${health.aws_region})`;

    renderMission(snap.topology);
    renderLedger(snap.ledger);

    el("btn-mode-scot").addEventListener("click", () => setMode("scot"));
    el("btn-mode-leo").addEventListener("click", () => setMode("leo"));

    el("demand-slider").addEventListener("input", (ev) => {
      const v = Number(ev.target.value);
      if (STATE.mode === "scot") {
        STATE.scotDemand = v;
        el("vol-readout").textContent = `${v.toLocaleString()} u/hr`;
      } else {
        STATE.leoDemandGbps = v;
        el("vol-readout").textContent = `${v.toFixed(1)} Gbps`;
      }
      clearTimeout(STATE.sliderTimer);
      STATE.sliderTimer = setTimeout(() => optimize({ invokeBedrock: false }), 85);
    });

    // SCOT Sabotage
    el("btn-scot-blizzard").addEventListener("click", () => {
      STATE.faultMode = "ord5_blizzard";
      optimize({ invokeBedrock: false, autoSettle: true });
    });
    el("btn-scot-den4").addEventListener("click", () => {
      STATE.faultMode = "den4_conveyor_jam";
      optimize({ invokeBedrock: false, autoSettle: true });
    });
    el("btn-scot-clear").addEventListener("click", () => {
      STATE.faultMode = "nominal";
      optimize({ invokeBedrock: false });
    });
    el("btn-scot-bedrock").addEventListener("click", async () => {
      const b = el("btn-scot-bedrock");
      const orig = b.innerHTML;
      b.textContent = "Querying Bedrock...";
      b.disabled = true;
      try {
        await optimize({ invokeBedrock: true });
      } finally {
        b.innerHTML = orig;
        b.disabled = false;
      }
    });

    // Leo Sabotage
    el("btn-leo-rain").addEventListener("click", async () => {
      STATE.faultMode = "rain_fade_and_isl_flap";
      const b = el("btn-leo-rain");
      const orig = b.innerHTML;
      b.textContent = "Rerouting ISL & Calling Bedrock...";
      b.disabled = true;
      try {
        await optimize({ invokeBedrock: true, autoSettle: true });
      } finally {
        b.innerHTML = orig;
        b.disabled = false;
      }
    });
    el("btn-leo-scint").addEventListener("click", () => {
      STATE.faultMode = "handoff_isl_congestion";
      optimize({ invokeBedrock: true });
    });
    el("btn-leo-clear").addEventListener("click", () => {
      STATE.faultMode = "nominal";
      optimize({ invokeBedrock: false });
    });
    el("btn-leo-bedrock").addEventListener("click", async () => {
      const b = el("btn-leo-bedrock");
      const orig = b.innerHTML;
      b.textContent = "Querying Bedrock...";
      b.disabled = true;
      try {
        await optimize({ invokeBedrock: true });
      } finally {
        b.innerHTML = orig;
        b.disabled = false;
      }
    });

    // Ledger
    el("btn-commit-route").addEventListener("click", async () => {
      const r = await api("/api/ledger/settle-route", {
        method: "POST",
        body: JSON.stringify({
          mode: STATE.mode,
          volume: STATE.mode === "scot" ? STATE.scotDemand : STATE.leoDemandGbps,
          fault_mode: STATE.faultMode,
        }),
      });
      renderLedger(r.ledger);
    });

    el("btn-storm").addEventListener("click", async () => {
      const b = el("btn-storm");
      const orig = b.innerHTML;
      b.textContent = "Replaying 60x SQS Storm...";
      b.disabled = true;
      try {
        const r = await api("/api/ledger/retry-storm", { method: "POST", body: "{}" });
        renderLedger(r.ledger);
      } finally {
        b.innerHTML = orig;
        b.disabled = false;
      }
    });

    el("btn-tamper").addEventListener("click", async () => {
      const t = await api("/api/ledger/tamper-test", { method: "POST", body: "{}" });
      const snap = await api("/api/state?mode=" + STATE.mode);
      renderLedger(snap.ledger, t);
    });

    el("btn-reset").addEventListener("click", async () => {
      const r = await api("/api/reset", { method: "POST", body: "{}" });
      STATE.mode = "scot";
      STATE.faultMode = "nominal";
      STATE.scotDemand = 3200;
      el("btn-mode-scot").className = "m-btn active-scot";
      el("btn-mode-leo").className = "m-btn";
      el("actions-scot").classList.remove("hidden");
      el("actions-leo").classList.add("hidden");
      syncSlider();
      renderMission(r.topology);
      renderLedger(r.ledger);
    });
  }

  boot().catch((err) => {
    el("health-label").textContent = "Offline: " + err.message;
  });
})();
