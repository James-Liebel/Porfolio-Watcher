/**
 * Polls the local advisor service (python -m agents.advisor_app) for LLM commentary.
 */
const ADVISOR_BASE = "http://127.0.0.1:8780";
const POLL_MS = 45_000;

function renderAdviceText(text) {
  const el = document.getElementById("advice-body");
  if (!el) return;
  el.innerHTML = "";
  const pre = document.createElement("pre");
  pre.className = "advice-pre";
  pre.textContent = text;
  el.appendChild(pre);
}

async function pollAdvice() {
  const status = document.getElementById("advice-status");
  const meta = document.getElementById("advice-meta");
  try {
    const res = await fetch(ADVISOR_BASE + "/advice");
    const data = await res.json();
    if (!data.ok) {
      status.textContent = "Advisor error";
      meta.textContent = data.error || "LLM call failed";
      if (data.partial_context) {
        renderAdviceText(JSON.stringify(data.partial_context, null, 2));
      }
      return;
    }
    status.textContent = data.cached ? "Cached brief" : "Fresh brief";
    meta.textContent = "Provider: " + (data.provider || "—");
    renderAdviceText(data.markdown || "—");
  } catch (e) {
    status.textContent = "Advisor offline";
    meta.textContent = String(e.message || e);
    const el = document.getElementById("advice-body");
    if (el) {
      el.innerHTML =
        '<p class="advice-hint">Start <code>python -m agents.advisor_app</code> or run <code>scripts/run_two_structural_agents.py</code> (includes advisor). Ollama: <code>ollama serve</code> + pull a model.</p>';
    }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  pollAdvice();
  setInterval(pollAdvice, POLL_MS);
  const btn = document.getElementById("advice-refresh");
  if (btn) {
    btn.addEventListener("click", async () => {
      try {
        await fetch(ADVISOR_BASE + "/advice?refresh=1");
      } catch (_) {
        /* ignore */
      }
      await pollAdvice();
    });
  }
});
