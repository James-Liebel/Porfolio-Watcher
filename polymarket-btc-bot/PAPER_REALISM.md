# Structural-arb paper trading realism

This document summarizes how closely `PaperExchange` + `OpportunityScanner` mirror live CLOB behavior, and what changed to align them.

## What is modeled faithfully

1. **Shared taker walk** — `src/arb/book_matching.py` implements the same price/size walk used by `PaperExchange._simulate_executions` and by `OpportunityScanner` when estimating complete-set and neg-risk costs. Multi-level books (worse prices deeper in the ladder) affect both **approved size** and **capital_required**.

2. **Shared fee math** — `src/arb/fees.py` supplies `taker_fee_on_notional` used by both the scanner (expected cash) and the exchange (fills). When `fees_enabled` is true on a market, `PAPER_TAKER_FEE_BPS` applies consistently.

3. **Sizing under `MAX_BASKET_NOTIONAL`** — The scanner binary-searches the largest complete-set / neg-risk size whose **estimated cash out** (including fees) stays under the cap, instead of using a naive `sum(best_ask) * size` cap that could exceed the cap after slippage.

4. **Float hygiene** — Tiny numerical overshoots vs `max_basket_notional` are clamped/toleranced so opportunities are not spuriously rejected in `ArbRiskManager.approve`.

## Defaults

- **`PAPER_TAKER_FEE_BPS`** defaults to **50** (0.5%) in `Settings`. Polymarket’s live schedule can differ by market and time; treat this as a **conservative placeholder** and tune from official docs or your account.
- Set to **`0`** only if you intentionally want fee-free paper (e.g. certain tests).

## Known limitations (unchanged)

- **Books reset each cycle** from the network: local `_consume_book` does not persist into the next `sync_books` refresh, so you do not model “your size already lifted the resting liquidity” across polls.
- **No latency, partial API failures, or queue priority** — FOK-style immediate match against a snapshot only.
- **Settlement** remains a simplified $1/share binary payout model; verify against Polymarket rules for edge cases.
- **Neg-risk conversion** in the exchange uses a simplified inventory split; scanner profit is cash-flow based (buy NO + sell YES legs) and should be close for the executed sequence but may not match every on-chain detail.

## Files touched for this pass

- `src/arb/book_matching.py` — shared walk
- `src/arb/fees.py` — shared fee helpers
- `src/arb/exchange.py` — uses walk + fees helpers
- `src/arb/pricing.py` — walk + fees for scanner; binary search sizing
- `src/arb/risk.py` — notional cap tolerance
- `src/config.py` — default `PAPER_TAKER_FEE_BPS`
- `tests/test_arb_system.py` — `test_scanner_capital_required_matches_exchange_cash_for_complete_set`
