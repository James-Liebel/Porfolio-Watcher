# Polymarket 5-Minute BTC Window Trading Bot

A fully automated, production-ready trading bot that monitors Polymarket's 5-minute Bitcoin Up/Down prediction markets, streams live BTC prices from Binance and Coinbase simultaneously, and works maker orders dynamically when it detects a statistical edge.

> **Start with `PAPER_TRADE=true` for at least one week before risking real money.**

---

## Architecture Overview

```
Binance WS ──┐
             ├──► PriceAggregator (median VWAP)
Coinbase WS ─┘         │
                        ▼
                  TradingLoop (500ms)
                        │
       ┌────────────────┼────────────────┐
       ▼                ▼                ▼
  MarketScanner   SignalCalc      RiskManager
  (Gamma API)    (edge/delta)    (all guardrails)
       │                │                │
       └────────────────┼────────────────┘
                        ▼
                     Trader
                  (py-clob-client)
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
           SQLite DB          Telegram
         (trades.db)           alerts
              │
         ControlAPI
        (localhost:8765)
              │
          OpenClaw
         (NL control)
```

---

## Prerequisites

- Python 3.11 or newer
- A Polygon wallet funded with USDC (see `setup/polymarket_wallet.md`)
- A Polymarket account with L2 API keys
- A Telegram bot token (see [Telegram setup](#5-create-your-telegram-bot))
- Windows 10/11 or Linux (Ubuntu 22.04+ recommended)
- 12 GB RAM laptop or desktop running 24/7

---

## 1. Installation

```bash
git clone https://github.com/yourname/polymarket-btc-bot.git
cd polymarket-btc-bot

# Create virtual environment
python -m venv .venv

# Activate it
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## 2. Configuration

```bash
cp .env.example .env
```

Open `.env` in a text editor and fill in all values. See the [Environment Variable Reference](#environment-variable-reference) below.

---

## 3. How to Get Polymarket API Keys

Full step-by-step: [`setup/polymarket_wallet.md`](setup/polymarket_wallet.md)

**Quick summary:**
1. Create a Polygon wallet (MetaMask recommended)
2. Deposit USDC to Polygon (via Coinbase or Binance withdrawal to Polygon network)
3. Sign in to [polymarket.com](https://polymarket.com) with MetaMask
4. Go to Settings → API Keys → Create new key
5. Copy API Key, Secret, and Passphrase into `.env`

---

## 4. How to Fund Your Wallet

1. Buy USDC on Coinbase or Binance
2. Withdraw to your MetaMask wallet **on the Polygon network**
3. Also send ~$2 of MATIC for gas fees
4. Deposit from your wallet into your Polymarket balance on the site

Minimum recommended: **$100 USDC** to allow meaningful position sizing.

---

## 5. Create Your Telegram Bot

1. Open Telegram → search for `@BotFather`
2. Send `/newbot` → follow prompts → get your **Bot Token**
3. Get your chat ID: search `@userinfobot` → send `/start` → copy your ID
4. Add both to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_CHAT_ID=987654321
   ```

---

## 6. Running in Development / Paper Trade Mode

```bash
# Make sure PAPER_TRADE=true in .env first
python -m src.main
```

The bot will:
- Connect to Binance and Coinbase WebSocket feeds
- Scan Polymarket for active BTC windows every 60 seconds
- Calculate signals and log them without placing real orders
- Send Telegram alerts for every simulated trade
- Log everything to `data/trades.db`

Monitor via the control API:
```bash
curl http://localhost:8765/stats
```

**Scanner shows `found: 0`?** The bot only trades markets whose **end time** is between 10 seconds and 15 minutes from now. Polymarket’s Gamma API may not always list the 5-minute crypto windows in the `crypto` / `5M` feeds. If you see `raw_markets: 50` but `found: 0`, the API is working but no market is in that time window—often normal. If `raw_markets: 0`, the API or tags may have changed.

---

## 7. Running 24/7

### Windows (NSSM service)
See [`setup/windows_service.md`](setup/windows_service.md) for full instructions including:
- Installing NSSM
- Registering the bot as a Windows Service
- Configuring auto-start and logging
- Setting power options to prevent sleep

### Linux (systemd)
See [`setup/linux_service.md`](setup/linux_service.md) for full instructions including:
- Creating a systemd unit file
- Enabling auto-start on boot
- Preventing lid-close sleep on laptops
- Viewing logs via journalctl

---

## 8. Installing the OpenClaw Skill

OpenClaw lets you monitor and control the bot using natural language on your laptop.

1. Copy the skill file to OpenClaw's skills directory:
   ```bash
   # Windows example:
   copy openclaw\OPENCLAW_SKILL.md "%APPDATA%\OpenClaw\skills\"
   
   # Linux example:
   cp openclaw/OPENCLAW_SKILL.md ~/.openclaw/skills/
   ```
2. Open OpenClaw → Settings → Reload Skills
3. Test it: say "bot status" or "check the bot"

See [`openclaw/OPENCLAW_SKILL.md`](openclaw/OPENCLAW_SKILL.md) for all supported commands.

---

## 9. Environment Variable Reference

| Variable | Default | Description |
|---|---|---|
| `POLYMARKET_API_KEY` | required | Polymarket L2 API key |
| `POLYMARKET_SECRET` | required | Polymarket L2 API secret |
| `POLYMARKET_PASSPHRASE` | required | Polymarket L2 passphrase |
| `POLYMARKET_WALLET_ADDRESS` | required | Your Polygon wallet address (0x...) |
| `WALLET_PRIVATE_KEY` | required | Polygon wallet private key for signing |
| `POLYGON_RPC_URL` | `https://polygon-rpc.com` | Polygon JSON-RPC endpoint |
| `TELEGRAM_BOT_TOKEN` | required | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | required | Your Telegram user/chat ID |
| `EDGE_THRESHOLD` | `0.06` | Minimum edge (6%) to place a trade |
| `ENTRY_WINDOW_SECONDS` | `30` | Only trade in last N seconds of window |
| `MIN_SECONDS_REMAINING` | `3` | Never trade with fewer than N seconds left |
| `MAX_BET_FRACTION` | `0.06` | Hard cap: max 6% of bankroll per trade |
| `KELLY_FRACTION` | `0.20` | Use 20% of full Kelly sizing |
| `TARGET_EDGE_FOR_MAX_SIZE` | `0.12` | Edge level where dynamic sizing reaches max multiplier |
| `MIN_BET_USD` | `1.0` | Minimum stake for a qualifying signal |
| `DAILY_LOSS_CAP` | `0.10` | Halt if daily drawdown exceeds 10% |
| `MIN_MARKET_LIQUIDITY` | `750` | Skip markets with < $750 total depth |
| `MAX_CONCURRENT_POSITIONS` | `3` | Max open positions at once |
| `INITIAL_BANKROLL` | `300.0` | Starting bankroll in USD |
| `CONTROL_API_PORT` | `8765` | Port for local REST API |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG/INFO/WARNING/ERROR) |
| `PAPER_TRADE` | `true` | Shadow live maker behavior without sending live orders |
| `MAX_REPOSTS_PER_WINDOW` | `4` | Maximum cancel/repost attempts before the window expires |
| `REPOST_STALE_TICKS` | `2` | Requote only when the ideal maker price moves by at least 2 ticks |
| `CANCEL_AT_SECONDS_REMAINING` | `6` | Cancel any resting maker order with 6 seconds left |
| `MAX_MAKER_AGGRESSION_TICKS` | `3` | Maximum number of ticks inside the spread for dynamic maker pricing |

---

## 10. Adjusting Strategy Parameters

**`EDGE_THRESHOLD`** — The minimum model edge before an entry is allowed. Start at `0.06` (6%). If you're seeing too few trades, lower to `0.05`. If you're over-trading, raise to `0.08` or higher.

**`KELLY_FRACTION`** — Controls aggression. `0.20` is the new default because the sizing engine now uses the correct binary-contract Kelly formula instead of the older simplified version.

**`TARGET_EDGE_FOR_MAX_SIZE`** — Edge level where the dynamic sizer is allowed to reach its full multiplier. `0.12` is a reasonable default for short-dated 5-minute windows.

**`MAX_BET_FRACTION`** — Hard cap on any single stake. `0.06` keeps sizing bounded even when Kelly and liquidity both favor a larger order.

**`DAILY_LOSS_CAP`** — Safety net. At `0.10` the bot halts after losing 10% of starting bankroll for the day. You'll get a Telegram alert and can `/resume` via Telegram or OpenClaw.

**`ENTRY_WINDOW_SECONDS`** — Only enter in the last N seconds. `20`–`30` is optimal for 5-minute windows. Earlier entries give more time but worse signal quality.

**Paper trading parity** — In `PAPER_TRADE=true`, the bot now shadows the public Polymarket order book with the same cancel/repost cadence, maker-price logic, and stake-to-share conversion used in live trading. Logged paper fills therefore reflect whether the quoted maker order would actually have been touched, instead of treating the stake itself as the contract quantity.

---

## 11. Tax Tracking

Every trade is logged to `data/trades.db` (SQLite). To export:

```bash
# Export full dump
sqlite3 data/trades.db .dump > trades_export.sql

# Export trades as CSV
sqlite3 -csv -header data/trades.db "SELECT * FROM trades;" > trades.csv

# Export daily summaries
sqlite3 -csv -header data/trades.db "SELECT * FROM daily_summary;" > daily.csv
```

---

## Telegram Commands

Once the bot is running, send these commands to your Telegram bot:

| Command | Action |
|---|---|
| `/status` | Current stats: bankroll, PnL, trade counts |
| `/resume` | Resume trading after a halt |
| `/halt` | Manually halt trading |
| `/trades` | Last 5 trades |

---

## Disclaimer

This bot trades real money on prediction markets. Past performance does not guarantee future results. The calibrated probability table is based on historical BTC 5-minute price data and may not reflect future market conditions. Always start with paper trading, use only money you can afford to lose, and consult a financial advisor before deploying real capital.
