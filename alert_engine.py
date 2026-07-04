"""
Alert Engine — Watchlist Buy-Cluster Scanner
=============================================
Combines several of the existing app's building blocks into one scoring pipeline:

  1. Scans watchlist wallets for tokens bought in the last N hours (like Tab 3/4)
  2. For each candidate token, checks whether the buying wallets held or dumped it
  3. Pulls top-holder buy/sell pressure for that token (like Tab 6)
  4. Pulls market data (age, market cap, liquidity) from DexScreener
  5. Produces a single 0-100 Alert Score + risk flags

Designed to run STANDALONE (e.g. via a GitHub Actions cron schedule), separate
from the Streamlit UI, since Streamlit Community Cloud has no background scheduler.
It writes results to alerts_output.json, which a "🚨 Alerts" tab in the Streamlit
app can simply read and render.

Env vars expected when run as a script:
    HELIUS_API_KEY        - required
    DISCORD_WEBHOOK_URL   - optional, sends alerts above threshold
    LOOKBACK_HOURS        - optional, default 1.25 (matches an hourly cron with slight overlap)
    MIN_WALLETS           - optional, default 2
    ALERT_THRESHOLD       - optional, default 55
    RENOTIFY_SCORE_DELTA  - optional, default 15  (re-alert if score climbs this much since last alert)
    RENOTIFY_WALLET_DELTA - optional, default 2   (re-alert if this many more wallets joined in)

State/lock files (created next to this script, safe to .gitignore):
    seen_alerts.json      - tracks the score/wallet-count at the time each mint was last notified,
                            so a still-active buy cluster doesn't spam a notification every hour
    alert_engine.lock     - prevents an hourly cron run from overlapping a still-running one
"""

import os
import math
import time
import json
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── reuse constants from the main app (keep these in sync, or import directly
#    from app.py if you split it into a shared module) ────────────────────────
SKIP_TOKENS = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
}

EXCHANGE_WALLETS = {
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "5tzFkiKscXHK5ZXCGbGuEgkrUjDA9b6AXetFnq5SxFBP",
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE", "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5", "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2",
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm", "BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6",
    "8i5HqznCcCPaFLXyUNtPNM1sPQSCyR7D7BQYUURNE2iV", "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",
    "Fc8SF1XqMqmxFrszJNAEKMbW8V6MNrDsmW5sFt2E9wfB", "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
}

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"


# ══════════════════════════════════════════════════════════════════════════════
# LOW-LEVEL HELIUS HELPERS  (same shape as app.py — kept independent on purpose
# so this file can run with zero dependency on streamlit)
# ══════════════════════════════════════════════════════════════════════════════
def _helius_post(helius_url: str, payload: dict, timeout: int = 30, max_retries: int = 4,
                  label: str = "") -> dict | None:
    """
    Shared POST wrapper for every Helius call. Retries with exponential backoff
    specifically on 429 (rate limit), so a burst of requests slows down instead
    of silently dropping wallets/tokens from the scan. Returns the parsed JSON
    response dict, or None if it ultimately failed.
    """
    delay = 1.5
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(helius_url, json=payload, timeout=timeout)
            if r.status_code == 429:
                if attempt < max_retries:
                    retry_after = r.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else delay
                    print(f"  [rate limited] {label} — retrying in {wait:.1f}s "
                          f"(attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait)
                    delay *= 2
                    continue
                print(f"  [rate limited] {label} — out of retries, giving up on this call.")
                return None
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                print(f"  [Helius error] {label}: {data['error']}")
                return None
            return data
        except Exception as e:
            if attempt < max_retries:
                print(f"  [request error] {label}: {e} — retrying in {delay:.1f}s")
                time.sleep(delay)
                delay *= 2
                continue
            print(f"  [request failed] {label}: {e} — out of retries, giving up on this call.")
            return None
    return None


def fetch_signatures(wallet: str, helius_url: str, limit: int = 150) -> list:
    payload = {"jsonrpc": "2.0", "id": "sigs", "method": "getSignaturesForAddress",
               "params": [wallet, {"limit": limit}]}
    data = _helius_post(helius_url, payload, label=f"getSignaturesForAddress {wallet[:8]}...")
    return data.get("result", []) if data else []


def fetch_transaction(sig: str, helius_url: str):
    payload = {"jsonrpc": "2.0", "id": "tx", "method": "getTransaction",
               "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]}
    data = _helius_post(helius_url, payload, label=f"getTransaction {sig[:12]}...")
    return data.get("result") if data else None


def get_token_largest_accounts(mint: str, helius_url: str) -> list:
    payload = {"jsonrpc": "2.0", "id": "tla", "method": "getTokenLargestAccounts",
               "params": [mint, {"commitment": "finalized"}]}
    data = _helius_post(helius_url, payload, label=f"getTokenLargestAccounts {mint[:8]}...")
    return data.get("result", {}).get("value", []) if data else []


def resolve_token_account_owner(token_account: str, helius_url: str) -> str:
    payload = {"jsonrpc": "2.0", "id": "gai", "method": "getAccountInfo",
               "params": [token_account, {"encoding": "jsonParsed"}]}
    data = _helius_post(helius_url, payload, timeout=20, label=f"getAccountInfo {token_account[:8]}...")
    if not data:
        return ""
    parsed = data.get("result", {}).get("value", {}).get("data", {}).get("parsed", {})
    return parsed.get("info", {}).get("owner", "")


def get_token_supply(mint: str, helius_url: str) -> float:
    payload = {"jsonrpc": "2.0", "id": "ts", "method": "getTokenSupply", "params": [mint]}
    data = _helius_post(helius_url, payload, timeout=20, label=f"getTokenSupply {mint[:8]}...")
    if not data:
        return 0.0
    return float(data.get("result", {}).get("value", {}).get("uiAmount") or 0)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — SCAN WATCHLIST FOR NET TOKEN DELTAS (bought AND sold, per mint)
# ══════════════════════════════════════════════════════════════════════════════
def scan_wallet_token_deltas(wallet: str, helius_url: str, cutoff_ts: int):
    """
    Returns (deltas, total_signatures_fetched).
    deltas = {mint: {bought, sold, buy_txs, sell_txs, last_buy_ts}} for everything
    this wallet touched since cutoff_ts. Unlike the Tab 3/4 scanner (which only
    tracks inflows), this also tracks outflows so we can tell buy-and-hold apart
    from buy-and-dump. total_signatures_fetched lets the caller distinguish
    "wallet was just quiet" from "API call returned nothing at all."
    """
    out = defaultdict(lambda: {"bought": 0.0, "sold": 0.0, "buy_txs": 0, "sell_txs": 0, "last_buy_ts": 0})
    sigs = fetch_signatures(wallet, helius_url, limit=150)

    for sig_info in sigs:
        bt = sig_info.get("blockTime", 0)
        if bt < cutoff_ts:
            break
        tx = fetch_transaction(sig_info["signature"], helius_url)
        if not tx:
            continue

        meta = tx.get("meta", {})
        pre = {e["accountIndex"]: e for e in meta.get("preTokenBalances", [])}
        post = {e["accountIndex"]: e for e in meta.get("postTokenBalances", [])}

        for idx in set(pre) | set(post):
            pre_e, post_e = pre.get(idx, {}), post.get(idx, {})
            mint = post_e.get("mint") or pre_e.get("mint")
            owner = post_e.get("owner") or pre_e.get("owner")
            if not mint or owner != wallet or mint in SKIP_TOKENS:
                continue

            pre_amt = float((pre_e.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            post_amt = float((post_e.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            delta = post_amt - pre_amt

            if delta > 0:
                out[mint]["bought"] += delta
                out[mint]["buy_txs"] += 1
                out[mint]["last_buy_ts"] = max(out[mint]["last_buy_ts"], bt)
            elif delta < 0:
                out[mint]["sold"] += abs(delta)
                out[mint]["sell_txs"] += 1

        time.sleep(0.07)

    return out, len(sigs)


def aggregate_watchlist_activity(wallets: list, helius_url: str, lookback_hours: int) -> dict:
    """{mint: {wallet: {bought, sold, buy_txs, sell_txs, last_buy_ts}}} — buys only."""
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp())
    token_activity = defaultdict(dict)

    wallets_with_zero_sigs = 0

    for wallet in wallets:
        deltas, sig_count = scan_wallet_token_deltas(wallet, helius_url, cutoff_ts)
        if sig_count == 0:
            wallets_with_zero_sigs += 1

        for mint, d in deltas.items():
            if d["bought"] > 0:
                token_activity[mint][wallet] = d

        time.sleep(0.15)  # courtesy gap between wallets on top of per-tx pacing, to reduce 429s

    # ── diagnostic: tells you whether "0 candidates" means quiet market vs broken API ──
    print(f"[diagnostic] {len(wallets) - wallets_with_zero_sigs}/{len(wallets)} wallets "
          f"returned at least one signature (any age) from Helius.")
    if wallets_with_zero_sigs == len(wallets):
        print("[diagnostic] ⚠️ EVERY wallet returned zero signatures. This almost certainly "
              "means the Helius API key/URL is invalid, rate-limited, or misconfigured — "
              f"not that all {len(wallets)} real wallets have zero transaction history ever. "
              "Check HELIUS_API_KEY.")
    elif wallets_with_zero_sigs > len(wallets) * 0.5:
        print(f"[diagnostic] ⚠️ Over half your wallets ({wallets_with_zero_sigs}/{len(wallets)}) "
              "returned zero signatures — worth double-checking those addresses are valid "
              "and the API key isn't being rate-limited mid-run.")

    return token_activity


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — MARKET DATA (age, market cap, liquidity) VIA DEXSCREENER
# ══════════════════════════════════════════════════════════════════════════════
def get_token_market_data(mint: str) -> dict | None:
    try:
        r = requests.get(DEXSCREENER_TOKEN_URL.format(mint=mint), timeout=15)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        if not pairs:
            return None
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        best = pairs[0]

        age_days = None
        created_ms = best.get("pairCreatedAt")
        if created_ms:
            age_days = (datetime.now(timezone.utc) -
                        datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)).days

        return {
            "symbol": (best.get("baseToken") or {}).get("symbol", mint[:8]),
            "name": (best.get("baseToken") or {}).get("name", "Unknown"),
            "price_usd": float(best.get("priceUsd") or 0),
            "liquidity_usd": float((best.get("liquidity") or {}).get("usd") or 0),
            "market_cap": float(best.get("marketCap") or best.get("fdv") or 0),
            "age_days": age_days,
            "dex": best.get("dexId"),
            "pair_url": best.get("url"),
        }
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — TOP-HOLDER PRESSURE (same math as the Whale Pressure tab, condensed)
# ══════════════════════════════════════════════════════════════════════════════
def conviction_score(bought: float, sold: float, buy_txs: int, sell_txs: int) -> float:
    total_vol, total_txs = bought + sold, buy_txs + sell_txs
    vol_score = ((bought - sold) / total_vol * 100) if total_vol > 0 else 0
    tx_score = ((buy_txs - sell_txs) / total_txs * 100) if total_txs > 0 else 0
    return round(0.7 * vol_score + 0.3 * tx_score, 1)


def get_top_holder_pressure(mint: str, helius_url: str, price_usd: float = 0.0,
                             top_n: int = 20, min_pct: float = 0.1,
                             headline_window: str = "1d") -> dict:
    """
    'Top holders' = the top `top_n` token accounts by balance (default 20),
    with known exchange wallets excluded (see EXCHANGE_WALLETS).

    Returns:
      score_0_100        - blended conviction score (0=heavy net selling, 50=neutral, 100=heavy net buying),
                            combining 1d/2d/7d activity weighted 50/30/20 toward recent. This feeds the
                            composite alert score.
      windows            - per-window (1d/2d/7d) conviction score, -100..100, for finer-grained inspection
      buying             - {count, usd}: how many of the top holders were net buyers in `headline_window`,
                            and the total USD value of what they bought
      selling            - {count, usd}: same, for net sellers
      top10_pct_supply   - % of total supply held by the top 10 addresses (concentration/rug-risk proxy)
      n_holders          - how many top-N holders were successfully resolved to owner wallets
      top_n_considered   - the top_n value actually used (so callers/messages can state it explicitly)
    """
    empty = {
        "score_0_100": 50.0, "windows": {}, "top10_pct_supply": None, "n_holders": 0,
        "top_n_considered": top_n, "headline_window": headline_window,
        "buying": {"count": 0, "usd": 0.0}, "selling": {"count": 0, "usd": 0.0},
    }

    total_supply = get_token_supply(mint, helius_url)
    largest = get_token_largest_accounts(mint, helius_url)
    if total_supply <= 0 or not largest:
        return empty

    top10_pct = sum(float(e.get("uiAmount") or 0) for e in largest[:10]) / total_supply * 100

    resolved = []
    for entry in largest[:top_n]:
        ui_amount = float(entry.get("uiAmount") or 0)
        pct = ui_amount / total_supply * 100
        if pct < min_pct:
            continue
        owner = resolve_token_account_owner(entry.get("address", ""), helius_url)
        if not owner or owner in EXCHANGE_WALLETS:
            continue
        resolved.append(owner)
        time.sleep(0.1)

    if not resolved:
        empty["top10_pct_supply"] = round(top10_pct, 2)
        return empty

    now_ts = int(datetime.now(timezone.utc).timestamp())
    windows = {"1d": 1, "2d": 2, "7d": 7}
    cutoffs = {k: now_ts - v * 86400 for k, v in windows.items()}
    window_scores = defaultdict(list)
    wallet_window_flows = {}  # wallet -> {wname: {bought, sold, buy_txs, sell_txs}}

    for wallet in resolved:
        sigs = fetch_signatures(wallet, helius_url, limit=150)
        txs = []
        for sig_info in sigs:
            bt = sig_info.get("blockTime", 0)
            if bt < cutoffs["7d"]:
                break
            tx = fetch_transaction(sig_info["signature"], helius_url)
            if not tx:
                continue
            meta = tx.get("meta", {})
            pre = {e["accountIndex"]: e for e in meta.get("preTokenBalances", [])}
            post = {e["accountIndex"]: e for e in meta.get("postTokenBalances", [])}
            for idx in set(pre) | set(post):
                pre_e, post_e = pre.get(idx, {}), post.get(idx, {})
                this_mint = post_e.get("mint") or pre_e.get("mint", "")
                owner = post_e.get("owner") or pre_e.get("owner", "")
                if this_mint != mint or owner != wallet:
                    continue
                pre_amt = float((pre_e.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                post_amt = float((post_e.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                delta = post_amt - pre_amt
                if delta != 0:
                    txs.append({"delta": delta, "ts": bt})
            time.sleep(0.07)

        wallet_window_flows[wallet] = {}
        for wname, _ in windows.items():
            relevant = [t for t in txs if t["ts"] >= cutoffs[wname]]
            bought = sum(t["delta"] for t in relevant if t["delta"] > 0)
            sold = sum(abs(t["delta"]) for t in relevant if t["delta"] < 0)
            buy_txs = sum(1 for t in relevant if t["delta"] > 0)
            sell_txs = sum(1 for t in relevant if t["delta"] < 0)
            wallet_window_flows[wallet][wname] = {
                "bought": bought, "sold": sold, "buy_txs": buy_txs, "sell_txs": sell_txs,
            }
            window_scores[wname].append(conviction_score(bought, sold, buy_txs, sell_txs))

    window_avgs = {w: (sum(s) / len(s) if s else 0) for w, s in window_scores.items()}
    blended = 0.5 * window_avgs.get("1d", 0) + 0.3 * window_avgs.get("2d", 0) + 0.2 * window_avgs.get("7d", 0)
    score_0_100 = round((blended + 100) / 2, 1)  # map -100..100 -> 0..100

    # Headline buying/selling snapshot: classify each top holder by net direction
    # in `headline_window` (default 1d — the most immediately actionable signal).
    buying_count, selling_count = 0, 0
    buying_usd, selling_usd = 0.0, 0.0
    for wallet, wf in wallet_window_flows.items():
        flow = wf.get(headline_window, {})
        net = flow.get("bought", 0) - flow.get("sold", 0)
        if net > 0:
            buying_count += 1
            buying_usd += flow["bought"] * price_usd
        elif net < 0:
            selling_count += 1
            selling_usd += flow["sold"] * price_usd

    return {
        "score_0_100": score_0_100,
        "windows": {w: round(v, 1) for w, v in window_avgs.items()},
        "top10_pct_supply": round(top10_pct, 2),
        "n_holders": len(resolved),
        "top_n_considered": top_n,
        "headline_window": headline_window,
        "buying": {"count": buying_count, "usd": round(buying_usd, 2)},
        "selling": {"count": selling_count, "usd": round(selling_usd, 2)},
    }


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — COMPOSITE SCORING
# ══════════════════════════════════════════════════════════════════════════════
def breadth_score(n_wallets: int, k: float = 3.0) -> float:
    """Diminishing-returns curve: 1 wallet ~28, 3 wallets ~63, 8 wallets ~93."""
    return round(100 * (1 - math.exp(-n_wallets / k)), 1)


def conviction_score_for_buyers(wallet_data: dict) -> float:
    """Average, across buying wallets, of how much of their buy they've since sold back."""
    scores = []
    for d in wallet_data.values():
        if d["bought"] <= 0:
            continue
        sold_fraction = min(1.0, d["sold"] / d["bought"]) if d["bought"] > 0 else 0
        scores.append(100 * (1 - sold_fraction))
    return round(sum(scores) / len(scores), 1) if scores else 0.0


def usd_score(total_usd: float, cap: float = 50_000) -> float:
    if total_usd <= 0:
        return 0.0
    return round(min(100, 100 * math.log10(total_usd + 1) / math.log10(cap)), 1)


def score_label(score: float) -> str:
    if score >= 80: return "🔥 Very Strong Signal"
    if score >= 65: return "🟢 Strong Signal"
    if score >= 50: return "🟩 Moderate Signal"
    if score >= 35: return "🔵 Weak Signal"
    return "⚪ Low Signal"


def score_token(mint: str, wallet_data: dict, helius_url: str) -> dict:
    n_wallets = len(wallet_data)
    market = get_token_market_data(mint) or {}
    price = market.get("price_usd", 0)

    per_wallet_usd = {w: round(d["bought"] * price, 2) for w, d in wallet_data.items()}
    total_usd_bought = sum(per_wallet_usd.values())
    avg_usd_bought = total_usd_bought / n_wallets if n_wallets else 0.0

    b_score = breadth_score(n_wallets)
    c_score = conviction_score_for_buyers(wallet_data)
    u_score = usd_score(total_usd_bought)
    pressure = get_top_holder_pressure(mint, helius_url, price_usd=price, top_n=20)
    p_score = pressure["score_0_100"]

    composite = 0.30 * b_score + 0.20 * c_score + 0.20 * u_score + 0.30 * p_score

    flags = []
    age_days = market.get("age_days")
    if age_days is not None:
        if age_days < 1:
            flags.append("🆕 Brand new token (<24h) — high risk")
        elif age_days < 7:
            flags.append(f"🌱 New token ({age_days}d old) — elevated risk")
        elif age_days > 90:
            flags.append(f"⚡ Established token ({age_days}d old) with a sudden buy cluster — often the more interesting case")
            composite = min(100, composite + 8)  # bonus: old + sudden interest is rarer signal

    liq = market.get("liquidity_usd")
    if liq is not None and liq < 10_000:
        flags.append(f"⚠️ Thin liquidity (${liq:,.0f}) — may be hard to exit")

    top10 = pressure.get("top10_pct_supply")
    if top10 is not None and top10 > 50:
        flags.append(f"⚠️ Top 10 holders control {top10:.0f}% of supply — concentration risk")

    if pressure["n_holders"] == 0:
        flags.append("ℹ️ Could not resolve top-holder wallets — pressure score defaulted to neutral")

    return {
        "mint": mint,
        "symbol": market.get("symbol", mint[:8]),
        "name": market.get("name", "Unknown"),
        "composite_score": round(composite, 1),
        "label": score_label(composite),
        "sub_scores": {
            "breadth": b_score,
            "conviction": c_score,
            "usd_size": u_score,
            "top_holder_pressure": p_score,
        },
        "n_wallets_bought": n_wallets,
        "total_usd_bought_est": round(total_usd_bought, 2),
        "avg_usd_bought_est": round(avg_usd_bought, 2),
        "per_wallet_usd_bought": per_wallet_usd,
        "wallets": {w: {k: v for k, v in d.items()} for w, d in wallet_data.items()},
        "market": market,
        "top_holder_windows": pressure.get("windows", {}),
        "top_holder_buying": pressure.get("buying", {"count": 0, "usd": 0.0}),
        "top_holder_selling": pressure.get("selling", {"count": 0, "usd": 0.0}),
        "top_holder_n_considered": pressure.get("top_n_considered", 20),
        "top_holder_headline_window": pressure.get("headline_window", "1d"),
        "flags": flags,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — ORCHESTRATION
# ══════════════════════════════════════════════════════════════════════════════
def run_alert_scan(wallets: list, helius_url: str, lookback_hours: float = 1.25,
                    min_wallets: int = 2, alert_threshold: float = 55.0) -> dict:
    activity = aggregate_watchlist_activity(wallets, helius_url, lookback_hours)
    candidates = {m: w for m, w in activity.items() if len(w) >= min_wallets}

    results = []
    for mint, wallet_data in candidates.items():
        results.append(score_token(mint, wallet_data, helius_url))

    results.sort(key=lambda x: -x["composite_score"])
    alerts = [r for r in results if r["composite_score"] >= alert_threshold]

    return {
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": lookback_hours,
        "wallets_scanned": len(wallets),
        "candidates_found": len(results),
        "alerts_triggered": len(alerts),
        "all_results": results,
        "alerts": alerts,
    }


# ── optional: push to Telegram ────────────────────────────────────────────────
def send_telegram_alert(bot_token: str, chat_id: str, alert: dict):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    flags_text = "\n".join(alert["flags"]) if alert["flags"] else "—"

    n_considered = alert.get("top_holder_n_considered", 20)
    window = alert.get("top_holder_headline_window", "1d")
    buying = alert.get("top_holder_buying", {"count": 0, "usd": 0.0})
    selling = alert.get("top_holder_selling", {"count": 0, "usd": 0.0})

    per_wallet = alert.get("per_wallet_usd_bought", {})
    per_wallet_lines = "\n".join(
        f"  {w[:6]}...{w[-4:]}: ${usd:,.0f}"
        for w, usd in sorted(per_wallet.items(), key=lambda x: -x[1])
    ) or "  —"

    text = (
        f"{alert['label']}  {alert['symbol']}  ({alert['composite_score']}/100)\n\n"
        f"Wallets bought: {alert['n_wallets_bought']}\n"
        f"Est. average USD bought: ${alert['avg_usd_bought_est']:,.0f}\n\n"
        f"Top {n_considered} Holders Buying ({window}): {buying['count']} holders · ${buying['usd']:,.0f}\n"
        f"Top {n_considered} Holders Selling ({window}): {selling['count']} holders · ${selling['usd']:,.0f}\n\n"
        f"Market cap: ${alert['market'].get('market_cap', 0):,.0f}\n"
        f"Liquidity: ${alert['market'].get('liquidity_usd', 0):,.0f}\n"
        f"Age: {alert['market'].get('age_days', '?')}d\n\n"
        f"Flags:\n{flags_text}\n\n"
        f"USD bought by wallet:\n{per_wallet_lines}\n\n"
        f"Mint: {alert['mint']}\n"
        f"{alert['market'].get('pair_url', '')}"
    )
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False}, timeout=15)
    except Exception:
        pass


# ── optional: push to Discord ─────────────────────────────────────────────────
def send_discord_alert(webhook_url: str, alert: dict):
    n_considered = alert.get("top_holder_n_considered", 20)
    window = alert.get("top_holder_headline_window", "1d")
    buying = alert.get("top_holder_buying", {"count": 0, "usd": 0.0})
    selling = alert.get("top_holder_selling", {"count": 0, "usd": 0.0})

    per_wallet = alert.get("per_wallet_usd_bought", {})
    per_wallet_lines = "\n".join(
        f"`{w[:6]}...{w[-4:]}`: ${usd:,.0f}"
        for w, usd in sorted(per_wallet.items(), key=lambda x: -x[1])
    ) or "—"

    embed = {
        "title": f"{alert['label']}  —  {alert['symbol']}  ({alert['composite_score']}/100)",
        "url": alert["market"].get("pair_url", ""),
        "description": (
            f"**{alert['n_wallets_bought']}** watchlist wallets bought · "
            f"~${alert['avg_usd_bought_est']:,.0f} est. average USD bought\n\n"
            f"**Top {n_considered} Holders Buying** ({window}): {buying['count']} holders · ${buying['usd']:,.0f}\n"
            f"**Top {n_considered} Holders Selling** ({window}): {selling['count']} holders · ${selling['usd']:,.0f}\n\n"
            + ("\n".join(alert["flags"]) if alert["flags"] else "")
        ),
        "fields": [
            {"name": "Mint", "value": f"`{alert['mint']}`", "inline": False},
            {"name": "Market Cap", "value": f"${alert['market'].get('market_cap', 0):,.0f}", "inline": True},
            {"name": "Liquidity", "value": f"${alert['market'].get('liquidity_usd', 0):,.0f}", "inline": True},
            {"name": "Age", "value": f"{alert['market'].get('age_days', '?')}d", "inline": True},
            {"name": "USD bought by wallet", "value": per_wallet_lines, "inline": False},
        ],
    }
    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# LOCK FILE — prevent an hourly cron run from overlapping a still-running scan
# ══════════════════════════════════════════════════════════════════════════════
def acquire_lock(lock_path: str = "alert_engine.lock", max_age_minutes: float = 55) -> bool:
    """
    Returns True if the lock was acquired (safe to proceed), False if another
    run appears to still be active. If a lock file exists but is older than
    max_age_minutes, it's treated as stale (from a crashed run) and overwritten
    rather than blocking forever.
    """
    if os.path.exists(lock_path):
        age_min = (time.time() - os.path.getmtime(lock_path)) / 60
        if age_min < max_age_minutes:
            print(f"Lock held (age {age_min:.1f}m) — another run appears active. Skipping this run.")
            return False
        print(f"Stale lock found (age {age_min:.1f}m) — assuming a previous run crashed, continuing.")

    with open(lock_path, "w") as f:
        f.write(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}")
    return True


def release_lock(lock_path: str = "alert_engine.lock"):
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# ALERT DEDUP STATE — only re-notify on meaningful escalation, not every run
# ══════════════════════════════════════════════════════════════════════════════
def load_alert_state(path: str = "seen_alerts.json") -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_alert_state(state: dict, path: str = "seen_alerts.json"):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def prune_alert_state(state: dict, max_age_days: float = 7) -> dict:
    """Drop mints we haven't alerted on recently, so the file doesn't grow forever."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    pruned = {}
    for mint, info in state.items():
        try:
            last = datetime.fromisoformat(info["last_alerted_at"])
        except Exception:
            continue
        if last >= cutoff:
            pruned[mint] = info
    return pruned


def filter_alerts_for_notification(alerts: list, state_path: str = "seen_alerts.json",
                                    score_delta: float = 15, wallet_delta: int = 2) -> list:
    """
    Given this run's full alert list, return only the ones worth actually
    pinging about: brand-new mints we haven't alerted on, or ones whose score
    or buying-wallet-count has climbed meaningfully since the last time we did.
    Updates and persists the state file as a side effect.
    """
    state = prune_alert_state(load_alert_state(state_path))
    to_notify = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for a in alerts:
        mint = a["mint"]
        prev = state.get(mint)
        is_new = prev is None
        score_jumped = (not is_new) and (a["composite_score"] - prev.get("last_alerted_score", 0) >= score_delta)
        wallets_jumped = (not is_new) and (a["n_wallets_bought"] - prev.get("last_alerted_wallets", 0) >= wallet_delta)

        if is_new or score_jumped or wallets_jumped:
            to_notify.append(a)
            state[mint] = {
                "symbol": a["symbol"],
                "last_alerted_score": a["composite_score"],
                "last_alerted_wallets": a["n_wallets_bought"],
                "last_alerted_at": now_iso,
            }

    save_alert_state(state, state_path)
    return to_notify


# ══════════════════════════════════════════════════════════════════════════════
# CLI / cron entrypoint
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Import your watchlist. Simplest path: keep PRESET_WALLETS in a shared
    # wallets.py that both app.py and this script import from.
    try:
        from wallets import PRESET_WALLETS
    except ImportError:
        PRESET_WALLETS = []

    api_key = os.environ.get("HELIUS_API_KEY", "")
    if not api_key or not PRESET_WALLETS:
        raise SystemExit("Set HELIUS_API_KEY and populate PRESET_WALLETS (wallets.py) before running.")

    if not acquire_lock():
        raise SystemExit(0)  # another run is active — exit quietly, cron will try again next hour

    try:
        helius_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"
        lookback = float(os.environ.get("LOOKBACK_HOURS", 1.25))
        min_wallets = int(os.environ.get("MIN_WALLETS", 2))
        threshold = float(os.environ.get("ALERT_THRESHOLD", 55))
        score_delta = float(os.environ.get("RENOTIFY_SCORE_DELTA", 15))
        wallet_delta = int(os.environ.get("RENOTIFY_WALLET_DELTA", 2))

        result = run_alert_scan(PRESET_WALLETS, helius_url, lookback, min_wallets, threshold)

        # Full results always written — this is what a Streamlit "Alerts" tab reads,
        # so it should reflect everything from this run, deduped or not.
        with open("alerts_output.json", "w") as f:
            json.dump(result, f, indent=2)

        to_notify = filter_alerts_for_notification(
            result["alerts"], score_delta=score_delta, wallet_delta=wallet_delta
        )

        print(f"Scanned {result['wallets_scanned']} wallets, {result['candidates_found']} candidates, "
              f"{result['alerts_triggered']} alerts ({len(to_notify)} new/escalated, "
              f"{result['alerts_triggered'] - len(to_notify)} suppressed as repeats).")

        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if webhook:
            for a in to_notify:
                send_discord_alert(webhook, a)

        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if tg_token and tg_chat_id:
            for a in to_notify:
                send_telegram_alert(tg_token, tg_chat_id, a)

    finally:
        release_lock()

