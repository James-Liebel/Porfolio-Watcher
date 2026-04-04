# Agents lane (this repo)

## Two structural paper traders + LLM advisor

1. **Ollama (free, local)**  
   - Install [Ollama](https://ollama.com), run `ollama serve`, then e.g. `ollama pull llama3.2`.  
   - In `.env`: `LLM_PROVIDER=ollama`, optional `OLLAMA_MODEL`, `OLLAMA_BASE_URL`.

2. **OpenAI-compatible API** (Groq free tier, OpenRouter, OpenAI, etc.)  
   - `LLM_PROVIDER=openai_compatible`  
   - `OPENAI_API_KEY=...`  
   - `OPENAI_API_BASE=https://api.groq.com/openai/v1` (example)  
   - `OPENAI_MODEL=...`

3. **Run** (from `polymarket-btc-bot/`):  
   `python scripts/run_two_structural_agents.py`  
   Opens two bots ($100 each, tighter `MAX_BASKET_NOTIONAL`) and **`python -m agents.advisor_app`** on port **8780**.  
   Use `--no-advisor` if you have no LLM.  
   Dashboard: `frontend/agents-split.html`.

4. **Advisor only:** `python -m agents.advisor_app` (expects agents on `AGENT_A_PORT` / `AGENT_B_PORT`).

## Official Polymarket AI agents

**[Polymarket/agents](https://github.com/polymarket/agents)** is a separate LLM/RAG framework. This folder adds a **lightweight advisor** on top of your existing structural engine, not a port of that repo.
