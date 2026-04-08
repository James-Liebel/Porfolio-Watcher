# Structural-arb runtime — current logic & tuning map

This document describes **how the bot behaves today**, how **paper trading stays aligned with public market data**, and **where to change things** when you fine-tune or extend event selection. It complements the strategy blueprint in `NEG_RISK_ARB_BLUEPRINT.md`.

---

## 1. End-to-end cycle (`ArbEngine.run_cycle`)

Each poll (every `ARB_POLL_SECONDS`, plus optional backoff after errors):

1. **Universe** — `GammaUniverseService.refresh()` loads active events + markets from **Gamma** (`GAMMA_BASE_URL`), builds `ArbEvent` snapshots, filters and caps the list (see §4).
2. **Books** — `ClobMarketDataService.refresh()` fetches **public CLOB** order books (`CLOB_HOST`) for YES/NO tokens, with bounded concurrency (`CLOB_BOOK_FETCH_CONCURRENCY`). If the CLOB client is unavailable, books are **synthetic** (derived from Gamma mid-style prices); see §3.
3. **Persistence** — Events and book snapshots are written via `ArbRepository`; exchange state is synced from books.
4. **Auto-settlement** — Optionally settles resolved events (`AUTO_SETTLE_RESOLVED_EVENTS`).
5. **Scan** — `OpportunityScanner.scan()` finds **complete-set** and **neg-risk** opportunities using the same `books` dict (see §5).
6. **Risk** — `ArbRiskManager.approve()` gates each opportunity (halt, taker flag, notional, baskets, cash, event exposure, cooldown).
7. **Execution** — Approved opportunities execute on **`PaperExchange`** (fills walk the same book levels the scanner used, plus configured paper fees).
8. **Summary** — Cycle metrics stored and logged (`arb_engine.cycle_done` at INFO).

**Parallel tasks at process level:** `main.py` runs **`engine.run()`** and **`control_api.run()`** concurrently: the engine loops on Gamma/CLOB while the control API serves HTTP on `CONTROL_API_PORT`.

---

## 2. Paper trading vs the live market (“parallel” behavior)

**What is parallel today**

- **Same public inputs as a live watcher:** Universe and (when the client works) books come from the **same Gamma and CLOB endpoints** you would use for real trading. `PAPER_TRADE` does **not** switch off these feeds; it only labels mode and affects other code paths that expect live keys.
- **Scanner and paper fills use one book snapshot per cycle:** Opportunities are sized with `walk_taker_levels` / best asks; `PaperExchange` applies fills against the books synced that cycle, so simulated execution is **tied to the same prices and depth** the scanner saw (modulo fee/slippage modeling below).
- **Same BUY cash model:** `PAPER_TAKER_FEE_BPS` and **`PAPER_SPREAD_PENALTY_BPS`** apply in both `OpportunityScanner` (expected `capital_required` / profit) and `PaperExchange` (fills), so paper PnL is not systematically more optimistic than the scanner.

**What is not identical to live**

- **Polling, not WebSockets:** The market moves continuously; you see discrete snapshots every `ARB_POLL_SECONDS`. Tighter poll = closer tracking; more load on APIs.
- **Synthetic books:** If `ClobClient` fails to initialize or a token fetch errors, that token falls back to a **synthetic** book. Logs warn (`arb_engine.synthetic_books_in_cycle`). Tuning `CLOB_BOOK_FETCH_CONCURRENCY` and network stability reduces this gap.
- **Paper-only economics:** `PAPER_TAKER_FEE_BPS`, `PAPER_MAKER_REBATE_BPS`, `PAPER_SPREAD_PENALTY_BPS` adjust simulated costs; align them with Polymarket for realistic P&L.
- **No on-chain or authenticated trading:** Orders are not sent to Polymarket; inventory and cash live only in SQLite + in-memory exchange state.

**Summary:** Paper mode **mirrors public market state in parallel** each cycle; it does **not** post to the market. To keep paper closest to reality: ensure CLOB books are live (watch synthetic warnings), tune fees/spread penalty, and set poll interval vs rate limits.

---

## 3. Key modules (where logic lives)

| Concern | Module | Role |
|--------|--------|------|
| Settings / env | `src/config.py` | All `Settings` fields and `category_is_allowed()` |
| Gamma fetch + event build | `src/arb/universe.py` | `_load_payload`, `_build_events`, `_parse_market`, filters, `max_tracked_events` cap |
| CLOB books | `src/arb/market_data.py` | `refresh`, gated `get_order_book`, synthetic fallback |
| Scanning | `src/arb/pricing.py` | `OpportunityScanner`, complete-set + neg-risk, edge thresholds |
| Book walking / fees | `src/arb/book_matching.py`, `src/arb/fees.py` | Depth-aware taker simulation, fee helpers |
| Risk gates | `src/arb/risk.py` | `approve`, cooldowns, halt, drawdown |
| Paper execution | `src/arb/exchange.py` | `PaperExchange`, positions, conversion, settlement |
| Orchestration | `src/arb/engine.py` | `run`, `run_cycle`, persistence hooks |
| HTTP API | `src/arb/control.py` | Dashboard / operator endpoints |
| Entry | `src/main.py` | Logging, engine + control tasks, signal shutdown |

---

## 4. Event universe — how events are chosen and how to optimize

**Data flow**

- Gamma returns **event** rows and **market** rows; markets are grouped by `eventId` (and related keys) in `_build_events`.

**Filters applied in `GammaUniverseService._build_events`** (`universe.py`)

- At least **`MIN_OUTCOMES_PER_EVENT`** parsed markets per event.
- **Liquidity** ≥ **`MIN_EVENT_LIQUIDITY`** (from event meta, else sum of market liquidities).
- **Status** not resolved/closed/finalized.
- **Category** must pass **`category_is_allowed()`** in `config.py`: optional **`CATEGORY_ALLOWLIST`** (comma substrings) and **`CATEGORY_BLOCKLIST`**.

**Ordering and cap**

- Surviving events are sorted by **liquidity descending**.
- Only the top **`MAX_TRACKED_EVENTS`** are kept.

**Ways to make the event set “more optimal” for your goals**

1. **Env-only (no code):** Raise/lower `MIN_EVENT_LIQUIDITY`, change `MAX_TRACKED_EVENTS`, set allow/block lists, adjust `MIN_OUTCOMES_PER_EVENT` if you want stricter mutual-exclusivity structure.
2. **Code extensions (typical hooks):**
   - Add predicates in `_build_events` (e.g. keyword on title, `neg_risk` only, minimum time to `endDate`).
   - Change sort key (e.g. mix liquidity + volume) before `[:max_tracked_events]`.
   - Inject **`fetch_payload`** in tests or custom runners to bypass HTTP (`GammaUniverseService(..., fetch_payload=...)`).

**Note:** Legacy directional flags (`TRADE_BTC`, `strategy_profile`, etc.) apply to **other** packages in the repo; the structural-arb engine in `python -m src` does **not** currently filter events by those asset toggles. To tie universe to “crypto only,” use **category/title filters** in `_build_events` or new Settings fields consumed there.

---

## 5. Opportunity logic (scanner)

**Complete-set (`strategy_type=complete_set`)**

- Requires **distinct normalized outcome names** for every market in the event.
- Buys **YES** on each outcome at **best ask** (taker path), sizes with **`MAX_BASKET_NOTIONAL`**, checks **`MIN_COMPLETE_SET_EDGE_BPS`** and positive profit.
- Ranked by annualized edge (with time-to-expiry) among candidates; list truncated before risk (`max_opportunities_per_cycle * 10` cap in scanner).

**Neg-risk (`strategy_type=neg_risk`)**

- Requires `neg_risk` or `enable_neg_risk` on the event; skips `neg_risk_augmented` and generic “Other” outcomes.
- Builds conversion-style legs from NO asks and YES bids per blueprint; gated by **`MIN_NEG_RISK_EDGE_BPS`**.

**Files to edit for strategy tuning:** `pricing.py` (thresholds, sizing, exclusions), plus `config.py` for new env knobs.

---

## 6. Risk and execution (after scan)

**`ArbRiskManager.approve`** — rejects if: halted, **per-cycle synthetic book gate** (when `ARB_HALT_EXECUTION_IF_SYNTHETIC_BOOKS_GE` > 0 and the cycle’s synthetic book count ≥ threshold — skips new executions while CLOB coverage is bad), `ALLOW_TAKER_EXECUTION=false`, basket notional over cap, too many open baskets / per-strategy baskets, insufficient cash, **event exposure** over `MAX_EVENT_EXPOSURE_PCT` × max(contributed capital, equity), or **cooldown** active (`OPPORTUNITY_COOLDOWN_SECONDS`). For paper runs with `MAX_BASKET_NOTIONAL` near bankroll size, keep **`MAX_EVENT_EXPOSURE_PCT` ≥ notional / bankroll** or baskets will be rejected as “event exposure cap breached.”

**Stops (minimize losses)** — `DAILY_LOSS_CAP` still halts on drawdown vs **contributed capital** (same as before). Additionally: **`ARB_TRAILING_EQUITY_DRAWDOWN_PCT`** (optional) halts when **mark-to-market equity** falls that fraction below the **session peak**; **`ARB_SESSION_REALIZED_LOSS_USD`** halts when **realized PnL** vs the level at first engine init drops by more than that amount; **`ARB_CONSECUTIVE_EXECUTION_FAILURES_HALT`** halts after N failed basket executions in a row. **Resume** clears trailing peak (re-anchors), consecutive failure count, and halt flags except you must still fix the underlying issue.

**Scanner quality / profitability** — Opportunities are ranked by **annualized edge**, then **expected profit per dollar of capital**, then **expected profit**. **`MAX_ARB_LEG_SPREAD_BPS`** (0 = off) drops legs whose YES/NO book is too wide vs mid. **`ARB_MIN_EXPECTED_PROFIT_USD`** (0 = off) drops tiny expected-profit arbs after fees.

**Execution** — Up to **`MAX_OPPORTUNITIES_PER_CYCLE`** approved opportunities executed per cycle (in scan order after sort).

---

## 7. Configuration quick reference (structural arb)

| Env var | Effect |
|---------|--------|
| `GAMMA_BASE_URL` / `GAMMA_HTTP_TIMEOUT_SECONDS` | Gamma API endpoint and client timeout |
| `CLOB_HOST` / `CLOB_BOOK_FETCH_CONCURRENCY` | CLOB endpoint and parallel book fetches |
| `ARB_POLL_SECONDS` / `ARB_CYCLE_ERROR_BACKOFF_SECONDS` | Normal poll interval and pause after a failed cycle |
| `MAX_TRACKED_EVENTS` | Hard cap on events after sort |
| `MIN_EVENT_LIQUIDITY` / `MIN_OUTCOMES_PER_EVENT` | Universe floor |
| `CATEGORY_ALLOWLIST` / `CATEGORY_BLOCKLIST` | Category gating |
| `MIN_COMPLETE_SET_EDGE_BPS` / `MIN_NEG_RISK_EDGE_BPS` | Scanner floors |
| `MAX_BASKET_NOTIONAL`, `MAX_TOTAL_OPEN_BASKETS`, `MAX_BASKETS_PER_STRATEGY`, `MAX_OPPORTUNITIES_PER_CYCLE` | Size and concurrency limits |
| `MAX_EVENT_EXPOSURE_PCT`, `OPPORTUNITY_COOLDOWN_SECONDS` | Risk (exposure vs max(equity, contributed)) |
| `MAX_ARB_LEG_SPREAD_BPS`, `ARB_MIN_EXPECTED_PROFIT_USD` | Skip wide books / dust arbs (0 disables each) |
| `ARB_HALT_EXECUTION_IF_SYNTHETIC_BOOKS_GE` | Pause executions when too many synthetic books this cycle (0 = off) |
| `ARB_TRAILING_EQUITY_DRAWDOWN_PCT`, `ARB_SESSION_REALIZED_LOSS_USD` | Optional equity trail and session realized-loss halts (0 = off) |
| `ARB_CONSECUTIVE_EXECUTION_FAILURES_HALT` | Halt after N failed baskets in a row (0 = off) |
| `PAPER_EQUITY_SNAPSHOT_LOG`, `PAPER_EQUITY_LOG_PATH` | Paper: append one JSON line per cycle (`summary` + `last_cycle`) |
| `ALLOW_TAKER_EXECUTION` | Must be true for current executor path |
| `PAPER_TAKER_FEE_BPS`, `PAPER_SPREAD_PENALTY_BPS` | Paper realism |
| `AUTO_SETTLE_RESOLVED_EVENTS` | Auto settlement behavior |
| `LOG_LEVEL` | Use `WARNING` to drop per-cycle INFO logs |

---

## 8. Measurement, replay, prediction backtest, overlay

**Paper arb stats (SQLite)** — `scripts/arb_session_report.py` rolls up `arb_opportunities`, `arb_fills`, `arb_settlements`, `arb_baskets`, and `arb_runtime_state` by UTC day. Use `--db` or `ARB_SQLITE_PATH` for multi-agent DB files. This answers “how many edges, fills, and settlement PnL” without a separate analytics DB.

**Paper time series (JSONL)** — When `PAPER_TRADE` and `PAPER_EQUITY_SNAPSHOT_LOG` are true, each cycle appends one line to `PAPER_EQUITY_LOG_PATH` (default `data/paper_tracking/equity.jsonl`): `summary` (same shape as the control API) plus `last_cycle` diagnostics. Use for quick plots or `jq` without querying SQLite.

**Single paper arb** — `python scripts/start_paper_split.py` kills **8765/8767/8780**, resets **`data/paper_arb.db`**, starts one `python -m src` on **8765** (structural arb only, no Ollama advisor). Dashboard: **`GET /split`** redirects to **`/ui/index.html`**. **`CONTROL_API_TOKEN`** is empty for local UI calls.

**Legacy dual paper + split UI** — `python scripts/run_two_structural_agents.py` starts two traders plus optional advisor on **8780**; use **`frontend/agents-split.html`** with `?left=&right=` if ports differ.

**Session recording & replay** — `scripts/record_arb_session.py` (optional `--write-meta`) and `scripts/replay_arb_session.py` verify deterministic replay. `scripts/run_historical_replay_suite.py --strict` batches JSONL files; committed fixtures live under `tests/fixtures/replay/`. `scripts/rigorous_backtest.py` records live cycles plus optional replay verification.

**Real-data directional evaluation** — `scripts/fetch_real_prediction_backtest.py` pulls closed Polymarket markets + CLOB/RSS/CoinGecko; use `--contested-only` for a less trivial YES-price band. `--train-fraction 0.7` adds a **chronological** train/test metric block in `report.json` (earlier cutoffs = train, later = test).

**Offline prediction CLI** — `scripts/run_prediction_backtest.py` on three JSONL files; `--train-fraction` matches the fetcher. Shared metrics live in `src/prediction/evaluate.py`.

**Second sleeve (optional)** — `ENABLE_DIRECTIONAL_OVERLAY` runs the news + momentum overlay after structural arb in the same process (see `src/alpha/overlay.py` and `.env.example`). It does not replace arb scanning.

---

## 9. Changing this document

When you change behavior in code, update the relevant **section and table** here so future tuning stays one jump away from the implementation.
