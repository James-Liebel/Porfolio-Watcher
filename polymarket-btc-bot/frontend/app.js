/**
 * app.js — Structural-arb dashboard
 * Polls ArbControlAPI (python -m src) at 127.0.0.1:8765.
 */

const API_BASE = "http://127.0.0.1:8765";
const POLL_INTERVAL = 5000;

let refreshCountdown = POLL_INTERVAL / 1000;
let countdownTimer = null;
let pollTimer = null;

document.addEventListener("DOMContentLoaded", () => {
  poll();
  startCountdown();
});

async function poll() {
  try {
    const [health, summary, orders, baskets, deposits] = await Promise.all([
      fetchJSON("/health"),
      fetchJSON("/summary"),
      fetchJSON("/orders?limit=30"),
      fetchJSON("/baskets?limit=20"),
      fetchJSON("/funds/history?limit=20"),
    ]);

    const events = await fetchJSON("/events").catch(() => []);

    renderHeader(health, summary);
    renderStatCards(summary);
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

function renderHeader(health, summary) {
  const badge = document.getElementById("mode-badge");
  if (health.paper_trade) {
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

  document.getElementById("bankroll").textContent = formatUSD(summary.equity);

  const pnlEl = document.getElementById("daily-pnl");
  const rp = summary.realized_pnl;
  pnlEl.textContent = formatPnL(rp);
  pnlEl.className = "pnl-amount " + pnlClass(rp);
}

function setOfflineState() {
  const pill = document.getElementById("status-pill");
  const label = document.getElementById("status-label");
  pill.className = "status-pill status-connecting";
  label.textContent = "Bot offline";
  document.getElementById("bankroll").textContent = "—";
  document.getElementById("daily-pnl").textContent = "—";
}

function renderStatCards(s) {
  setText("stat-trades", s.latest_opportunities ?? "—");
  setText("stat-wins", s.executed_count ?? "—");
  setText("stat-losses", s.rejected_count ?? "—");
  const lc = s.last_cycle && s.last_cycle.opportunities != null ? s.last_cycle.opportunities : "—";
  setText("stat-winrate", lc);
  setText("stat-open", s.open_baskets ?? "—");
  setText("stat-notfilled", s.open_positions ?? "—");
}

function renderEventsList(events) {
  const el = document.getElementById("events-list");
  if (!el) return;
  const slice = events.slice(0, 14);
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
    tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No baskets yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows
    .map((b) => {
      const pnl = b.realized_net_pnl != null ? formatPnL(Number(b.realized_net_pnl)) : "—";
      const pnlCls = b.realized_net_pnl != null ? pnlClass(Number(b.realized_net_pnl)) : "val-neutral";
      return `<tr>
        <td>${formatTime(b.created_at)}</td>
        <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;" title="${escHtml(
          b.event_id || ""
        )}">${escHtml((b.strategy_type || "").replace(/_/g, " "))}</td>
        <td>${escHtml(b.status || "—")}</td>
        <td>${formatUSD(b.capital_reserved)}</td>
        <td class="${pnlCls}">${pnl}</td>
        <td style="font-size:0.85rem;color:var(--text-secondary)">${escHtml(String(b.id || b.basket_id || "").slice(0, 14))}</td>
      </tr>`;
    })
    .join("");
}

function renderOrdersTable(orders) {
  const tbody = document.getElementById("orders-body");
  if (!orders || orders.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state">No orders recorded yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = orders
    .map((o) => {
      const st = escHtml(o.status || "—");
      return `<tr>
        <td>${formatTime(o.updated_at || o.created_at)}</td>
        <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;" title="${escHtml(
          o.market_id || ""
        )}">${escHtml(o.market_id || "—")}</td>
        <td class="${String(o.side).toUpperCase() === "YES" ? "side-yes" : "side-no"}">${escHtml(
          String(o.side || "").toUpperCase()
        )}</td>
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
