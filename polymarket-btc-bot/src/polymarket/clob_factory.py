"""
Build py_clob_client.ClobClient from Settings.

Live trading needs:
  • `key` = wallet private key (signs orders), not the public address
  • L2 API credentials — either set POLYMARKET_* in .env or omit them and derive via
    `create_or_derive_api_creds()` (same as scripts/derive_polymarket_api_creds.py)

Optional Builder Program (docs: builder methods on ClobClient): set all of
BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE.

Settings → "Relayer API Keys" (single key + signer address) is not the same as the L2 triple;
store those in RELAYER_* for reference. You still need WALLET_PRIVATE_KEY + L2 creds (or derive).
"""
from __future__ import annotations

from typing import Any

import structlog

from ..config import Settings

logger = structlog.get_logger(__name__)


def _normalize_pk(raw: str) -> str:
    s = raw.strip().strip('"').strip("'")
    if not s:
        return ""
    h = s[2:] if s.startswith(("0x", "0X")) else s
    if len(h) != 64:
        return ""
    try:
        int(h, 16)
    except ValueError:
        return ""
    return "0x" + h.lower()


def _l2_creds_filled(cfg: Settings) -> bool:
    return bool(
        (cfg.polymarket_api_key or "").strip()
        and (cfg.polymarket_secret or "").strip()
        and (cfg.polymarket_passphrase or "").strip()
    )


def build_live_clob_client(cfg: Settings) -> Any:
    """Authenticated CLOB client: private key + L2 creds + optional BuilderConfig."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    from py_clob_client.constants import POLYGON

    pk = _normalize_pk(cfg.wallet_private_key or "")
    if not pk:
        raise ValueError(
            "WALLET_PRIVATE_KEY missing or invalid — required for live ClobClient(key=...)"
        )

    host = (cfg.clob_host or "https://clob.polymarket.com").rstrip("/")

    # Signature type for ORDER signing (not L2 auth):
    #   0 = standard EOA — funder IS the signing address; funds must be in EOA on CLOB
    #   1 = Poly Proxy — funder is the Polymarket Proxy contract (Magic Link users)
    #   2 = Gnosis Safe — funder is the Safe contract deployed by Polymarket on first login
    #
    # MetaMask users created after ~2024 get a Gnosis Safe (type 2).
    # POLYMARKET_WALLET_ADDRESS must be the Safe address (polymarket.com/settings), NOT the EOA.
    # The EOA private key is still used for signing — signer = EOA, maker = Safe.
    sig_type = int(getattr(cfg, "clob_signature_type", 0) or 0)

    if _l2_creds_filled(cfg):
        creds = ApiCreds(
            api_key=cfg.polymarket_api_key.strip(),
            api_secret=cfg.polymarket_secret.strip(),
            api_passphrase=cfg.polymarket_passphrase.strip(),
        )
    else:
        tmp = ClobClient(host, chain_id=POLYGON, key=pk, signature_type=sig_type)
        creds = tmp.create_or_derive_api_creds()
        logger.info(
            "clob_factory.derived_l2_creds",
            note="POLYMARKET_* empty — derived L2 creds from wallet key",
        )

    builder_config = _optional_builder_config(cfg)

    funder = (cfg.polymarket_wallet_address or "").strip()
    if not funder.startswith("0x") or len(funder) != 42:
        raise ValueError(
            "POLYMARKET_WALLET_ADDRESS must be your public 0x address (42 chars)"
        )

    logger.info("clob_factory.build", signature_type=sig_type, funder=funder[:10] + "…")

    kwargs: dict[str, Any] = dict(
        host=host,
        key=pk,
        chain_id=POLYGON,
        creds=creds,
        signature_type=sig_type,
        funder=funder,
    )
    if builder_config is not None:
        kwargs["builder_config"] = builder_config

    return ClobClient(**kwargs)


def _optional_builder_config(cfg: Settings) -> Any | None:
    k = (cfg.builder_api_key or "").strip()
    s = (cfg.builder_secret or "").strip()
    p = (cfg.builder_passphrase or "").strip()
    if not (k and s and p):
        return None
    try:
        from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

        return BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=k,
                secret=s,
                passphrase=p,
            ),
        )
    except Exception as exc:
        logger.warning("clob_factory.builder_config_skip", error=str(exc))
        return None


def build_public_clob_client(cfg: Settings) -> Any:
    """Read-only order books (no wallet auth)."""
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON

    host = (cfg.clob_host or "https://clob.polymarket.com").rstrip("/")
    return ClobClient(host, chain_id=POLYGON, signature_type=2)
