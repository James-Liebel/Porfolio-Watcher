# Recommended Multi-Coin Paper Portfolio

Use this as a practical default portfolio for paper trading with this bot.
It prioritizes liquidity and the likelihood of active short-window markets.

## Core Basket (enable first)

- BTC: 30%
- ETH: 20%
- SOL: 15%
- XRP: 10%

## Expansion Basket (enable after 3-5 days stable run)

- ADA: 8%
- DOGE: 7%
- AVAX: 5%
- LINK: 5%

## Why this set works

- These coins usually have deep Binance spot liquidity.
- They frequently appear in crypto prediction market coverage.
- The mix balances high-liquidity majors (BTC/ETH) with higher-beta alts.

## Suggested enablement plan

1. Days 1-3: BTC, ETH, SOL, XRP only.
2. Days 4-7: enable ADA and DOGE.
3. If fill quality remains stable, add AVAX and LINK.

## .env switches

Set these in `.env`:

- `TRADE_BTC=true`
- `TRADE_ETH=true`
- `TRADE_SOL=true`
- `TRADE_XRP=true`
- `TRADE_ADA=true`
- `TRADE_DOGE=true`
- `TRADE_AVAX=true`
- `TRADE_LINK=true`

If any asset underperforms, disable it without stopping the bot by using
the control API `POST /halt/asset` endpoint.
