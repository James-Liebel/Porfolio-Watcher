# Polymarket Structural-Arbitrage Bot

This repository now boots a **paper-first structural-arbitrage runtime** for Polymarket.

Active runtime:
- Negative-risk conversion scanning
- Complete-set / basket mispricing scanning
- Queue-aware enough paper exchange state for fills, positions, conversions, and settlement
- Local control API for paper operations and monitoring

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

## Quick Start

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m src
```

The default mode is paper trading.

## Control API

Default host: `127.0.0.1`

Default port: `8765`

Optional auth:
- Set `CONTROL_API_TOKEN` to require `X-Control-Token: <token>` or `Authorization: Bearer <token>` on all routes except `/health`.

Useful endpoints:
- `GET /health`
- `GET /summary`
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

Discovery / execution:
- `GAMMA_BASE_URL`
- `CLOB_HOST`
- `ARB_POLL_SECONDS`
- `MAX_TRACKED_EVENTS`
- `MIN_EVENT_LIQUIDITY`
- `MIN_OUTCOMES_PER_EVENT`
- `MIN_COMPLETE_SET_EDGE_BPS`
- `MIN_NEG_RISK_EDGE_BPS`
- `MAX_OPPORTUNITIES_PER_CYCLE`
- `OPPORTUNITY_COOLDOWN_SECONDS`
- `ALLOW_TAKER_EXECUTION`
- `PAPER_TAKER_FEE_BPS`
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
