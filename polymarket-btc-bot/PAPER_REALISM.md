# Structural-arb paper trading realism

This document summarizes how closely `PaperExchange` + `OpportunityScanner` mirror live CLOB behavior, and what changed to align them.

## What is modeled faithfully

1. **Shared taker walk** — `src/arb/book_matching.py` implements the same price/size walk used by `PaperExchange._simulate_executions` and by `OpportunityScanner` when estimating complete-set and neg-risk costs. Multi-level books (worse prices deeper in the ladder) affect both **approved size** and **capital_required**.

2. **Shared fee + buy spread math** — `src/arb/fees.py` supplies `taker_fee_on_notional` and `paper_structural_taker_buy_cash` (principal + taker fee + `PAPER_SPREAD_PENALTY_BPS` on each BUY slice). The **scanner** and **`PaperExchange`** use the same buy-side cash formula so expected profit and realized PnL stay aligned when spread penalty is non-zero.

3. **Sizing under `MAX_BASKET_NOTIONAL`** — The scanner binary-searches the largest complete-set / neg-risk size whose **estimated cash out** (including fees) stays under the cap, instead of using a naive `sum(best_ask) * size` cap that could exceed the cap after slippage.

4. **Float hygiene** — Tiny numerical overshoots vs `max_basket_notional` are clamped/toleranced so opportunities are not spuriously rejected in `ArbRiskManager.approve`.

## Defaults

- **`PAPER_TAKER_FEE_BPS`** defaults to **50** (0.5%) in `Settings`. Polymarket’s live schedule can differ by market and time; treat this as a **conservative placeholder** and tune from official docs or your account.
- **`PAPER_SPREAD_PENALTY_BPS`** defaults to **15** (0.15% of notional per BUY fill) so paper is not systematically **more optimistic** than the exchange. Set to **`0`** for fee-only tests.

## Observability

Each cycle summary (and `GET /summary` → `last_cycle`) includes **`books_clob`**, **`books_synthetic`**, **`books_other`**: counts of `TokenBook.source` after the market-data refresh. If **`books_synthetic` > 0**, the engine logs **`arb_engine.synthetic_books_in_cycle`** at warning — those books are **not** live CLOB snapshots (client missing, empty book, or parse fallback). Treat opportunities as **lower confidence** until all tracked books are `clob`.

Replay canonicalization includes these fields (default **0** for older JSONL sessions).

## Known limitations (unchanged)

- **Books reset each cycle** from the network: local `_consume_book` does not persist into the next `sync_books` refresh, so you do not model “your size already lifted the resting liquidity” across polls. (Mitigation: watch `books_*` counts and size small vs displayed depth.)
- **No latency, partial API failures, or queue priority** — FOK-style immediate match against a snapshot only.
- **Settlement** remains a simplified $1/share binary payout model; verify against Polymarket rules for edge cases.
- **Neg-risk conversion** in the exchange uses a simplified inventory split; scanner profit is cash-flow based (buy NO + sell YES legs) and should be close for the executed sequence but may not match every on-chain detail.
- **Book staleness warnings** — `paper_exchange.stale_book_fill` is logged when a fill simulates against a book snapshot older than 30 s. This approximates the latency risk of the 20 s polling cycle; real fills in live mode hit a book that may have moved since the last refresh.
- **`PAPER_SPREAD_PENALTY_BPS`** — optional extra cost on **BUY** fills (scanner + exchange). Default **15** bps; raise toward **20** for more conservative alignment with live.
- **Cooldown persistence** — cooldowns now survive restarts via `arb_cooldowns` SQLite table; expired entries are pruned on load.
- **Synthetic book filtering** — books with `source=="synthetic"` are now excluded from scanner input. They are still recorded in the DB and displayed in the dashboard.

## Files touched for this pass

- `src/arb/book_matching.py` — shared walk
- `src/arb/fees.py` — shared fee helpers
- `src/arb/exchange.py` — uses walk + fees helpers
- `src/arb/pricing.py` — walk + fees for scanner; binary search sizing
- `src/arb/risk.py` — notional cap tolerance
- `src/config.py` — default `PAPER_TAKER_FEE_BPS`
- `tests/test_arb_system.py` — `test_scanner_capital_required_matches_exchange_cash_for_complete_set`
