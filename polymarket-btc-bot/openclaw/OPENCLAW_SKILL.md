# Skill: Polymarket Structural-Arb Controller

**Name:** Polymarket Structural-Arb Controller  
**Description:** Monitor and control the **paper-first structural-arbitrage** Polymarket bot on this machine (`python -m src`). It scans complete-set and negative-risk opportunities, executes on a paper exchange, and exposes a local REST API. Legacy per-crypto directional trading is **not** the active runtime.

---

## Triggers and Commands

### Check bot status

**Triggers:** "bot status", "how is the bot doing", "check bot", "bot health", "is the bot running"

**Preferred (canonical):**
```bash
curl -s http://127.0.0.1:8765/summary | python -m json.tool
```

**Legacy-shaped JSON (maps arb fields into old key names for scripts):**
```bash
curl -s http://127.0.0.1:8765/stats | python -m json.tool
```

**What it returns:** Halt state, equity (`bankroll` in `/stats` compat), realized PnL, open baskets/positions, session execution counters, tracked event count.

---

### Pause / halt the bot

**Triggers:** "pause bot", "stop trading", "halt bot", "stop the bot", "pause trading"

```bash
curl -s -X POST http://127.0.0.1:8765/halt \
  -H "Content-Type: application/json" \
  -d "{\"reason\": \"manual via OpenClaw\"}"
```

New opportunities are not executed while halted. Existing paper positions remain until settlement.

---

### Resume trading

**Triggers:** "resume bot", "start trading again", "resume trading", "unpause bot"

```bash
curl -s -X POST http://127.0.0.1:8765/resume \
  -H "Content-Type: application/json" \
  -d "{}"
```

---

### Show recent activity (orders)

**Triggers:** "show recent trades", "last trades", "what trades did it make"

**Canonical:**
```bash
curl -s "http://127.0.0.1:8765/orders?limit=15" | python -m json.tool
```

**Legacy list shape (maps order rows into old trade-like fields):**
```bash
curl -s "http://127.0.0.1:8765/trades?limit=15" | python -m json.tool
```

---

### Trigger one scan cycle manually

```bash
curl -s -X POST http://127.0.0.1:8765/cycle \
  -H "Content-Type: application/json" \
  -d "{}"
```

---

### Restart the bot (Windows)

**Command:**
```cmd
nssm restart polymarket-bot
```

**Note:** Requires NSSM and a registered service. See `setup/windows_service.md`.

---

### Restart the bot (Linux)

```bash
sudo systemctl restart polymarket-bot
```

---

### Quick health

```bash
curl -s http://127.0.0.1:8765/health
```

---

## Schedule Hooks

### Morning summary

```bash
curl -s http://127.0.0.1:8765/summary
```

**Suggested narration:** Equity, realized PnL, halted yes/no, open baskets, last cycle opportunity count.

---

### Per-asset breakdown (legacy endpoint)

```bash
curl -s http://127.0.0.1:8765/stats/assets | python -m json.tool
```

**Note:** The structural-arb engine does not maintain per-spot-asset (BTC/ETH/…) PnL. This endpoint returns a **stable empty grid** for compatibility. Use `/summary`, `/positions`, and `/baskets` for real state.

---

### Disable a "specific asset" (legacy curl)

**Triggers:** "disable SOL trading", "halt ETH", …

```bash
curl -s -X POST http://127.0.0.1:8765/halt/asset \
  -H "Content-Type: application/json" \
  -d "{\"asset\": \"SOL\"}"
```

**Behavior:** Validates the asset symbol then applies a **global** arb halt (same as `POST /halt`). There is no per-asset arb isolation.

---

### Re-enable after legacy per-asset halt

```bash
curl -s -X POST http://127.0.0.1:8765/resume/asset \
  -H "Content-Type: application/json" \
  -d "{\"asset\": \"SOL\"}"
```

**Behavior:** **Global** resume (same as `POST /resume`).

---

## Notes for OpenClaw

- API binds to `127.0.0.1:8765` by default (`CONTROL_API_PORT`).
- If `CONTROL_API_TOKEN` is set, send `X-Control-Token: <token>` or `Authorization: Bearer <token>` on requests **except** `GET /health`.
- Default mode is **paper** (`PAPER_TRADE=true`).
- State lives in `data/trades.db` (legacy tables plus `arb_*` tables).
- Start the bot from the project root: `python -m src` (not `python -m src.main`).
