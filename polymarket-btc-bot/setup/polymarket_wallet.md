# Polymarket Wallet Setup Guide

This guide covers creating a Polygon wallet, funding it with USDC, and generating L2 API keys on Polymarket.

---

## Part 1 — Create a Polygon Wallet

You need a wallet private key to sign EIP-712 orders on Polygon mainnet.

### Option A: MetaMask (recommended for beginners)

1. Install MetaMask browser extension from [https://metamask.io](https://metamask.io)
2. Create a new wallet — **write down your seed phrase and store it offline**
3. Add Polygon Mainnet:
   - Network Name: `Polygon Mainnet`
   - RPC URL: `https://polygon-rpc.com`
   - Chain ID: `137`
   - Currency: `MATIC`
   - Block Explorer: `https://polygonscan.com`
4. Export your private key:
   - Click account menu → Account Details → Export Private Key
   - Enter your MetaMask password
   - Copy the key — this is your `WALLET_PRIVATE_KEY` in `.env`

### Option B: Generate a fresh wallet programmatically

```python
from eth_account import Account
account = Account.create()
print("Address:", account.address)
print("Private key:", account.key.hex())
```

> **Security warning:** Never share your private key with anyone. Never commit it to git. Store it only in your `.env` file which is in `.gitignore`.

---

## Part 2 — Fund Your Wallet with USDC on Polygon

Polymarket uses USDC on Polygon. You need to get USDC to your Polygon wallet address.

### Option A: Buy USDC directly on Polygon (simplest)

Use a centralised exchange that supports direct Polygon withdrawals:
- **Coinbase** → Buy USDC → Withdraw → Select network: **Polygon** → Paste your wallet address
- **Binance** → Buy USDC → Withdraw → Network: **MATIC (Polygon)**

Minimum recommended starting balance: **$50+ USDC** (the bot uses $1–30 per trade)

Also send a small amount of **MATIC** for gas fees (~$2 worth is plenty for months of transactions).

### Option B: Bridge from Ethereum

If you already have USDC on Ethereum mainnet:
1. Go to [https://wallet.polygon.technology/polygon/bridge](https://wallet.polygon.technology/polygon/bridge)
2. Connect MetaMask
3. Bridge USDC from Ethereum → Polygon (takes ~10 minutes, costs ~$5–15 in ETH gas)

---

## Part 3 — Create a Polymarket Account and Enable Trading

1. Go to [https://polymarket.com](https://polymarket.com)
2. Click "Sign In" → Connect with MetaMask (same wallet)
3. Sign the message to authenticate (no gas required)
4. On first login, Polymarket will prompt you to **deposit** — transfer USDC from your wallet into your Polymarket balance

---

## Part 4 — Generate L2 API Keys

Polymarket uses a Layer 2 system (Polygon) with separate API credentials for programmatic trading.

1. Go to [https://polymarket.com/settings](https://polymarket.com/settings)
2. Find the **API Keys** section (or visit [https://polymarket.com/api-keys](https://polymarket.com/api-keys))
3. Click "Create new API key"
4. You will receive three values:
   - **API Key** → `POLYMARKET_API_KEY` in `.env`
   - **Secret** → `POLYMARKET_SECRET` in `.env`
   - **Passphrase** → `POLYMARKET_PASSPHRASE` in `.env`
5. Your wallet address → `POLYMARKET_WALLET_ADDRESS` in `.env`

> Store these values immediately — the secret is shown only once.

---

## Part 5 — Verify Setup

Test your credentials with the py-clob-client:

```python
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

client = ClobClient(
    host="https://clob.polymarket.com",
    key="0xYourWalletAddress",
    chain_id=137,
    creds=ApiCreds(
        api_key="your_api_key",
        api_secret="your_secret",
        api_passphrase="your_passphrase",
    ),
)
print(client.get_ok())  # Should print: {'ok': True}
```

---

## Security Checklist

- [ ] Seed phrase stored offline (paper/hardware wallet)
- [ ] Private key in `.env` only — never hardcoded
- [ ] `.env` is in `.gitignore` — verified by running `git status`
- [ ] Start with PAPER_TRADE=true for at least 1 week
- [ ] Use a dedicated trading wallet — not your main personal wallet
- [ ] Only fund with money you can afford to lose
