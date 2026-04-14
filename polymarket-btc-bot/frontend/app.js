/**
 * app.js — Structural-arb dashboard
 * Polls the control API on the same host/port that serves /ui/ (see API_BASE).
 */

// Same host when dashboard is served from /ui/ on the control API; file:// falls back to loopback.
const API_BASE =
  window.location.protocol === "file:"
    ? "http://127.0.0.1:8765"
    : `${window.location.protocol}//${window.location.host}`;
const POLL_INTERVAL = 5000;

let refreshCountdown = POLL_INTERVAL / 1000;
let countdownTimer = null;
let pollTimer = null;

const EMBED_SPLIT =
  typeof window !== "undefined" &&
  new URLSearchParams(window.location.search).get("embed") === "split";

document.addEventListener("DOMContentLoaded", () => {
  if (EMBED_SPLIT) {
    document.body.classList.add("embed-split");
  }
  poll();
  startCountdown();
});

async function poll() {
  try {
    const [summary, orders, baskets, deposits] = await Promise.all([
      fetchJSON("/summary"),
      fetchJSON("/orders?limit=30"),
      fetchJSON("/baskets?limit=20"),
      fetchJSON("/funds/history?limit=20"),
    ]);

    const events = await fetchJSON("/events").catch(() => []);

    renderHeader(summary);
    renderStatCards(summary);
    renderProgress(summary);
    renderDiagnostics(summary);
    renderEventsList(Array.isArray(events) ? events : []);
    renderBasketsTable(Array.isArray(baskets) ? baskets : []);
    renderOrdersTable(Array.isArray(orders) ? orders : []);
    renderDepositsTable(Array.isArray(deposits) ? deposits : []);
  } catch (err) {
    setOfflineState();
  }

  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, POLL_INTERVAL);
  resetCountdown();
}

async function fetchJSON(path) {
  const res = await fetch(API_BASE + path);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function renderHeader(summary) {
  const agentEl = document.getElementById("agent-label");
  if (agentEl) {
    const an = String(summary.agent_display_name || "").trim();
    if (an) {
      agentEl.textContent = an;
      agentEl.classList.remove("hidden");
    } else {
      agentEl.textContent = "";
      agentEl.classList.add("hidden");
    }
  }

  const badge = document.getElementById("mode-badge");
  if (summary.paper_trade) {
    badge.textContent = "PAPER";
    badge.className = "badge badge-paper";
  } else {
    badge.textContent = "LIVE";
    badge.className = "badge badge-live";
  }

  const pill = document.getElementById("status-pill");
  const label = document.getElementById("status-label");
  if (summary.trading_halted) {
    pill.className = "status-pill status-halted";
    label.textContent = `Halted — ${summary.halt_reason || "unknown reason"}`;
  } else {
    pill.className = "status-pill status-running";
    label.textContent = "Running";
  }

  setText("hdr-cash", formatUSD(summary.cash));
  setText("bankroll", formatUSD(summary.equity));
  setText("hdr-contributed", formatUSD(summary.contributed_capital));

  const pnlEl = document.getElementById("daily-pnl");
  const rp = summary.realized_pnl;
  if (pnlEl) {
    pnlEl.textContent = formatPnL(rp);
    pnlEl.className = "fin-value pnl-amount " + pnlClass(rp);
  }
}

function setOfflineState() {
  const pill = document.getElementById("status-pill");
  const label = document.getElementById("status-label");
  pill.className = "status-pill status-connecting";
  label.textContent = "Bot offline";
  setText("hdr-cash", "—");
  setText("bankroll", "—");
  setText("hdr-contributed", "—");
  const pnlEl = document.getElementById("daily-pnl");
  if (pnlEl) {
    pnlEl.textContent = "—";
    pnlEl.className = "fin-value pnl-amount val-neutral";
  }
}

function renderStatCards(s) {
  const lcOp =
    s.last_cycle && s.last_cycle.opportunities != null ? s.last_cycle.opportunities : "—";
  setText("stat-opportunities-last-cycle", lcOp);
  setText("stat-executed-session", s.executed_count ?? "—");
  setText("stat-rejected-session", s.rejected_count ?? "—");
  const syn =
    s.last_cycle && s.last_cycle.books_synthetic != null ? s.last_cycle.books_synthetic : "—";
  setText("stat-books-synthetic", syn);
  setText("stat-open-baskets", s.open_baskets ?? "—");
  setText("stat-open-positions", s.open_positions ?? "—");
}

function renderDiagnostics(s) {
  const el = document.getElementById("diag-panel");
  if (!el) return;
  const d = s.last_cycle && s.last_cycle.diagnostics;
  const step = String(s.cycle_step || "").toLowerCase();
  const busy =
    step === "fetching_books" || step === "evaluating" || step === "scanning";
  if (!d || typeof d !== "object") {
    if (busy) {
      el.innerHTML =
        '<p class="empty-inline">Scanner diagnostics fill in when this cycle completes (books fetched → scan → risk → execution).</p>';
      return;
    }
    el.innerHTML = '<p class="empty-inline">No diagnostics yet (wait for one engine cycle).</p>';
    return;
  }
  const fmtBps = (v) => (v == null || Number.isNaN(Number(v)) ? "—" : formatNum(Number(v), 2));
  const fmtNrFloor = (v) => {
    const n = Number(v);
    if (v == null || Number.isNaN(n)) return "—";
    if (n >= 999000) return "off (execution disabled; diagnostics only)";
    return formatNum(n, 2);
  };
  const meetsCs = d.complete_set_best_edge_meets_floor === true;
  const nrOff = d.neg_risk_execution_disabled === true;
  const meetsNr =
    !nrOff &&
    d.neg_risk_best_edge_meets_floor === true;
  let alertHtml = "";
  if (!meetsCs) {
    alertHtml +=
      '<p class="diag-alert diag-alert-warn" role="status">Best <strong>complete-set</strong> edge at top-of-book is <strong>below</strong> your floor — the scanner will not open new complete-set arbs this cycle. Negative values mean the cheapest full YES basket still costs more than $1 per $1 of payout (after modeled taker fees), which is common in tight markets.</p>';
  }
  if (nrOff) {
    alertHtml +=
      '<p class="diag-alert diag-alert-muted" role="status">Neg-risk <strong>execution</strong> is disabled by config (high <code>MIN_NEG_RISK_EDGE_BPS</code>). Raw neg-risk edges are shown for monitoring only.</p>';
  } else if (!meetsNr && (d.neg_risk_priceable_events ?? 0) > 0) {
    alertHtml +=
      '<p class="diag-alert diag-alert-warn" role="status">Best <strong>neg-risk</strong> edge at top-of-book is below your floor — no new neg-risk baskets this cycle.</p>';
  }
  const lc = s.last_cycle || {};
  const effMax =
    lc.effective_max_basket_notional != null ? formatNum(Number(lc.effective_max_basket_notional), 2) : "—";
  const rows = [
    ["Effective max basket (this cycle)", effMax],
    ["Events in universe", d.events_in_universe ?? "—"],
    ["Neg-risk tagged (eligible structure)", d.neg_risk_tagged_events ?? "—"],
    [
      "Neg-risk: priced at TOB (not necessarily profitable)",
      d.neg_risk_priceable_events ?? "—",
    ],
    [
      "Complete-set: priced at TOB (not necessarily profitable)",
      d.complete_set_priceable_events ?? "—",
    ],
    ["Best complete-set edge vs floor", meetsCs ? "meets floor ✓" : "below floor"],
    ["Best neg-risk edge vs floor", nrOff ? "N/A (off)" : meetsNr ? "meets floor ✓" : "below floor"],
    ["Max raw complete-set edge (bps)", fmtBps(d.max_raw_complete_set_edge_bps)],
    ["Max raw neg-risk edge (bps)", fmtBps(d.max_raw_neg_risk_edge_bps)],
    ["Config floor complete-set (bps)", fmtBps(d.min_complete_set_edge_bps_config)],
    ["Config floor neg-risk (bps)", fmtNrFloor(d.min_neg_risk_edge_bps_config)],
  ];
  el.innerHTML = `${alertHtml}<dl class="diag-dl">${rows
    .map(
      ([k, v]) =>
        `<div class="diag-row"><dt>${escHtml(k)}</dt><dd>${escHtml(String(v))}</dd></div>`
    )
    .join("")}</dl>`;
}

function renderProgress(s) {
  const container = document.getElementById("cycle-progress");
  const stepLabel = document.getElementById("progress-step-label");
  const pctLabel = document.getElementById("progress-pct-label");
  const bar = document.getElementById("progress-bar-inner");

  const stepRaw = String(s && s.cycle_step != null ? s.cycle_step : "").toLowerCase();
  const isIdle =
    !s || stepRaw === "" || stepRaw === "idle" || stepRaw === "waiting_next_poll";

  if (isIdle) {
    if (container) container.classList.add("hidden");
    if (bar) bar.classList.remove("progress-bar-indeterminate");
    return;
  }

  if (container) container.classList.remove("hidden");

  const pctVal = s.cycle_progress_pct;
  const indeterminate =
    stepRaw === "evaluating" ||
    stepRaw === "scanning" ||
    pctVal === null ||
    pctVal === undefined ||
    Number.isNaN(Number(pctVal));

  if (indeterminate) {
    if (stepLabel) {
      stepLabel.textContent =
        stepRaw === "fetching_books"
          ? "Fetching Polymarket order books…"
          : "Running scanner, risk checks, execution, and settlement…";
    }
    if (pctLabel) pctLabel.textContent = "…";
    if (bar) {
      bar.style.width = "100%";
      bar.classList.add("progress-bar-indeterminate");
    }
    return;
  }

  if (bar) bar.classList.remove("progress-bar-indeterminate");
  const pct = Number(pctVal);
  const step = String(s.cycle_step || "active").replace(/_/g, " ");
  if (stepLabel) stepLabel.textContent = "Current task: " + step.charAt(0).toUpperCase() + step.slice(1) + "…";
  if (pctLabel) pctLabel.textContent = pct + "%";
  if (bar) bar.style.width = pct + "%";
}


function renderEventsList(events) {
  const el = document.getElementById("events-list");
  if (!el) return;
  const maxEv = EMBED_SPLIT ? 6 : 14;
  const slice = events.slice(0, maxEv);
  if (!slice.length) {
    el.innerHTML = '<p class="empty-inline">No events loaded yet (waiting for a cycle).</p>';
    return;
  }
  el.innerHTML = slice
    .map(
      (e) =>
        `<div class="event-row" title="${escHtml(e.event_id || "")}"><span class="ev-title">${escHtml(
          e.title || e.event_id || "—"
        )}</span><span class="ev-meta">${escHtml(e.status || "")}</span></div>`
    )
    .join("");
}

function renderBasketsTable(rows) {
  const tbody = document.getElementById("baskets-body");
  if (!tbody) return;
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No baskets yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows
    .map((b) => {
      const pnl = b.realized_net_pnl != null ? formatPnL(Number(b.realized_net_pnl)) : "—";
      const pnlCls = b.realized_net_pnl != null ? pnlClass(Number(b.realized_net_pnl)) : "val-neutral";
      const slippage = b.fill_slippage_bps != null ? formatNum(b.fill_slippage_bps, 2) : "—";
      return `<tr>
        <td>${formatTime(b.created_at)}</td>
        <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;" title="${escHtml(
          b.event_id || ""
        )}">${escHtml((b.strategy_type || "").replace(/_/g, " "))}</td>
        <td>${escHtml(b.status || "—")}</td>
        <td>${formatUSD(b.capital_reserved)}</td>
        <td class="${pnlCls}">${pnl}</td>
        <td>${slippage}</td>
        <td style="font-size:0.85rem;color:var(--text-secondary)">${escHtml(String(b.id || b.basket_id || "").slice(0, 14))}</td>
      </tr>`;
    })
    .join("");
}

function orderRouteClass(side) {
  const u = String(side || "").toUpperCase();
  if (u === "BUY") return "side-buy";
  if (u === "SELL") return "side-sell";
  return "";
}

function orderContractClass(cs) {
  const u = String(cs || "").toUpperCase();
  if (u === "YES") return "contract-yes";
  if (u === "NO") return "contract-no";
  return "contract-unknown";
}

function renderOrdersTable(orders) {
  const tbody = document.getElementById("orders-body");
  if (!tbody) return;
  if (!orders || orders.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty-state">No orders recorded yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = orders
    .map((o) => {
      const st = escHtml(o.status || "—");
      const leg = String(o.side || "").toUpperCase();
      const routeCls = orderRouteClass(o.side);
      const cs = (o.contract_side != null && String(o.contract_side).trim() !== "")
        ? String(o.contract_side).toUpperCase()
        : "";
      const outDisp = cs || "—";
      const outCls = orderContractClass(cs);
      return `<tr>
        <td>${formatTime(o.updated_at || o.created_at)}</td>
        <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;" title="${escHtml(
          o.market_id || ""
        )}">${escHtml(o.market_id || "—")}</td>
        <td class="${routeCls}">${escHtml(leg || "—")}</td>
        <td class="${outCls}">${escHtml(outDisp)}</td>
        <td>${formatNum(o.size)}</td>
        <td>${formatNum(o.price, 4)}</td>
        <td>${escHtml(o.maker_or_taker || "—")}</td>
        <td><span class="status-pill mini">${st}</span></td>
        <td style="font-size:0.8rem;color:var(--text-secondary)">${escHtml((o.basket_id || "").slice(0, 10))}</td>
      </tr>`;
    })
    .join("");
}

function renderDepositsTable(deposits) {
  const tbody = document.getElementById("deposits-body");
  if (!deposits || deposits.length === 0) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty-state">No deposits yet. Use "Add Money" to fund paper capital.</td></tr>`;
    return;
  }
  tbody.innerHTML = deposits
    .map(
      (d) => `
    <tr>
      <td>${formatTime(d.timestamp)}</td>
      <td class="val-positive">${formatUSD(d.amount)}</td>
      <td style="font-family:'Inter',sans-serif;color:var(--text-secondary)">${escHtml(d.note || "—")}</td>
    </tr>`
    )
    .join("");
}

async function haltAll() {
  try {
    await apiFetch("/halt", { method: "POST", body: { reason: "Manual halt via dashboard" } });
    poll();
  } catch (e) {
    alert("Failed to halt: " + e.message);
  }
}

async function resumeAll() {
  try {
    await apiFetch("/resume", { method: "POST", body: {} });
    poll();
  } catch (e) {
    alert("Failed to resume: " + e.message);
  }
}

function openAddMoneyModal() {
  document.getElementById("deposit-amount").value = "";
  document.getElementById("deposit-note").value = "";
  document.getElementById("modal-error").classList.add("hidden");
  document.getElementById("deposit-success").classList.add("hidden");
  document.getElementById("deposit-submit-btn").disabled = false;
  document.getElementById("modal-overlay").classList.remove("hidden");
  setTimeout(() => document.getElementById("deposit-amount").focus(), 80);
}

function closeAddMoneyModal(event) {
  if (event && event.target !== document.getElementById("modal-overlay")) return;
  document.getElementById("modal-overlay").classList.add("hidden");
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.getElementById("modal-overlay").classList.add("hidden");
});

async function submitDeposit() {
  const amountRaw = document.getElementById("deposit-amount").value.trim();
  const note = document.getElementById("deposit-note").value.trim();
  const errorEl = document.getElementById("modal-error");
  const successEl = document.getElementById("deposit-success");
  const submitBtn = document.getElementById("deposit-submit-btn");

  errorEl.classList.add("hidden");
  successEl.classList.add("hidden");

  const amount = parseFloat(amountRaw);
  if (!amountRaw || isNaN(amount) || amount <= 0) {
    errorEl.textContent = "Please enter a valid positive amount.";
    errorEl.classList.remove("hidden");
    return;
  }

  submitBtn.disabled = true;
  submitBtn.textContent = "Processing…";

  try {
    const result = await apiFetch("/funds/add", {
      method: "POST",
      body: { amount, note },
    });

    if (!result.ok) {
      errorEl.textContent = result.error || "Deposit failed.";
      errorEl.classList.remove("hidden");
      submitBtn.disabled = false;
      submitBtn.textContent = "Confirm Deposit";
      return;
    }

    const br = result.new_bankroll ?? result.new_equity;
    document.getElementById("new-bankroll-val").textContent = formatUSD(br);
    successEl.classList.remove("hidden");
    submitBtn.textContent = "Done ✓";

    setTimeout(() => {
      poll();
    }, 600);
  } catch (err) {
    errorEl.textContent = "Could not connect to the bot API. Is the bot running?";
    errorEl.classList.remove("hidden");
    submitBtn.disabled = false;
    submitBtn.textContent = "Confirm Deposit";
  }
}

async function apiFetch(path, { method = "GET", body } = {}) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(API_BASE + path, opts);
  return res.json();
}

function startCountdown() {
  clearInterval(countdownTimer);
  countdownTimer = setInterval(() => {
    refreshCountdown = Math.max(0, refreshCountdown - 1);
    const el = document.getElementById("refresh-timer");
    if (el) el.textContent = `Refreshing in ${refreshCountdown}s`;
  }, 1000);
}

function resetCountdown() {
  refreshCountdown = POLL_INTERVAL / 1000;
}

function formatUSD(val) {
  if (val == null || val === "") return "—";
  return "$" + Number(val).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatNum(val, digits = 2) {
  if (val == null || val === "") return "—";
  return Number(val).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function formatPnL(val) {
  if (val == null) return "—";
  const n = Number(val);
  const sign = n >= 0 ? "+" : "";
  return sign + formatUSD(Math.abs(n)).replace("$", n >= 0 ? "+$" : "-$").replace("+-$", "-$").replace("++$", "+$");
}

function pnlClass(val) {
  if (val == null) return "val-neutral";
  return val > 0 ? "val-positive" : val < 0 ? "val-negative" : "val-neutral";
}

function formatTime(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return ts;
  }
}

function escHtml(str) {
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? "—";
}
