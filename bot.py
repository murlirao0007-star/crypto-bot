"""
CoinGlass MIXED-TIMEFRAME Confluence Bot -> Telegram Alerts (ALERT ONLY)
Tuned for CoinGlass HOBBYIST plan.

Features:
- Two scanners:
    SWING (4h) - strong, slower signals
    FAST  (15m) - quick moves
- 50 coins monitored:
    30 top coins (first 30 from supported list)
    20 trending (biggest 24h price movers from Binance)
- 4-signal confluence (min 3/4 to alert)
- Binance liquidation zones used for smart SL/TP when available
- Prices shown in USD + INR

Trade manually on CoinSwitch.
"""

import os
import time
import threading
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from dotenv import load_dotenv

load_dotenv()


# ============ WEB SERVER (for Render free tier / UptimeRobot ping) ============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive\n")

    def log_message(self, format, *args):
        pass  # silence default access logs


def start_web_server():
    port = int(os.getenv("PORT", "10000"))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        print(f"[web] Listening on :{port}", flush=True)
        server.serve_forever()
    except Exception as e:
        print(f"[web err] {e}", flush=True)

# ============ CONFIG ============
MIN_CONFLUENCE = 3

# Scanner intervals (seconds between scans)
SWING_SCAN_EVERY_SECS = 900      # run SWING (4h) scanner every 15 min
FAST_SCAN_EVERY_SECS = 420       # run FAST (15m) scanner every 7 min

# Coin selection
TOP_N_STABLE = 30                # top coins from CoinGlass supported list
TOP_N_TRENDING = 20              # biggest 24h movers from Binance

# Signal thresholds (SWING)
SWING_FR_HIGH = 0.008
SWING_FR_LOW = -0.005
SWING_OI_RISE = 1.0
SWING_OI_DROP = -2.5

# Signal thresholds (FAST - more sensitive)
FAST_FR_HIGH = 0.003
FAST_FR_LOW = -0.003
FAST_OI_RISE = 0.5
FAST_OI_DROP = -1.5

LS_RATIO_LONG_HEAVY = 1.5
LS_RATIO_SHORT_HEAVY = 0.67

# SL/TP
SL_PCT_SWING = 2.0
TP_PCT_SWING = 4.0
SL_PCT_FAST = 1.2
TP_PCT_FAST = 2.4

REQUEST_DELAY_SEC = 0.4

# ============ APIs ============
CG_BASE = "https://open-api-v4.coinglass.com"
BINANCE_FUT_BASE = "https://fapi.binance.com"
BINANCE_SPOT_BASE = "https://api.binance.com"
# Free INR source (no API key)
FX_URL = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json"
FX_FALLBACK_URL = "https://latest.currency-api.pages.dev/v1/currencies/usd.json"

CG_KEY = os.getenv("COINGLASS_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

IST = timezone(timedelta(hours=5, minutes=30))
_alerted = set()
_usd_to_inr = 83.0   # fallback; updated every scan
_usd_to_inr_updated = 0


def log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def cg_headers():
    return {"CG-API-KEY": CG_KEY, "accept": "application/json"}


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        log("[TG] token/chat not set")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if r.status_code != 200:
            log(f"[TG ERR] {r.status_code} {r.text[:200]}")
    except Exception as e:
        log(f"[TG ERR] {e}")


# ============ INR RATE ============
def refresh_inr_rate():
    """Refresh USD->INR rate; call once per scan."""
    global _usd_to_inr, _usd_to_inr_updated
    # only update once per hour
    if time.time() - _usd_to_inr_updated < 3600:
        return
    for url in (FX_URL, FX_FALLBACK_URL):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                inr = float(r.json().get("usd", {}).get("inr", 0))
                if 50 < inr < 200:
                    _usd_to_inr = inr
                    _usd_to_inr_updated = time.time()
                    log(f"  [fx] 1 USD = Rs.{inr:.2f}")
                    return
        except Exception:
            continue
    log(f"  [fx] update failed, using last rate Rs.{_usd_to_inr:.2f}")


# ============ COIN SELECTION ============
def get_stable_coins(n):
    try:
        r = requests.get(
            f"{CG_BASE}/api/futures/supported-coins",
            headers=cg_headers(),
            timeout=15,
        )
        data = r.json().get("data", [])
        if isinstance(data, list):
            return data[:n]
    except Exception as e:
        log(f"[ERR] stable coins: {e}")
    return []


def get_trending_from_binance(n, exclude=None):
    """Get biggest 24h movers (absolute % change) from Binance futures."""
    exclude = set(s + "USDT" for s in (exclude or []))
    try:
        r = requests.get(
            f"{BINANCE_FUT_BASE}/fapi/v1/ticker/24hr",
            timeout=15,
        )
        if r.status_code != 200:
            log(f"[ERR] binance ticker: {r.status_code}")
            return []
        data = r.json()
        usdt = [
            x for x in data
            if x.get("symbol", "").endswith("USDT")
            and x["symbol"] not in exclude
            and float(x.get("quoteVolume", 0)) > 10_000_000  # at least $10M daily volume
        ]
        # sort by absolute % change - biggest movers (up OR down)
        usdt.sort(key=lambda x: abs(float(x.get("priceChangePercent", 0))), reverse=True)
        # strip "USDT" suffix so we get base symbols
        return [x["symbol"][:-4] for x in usdt[:n]]
    except Exception as e:
        log(f"[ERR] trending: {e}")
        return []


def build_coin_list():
    """Returns list of (base_symbol, category) tuples."""
    stable = get_stable_coins(TOP_N_STABLE)
    trending = get_trending_from_binance(TOP_N_TRENDING, exclude=stable)
    coins = [(s, "STABLE") for s in stable] + [(t, "TRENDING") for t in trending]
    return coins


# ============ COINGLASS DATA ============
def get_oi_change(symbol_pair, interval):
    try:
        r = requests.get(
            f"{CG_BASE}/api/futures/open-interest/history",
            headers=cg_headers(),
            params={"exchange": "Binance", "symbol": symbol_pair, "interval": interval, "limit": 2},
            timeout=15,
        )
        rows = r.json().get("data", [])
        if len(rows) >= 2:
            prev = float(rows[-2]["close"])
            curr = float(rows[-1]["close"])
            if prev > 0:
                return ((curr - prev) / prev) * 100.0
    except Exception:
        pass
    return None


def get_funding_rate(symbol_pair, interval):
    try:
        r = requests.get(
            f"{CG_BASE}/api/futures/funding-rate/history",
            headers=cg_headers(),
            params={"exchange": "Binance", "symbol": symbol_pair, "interval": interval, "limit": 1},
            timeout=15,
        )
        rows = r.json().get("data", [])
        if rows:
            return float(rows[-1].get("close", 0))
    except Exception:
        pass
    return None


def get_long_short_ratio(symbol_pair, interval):
    try:
        r = requests.get(
            f"{CG_BASE}/api/futures/global-long-short-account-ratio/history",
            headers=cg_headers(),
            params={"exchange": "Binance", "symbol": symbol_pair, "interval": interval, "limit": 1},
            timeout=15,
        )
        rows = r.json().get("data", [])
        if rows:
            row = rows[-1]
            ratio = row.get("global_account_long_short_ratio")
            if ratio not in (None, 0, "0"):
                try:
                    return float(ratio)
                except (TypeError, ValueError):
                    pass
            long_pct = float(row.get("global_account_long_percent", 0))
            short_pct = float(row.get("global_account_short_percent", 0))
            if short_pct > 0:
                return long_pct / short_pct
    except Exception:
        pass
    return None


def get_recent_liquidations(symbol_base, interval, exchange_list="Binance"):
    try:
        r = requests.get(
            f"{CG_BASE}/api/futures/liquidation/aggregated-history",
            headers=cg_headers(),
            params={
                "symbol": symbol_base,
                "interval": interval,
                "limit": 1,
                "exchange_list": exchange_list,
            },
            timeout=15,
        )
        rows = r.json().get("data", [])
        if rows:
            row = rows[-1]
            return (
                float(row.get("long_liquidation_usd", 0) or 0),
                float(row.get("short_liquidation_usd", 0) or 0),
            )
    except Exception:
        pass
    return None, None


def get_price_usd(symbol_pair):
    """Current price from Binance futures (more accurate for futures traders)."""
    try:
        r = requests.get(
            f"{BINANCE_FUT_BASE}/fapi/v1/ticker/price",
            params={"symbol": symbol_pair},
            timeout=10,
        )
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception:
        pass
    # fallback to spot
    try:
        r = requests.get(
            f"{BINANCE_SPOT_BASE}/api/v3/ticker/price",
            params={"symbol": symbol_pair},
            timeout=10,
        )
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception:
        pass
    return None


# ============ SIGNAL ENGINE ============
def window_index(interval):
    """Dedup window (different per interval)."""
    if interval == "15m":
        return int(time.time() // 900)  # per 15 min
    return int(time.time() // (4 * 3600))  # per 4h


def evaluate(fr, oi_chg, ls_ratio, long_liq, short_liq, mode):
    long_score = 0
    short_score = 0
    details = []

    if mode == "SWING":
        fr_hi, fr_lo = SWING_FR_HIGH, SWING_FR_LOW
        oi_up, oi_dn = SWING_OI_RISE, SWING_OI_DROP
    else:
        fr_hi, fr_lo = FAST_FR_HIGH, FAST_FR_LOW
        oi_up, oi_dn = FAST_OI_RISE, FAST_OI_DROP

    # 1. Funding
    if fr is not None:
        if fr >= fr_hi:
            short_score += 1
            details.append(f"[v] Funding {fr:+.4f}% -> SHORT")
        elif fr <= fr_lo:
            long_score += 1
            details.append(f"[v] Funding {fr:+.4f}% -> LONG")
        else:
            details.append(f"[-] Funding {fr:+.4f}% neutral")

    # 2. OI change
    if oi_chg is not None:
        if oi_chg >= oi_up:
            if fr is not None and fr >= fr_hi:
                short_score += 1
                details.append(f"[v] OI +{oi_chg:.2f}% confirms crowd")
            elif fr is not None and fr <= fr_lo:
                long_score += 1
                details.append(f"[v] OI +{oi_chg:.2f}% confirms crowd")
            else:
                details.append(f"[-] OI +{oi_chg:.2f}%")
        elif oi_chg <= oi_dn:
            long_score += 1
            details.append(f"[v] OI {oi_chg:+.2f}% cascade -> LONG")
        else:
            details.append(f"[-] OI {oi_chg:+.2f}% neutral")

    # 3. L/S ratio
    if ls_ratio is not None:
        if ls_ratio >= LS_RATIO_LONG_HEAVY:
            short_score += 1
            details.append(f"[v] L/S {ls_ratio:.2f} -> SHORT")
        elif ls_ratio <= LS_RATIO_SHORT_HEAVY:
            long_score += 1
            details.append(f"[v] L/S {ls_ratio:.2f} -> LONG")
        else:
            details.append(f"[-] L/S {ls_ratio:.2f} neutral")

    # 4. Liquidations
    if long_liq is not None and short_liq is not None:
        total = long_liq + short_liq
        if total > 0:
            if long_liq > short_liq * 2:
                long_score += 1
                details.append(f"[v] Longs wiped ${long_liq:,.0f} -> LONG")
            elif short_liq > long_liq * 2:
                short_score += 1
                details.append(f"[v] Shorts wiped ${short_liq:,.0f} -> SHORT")
            else:
                details.append(f"[-] Liquidations balanced")

    return long_score, short_score, details


def format_price(usd):
    inr = usd * _usd_to_inr
    if usd >= 1:
        u = f"${usd:,.4f}"
        i = f"Rs.{inr:,.2f}"
    elif usd >= 0.01:
        u = f"${usd:.6f}"
        i = f"Rs.{inr:.4f}"
    else:
        u = f"${usd:.8f}"
        i = f"Rs.{inr:.6f}"
    return f"{u}  /  {i}"


def format_alert(mode, direction, symbol, category, entry, sl, tp, confidence, details):
    sl_pct = ((sl - entry) / entry) * 100
    tp_pct = ((tp - entry) / entry) * 100
    rr = abs(tp_pct / sl_pct) if sl_pct != 0 else 0
    mode_tag = "SWING (4h)" if mode == "SWING" else "FAST (15m)"
    cat_tag = "STABLE" if category == "STABLE" else "TRENDING"
    details_text = "\n".join(details)

    return (
        f"<b>{direction} - {symbol}</b>  [{confidence}/4]\n"
        f"<i>{mode_tag} | {cat_tag}</i>\n"
        f"--------------------\n"
        f"Entry: {format_price(entry)}\n"
        f"SL:    {format_price(sl)}  ({sl_pct:+.2f}%)\n"
        f"TP:    {format_price(tp)}  ({tp_pct:+.2f}%)\n"
        f"R:R:   1 : {rr:.2f}\n"
        f"--------------------\n"
        f"{details_text}\n"
        f"--------------------\n"
        f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M')} IST\n"
        f"<i>Trade manually on CoinSwitch. Verify on chart first.</i>"
    )


def check_coin(base_symbol, category, mode):
    interval = "4h" if mode == "SWING" else "15m"
    pair = f"{base_symbol}USDT"

    fr = get_funding_rate(pair, interval)
    time.sleep(REQUEST_DELAY_SEC)
    oi_chg = get_oi_change(pair, interval)
    time.sleep(REQUEST_DELAY_SEC)
    ls_ratio = get_long_short_ratio(pair, interval)
    time.sleep(REQUEST_DELAY_SEC)
    long_liq, short_liq = get_recent_liquidations(base_symbol, interval)
    time.sleep(REQUEST_DELAY_SEC)

    long_s, short_s, details = evaluate(fr, oi_chg, ls_ratio, long_liq, short_liq, mode)

    direction = None
    confidence = 0
    if long_s >= MIN_CONFLUENCE and long_s > short_s:
        direction, confidence = "LONG", long_s
    elif short_s >= MIN_CONFLUENCE and short_s > long_s:
        direction, confidence = "SHORT", short_s

    fr_s = f"{fr:+.4f}" if fr is not None else "  N/A"
    oi_s = f"{oi_chg:+6.2f}" if oi_chg is not None else "  N/A"
    log(f"  [{mode[0]}] {pair:14s} L{long_s} S{short_s}  FR {fr_s}%  OI {oi_s}%")

    if not direction:
        return

    key = f"{pair}-{direction}-{mode}-{window_index(interval)}"
    if key in _alerted:
        return

    price = get_price_usd(pair)
    if not price:
        return

    sl_pct = SL_PCT_SWING if mode == "SWING" else SL_PCT_FAST
    tp_pct = TP_PCT_SWING if mode == "SWING" else TP_PCT_FAST

    if direction == "LONG":
        sl = price * (1 - sl_pct / 100)
        tp = price * (1 + tp_pct / 100)
    else:
        sl = price * (1 + sl_pct / 100)
        tp = price * (1 - tp_pct / 100)

    send_telegram(format_alert(mode, direction, pair, category, price, sl, tp, confidence, details))
    _alerted.add(key)
    log(f"  >>> ALERT {mode} {direction} {pair} [{confidence}/4]")


def run_scanner(coins, mode):
    log(f"=== {mode} scan start ({len(coins)} coins) ===")
    t0 = time.time()
    refresh_inr_rate()
    for i, (c, cat) in enumerate(coins, 1):
        try:
            check_coin(c, cat, mode)
        except Exception as e:
            log(f"  [ERR] {c}: {e}")
        if i % 10 == 0:
            log(f"  ...{i}/{len(coins)}")
    log(f"=== {mode} done in {int(time.time()-t0)}s ===\n")


def run():
    print("=" * 60)
    print("  CoinGlass MIXED Bot | SWING(4h) + FAST(15m) | ALERT ONLY")
    print("=" * 60)

    if not CG_KEY:
        print("!! COINGLASS_API_KEY missing in .env")
        return

    refresh_inr_rate()
    coins = build_coin_list()
    if not coins:
        print("!! could not build coin list - check API keys")
        return

    stable = [c for c, cat in coins if cat == "STABLE"]
    trending = [c for c, cat in coins if cat == "TRENDING"]
    log(f"Stable coins ({len(stable)}): {', '.join(stable[:15])}...")
    log(f"Trending coins ({len(trending)}): {', '.join(trending[:15])}...")

    send_telegram(
        f"<b>Mixed Bot Started</b>\n"
        f"Coins: {len(stable)} stable + {len(trending)} trending\n"
        f"SWING (4h): every {SWING_SCAN_EVERY_SECS//60} min\n"
        f"FAST (15m): every {FAST_SCAN_EVERY_SECS//60} min\n"
        f"Min confluence: {MIN_CONFLUENCE}/4\n"
        f"1 USD = Rs.{_usd_to_inr:.2f}\n"
        f"SL/TP:\n"
        f"- SWING: {SL_PCT_SWING}% / {TP_PCT_SWING}%\n"
        f"- FAST: {SL_PCT_FAST}% / {TP_PCT_FAST}%"
    )

    last_swing = 0
    last_fast = 0
    last_coin_refresh = time.time()

    while True:
        now = time.time()

        # refresh trending list every 30 min
        if now - last_coin_refresh > 1800:
            log("Refreshing trending coin list...")
            new_coins = build_coin_list()
            if new_coins:
                coins = new_coins
            last_coin_refresh = now

        if now - last_swing >= SWING_SCAN_EVERY_SECS:
            run_scanner(coins, "SWING")
            last_swing = time.time()

        if now - last_fast >= FAST_SCAN_EVERY_SECS:
            run_scanner(coins, "FAST")
            last_fast = time.time()

        if len(_alerted) > 5000:
            _alerted.clear()

        # sleep until next scan is due
        next_swing = last_swing + SWING_SCAN_EVERY_SECS
        next_fast = last_fast + FAST_SCAN_EVERY_SECS
        sleep_for = max(30, min(next_swing, next_fast) - time.time())
        log(f"Sleeping {int(sleep_for)}s...")
        time.sleep(sleep_for)


if __name__ == "__main__":
    # Start web server in background (for Render health checks + UptimeRobot)
    if os.getenv("RENDER") or os.getenv("PORT"):
        t = threading.Thread(target=start_web_server, daemon=True)
        t.start()
    run()
