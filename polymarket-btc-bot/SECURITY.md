# Security notes

This project is intended to run **on your own machine** with a **loopback-only** control API and secrets in a local `.env` file.

## What is safe by default

- The structural-arb and legacy control HTTP servers bind to **`127.0.0.1`** only (not `0.0.0.0`), so they are not reachable from other hosts unless you add port forwarding or a reverse proxy yourself.
- **`.env` is listed in `.gitignore`**. Use **`.env.example`** as a template; it must contain only **empty** secret fields and comments, never live credentials. (Filled-in fake values like `password=example` trigger secret scanners and are intentionally avoided.)

## What you should configure

- Set **`CONTROL_API_TOKEN`** in `.env` if there is any chance other users or processes on the machine (or port-forwarded sessions) could reach the control port. When empty, the API does not enforce token auth. When set, **`GET /health` stays unauthenticated** on purpose for simple liveness checks; treat that as a deliberate tradeoff (see `src/arb/control.py`).
- **CORS** is set to allow any origin (`*`) so a dashboard opened via `file://` can call `http://127.0.0.1:8765`. That is convenient for local use; it is not a substitute for network isolation. Do not expose this service to untrusted networks without additional controls.

## If the repository is public

- Rotate **Polymarket API credentials**, **wallet keys**, **Telegram bot token**, and **control API token** if they were ever committed, pasted into issues, or shared in a fork.
- If a secret was committed historically, removing it from the current tree is **not enough**; use secret scanning and consider **history rewrite** (`git filter-repo`) or treat those credentials as compromised.

## Reporting

For vulnerability reports specific to this repository, open a private security advisory with the maintainer (GitHub **Security** tab) if enabled.
