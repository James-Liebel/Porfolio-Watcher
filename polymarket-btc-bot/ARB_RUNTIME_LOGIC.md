# Structural-arb runtime — current logic & tuning map

This document describes **how the bot behaves today**, how **paper trading stays aligned with public market data**, and **where to change things** when you fine-tune or extend event selection. It complements the strategy blueprint in `NEG_RISK_ARB_BLUEPRINT.md`.

---

## 0. Two run modes: streaming (default) vs. polling

`ArbEngine.run()` picks a mode from `ARB_STREAMING_ENABLED`:

- **Streaming (default, `true`) — the latency edge.** A `ClobBookStream`
  (`src/arb/streaming.py`) holds a live order-book cache over the Polymarket CLOB
  **market WebSocket** (`CLOB_WS_URL`). When a tracked token's book changes, the
  engine re-scans **only that event** and executes immediately — reacting in
  **milliseconds** instead of waiting out a poll. Three concurrent jobs run under
  one stop signal: the **stream** (book cache), a **slow loop**
  (`ARB_UNIVERSE_REFRESH_SECONDS`) that refreshes the Gamma universe + stream
  subscriptions and runs a full reconcile (also drives auto-settlement), and the
  **hot loop** that drains book-change notifications and re-scans the dirty events.
  Stale books (no update within `ARB_BOOK_STALENESS_SECONDS`) are excluded so a
  frozen quote is never traded.
- **Polling (`false`) — legacy.** The original `run_cycle` every `ARB_POLL_SECONDS`.

The per-cycle pipeline below (`run_cycle`) is identical in both modes — in
streaming mode it is the slow-loop reconcile, and `market_data.refresh()` serves
books from the live cache first, falling back to REST then synthetic.

## 1. End-to-end cycle (`ArbEngine.run_cycle`)

Each cycle (slow-loop reconcile while streaming, or every `ARB_POLL_SECONDS` when polling):

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

**What is not identical to live**

- **Snapshots, not continuous truth:** Even streaming, you act on the cache as of the last delta. In streaming mode that lag is milliseconds; in polling mode it is up to `ARB_POLL_SECONDS`.
- **Synthetic books:** If a token has no fresh stream book and `ClobClient` REST also fails, that token falls back to a **synthetic** book. Logs warn (`arb_engine.synthetic_books_in_cycle`). Synthetic books are stripped before scanning so false edges are never scored.
- **Paper-only economics:** `PAPER_TAKER_FEE_BPS`, `PAPER_MAKER_REBATE_BPS`, `PAPER_SPREAD_PENALTY_BPS` adjust simulated costs; align them with Polymarket for realistic P&L.

**Live execution (gated, real money).** A `LiveClobExchange` (`src/arb/live_exchange.py`) subclasses `PaperExchange`, so all accounting is identical — only how a single order fills differs. It is attached **only** when fully configured and stays OFF by default. Gates (all required to POST real orders): `PAPER_TRADE=false`, `ENABLE_LIVE_EXECUTION=true`, `LIVE_DRY_RUN=false`, and all live secrets present (`Settings.live_execution_armed()`). With `LIVE_DRY_RUN=true` the live adapter is exercised end to end (builds + signs the real `OrderArgs`, logs them) but **withholds the POST** and simulates locally. Live orders are Fill-or-Kill takers with a hard `LIVE_MAX_ORDER_USDC` per-order ceiling; neg-risk *conversion* baskets are refused when armed (the on-chain `NegRiskAdapter` call is not wired), gated in `ArbRiskManager.approve` before any leg is bought.

- **Honest mode reporting:** `GET /summary` reports `execution_mode` from the **exchange actually attached** — `paper`, `live_dry_run`, or `live` — never inferred from the flag. It also surfaces `effective_min_complete_set_edge_bps` / `effective_min_neg_risk_edge_bps` (floor + buffer), per-cycle `books_clob` / `books_synthetic`, and a `streaming` block (connected, subscribed, hot-eval count, last hot-path latency). `GET /streaming` (alias `GET /latency`) gives the full stream + latency view.

**Summary:** In streaming paper mode the bot **mirrors public market state in near-real-time** and never posts. Switching to armed live trades for real, behind the gates above; keep `LIVE_DRY_RUN=true` until you have validated fills against the logged orders.

---

## 3. Key modules (where logic lives)

| Concern | Module | Role |
|--------|--------|------|
| Settings / env | `src/config.py` | All `Settings` fields, `category_is_allowed()`, `live_execution_configured/armed()` |
| Gamma fetch + event build | `src/arb/universe.py` | `_load_payload`, `_build_events`, `_parse_market`, filters, `max_tracked_events` cap |
| Live book stream | `src/arb/streaming.py` | `ClobBookStream`: WebSocket cache, `apply_raw` (book/price_change), `fresh_books`, reconnect, metrics |
| CLOB books | `src/arb/market_data.py` | `refresh` (stream cache → REST → synthetic), `attach_stream`, synthetic fallback |
| Scanning | `src/arb/pricing.py` | `OpportunityScanner`, complete-set + neg-risk, edge thresholds |
| Book walking / fees | `src/arb/book_matching.py`, `src/arb/fees.py` | Depth-aware taker simulation, fee helpers |
| Risk gates | `src/arb/risk.py` | `approve`, cooldowns, halt, drawdown, live-conversion gate |
| Paper execution | `src/arb/exchange.py` | `PaperExchange`, positions, conversion, settlement, `update_books` merge |
| Live execution | `src/arb/live_exchange.py` | `LiveClobExchange`: gated FOK POST, dry-run, USDC ceiling, fill reconcile |
| Orchestration | `src/arb/engine.py` | `run` (streaming/polling), `run_cycle`, `_hot_evaluate`, exchange selection |
| HTTP API | `src/arb/control.py` | Dashboard / operator endpoints, `/streaming` + `/latency` |
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

- **Requires negative-risk structure** — `neg_risk` or `enable_neg_risk` on the event, and **not** `neg_risk_augmented`. This is the precondition that makes the set redeem for exactly $1.00: the outcomes must be mutually exclusive **and** collectively exhaustive (exactly one resolves YES). A bare Gamma `eventId` is only a grouping and may bundle independent questions; buying every YES there is a basket of longs, not an arb. Augmented events can gain outcomes after purchase, breaking exhaustiveness, so they are excluded.
- Requires **distinct normalized outcome names** for every market in the event.
- Buys **YES** on each outcome at **best ask** (taker path), sizes with **`MAX_BASKET_NOTIONAL`**, checks **`MIN_COMPLETE_SET_EDGE_BPS` + `ARB_SLIPPAGE_BUFFER_BPS`** and positive profit.
- Ranked by annualized edge (with time-to-expiry) among candidates; list truncated before risk (`max_opportunities_per_cycle * 10` cap in scanner).

**Neg-risk (`strategy_type=neg_risk`)**

- Requires `neg_risk` or `enable_neg_risk` on the event; skips `neg_risk_augmented` and generic “Other” outcomes.
- Builds conversion-style legs from NO asks and YES bids per blueprint; gated by **`MIN_NEG_RISK_EDGE_BPS` + `ARB_SLIPPAGE_BUFFER_BPS`**.

**Safety buffer (both strategies)**

- The effective edge requirement is the per-strategy floor **plus `ARB_SLIPPAGE_BUFFER_BPS`** (default 15). This cushions the gap between the polled snapshot and a live fill: snapshot staleness (`ARB_POLL_SECONDS`), your own market impact, and fee uncertainty. Set to `0` only for tests/replay where fills match the scanned snapshot exactly.

**Files to edit for strategy tuning:** `pricing.py` (thresholds, sizing, exclusions), plus `config.py` for new env knobs.

---

## 6. Risk and execution (after scan)

**`ArbRiskManager.approve`** — rejects if: halted, `ALLOW_TAKER_EXECUTION=false`, basket notional over cap, too many open baskets / per-strategy baskets, insufficient cash, **event exposure** over `MAX_EVENT_EXPOSURE_PCT` of equity, or **cooldown** active (`OPPORTUNITY_COOLDOWN_SECONDS`).

**Execution** — Up to **`MAX_OPPORTUNITIES_PER_CYCLE`** approved opportunities executed per cycle (in scan order after sort). Within a complete-set basket, legs are placed **thinnest-available-depth first** (`ArbEngine._order_legs_by_fill_risk`): because baskets are non-atomic, a leg that fails its FOK should reject before capital is committed to the easy legs, minimizing the half-built-basket unwind handled by `_liquidate_event_positions`. (Paper fills are identical regardless of order — all legs match one frozen snapshot — so this only changes live behavior.)

---

## 7. Configuration quick reference (structural arb)

| Env var | Effect |
|---------|--------|
| `ARB_STREAMING_ENABLED` | `true` (default): WebSocket book stream + ms-latency hot path. `false`: legacy REST poll loop |
| `CLOB_WS_URL` | Polymarket CLOB market WebSocket endpoint |
| `ARB_UNIVERSE_REFRESH_SECONDS` | Streaming: cadence for Gamma universe refresh + resubscribe + full reconcile |
| `ARB_BOOK_STALENESS_SECONDS` | Streamed books older than this are excluded from scans (never trade a frozen quote) |
| `ARB_HOT_SCAN_DEBOUNCE_MS` | Coalesce a burst of book deltas for one event before re-scanning (0 = scan every delta) |
| `ARB_MAX_BOOK_SUBSCRIPTIONS` / `ARB_WS_RECONNECT_MAX_SECONDS` | Subscription cap and reconnect backoff ceiling |
| `ENABLE_LIVE_EXECUTION` / `LIVE_DRY_RUN` / `LIVE_MAX_ORDER_USDC` | Live execution gates: master switch, dry-run (build+log, don't POST), per-order USDC ceiling |
| `CLOB_SIGNATURE_TYPE` | py-clob-client signature type (2 = proxy/email wallet) |
| `GAMMA_BASE_URL` / `GAMMA_HTTP_TIMEOUT_SECONDS` | Gamma API endpoint and client timeout |
| `CLOB_HOST` / `CLOB_BOOK_FETCH_CONCURRENCY` | CLOB endpoint and parallel REST book fetches (fallback/reconcile) |
| `ARB_POLL_SECONDS` / `ARB_CYCLE_ERROR_BACKOFF_SECONDS` | Poll interval (polling mode) and pause after a failed cycle |
| `MAX_TRACKED_EVENTS` | Hard cap on events after sort |
| `MIN_EVENT_LIQUIDITY` / `MIN_OUTCOMES_PER_EVENT` | Universe floor |
| `CATEGORY_ALLOWLIST` / `CATEGORY_BLOCKLIST` | Category gating |
| `MIN_COMPLETE_SET_EDGE_BPS` / `MIN_NEG_RISK_EDGE_BPS` | Per-strategy edge floors |
| `ARB_SLIPPAGE_BUFFER_BPS` | Extra edge required on top of each floor (snapshot staleness + impact + fee uncertainty); default 15 |
| `MAX_BASKET_NOTIONAL`, `MAX_TOTAL_OPEN_BASKETS`, `MAX_BASKETS_PER_STRATEGY`, `MAX_OPPORTUNITIES_PER_CYCLE` | Size and concurrency limits |
| `MAX_EVENT_EXPOSURE_PCT`, `OPPORTUNITY_COOLDOWN_SECONDS` | Risk |
| `ALLOW_TAKER_EXECUTION` | Must be true for current executor path |
| `PAPER_TAKER_FEE_BPS`, `PAPER_SPREAD_PENALTY_BPS` | Paper realism |
| `AUTO_SETTLE_RESOLVED_EVENTS` | Auto settlement behavior |
| `LOG_LEVEL` | Use `WARNING` to drop per-cycle INFO logs |

---

## 8. Changing this document

When you change behavior in code, update the relevant **section and table** here so future tuning stays one jump away from the implementation.
