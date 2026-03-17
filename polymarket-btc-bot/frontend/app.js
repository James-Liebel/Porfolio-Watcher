/**
 * app.js — Polymarket Bot Dashboard
 * Polls the control API at localhost:8765 every 5 seconds and updates the UI.
 */

const API_BASE = "http://127.0.0.1:8765";
const POLL_INTERVAL = 5000;  // ms

// Tracks halted state per asset so the button label can toggle
const assetHaltedState = { BTC: false, ETH: false, SOL: false, XRP: false };

let refreshCountdown = POLL_INTERVAL / 1000;
let countdownTimer = null;
let pollTimer = null;

// ── Initialise ──────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  poll();
  startCountdown();
});

// ── Polling ─────────────────────────────────────────────────────────────────

async function poll() {
  try {
    const [health, stats, assets, trades, deposits] = await Promise.all([
      fetchJSON("/health"),
      fetchJSON("/stats"),
      fetchJSON("/stats/assets"),
      fetchJSON("/trades?limit=20"),
      fetchJSON("/funds/history?limit=20"),
    ]);

    renderHeader(health, stats);
    renderStatCards(stats);
    renderAssetGrid(assets);
    renderTradesTable(trades);
    renderDepositsTable(deposits);

  } catch (err) {
    setOfflineState();
  }

  // Schedule next poll
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, POLL_INTERVAL);
  resetCountdown();
}

async function fetchJSON(path) {
  const res = await fetch(API_BASE + path);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── Header / Status ─────────────────────────────────────────────────────────

function renderHeader(health, stats) {
  // Mode badge
  const badge = document.getElementById("mode-badge");
  if (health.paper_trade) {
    badge.textContent = "PAPER";
    badge.className = "badge badge-paper";
  } else {
    badge.textContent = "LIVE";
    badge.className = "badge badge-live";
  }

  // Status pill
  const pill = document.getElementById("status-pill");
  const label = document.getElementById("status-label");
  if (stats.trading_halted) {
    pill.className = "status-pill status-halted";
    label.textContent = `Halted — ${stats.halt_reason || "unknown reason"}`;
  } else {
    pill.className = "status-pill status-running";
    label.textContent = "Running";
  }

  // Bankroll
  document.getElementById("bankroll").textContent = formatUSD(stats.bankroll);

  // Daily PnL
  const pnlEl = document.getElementById("daily-pnl");
  pnlEl.textContent = formatPnL(stats.daily_pnl);
  pnlEl.className = "pnl-amount " + pnlClass(stats.daily_pnl);
}

function setOfflineState() {
  const pill = document.getElementById("status-pill");
  const label = document.getElementById("status-label");
  pill.className = "status-pill status-connecting";
  label.textContent = "Bot offline";
  document.getElementById("bankroll").textContent = "—";
  document.getElementById("daily-pnl").textContent = "—";
}

// ── Stat Cards ───────────────────────────────────────────────────────────────

function renderStatCards(stats) {
  const wins   = stats.daily_wins   || 0;
  const losses = stats.daily_losses || 0;
  const trades = stats.daily_trade_count || 0;
  const filled = wins + losses;
  const winrate = filled > 0 ? `${Math.round((wins / filled) * 100)}%` : "—";

  setText("stat-trades",    trades);
  setText("stat-wins",      wins);
  setText("stat-losses",    losses);
  setText("stat-winrate",   winrate);
  setText("stat-open",      stats.open_positions ?? "—");
  setText("stat-notfilled", stats.daily_not_filled ?? "—");
}

// ── Asset Grid ───────────────────────────────────────────────────────────────

function renderAssetGrid(assets) {
  for (const [asset, data] of Object.entries(assets)) {
    setText(`asset-${asset}-trades`, data.trades ?? 0);
    setText(`asset-${asset}-wins`,   data.wins   ?? 0);

    const pnlEl = document.getElementById(`asset-${asset}-pnl`);
    if (pnlEl) {
      pnlEl.textContent = formatPnL(data.pnl ?? 0);
      pnlEl.className   = `as-val pnl-val ${pnlClass(data.pnl ?? 0)}`;
    }

    setText(`asset-${asset}-open`, data.open ?? 0);

    // Card halted styling + button label
    const card    = document.getElementById(`asset-${asset}`);
    const haltBtn = document.getElementById(`halt-btn-${asset}`);
    if (data.halted) {
      card?.classList.add("halted");
      if (haltBtn) { haltBtn.textContent = "Resume"; haltBtn.classList.add("is-halted"); }
      assetHaltedState[asset] = true;
    } else {
      card?.classList.remove("halted");
      if (haltBtn) { haltBtn.textContent = "Halt";   haltBtn.classList.remove("is-halted"); }
      assetHaltedState[asset] = false;
    }
  }
}

// ── Trades Table ─────────────────────────────────────────────────────────────

function renderTradesTable(trades) {
  const tbody = document.getElementById("trades-body");
  if (!trades || trades.length === 0) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty-state">No trades recorded yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = trades.map(t => {
    const rowClass = t.outcome === "WIN" ? "row-win" : t.outcome === "LOSS" ? "row-loss" : "";
    const pnl = t.pnl != null ? formatPnL(t.pnl) : "—";
    const pnlCls = t.pnl != null ? pnlClass(t.pnl) : "val-neutral";
    return `
      <tr class="${rowClass}">
        <td>${formatTime(t.timestamp)}</td>
        <td>${t.asset || "BTC"}</td>
        <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;" title="${escHtml(t.question || t.market_id)}">${shortQuestion(t.question || t.market_id)}</td>
        <td class="${t.side === 'YES' ? 'side-yes' : 'side-no'}">${t.side}</td>
        <td>${formatUSD(t.bet_size)}</td>
        <td>${(t.limit_price ?? 0).toFixed(2)}</td>
        <td>${t.edge != null ? (t.edge * 100).toFixed(1) + "%" : "—"}</td>
        <td>${outcomeBadge(t.outcome)}</td>
        <td class="${pnlCls}">${pnl}</td>
        <td>${t.paper_trade ? "📄" : "🔴"}</td>
      </tr>`;
  }).join("");
}

// ── Deposits Table ────────────────────────────────────────────────────────────

function renderDepositsTable(deposits) {
  const tbody = document.getElementById("deposits-body");
  if (!deposits || deposits.length === 0) {
    tbody.innerHTML = `<tr><td colspan="3" class="empty-state">No deposits yet. Use "Add Money" to fund the bot.</td></tr>`;
    return;
  }
  tbody.innerHTML = deposits.map(d => `
    <tr>
      <td>${formatTime(d.timestamp)}</td>
      <td class="val-positive">${formatUSD(d.amount)}</td>
      <td style="font-family:'Inter',sans-serif;color:var(--text-secondary)">${escHtml(d.note || "—")}</td>
    </tr>`).join("");
}

// ── Action Handlers ───────────────────────────────────────────────────────────

async function haltAll() {
  try {
    await apiFetch("/halt", { method: "POST", body: { reason: "Manual halt via dashboard" } });
    poll();
  } catch (e) { alert("Failed to halt: " + e.message); }
}

async function resumeAll() {
  try {
    await apiFetch("/resume", { method: "POST", body: {} });
    poll();
  } catch (e) { alert("Failed to resume: " + e.message); }
}

async function toggleAssetHalt(asset) {
  const isHalted = assetHaltedState[asset];
  try {
    const endpoint = isHalted ? "/resume/asset" : "/halt/asset";
    await apiFetch(endpoint, { method: "POST", body: { asset } });
    poll();
  } catch (e) { alert(`Failed to toggle ${asset}: ` + e.message); }
}

// ── Add Money Modal ───────────────────────────────────────────────────────────

function openAddMoneyModal() {
  document.getElementById("deposit-amount").value = "";
  document.getElementById("deposit-note").value   = "";
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

// Allow Escape key to close
document.addEventListener("keydown", e => {
  if (e.key === "Escape") document.getElementById("modal-overlay").classList.add("hidden");
});

async function submitDeposit() {
  const amountRaw = document.getElementById("deposit-amount").value.trim();
  const note      = document.getElementById("deposit-note").value.trim();
  const errorEl   = document.getElementById("modal-error");
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

    document.getElementById("new-bankroll-val").textContent = formatUSD(result.new_bankroll);
    successEl.classList.remove("hidden");
    submitBtn.textContent = "Done ✓";

    // Refresh data after a moment
    setTimeout(() => { poll(); }, 600);

  } catch (err) {
    errorEl.textContent = "Could not connect to the bot API. Is the bot running?";
    errorEl.classList.remove("hidden");
    submitBtn.disabled = false;
    submitBtn.textContent = "Confirm Deposit";
  }
}

// ── HTTP helpers ──────────────────────────────────────────────────────────────

async function apiFetch(path, { method = "GET", body } = {}) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(API_BASE + path, opts);
  return res.json();
}

// ── Countdown timer ───────────────────────────────────────────────────────────

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

// ── Formatting helpers ────────────────────────────────────────────────────────

function formatUSD(val) {
  if (val == null || val === "") return "—";
  return "$" + Number(val).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatPnL(val) {
  if (val == null) return "—";
  const n = Number(val);
  const sign = n >= 0 ? "+" : "";
  return sign + formatUSD(Math.abs(n)).replace("$", (n >= 0 ? "+$" : "-$")).replace("+-$", "-$").replace("++$","+$");
}

// Simpler P&L formatter
function _formatPnL(val) {
  if (val == null) return "—";
  const n = Number(val);
  const prefix = n >= 0 ? "+$" : "-$";
  return prefix + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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
  } catch { return ts; }
}

function shortQuestion(q) {
  if (!q) return "—";
  return q.length > 38 ? q.slice(0, 38) + "…" : q;
}

function outcomeBadge(outcome) {
  const map = {
    WIN:        `<span class="outcome-badge badge-win">WIN</span>`,
    LOSS:       `<span class="outcome-badge badge-loss">LOSS</span>`,
    PENDING:    `<span class="outcome-badge badge-pending">PENDING</span>`,
    NOT_FILLED: `<span class="outcome-badge badge-not-filled">NOT FILLED</span>`,
  };
  return map[outcome] || `<span class="outcome-badge badge-not-filled">${escHtml(outcome || "—")}</span>`;
}

function escHtml(str) {
  return String(str).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? "—";
}
