# Go-Live Runbook (structural arb)

Ordered, enforced path from paper to real-money trading. Each step references a real script or
config flag in this repo. Do not skip steps — the engine enforces several of them at startup.

## 0. Preconditions

- A funded Polymarket wallet (USDC on Polygon) and L2 API credentials (`POLYMARKET_API_KEY/SECRET/PASSPHRASE`, `POLYMARKET_WALLET_ADDRESS`, `WALLET_PRIVATE_KEY`).
- `POLYGON_RPC_URL` set if you intend neg-risk on-chain conversion (EOA wallet only; Gnosis Safe conversion is not wired — complete-set works regardless).

## 1. Validate environment

```
python scripts/check_env.py
```
Confirms required secrets/fields are present and internally consistent.

## 2. Prove the strategy in paper

Run the bot in paper mode (`PAPER_TRADE=true`, the default) against live public feeds until it has
a real track record. The engine appends per-cycle JSONL (`PAPER_EQUITY_SNAPSHOT_LOG`) and writes
baskets to the paper DB (`data/paper_arb.db`).

Target the blueprint's §14 acceptance criteria (see `ARB_RUNTIME_LOGIC.md` §9):
- ≥ 200 resolved baskets,
- ≥ 99% basket completion rate (legs filled without a FAILED unwind),
- net realized PnL > 0 after modeled fees/slippage.

## 3. Certify live-readiness

```
python scripts/check_live_readiness.py --db data/paper_arb.db
```
Exit 0 = ready, 1 = not ready. This reads the same logic the engine's startup gate enforces
(`src/arb/live_readiness.py`). Tune thresholds with `--min-resolved / --min-completion / --min-net-pnl`
only with a clear reason; defaults match §14.

## 4. Check live connectivity

```
python scripts/check_live_connections.py
```
Verifies Gamma + CLOB reachability, L2 auth, wallet/key match, and warns on zero USDC.

## 5. Configure a tiny supervised pilot

In `.env`:
```
PAPER_TRADE=false
ARB_LIVE_EXECUTION=true
ALLOW_TAKER_EXECUTION=true
PAPER_SPREAD_PENALTY_BPS=0          # live uses real fills; keep scanner edge honest
ARB_REQUIRE_LIVE_READINESS_PROOF=true
ARB_READINESS_PROOF_DB=data/paper_arb.db
MAX_BASKET_NOTIONAL=3               # a few dollars
MAX_TOTAL_OPEN_BASKETS=1
MAX_OPPORTUNITIES_PER_CYCLE=1
```
The engine refuses to start live unless the readiness gate passes (or you set the explicit, logged
override `ARB_ALLOW_LIVE_WITHOUT_READINESS_PROOF=true`). It also requires `PAPER_TRADE=false` and
`ALLOW_TAKER_EXECUTION=true`.

## 6. Start and watch

Launch the live stack (e.g. `python -m src`). Keep the dashboard/control API open. Watch:
- basket completion vs `FAILED` (failed baskets auto-unwind only their own legs at market),
- `arb_engine.execution_failed` and unwind warnings (`unwind_position_abandoned` = residual exposure to settle manually),
- cash/collateral sync vs the Polymarket UI.

## 7. Kill switch

Halt all new executions immediately:
```
curl -X POST http://localhost:<CONTROL_API_PORT>/halt
curl -X POST http://localhost:<CONTROL_API_PORT>/resume
```
Open positions are not force-closed by `/halt`; resolve them via settlement or `POST /settle`.

## 8. Scale gradually

Only after the pilot shows live completion rate and net PnL consistent with paper, raise
`MAX_BASKET_NOTIONAL`, `MAX_TOTAL_OPEN_BASKETS`, and `MAX_OPPORTUNITIES_PER_CYCLE` in small steps.
Re-check `check_live_readiness.py` periodically against the growing live/paper history.

---

### Safety mechanisms already enforced in code

- **Complete-set mutual-exclusivity gate** — complete-set arbs only fire on neg-risk (exclusive+exhaustive) events; non-partitioned grouped markets can't produce phantom arbs (`COMPLETE_SET_REQUIRE_MUTUAL_EXCLUSIVITY`).
- **Live-readiness gate** — real-money start blocked until §14 criteria met (step 3).
- **Per-basket failure unwind** — a failed multi-leg basket reverses only its own fills, never a sibling basket's held legs.
- **Risk halts** — daily loss cap, trailing-equity drawdown, session realized-loss, consecutive-failure halt, synthetic-book halt (see `ARB_RUNTIME_LOGIC.md` §6).
