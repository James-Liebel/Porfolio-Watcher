# Polymarket Structural-Arbitrage Bot

This repository now boots a **paper-first structural-arbitrage runtime** for Polymarket.

Active runtime:
- **Real-time order-book streaming** over the Polymarket CLOB market WebSocket, with an event-driven hot path that re-scans and executes in **milliseconds** when a book changes (built to place trades before REST pollers)
- Negative-risk conversion scanning
- Complete-set / basket mispricing scanning
- Queue-aware enough paper exchange state for fills, positions, conversions, and settlement
- **Gated live execution adapter** for real Polymarket orders (OFF by default, behind multiple hard safety gates + dry-run)
- Local control API for operations and monitoring, including a `/streaming` (`/latency`) health view

The older 5-minute directional crypto modules are still in the tree as legacy code, but `python -m src` now starts the structural-arb system.

## Runtime Layout

```text
Gamma API ───────────────► Universe Service
                               │
Public CLOB books ───────► Market Data Service
                               │
                               ▼
                        Opportunity Scanner
                     (complete-set + neg-risk)
                               │
                               ▼
                          Risk Manager
                               │
                               ▼
                          Paper Exchange
                  (orders, fills, positions, conversion,
                   settlement, bankroll locks)
                               │
                               ▼
                           SQLite Store
                               │
                               ▼
                          Local Control API
```

Primary modules:
- `src/arb/universe.py`
- `src/arb/market_data.py`
- `src/arb/pricing.py`
- `src/arb/exchange.py`
- `src/arb/engine.py`
- `src/arb/control.py`

Implementation blueprint:
- `NEG_RISK_ARB_BLUEPRINT.md`

Current runtime behavior, paper-vs-market alignment, and tuning/extension hooks:
- `ARB_RUNTIME_LOGIC.md`

## Quick Start (first-time setup)

From the `polymarket-btc-bot` directory:

**Windows (PowerShell)**

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env: at minimum keep PAPER_TRADE=true for structural arb
.\.venv\Scripts\python.exe scripts\check_env.py
python -m src
```

**macOS / Linux (bash)**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: at minimum keep PAPER_TRADE=true for structural arb
python scripts/check_env.py
python -m src
```

The bot prints JSON logs to the console and serves the control API on `127.0.0.1:8765` (see below). Open `frontend/index.html` in a browser if you want the local dashboard (with the process above still running).

---

## Running paper trading vs live trading

### Paper trading (structural arb — supported)

This is the **default** and what `python -m src` is built for.

1. In `.env` set **`PAPER_TRADE=true`** (already the default in `.env.example`).
2. Fill `.env` with placeholders or optional values: live Polymarket keys are **not** required for paper mode (`scripts/check_env.py` explains what is optional).
3. Run from `polymarket-btc-bot`:

   ```powershell
   .\.venv\Scripts\python.exe -m src
   ```

   or, after `source .venv/bin/activate`:

   ```bash
   python -m src
   ```

4. Optional checks before a long session:

   ```powershell
   .\.venv\Scripts\python.exe scripts\check_env.py
   .\.venv\Scripts\python.exe -m pytest tests -q
   ```

**Important:** With default settings the structural-arb engine uses the in-repo **`PaperExchange`** (simulated fills, positions, and settlement). `PAPER_TRADE=true` keeps you there. A real-money **`LiveClobExchange`** is attached only when you explicitly arm it (next section); `GET /summary` always reports the **actual** `execution_mode` (`paper` / `live_dry_run` / `live`).

### Live / real-money trading (read this before changing `.env`)

Live execution is wired but **OFF by default and behind layered hard gates**. Real orders are POSTed only when **all** of these hold:

1. `PAPER_TRADE=false`
2. `ENABLE_LIVE_EXECUTION=true`
3. `LIVE_DRY_RUN=false`
4. All live secrets present: `POLYMARKET_API_KEY`, `POLYMARKET_SECRET`, `POLYMARKET_PASSPHRASE`, `POLYMARKET_WALLET_ADDRESS`, `WALLET_PRIVATE_KEY`

If any gate is unmet you stay on the simulated exchange. The recommended path before risking money:

- **Dry-run first.** Set `PAPER_TRADE=false`, `ENABLE_LIVE_EXECUTION=true`, keep **`LIVE_DRY_RUN=true`**, and add your secrets. The engine attaches the live adapter and exercises the *entire* live path — it builds and signs the exact `OrderArgs` and logs them (`live_exchange.dry_run_order`) — but **does not POST**, simulating fills locally instead. `execution_mode` reads `live_dry_run`. Validate the logged orders against the markets you expect.
- **Arm it.** Only once dry-run looks correct, set `LIVE_DRY_RUN=false`. `execution_mode` becomes `live` and orders are sent as Fill-or-Kill takers, each capped at `LIVE_MAX_ORDER_USDC` USDC notional. Start with a small ceiling.

Safety characteristics of the live adapter (`src/arb/live_exchange.py`):

- **Per-order USDC ceiling** (`LIVE_MAX_ORDER_USDC`) independent of basket sizing — a last-line cap against a fat-finger config.
- **Conservative fill interpretation:** an ambiguous POST response is treated as *not filled* (a phantom fill is the dangerous error), so the engine never books inventory it doesn't hold.
- **Neg-risk conversion is refused when armed** (the on-chain `NegRiskAdapter` call is not wired); only complete-set baskets — pure CLOB taker orders — execute live. This is enforced in the risk gate *before* any leg is bought.

Follow wallet/API setup in **`setup/polymarket_wallet.md`**, use a strong **`CONTROL_API_TOKEN`**, and never commit **`.env`**. Run **`python scripts/check_env.py`** — it must exit **0** in live mode (all required secrets present).

---

## Control API

Default host: `127.0.0.1`

Default port: `8765`

Optional auth:
- Set `CONTROL_API_TOKEN` to require `X-Control-Token: <token>` or `Authorization: Bearer <token>` on all routes except `/health`.

Useful endpoints:
- `GET /health`
- `GET /summary` (includes `execution_mode`, a `streaming` health block, and `last_cycle.books_clob` / `books_synthetic` / `books_other` for data-quality checks)
- `GET /streaming` (alias `GET /latency`) — stream connection health, book freshness, hot-path eval count, and last detection→execute latency
- `GET /events`
- `GET /opportunities`
- `GET /orders`
- `GET /positions`
- `GET /baskets`
- `POST /cycle`
- `POST /halt`
- `POST /resume`
- `POST /settle`
- `POST /funds/add`
- `GET /funds/history`

Legacy aliases (same server; map to arb semantics — see `src/arb/control.py`):
- `GET /stats` — JSON shaped like the old directional `/stats` (equity as `bankroll`, etc.)
- `GET /stats/assets` — static empty per-asset grid (no spot-asset book in arb mode)
- `GET /trades` — recent `arb_orders` rows as trade-shaped objects
- `POST /halt/asset`, `POST /resume/asset` — validate symbol then **global** halt/resume

Example:

```bash
curl http://127.0.0.1:8765/summary
curl -X POST http://127.0.0.1:8765/cycle
curl -X POST http://127.0.0.1:8765/settle ^
  -H "Content-Type: application/json" ^
  -d "{\"event_id\":\"...\",\"resolution_market_id\":\"...\"}"
```

## Key Configuration

Core bankroll / risk:
- `INITIAL_BANKROLL`
- `MAX_BASKET_NOTIONAL`
- `MAX_EVENT_EXPOSURE_PCT`
- `MAX_TOTAL_OPEN_BASKETS`
- `DAILY_LOSS_CAP`

Real-time streaming (latency edge):
- `ARB_STREAMING_ENABLED` (default `true`)
- `CLOB_WS_URL`
- `ARB_UNIVERSE_REFRESH_SECONDS`
- `ARB_BOOK_STALENESS_SECONDS`
- `ARB_HOT_SCAN_DEBOUNCE_MS`

Live execution (real money — OFF by default):
- `ENABLE_LIVE_EXECUTION`
- `LIVE_DRY_RUN`
- `LIVE_MAX_ORDER_USDC`

Discovery / execution:
- `GAMMA_BASE_URL`
- `CLOB_HOST`
- `ARB_POLL_SECONDS`
- `ARB_CYCLE_ERROR_BACKOFF_SECONDS` (extra pause after a failed cycle before the next poll)
- `GAMMA_HTTP_TIMEOUT_SECONDS` (Gamma REST total timeout)
- `CLOB_BOOK_FETCH_CONCURRENCY` (parallel CLOB book fetches per cycle; lower if rate-limited)
- `MAX_TRACKED_EVENTS`
- `MIN_EVENT_LIQUIDITY`
- `MIN_OUTCOMES_PER_EVENT`
- `MIN_COMPLETE_SET_EDGE_BPS`
- `MIN_NEG_RISK_EDGE_BPS`
- `MAX_OPPORTUNITIES_PER_CYCLE`
- `OPPORTUNITY_COOLDOWN_SECONDS`
- `ALLOW_TAKER_EXECUTION`
- `PAPER_TAKER_FEE_BPS` (default **50** bps in code — see `PAPER_REALISM.md`)
- `PAPER_MAKER_REBATE_BPS`
- `CONTROL_API_TOKEN`

Notes:
- The current arb executor is **taker-first**. If `ALLOW_TAKER_EXECUTION=false`, opportunities will be rejected rather than silently drifting into partial maker logic.
- Manual settlement is part of the paper workflow right now. Use `POST /settle` once you know the winning market for a tracked event.
- Legacy directional env vars remain in `.env.example`, but they are not the active runtime path.

## Paper Workflow

1. Start the bot with `python -m src`.
2. Let `/cycle` run automatically or trigger it manually.
3. Inspect `/opportunities`, `/orders`, `/positions`, and `/baskets`.
4. Add paper capital through `/funds/add` when needed.
5. Let the engine auto-settle resolved events when Gamma provides an unambiguous winner, and use `/settle` only as a manual override.
6. Review results in `data/trades.db`, which now holds both the legacy tables and the new `arb_*` tables.

## Session Recording And Replay

Record live paper cycles to JSONL:

```bash
.venv\Scripts\python.exe scripts\record_arb_session.py --cycles 25
```

Replay a captured session through the current pricing engine:

```bash
.venv\Scripts\python.exe scripts\replay_arb_session.py data\replays\arb-session-YYYYMMDD-HHMMSS.jsonl
```

Useful env vars:
- `AUTO_SETTLE_RESOLVED_EVENTS`
- `REPLAY_OUTPUT_DIR`

## Security

- **Never commit `.env`** (it is gitignored). Use `.env.example` with placeholders only.
- The control API listens on **`127.0.0.1`** by default. Set **`CONTROL_API_TOKEN`** if anything beyond you can reach that port.
- See **`SECURITY.md`** for CORS, health endpoint behavior, and rotation guidance before publishing a fork.

## Testing

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

Current coverage includes:
- complete-set detection
- neg-risk conversion detection
- paper exchange conversion / execution
- engine cycle + settlement
- legacy `RiskManager` / `ControlAPI` deposits tests, plus arb control API and legacy-route compatibility

## Legacy Modules

The old directional system remains available in the repository for reference:
- `src/feeds/`
- `src/markets/`
- `src/signal/`
- `src/execution/`
- `src/risk/manager.py`
- `src/control/api.py`

It is no longer the active entrypoint.
