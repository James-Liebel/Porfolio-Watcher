/**
 * Polls the local advisor service (python -m agents.advisor_app) for LLM commentary.
 */
const ADVISOR_BASE =
  (typeof window !== "undefined" && window.__ADVISOR_BASE__) || "http://127.0.0.1:8780";
const POLL_MS = 45_000;

function escAdvice(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

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
      status.classList.add("advice-error");
      const errLine = data.error || "LLM call failed";
      meta.textContent = errLine;
      const body = document.getElementById("advice-body");
      if (body) {
        const hint =
          "<p class=\"advice-hint\">Check <code>ollama serve</code>, <code>ollama pull " +
          (typeof window !== "undefined" && window.__OLLAMA_MODEL__
            ? window.__OLLAMA_MODEL__
            : "llama3.2") +
          "</code>, and <code>ADVISOR_HTTP_TIMEOUT</code> (default 240s) if the model is slow.</p>";
        let details = "";
        if (data.partial_context) {
          details =
            '<details class="advice-raw"><summary>Technical context (JSON)</summary><pre class="advice-pre">' +
            escAdvice(JSON.stringify(data.partial_context, null, 2)) +
            "</pre></details>";
        }
        body.innerHTML = hint + details;
      }
      return;
    }
    status.classList.remove("advice-error");
    status.textContent = data.cached ? "Cached brief" : "Fresh brief";
    meta.textContent = "Provider: " + (data.provider || "—");
    renderAdviceText(data.markdown || "—");
  } catch (e) {
    status.classList.add("advice-error");
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
