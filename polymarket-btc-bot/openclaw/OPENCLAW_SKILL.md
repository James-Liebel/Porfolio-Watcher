# Skill: Polymarket Bot Controller

**Name:** Polymarket Bot Controller  
**Description:** Monitor and control the Polymarket 5-minute BTC/ETH/SOL/XRP trading bot running on this machine. Uses GTC maker orders with postOnly flag to earn maker rebates. Manages four assets independently via per-asset halt/resume controls.

---

## Triggers and Commands

### Check bot status

**Triggers:** "bot status", "how is the bot doing", "check bot", "bot health", "is the bot running"

**Command:**
```bash
curl -s http://localhost:8765/stats | python -m json.tool
```

**What it returns:** JSON with current bankroll, daily PnL, trade counts, open positions, and whether trading is halted.

---

### Pause / halt the bot

**Triggers:** "pause bot", "stop trading", "halt bot", "stop the bot", "pause trading"

**Command:**
```bash
curl -s -X POST http://localhost:8765/halt \
  -H "Content-Type: application/json" \
  -d '{"reason": "manual via OpenClaw"}'
```

**Note:** The bot will stop placing new orders immediately. Open positions will still be monitored and resolved.

---

### Resume trading

**Triggers:** "resume bot", "start trading again", "resume trading", "unpause bot"

**Command:**
```bash
curl -s -X POST http://localhost:8765/resume
```

---

### Show recent trades

**Triggers:** "show recent trades", "last trades", "what trades did it make", "show me the trades"

**Command:**
```bash
curl -s "http://localhost:8765/trades?limit=10" | python -m json.tool
```

---

### Restart the bot (Windows)

**Triggers:** "restart bot" (on Windows)

**Command:**
```cmd
nssm restart polymarket-bot
```

**Note:** Requires NSSM to be installed and the service to be registered. See `setup/windows_service.md`.

---

### Restart the bot (Linux)

**Triggers:** "restart bot" (on Linux)

**Command:**
```bash
sudo systemctl restart polymarket-bot
```

---

### Check bot health (quick)

**Triggers:** "is the bot healthy", "quick health check"

**Command:**
```bash
curl -s http://localhost:8765/health
```

---

## Schedule Hooks

### Morning Daily Summary

**Schedule:** Every day at 08:00 local time

**Action:** Call the stats endpoint and report back a summary:

```bash
curl -s http://localhost:8765/stats
```

**OpenClaw should then say:**
> "Good morning! Here's the Polymarket bot summary for today:
> Bankroll: $[bankroll] | Daily PnL: $[daily_pnl] | Trades: [count] | Status: [halted/active]"

---

---

### Asset performance breakdown

**Triggers:** "which asset is doing best", "asset breakdown", "how are the assets doing", "per-asset stats"

**Command:**
```bash
curl -s http://localhost:8765/stats/assets | python -m json.tool
```

**What it returns:** JSON with trades, wins, PnL, and open positions for each of BTC, ETH, SOL, and XRP today.

---

### Disable a specific asset

**Triggers:** "disable SOL trading", "pause SOL", "stop trading SOL", "halt ETH", "disable XRP"

**Command (replace SOL with the target asset):**
```bash
curl -s -X POST http://localhost:8765/halt/asset \
  -H "Content-Type: application/json" \
  -d '{"asset": "SOL"}'
```

**Note:** Only stops new orders for that asset. Other assets continue trading normally.

---

### Re-enable a specific asset

**Triggers:** "enable SOL trading", "resume SOL", "start trading SOL again", "enable ETH", "resume XRP"

**Command (replace SOL with the target asset):**
```bash
curl -s -X POST http://localhost:8765/resume/asset \
  -H "Content-Type: application/json" \
  -d '{"asset": "SOL"}'
```

---

## Notes for OpenClaw

- The bot runs on `localhost:8765` — this is only accessible from the local machine.
- If `curl` returns a connection error, the bot process is not running. Use the restart command above.
- The bot operates in **PAPER_TRADE** mode by default. Real money orders are only placed when `PAPER_TRADE=false` is set in `.env`.
- All trade history is stored in `data/trades.db` (SQLite). Export with:
  ```bash
  sqlite3 data/trades.db .dump > trades_export.sql
  ```
- To check live logs on Windows: `Get-Content -Path logs\bot.log -Wait`
- To check live logs on Linux: `journalctl -u polymarket-bot -f`
