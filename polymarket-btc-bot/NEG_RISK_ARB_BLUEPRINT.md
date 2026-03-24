# Polymarket Negative-Risk / Basket-Arbitrage Bot Blueprint

Status: implementation blueprint

Date: March 22, 2026

Audience: single-operator, small-team, or solo developer building a production-grade Polymarket bot with a fully featured paper-trading stack first.

## 1. Goal

Build a Polymarket bot optimized for:

- Highest risk-adjusted return over time
- Lowest structural risk relative to directional trading
- Capital efficiency for small-to-medium bankrolls
- Clean migration from paper to live without changing strategy code

Primary design choice:

- Focus on negative-risk and basket arbitrage in fee-free or structurally favorable markets
- Treat directional crypto trading as a secondary module, not the core strategy

## 2. Current Platform Facts

These assumptions are current as of March 22, 2026 and should be re-verified before implementation starts.

- Polymarket exposes three main APIs:
  - Gamma API for discovery and market metadata
  - Data API for positions, trades, activity, holders, and analytics
  - CLOB API for orderbook data and trading operations
- Polymarket provides market and user WebSocket channels for near real-time book and fill updates.
- Most Polymarket markets remain fee-free.
- All crypto markets created on or after March 6, 2026 have taker fees and are eligible for maker rebates.
- Polymarket supports negative-risk events and exposes a `negRisk` flag in Gamma.
- For negative-risk events, conversion is a first-class concept and requires `negRisk: true` order handling in supported SDK paths.

References:

- https://docs.polymarket.com/api-reference
- https://docs.polymarket.com/quickstart/websocket/WSS-Quickstart
- https://docs.polymarket.com/polymarket-learn/trading/fees
- https://docs.polymarket.com/market-makers/maker-rebates
- https://docs.polymarket.com/developers/neg-risk/overview
- https://docs.polymarket.com/market-makers/trading

## 3. Strategy Thesis

The bot should prioritize sources of edge in this order:

1. Negative-risk conversion arbitrage
2. Multi-outcome basket mispricing
3. Complete-set and redeem arbitrage
4. Maker-only spread capture on highly liquid markets
5. Directional trading only after the structural-arb engine is proven

Why:

- Structural arbitrage relies more on market inconsistency than on predicting the future.
- Basket completion and conversion math are easier to validate than subjective forecasting.
- Fee-free markets are structurally better for small bankrolls than taker-fee crypto markets.
- Maker quoting can be profitable, but it introduces queue, inventory, and adverse-selection risk that a pure arb design avoids.

## 4. High-Level Architecture

```text
Gamma API ───────────────┐
                         │
Data API ────────────────┼──► Universe / Metadata Service
                         │
CLOB REST ───────────────┘
        │
        ▼
CLOB WebSocket ─────────────► Market Data Service ─────► Event Bus
                                                     │
                                                     ├──► Pricing Engine
                                                     ├──► Strategy Engine
                                                     ├──► Risk Engine
                                                     ├──► Execution Planner
                                                     └──► Replay Recorder

Pricing + Strategy + Risk + Execution Planner
                    │
                    ▼
             Exchange Router
            ┌────────┴────────┐
            ▼                 ▼
   PaperExchangeAdapter   LiveCLOBAdapter
            │                 │
            ▼                 ▼
       Paper Ledger        Polymarket CLOB

                    │
                    ▼
               State Store
                    │
                    ▼
        Dashboard / Control API / Alerts
```

## 5. Core Services

### 5.1 Universe Service

Responsibilities:

- Pull events and markets from Gamma
- Normalize event, market, outcome, and token metadata
- Track category, tags, resolution rules, timing, neg-risk flags, fee flags
- Exclude invalid or unsuitable markets

Filters:

- Must have machine-readable or unambiguous objective rules
- Must not be suspended or resolved
- Must have sufficient liquidity and acceptable tick size
- Must not be in a banned category list

Outputs:

- `EventSnapshot`
- `MarketSnapshot`
- `TokenMap`
- `UniverseDelta`

### 5.2 Market Data Service

Responsibilities:

- Subscribe to CLOB market WebSocket
- Maintain local orderbooks per token
- Emit top-of-book, spread, midpoint, depth, trade, and tick-size events
- Detect stale data and heartbeat failures

Local state:

- Full book by price level
- Best bid / ask
- Executed trades
- Book timestamps
- Tick size and fee-enabled flags

Outputs:

- `BookSnapshot`
- `BookDelta`
- `BestBidAsk`
- `TradePrint`
- `TickSizeChanged`
- `MarketResolved`

### 5.3 Data Service

Responsibilities:

- Pull your positions and fills from Data API and user WebSocket
- Reconcile live and paper state with internal ledger
- Produce account-level analytics

Outputs:

- `PositionSnapshot`
- `FillEvent`
- `AccountSummary`

### 5.4 Pricing Engine

Responsibilities:

- Compute fair value, basket parity, and conversion value
- Adjust for fees, slippage, queue delay, and incomplete-basket risk
- Reject opportunities with insufficient net edge

Core calculations:

- Sum of mutually exclusive YES prices
- Sum of mutually exclusive NO prices where applicable
- Negative-risk conversion value:
  - `1 NO on outcome A` can convert into `1 YES` on every other named outcome
- Fee-adjusted edge
- Slippage-adjusted edge
- Fill-probability-adjusted edge

Outputs:

- `OpportunityQuote`
- `BasketOpportunity`
- `NegRiskOpportunity`
- `MakerCaptureOpportunity`

### 5.5 Strategy Engine

Strategies:

#### A. `NegRiskArbStrategy`

Purpose:

- Exploit conversion asymmetry in `negRisk=true` events

Inputs:

- Event structure
- Conversion rules
- Live books
- Position inventory

Trades:

- Buy underpriced NO leg and convert into basket of YES legs
- Buy underpriced basket of YES legs when combined value dominates alternatives

Constraints:

- Never trade the `Other` outcome directly unless explicitly supported by strategy rules
- Avoid augmented neg-risk edge cases until separately tested

#### B. `BasketMispricingStrategy`

Purpose:

- Trade multi-outcome events where the basket is too cheap or too expensive relative to redemption value

Patterns:

- `sum(YES outcomes) < 1 - costs`
- Synthetic parity mismatch between YES and NO structures

#### C. `CompleteSetStrategy`

Purpose:

- Build complete sets when total acquisition cost is below guaranteed redeem value

#### D. `MakerCaptureStrategy`

Purpose:

- Optional strategy for deep books with spread and rebate capture

Use only after the arb engine is stable.

### 5.6 Risk Engine

Responsibilities:

- Protect capital from unmatched legs, stale data, and event concentration
- Enforce hard size and exposure limits

Rules:

- Max exposure per event
- Max exposure per category
- Max unmatched leg lifetime
- Max simultaneous basket executions
- Max notional in fee-enabled markets
- Max daily drawdown
- Auto-halt on stale orderbooks or reconciliation drift

Kill-switch triggers:

- Book stale beyond threshold
- Order placement errors exceed threshold
- Hedge leg not completed in time
- Position mismatch vs exchange
- Daily drawdown breached

### 5.7 Execution Planner

Responsibilities:

- Convert opportunities into executable orders
- Decide leg sequence, maker/taker choice, and fallback behavior

Policies:

- Maker-first if edge remains adequate
- Taker only if basket still clears net-edge threshold
- Prefer all-or-none basket completion logic at strategy level even if exchange does not support atomic baskets
- Abort if partial completion risk exceeds allowed budget

Execution model:

- `plan_basket()`
- `reserve_risk_budget()`
- `submit_leg_1()`
- `wait_or_cancel()`
- `submit_remaining_legs()`
- `convert_or_redeem()`
- `close_or_settle()`

### 5.8 Exchange Router

Purpose:

- Single abstraction so paper and live share the same execution logic

Interface:

- `get_book(token_id)`
- `place_order(order_intent)`
- `cancel_order(order_id)`
- `cancel_all()`
- `get_open_orders()`
- `get_positions()`
- `convert_neg_risk(position_set)`
- `redeem_complete_set(event_id)`
- `resolve_market(market_id)`

Implementations:

- `PaperExchangeAdapter`
- `LiveCLOBAdapter`

## 6. Paper Trader Design

The paper trader is a first-class exchange simulator, not a simplified backtester.

### 6.1 Required Modes

#### Replay Mode

- Runs on recorded historical event metadata, books, and trades
- Used for deterministic regression and strategy development

#### Paper-Live Mode

- Subscribes to live Polymarket data
- Simulates order placement and fills without live execution

#### Shadow-Live Mode

- Mirrors exact live order plans against current books
- Used to compare live execution quality vs expected fills before enabling trading

### 6.2 Fill Simulation Rules

Must simulate:

- Queue position at each price level
- Partial fills
- Tick-size rounding
- Order repost latency
- Maker vs taker execution
- Cancellations
- Book gaps
- Crossed/invalid orders
- Market resolution and settlement
- Conversion and redemption effects

Paper-fill model:

1. On order placement, capture full visible book and queue ahead
2. Track subsequent trades and book deltas
3. Decrease queue ahead only when sufficient opposing volume trades through
4. Fill our order only after queue ahead is consumed
5. Respect latency assumptions and cancellation lag

### 6.3 Paper Ledger

Track:

- Cash balance
- Reserved capital
- Token inventory
- Open orders
- Pending conversions
- Pending redemptions
- Realized PnL
- Unrealized PnL
- Fees
- Rebates

### 6.4 Paper Events

Emit:

- `PaperOrderAccepted`
- `PaperOrderRejected`
- `PaperOrderPartiallyFilled`
- `PaperOrderFilled`
- `PaperOrderCancelled`
- `PaperConversionCompleted`
- `PaperRedeemCompleted`
- `PaperSettlementApplied`

### 6.5 Paper Validation Metrics

- Fill-rate error vs live shadow fills
- Slippage error vs actual execution
- Queue-model error
- Basket completion rate
- Unmatched-leg loss rate
- Rebate estimate error

## 7. Storage Blueprint

SQLite is acceptable for development and single-node operation. Postgres is better if the bot grows into multi-process live trading.

### 7.1 Core Tables

#### `events`

- `id`
- `title`
- `category`
- `neg_risk`
- `enable_neg_risk`
- `neg_risk_augmented`
- `rules_text`
- `status`
- `start_time`
- `end_time`
- `resolved_at`

#### `markets`

- `id`
- `event_id`
- `question`
- `outcome_name`
- `token_yes_id`
- `token_no_id`
- `fees_enabled`
- `tick_size`
- `status`

#### `books`

- `book_id`
- `token_id`
- `timestamp`
- `best_bid`
- `best_ask`
- `mid`
- `spread`
- `depth_json`

#### `opportunities`

- `id`
- `strategy_type`
- `event_id`
- `market_ids_json`
- `gross_edge_bps`
- `net_edge_bps`
- `capital_required`
- `decision`
- `reason`
- `created_at`

#### `orders`

- `id`
- `environment` (`paper` or `live`)
- `strategy_run_id`
- `token_id`
- `side`
- `price`
- `size`
- `order_type`
- `maker_or_taker`
- `status`
- `created_at`
- `updated_at`

#### `fills`

- `id`
- `order_id`
- `token_id`
- `price`
- `size`
- `fee_paid`
- `rebate_earned`
- `timestamp`

#### `positions`

- `id`
- `environment`
- `token_id`
- `market_id`
- `size`
- `avg_price`
- `event_id`

#### `baskets`

- `id`
- `strategy_type`
- `event_id`
- `status`
- `capital_reserved`
- `target_net_edge_bps`
- `realized_net_pnl`
- `created_at`
- `closed_at`

#### `conversions`

- `id`
- `basket_id`
- `event_id`
- `input_token_id`
- `output_tokens_json`
- `status`
- `requested_at`
- `completed_at`

#### `settlements`

- `id`
- `market_id`
- `resolution`
- `pnl_realized`
- `timestamp`

## 8. Event Model

Use an internal event bus so every service is loosely coupled and replayable.

Core bus topics:

- `universe.event.updated`
- `market.book.snapshot`
- `market.book.delta`
- `market.trade.print`
- `strategy.opportunity.created`
- `risk.opportunity.approved`
- `execution.order.intent`
- `execution.order.placed`
- `execution.order.filled`
- `execution.order.cancelled`
- `basket.completed`
- `basket.failed`
- `conversion.completed`
- `market.resolved`
- `settlement.applied`
- `risk.halt.triggered`

## 9. Opportunity Logic

### 9.1 Negative-Risk Opportunity

Inputs:

- Event with `negRisk=true`
- All market outcome books
- Conversion mechanics

Decision:

- Compare cost of acquiring a convertible structure vs value of converted outputs
- Trade only if:
  - `net_edge_bps >= min_edge_bps`
  - liquidity on all required legs is sufficient
  - unmatched-leg probability is acceptable

### 9.2 Basket Opportunity

Trade when:

- `sum(YES prices) + all fees + expected slippage < 1.0`

Avoid when:

- market books are too thin
- one leg is stale
- one leg carries subjective resolution risk

### 9.3 Maker Capture Opportunity

Trade when:

- spread is wide enough
- inventory skew is acceptable
- fee/rebate regime is favorable
- adverse-selection model is within limit

## 10. Execution Policies

### 10.1 Maker-First Policy

Use maker orders if:

- the opportunity survives expected queue delay
- quote distance stays inside acceptable drift
- basket completion probability remains high

### 10.2 Taker-Allowed Policy

Use taker only if:

- opportunity still exceeds `min_net_edge_bps_after_fees`
- execution completes a basket or hedge leg
- total taker cost is explicitly budgeted

### 10.3 Basket Safety Policy

Never leave a basket half-built beyond the configured timeout.

Fallbacks:

- cancel remaining legs
- unwind completed legs
- hedge with synthetic alternative if available
- alert operator

## 11. Configuration Profiles

### Conservative

Best for:

- bankroll under $1,000
- first live deployment

Defaults:

- low event concentration
- maker-first only
- fee-free markets only by default
- no directional crypto

### Balanced

Best for:

- bankroll above $1,000
- proven paper performance

Defaults:

- fee-free arb first
- selective maker capture
- limited fee-enabled markets

### Aggressive

Best for:

- only after stable live proof

Defaults:

- broader market set
- more simultaneous baskets
- optional secondary maker module

## 12. Monitoring and Control

Dashboard panels:

- Equity curve
- Open baskets
- Basket completion rate
- Unmatched-leg exposure
- Top opportunities by strategy
- Fill rate by market and strategy
- Fees and rebates
- Resolver / settlement lag
- WebSocket health

Control endpoints:

- `/status`
- `/positions`
- `/orders`
- `/baskets`
- `/opportunities`
- `/halt`
- `/resume`
- `/replay/start`
- `/replay/result`

Alerts:

- hedge failure
- stale book
- missed cancel
- drawdown breach
- reconciliation mismatch
- conversion failure

## 13. Testing Blueprint

### Unit Tests

- pricing math
- basket parity
- neg-risk conversion logic
- fee calculations
- risk gates
- tick-size rounding

### Integration Tests

- book reconstruction from snapshots and deltas
- planner to paper exchange
- order lifecycle
- partial-fill handling
- conversion and redemption flows

### Replay Tests

- deterministic strategy re-run on recorded sessions
- compare PnL and fills against golden outputs

### Shadow Tests

- compare paper-live expected fills vs real market prints

### Chaos Tests

- delayed market data
- dropped WebSocket messages
- API timeout
- stale tick size
- one-leg fill only

## 14. Acceptance Criteria Before Live

The system should not go live until all are true:

- 200+ completed paper baskets or equivalent resolved trade units
- positive net PnL after simulated fees and slippage
- basket completion rate above 99%
- unmatched-leg realized loss below predefined threshold
- replay suite stable across multiple days
- reconciliation drift zero or near zero
- operator kill switch tested

## 15. Suggested Repo Layout

```text
src/
  app/
    main.py
    config.py
  adapters/
    gamma_client.py
    data_client.py
    clob_client.py
    websocket_client.py
  domain/
    models.py
    events.py
    enums.py
  services/
    universe_service.py
    market_data_service.py
    position_service.py
    settlement_service.py
  pricing/
    basket_pricer.py
    neg_risk_pricer.py
    fee_model.py
    slippage_model.py
  strategy/
    base.py
    neg_risk_arb.py
    basket_mispricing.py
    complete_set.py
    maker_capture.py
  risk/
    policy.py
    limits.py
    kill_switch.py
  execution/
    planner.py
    router.py
    live_adapter.py
    paper_adapter.py
    order_manager.py
    conversion_manager.py
  storage/
    db.py
    repositories/
  replay/
    recorder.py
    player.py
    fixtures/
  control/
    api.py
    dashboard.py
  alerts/
    telegram.py
    email.py

scripts/
  record_session.py
  replay_session.py
  paper_live.py
  shadow_live.py
  daily_report.py
  reconcile.py

tests/
  unit/
  integration/
  replay/
  chaos/
```

## 16. Build Phases

### Phase 1: Market Recorder

- Universe service
- Market-data service
- Recorder
- Raw event storage

Deliverable:

- deterministic replay dataset

### Phase 2: Paper Exchange

- Orderbook simulator
- Queue model
- Fill model
- Ledger
- Settlement model

Deliverable:

- replay and paper-live modes

### Phase 3: Structural Arb Engine

- Neg-risk pricer
- Basket pricer
- Risk engine
- Execution planner

Deliverable:

- end-to-end paper arb bot

### Phase 4: Control Surface

- status API
- reporting
- dashboard
- kill switch

Deliverable:

- operator-ready testing stack

### Phase 5: Tiny Live Rollout

- live adapter
- shadow-live burn-in
- tiny-size maker/taker rollout with strict limits

Deliverable:

- low-risk live pilot

## 17. Explicit Non-Goals

Do not start with:

- generic news sentiment bots
- altcoin directional bots
- fully discretionary event parsing
- complex ML before structural edge is proven
- high-frequency crypto taker strategies for small capital

## 18. Recommended First Version

Version 1 should be:

- fee-free markets only
- negative-risk and basket-arb only
- paper-live and replay only
- no live trading
- no maker-capture module

This keeps the first version simple, testable, and aligned with the target objective: high return per unit of risk over time.

## 19. Implementation Note

If this blueprint is implemented in the current repository, the cleanest path is:

- keep the current 5-minute crypto bot intact as a separate strategy family
- build the new system under new modules instead of overloading the directional crypto codepath
- share only generic utilities where behavior truly overlaps

That reduces confusion between:

- directional short-window trading
- structural arbitrage

They are different businesses and should not share risk policy by accident.
