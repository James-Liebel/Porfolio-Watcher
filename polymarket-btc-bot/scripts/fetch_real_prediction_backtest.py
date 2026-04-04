"""
Build a real dataset from public APIs and run the prediction backtest + analysis.

Data sources (no Polymarket API key):
  - Gamma: closed binary Yes/No markets (labels + metadata)
  - CLOB prices-history: YES token price nearest *before* cutoff (market-implied baseline)
  - CoinGecko (optional): 7d and 1d log returns of BTC or ETH when the question mentions them
  - Google News RSS: headlines matching the question keywords published before cutoff

Writes under data/prediction_training/autorun-<utc>/ then prints metrics and a short analysis.

Usage (from polymarket-btc-bot):
  .venv\\Scripts\\python.exe scripts\\fetch_real_prediction_backtest.py --limit 25
  .venv\\Scripts\\python.exe scripts\\fetch_real_prediction_backtest.py --limit 40 --contested-only --train-fraction 0.7
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.prediction.cases import build_event_cases  # noqa: E402
from src.prediction.evaluate import compute_prediction_metrics, split_cases_chronologically  # noqa: E402

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
COINGECKO = "https://api.coingecko.com/api/v3"

UA = "Mozilla/5.0 (compatible; prediction-backtest/1.0; +https://polymarket.com)"


def _get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_iso_utc(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_yes_no(m: dict[str, Any]) -> bool:
    try:
        oc = ast.literal_eval(m.get("outcomes", "[]"))
    except (SyntaxError, ValueError):
        return False
    return oc == ["Yes", "No"]


def _is_resolved(m: dict[str, Any]) -> bool:
    try:
        pr = ast.literal_eval(m.get("outcomePrices", "[]"))
        y, n = float(pr[0]), float(pr[1])
    except (SyntaxError, ValueError, IndexError, TypeError):
        return False
    return max(y, n) >= 0.89


def _resolved_yes(m: dict[str, Any]) -> bool:
    pr = ast.literal_eval(m["outcomePrices"])
    return float(pr[0]) > float(pr[1])


def _yes_token_id(m: dict[str, Any]) -> str:
    tids = ast.literal_eval(m["clobTokenIds"])
    return str(tids[0])


def clob_yes_price_before(token_id: str, cutoff_ts: int) -> float | None:
    """Last CLOB midpoint in history at or before cutoff_ts (unix)."""
    tid = urllib.parse.quote(token_id)
    for fidelity in (720, 360, 1440, 60, 1):
        url = f"{CLOB_BASE}/prices-history?market={tid}&interval=max&fidelity={fidelity}"
        try:
            raw = _get(url, timeout=45.0)
        except (urllib.error.HTTPError, OSError):
            continue
        data = json.loads(raw.decode("utf-8"))
        hist = data.get("history") or []
        if not hist:
            continue
        eligible = [h for h in hist if int(h["t"]) <= cutoff_ts]
        if eligible:
            return float(eligible[-1]["p"])
        return float(hist[0]["p"])
    return None


def coingecko_log_return(coin_id: str, cutoff: datetime, lookback_days: int) -> float | None:
    """log(P_cutoff / P_start) over ~lookback_days window from CoinGecko market_chart/range."""
    t_end = int(cutoff.timestamp())
    t_start = t_end - (max(1, lookback_days) + 1) * 86400
    url = (
        f"{COINGECKO}/coins/{coin_id}/market_chart/range"
        f"?vs_currency=usd&from={t_start}&to={t_end}"
    )
    try:
        raw = _get(url, timeout=45.0)
    except OSError:
        return None
    data = json.loads(raw.decode("utf-8"))
    prices = data.get("prices") or []
    if len(prices) < 2:
        return None
    first = float(prices[0][1])
    last = float(prices[-1][1])
    if first <= 0 or last <= 0:
        return None
    return max(-2.0, min(2.0, math.log(last / first)))


def coin_for_question(q: str) -> str | None:
    ql = q.lower()
    if "bitcoin" in ql or "btc" in ql or "satoshi" in ql:
        return "bitcoin"
    if "ethereum" in ql or "ether" in ql or "eth " in ql or ql.startswith("will eth"):
        return "ethereum"
    return None


def google_news_headlines(query: str, cutoff: datetime, max_items: int = 12) -> list[tuple[datetime, str]]:
    q = " ".join(query.split()[:14])
    enc = urllib.parse.quote(q)
    url = f"https://news.google.com/rss/search?q={enc}&hl=en-US&gl=US&ceid=US:en"
    try:
        raw = _get(url, timeout=30.0)
    except OSError:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item") + root.findall(".//atom:entry", ns)
    out: list[tuple[datetime, str]] = []
    for it in items:
        title_el = it.find("title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        pub_el = it.find("pubDate")
        if pub_el is None or not pub_el.text:
            dt_el = it.find("atom:updated", ns)
            if dt_el is None or not (dt_el.text or "").strip():
                continue
            try:
                t = _parse_iso_utc(dt_el.text.strip())
            except ValueError:
                continue
        else:
            try:
                t = parsedate_to_datetime(pub_el.text.strip())
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                else:
                    t = t.astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
        if t >= cutoff:
            continue
        if title:
            out.append((t, title))
        if len(out) >= max_items:
            break
    return out


@dataclass
class BuiltRow:
    event_id: str
    question: str
    cutoff: datetime
    resolved_yes: bool
    market_yes: float
    clob_ok: bool
    news_count: int
    history_ok: bool


def fetch_markets_page(offset: int, limit: int) -> list[dict[str, Any]]:
    q = urllib.parse.urlencode(
        {
            "closed": "true",
            "limit": str(limit),
            "offset": str(offset),
            "order": "volumeNum",
            "ascending": "false",
        }
    )
    url = f"{GAMMA_BASE}/markets?{q}"
    return json.loads(_get(url, timeout=120.0).decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch real Polymarket + news data and run prediction backtest.")
    ap.add_argument("--limit", type=int, default=30, help="Target number of markets after filters.")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--min-volume", type=float, default=3000.0)
    ap.add_argument("--cutoff-days", type=int, default=14, help="Cutoff is this many days before endDate.")
    ap.add_argument("--rss-sleep", type=float, default=1.2, help="Seconds between Google RSS requests.")
    ap.add_argument("--out-dir", type=str, default="", help="Override output directory.")
    ap.add_argument(
        "--contested-only",
        action="store_true",
        help="Keep only markets whose CLOB YES at cutoff is in the uncertainty band (fairer vs trivial 0.02/0.98).",
    )
    ap.add_argument("--contested-min", type=float, default=0.28, help="Min YES price at cutoff (with --contested-only).")
    ap.add_argument("--contested-max", type=float, default=0.72, help="Max YES price at cutoff (with --contested-only).")
    ap.add_argument(
        "--shrink-weight",
        type=float,
        default=0.28,
        help="Market weight in historical shrink blend (see predict_history_shrunk).",
    )
    ap.add_argument(
        "--train-fraction",
        type=float,
        default=None,
        help="If set (e.g. 0.7), also report metrics on train vs test split by event cutoff (chronological).",
    )
    args = ap.parse_args()

    collected: list[dict[str, Any]] = []
    offset = 0
    cap = max(args.limit * (100 if args.contested_only else 3), 80)
    scan_limit = 25000 if args.contested_only else 8000
    while len(collected) < cap and offset < scan_limit:
        page = fetch_markets_page(offset, args.page_size)
        if not page:
            break
        for m in page:
            if not _is_yes_no(m) or not _is_resolved(m):
                continue
            if float(m.get("volumeNum") or 0) < args.min_volume:
                continue
            collected.append(m)
            if len(collected) >= cap:
                break
        if len(collected) >= cap:
            break
        offset += args.page_size
    if not collected:
        print("No markets matched filters.")
        return 1

    # High-volume pages are rarely crypto; pull extra closed Yes/No crypto/ETH markets for real history signal.
    seen_ids = {str(m["id"]) for m in collected}
    crypto_want = max(6, min(12, args.limit // 2))
    crypto_have = sum(1 for m in collected if coin_for_question(str(m.get("question") or "")))
    off2 = 0
    while crypto_have < crypto_want and off2 < scan_limit:
        page = fetch_markets_page(off2, args.page_size)
        if not page:
            break
        for m in page:
            if str(m["id"]) in seen_ids:
                continue
            if not _is_yes_no(m) or not _is_resolved(m):
                continue
            if float(m.get("volumeNum") or 0) < 1500.0:
                continue
            if not coin_for_question(str(m.get("question") or "")):
                continue
            collected.append(m)
            seen_ids.add(str(m["id"]))
            crypto_have += 1
            if crypto_have >= crypto_want:
                break
        off2 += args.page_size

    collected.sort(
        key=lambda m: (0 if coin_for_question(str(m.get("question") or "")) else 1, -float(m.get("volumeNum") or 0)),
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out_dir) if args.out_dir else ROOT / "data" / "prediction_training" / f"autorun-{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    events_rows: list[dict[str, Any]] = []
    news_rows: list[dict[str, Any]] = []
    hist_rows: list[dict[str, Any]] = []
    meta_rows: list[BuiltRow] = []

    for i, m in enumerate(collected):
        if len(meta_rows) >= args.limit:
            break
        eid = f"pm-{m['id']}"
        q = str(m.get("question") or "")
        if not m.get("endDate"):
            continue
        end = _parse_iso_utc(str(m["endDate"]))
        cutoff = end - timedelta(days=args.cutoff_days)
        if cutoff <= _parse_iso_utc(str(m.get("createdAt", m["endDate"]))) + timedelta(hours=1):
            continue

        yes = _resolved_yes(m)
        tid = _yes_token_id(m)
        cutoff_ts = int(cutoff.timestamp())
        myes = clob_yes_price_before(tid, cutoff_ts)
        clob_ok = myes is not None
        if myes is None:
            myes = 0.5

        myes = max(0.02, min(0.98, float(myes)))

        if args.contested_only and not (args.contested_min <= myes <= args.contested_max):
            continue

        events_rows.append(
            {
                "event_id": eid,
                "title": q,
                "cutoff_time": cutoff.isoformat(),
                "resolved_yes": yes,
                "market_yes_price": myes,
            }
        )

        coin = coin_for_question(q)
        sig_7: float | None = None
        sig_1: float | None = None
        if coin:
            time.sleep(0.35)
            sig_7 = coingecko_log_return(coin, cutoff, 7)
            time.sleep(0.35)
            sig_1 = coingecko_log_return(coin, cutoff, 1)
        t_hist = cutoff - timedelta(hours=1)
        if sig_7 is not None:
            hist_rows.append(
                {
                    "event_id": eid,
                    "time": t_hist.isoformat(),
                    "metric": "signal_7d",
                    "value": float(sig_7),
                }
            )
        if sig_1 is not None:
            hist_rows.append(
                {
                    "event_id": eid,
                    "time": (t_hist - timedelta(minutes=2)).isoformat(),
                    "metric": "signal_1d",
                    "value": float(sig_1),
                }
            )
        history_ok = sig_7 is not None or sig_1 is not None

        time.sleep(args.rss_sleep)
        headlines = google_news_headlines(q, cutoff)
        for t, title in headlines:
            news_rows.append(
                {
                    "event_id": eid,
                    "time": t.isoformat(),
                    "headline": title,
                    "body": "",
                }
            )

        meta_rows.append(
            BuiltRow(
                event_id=eid,
                question=q,
                cutoff=cutoff,
                resolved_yes=yes,
                market_yes=myes,
                clob_ok=clob_ok,
                news_count=len(headlines),
                history_ok=history_ok,
            )
        )

    ev_path = out / "events.jsonl"
    nw_path = out / "news.jsonl"
    hi_path = out / "history.jsonl"
    for path, rows in [(ev_path, events_rows), (nw_path, news_rows), (hi_path, hist_rows)]:
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")

    report_path = out / "report.json"
    cases = build_event_cases(ev_path, nw_path, hi_path)
    full_eval = compute_prediction_metrics(cases, shrink_weight=float(args.shrink_weight))
    metrics = full_eval["metrics"]

    n_total = len(cases)
    train_test_block: dict[str, Any] | None = None
    if args.train_fraction is not None and n_total >= 2:
        tr, te = split_cases_chronologically(cases, float(args.train_fraction))
        train_test_block = {
            "train_fraction_arg": float(args.train_fraction),
            "train_n": len(tr),
            "test_n": len(te),
            "train_metrics": compute_prediction_metrics(tr, shrink_weight=float(args.shrink_weight))["metrics"],
            "test_metrics": compute_prediction_metrics(te, shrink_weight=float(args.shrink_weight))["metrics"]
            if te
            else [],
        }
    n_clob = sum(1 for r in meta_rows if r.clob_ok)
    n_news = sum(1 for r in meta_rows if r.news_count > 0)
    n_hist = sum(1 for r in meta_rows if r.history_ok)

    analysis = {
        "output_dir": str(out.resolve()),
        "events_used": n_total,
        "clob_price_at_cutoff_ok": n_clob,
        "events_with_any_news_before_cutoff": n_news,
        "events_with_coingecko_crypto_signal": n_hist,
        "contested_only": bool(args.contested_only),
        "contested_band": [args.contested_min, args.contested_max] if args.contested_only else None,
        "shrink_weight": args.shrink_weight,
        "metrics": metrics,
        "train_test": train_test_block,
        "notes": [
            "Baseline uses CLOB YES price at/ before cutoff when available; else 0.5.",
            "Historical uses mean of latest CoinGecko 7d/1d log-return signals (BTC/ETH-titled markets); shrink blends that with market YES.",
            "News uses expanded lexicon, negation window, phrases, recency weights on RSS headlines.",
        ],
    }
    report_path.write_text(json.dumps(analysis, indent=2), encoding="utf-8")

    print("=== Real-data prediction backtest ===\n")
    print(f"Output: {out.resolve()}")
    print(f"Events in backtest: {n_total}")
    print(f"CLOB YES snap @ cutoff: {n_clob}/{n_total}")
    print(f"Any pre-cutoff RSS headlines: {n_news}/{n_total}")
    print(f"CoinGecko crypto signal rows (7d and/or 1d): {n_hist}/{n_total}")
    if args.contested_only:
        print(
            f"Contested filter: YES at cutoff in [{args.contested_min:.2f}, {args.contested_max:.2f}] only\n"
        )
    else:
        print()

    best = min(metrics, key=lambda x: x["brier"])
    print("Metric (lower Brier / LogLoss is better on probabilities):\n")
    for m in metrics:
        mark = "  <-- best Brier" if m["name"] == best["name"] else ""
        print(f"  {m['name']:<22}  Brier={m['brier']:.4f}  LogLoss={m['log_loss']:.4f}{mark}")

    if train_test_block and train_test_block.get("test_n", 0) > 0:
        print("\n--- Train / test (by cutoff time) ---\n")
        print(
            f"  train_n={train_test_block['train_n']}  test_n={train_test_block['test_n']}  "
            f"fraction={train_test_block['train_fraction_arg']}\n"
        )
        tm = {m["name"]: m for m in train_test_block["test_metrics"]}
        for name in ("baseline_market", "news_keywords", "blend_shrunk_news"):
            if name in tm:
                m = tm[name]
                print(f"  TEST {name:<20}  Brier={m['brier']:.4f}  LogLoss={m['log_loss']:.4f}")
        print("  (Use full report.json for train metrics and all model rows.)")

    print("\n--- Analysis ---\n")
    if n_total < 5:
        print("Very small sample — treat scores as illustrative only.\n")
    if n_hist:
        print(
            f"CoinGecko supplied a 7d BTC/ETH log-return `signal` for {n_hist}/{n_total} events "
            f"(crypto-titled questions only). The rest use neutral 0.5 on the history branch.\n"
        )

    b_base = metrics[0]["brier"]
    b_h = metrics[1]["brier"]
    b_hs = metrics[2]["brier"]
    b_n = metrics[3]["brier"]
    b_bl = metrics[4]["brier"]
    b_bls = metrics[5]["brier"]

    if b_hs < b_h:
        print(
            "Historical shrink (toward market YES) improved Brier vs raw crypto momentum - "
            "heuristic was over-dispersed relative to prices.\n"
        )
    if b_h < b_base:
        print(
            "The crude crypto momentum signal beat the market snapshot baseline on Brier. "
            "That can happen when many markets are BTC/ETH price bins and momentum lined up with resolution."
        )
    else:
        print(
            "The market-implied price at cutoff (when available) was at least as sharp as the crypto-only heuristic. "
            "That is common: Polymarket prices already encode public information."
        )
    print()

    if b_bls < b_bl:
        print("Blend using shrunk history + news beat raw history + news on this sample.\n")

    if b_n < b_base:
        print("Keyword news improved on the baseline — verify on a larger pull; RSS remains noisy.")
    else:
        print(
            "News lexicon still did not beat the market baseline on this draw. "
            "For LLM news scores, use run_prediction_backtest.py --news-llm on the same JSONL files."
        )
    print()

    print(f"Full JSON report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
