#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║  DSS MARKET - SISTEM ANALISA SINYAL SWING INTERDAY      ║
║  Platform: Termux Android | Binance Futures             ║
║  Library: Hanya requests                                ║
║  Siklus: 45 menit (anti-drift)                          ║
║                                                        ║
║  VERSI: 3.2.0 (2026-06-12) — STRUCTURAL SAFETY PATCH    ║
║  • Cache integrity validation                           ║
║  • MACD alignment fix                                   ║
║  • RSI honest None fallback                             ║
║  • TP/SL early gate (no ghost signal)                   ║
║  • BTC context multi‑factor                             ║
║  • Gainers spike trap filter                            ║
║  • Lock recovery safe                                   ║
║  • Telegram error detail                                ║
╚══════════════════════════════════════════════════════════╝
"""

import requests, json, time, os, subprocess, threading
from datetime import datetime, timedelta

# ============================================================
# THREAD‑SAFE ANALYSIS LOCK
# ============================================================
ANALYSIS_LOCK = threading.Lock()
DEBUG = False

# ============================================================
# KONFIGURASI
# ============================================================
PAIR_TETAP = ["BTCUSDT","ETHUSDT","SOLUSDT","SUIUSDT","DOGEUSDT","UNIUSDT","ZECUSDT"]
TF_15M, TF_1H, TF_4H = "15m", "1h", "4h"
SIKLUS_DETIK = 45 * 60

TELEGRAM_BOT_TOKEN = "8440657002:AAEqJIJziZ37HVRKOd0e3TcXyEAb3PclrwQ"
TELEGRAM_FREE_ID = "-1004295086287"
TELEGRAM_VIP_ID = "-1003913950288"

MIN_TOTAL_SUPPORT = 4
MIN_VOLUME_USDT = 5_000_000
MAX_PAIR_ANALISA = 14

BTC_SIDEWAYS_RANGE = 3.0
BTC_BEARISH_THRESHOLD = -5.0
BTC_BULLISH_THRESHOLD = 3.0
MAX_FUNDING_RATE = 0.0005

TREND_WEIGHT = 2
MOMENTUM_WEIGHT = 2
VOLATILITY_WEIGHT = 1
OI_WEIGHT = 1

MAX_RETRIES = 3
RETRY_DELAY = 3
REQUEST_TIMEOUT = 15

SIGNAL_FILE = "signals.json"
WEB_FILE = "web.json"
SIGNAL_HISTORY_FILE = "signal_history.json"
TELEGRAM_FAILED_LOG = "telegram_failed.log"
MAX_HISTORY_ENTRIES = 1000
SESSION_REFRESH_INTERVAL = 10
SEND_DELAY = 0.3
CACHE_TTL = 300
GIT_REPO_PATH = os.path.expanduser("~/Dss_Web2")

OI_CACHE = {}
FUNDING_CACHE = {}

# ============================================================
# SAFE JSON & CACHE
# ============================================================
def safe_load_json(path, default=None):
    if default is None: default = {}
    try:
        if not os.path.exists(path): return default
        with open(path) as f:
            data = json.load(f)
            return data if data else default
    except: return default

def atomic_write_json(filepath, data):
    temp = filepath + ".tmp"
    try:
        with open(temp, "w") as f: json.dump(data, f, indent=2)
        os.replace(temp, filepath)
        return True
    except Exception as e:
        print(f"[ERROR] Gagal menulis {filepath}: {e}")
        return False

def is_cache_valid(cache, symbol):
    """Cache validity + data integrity check."""
    if symbol not in cache:
        return False
    ts, data = cache[symbol]
    # Time expiry
    if (time.time() - ts) >= CACHE_TTL:
        return False
    # Structure check
    if not isinstance(data, dict):
        return False
    # Numeric check for OI
    try:
        float(data.get("openInterest", 0))
    except (ValueError, TypeError):
        return False
    return True

def cleanup_cache(cache):
    now = time.time()
    expired = [k for k, v in cache.items() if now - v[0] > CACHE_TTL]
    for k in expired: cache.pop(k, None)

# ============================================================
# BINANCE FUTURES API (DENGAN VALIDASI NUMERIK)
# ============================================================
BASE_URL = "https://fapi.binance.com"
session = None

def get_session():
    global session
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Android; Termux)","Accept": "application/json"})
    return session

def refresh_session():
    global session
    if session:
        try: session.close()
        except: pass
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Android; Termux)","Accept": "application/json"})
    print("[SESSION] Refreshed")

def fetch_with_retry(url, params=None, max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT):
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = get_session().get(url, params=params, timeout=timeout)
            if resp.status_code == 200: return resp.json()
            elif resp.status_code == 429:
                wait = RETRY_DELAY * (attempt+1)*2
                print(f"[RETRY] Rate limit, menunggu {wait}s...")
                time.sleep(wait)
            else:
                print(f"[RETRY] HTTP {resp.status_code}, attempt {attempt+1}/{max_retries}")
                time.sleep(RETRY_DELAY)
        except requests.exceptions.Timeout:
            print(f"[RETRY] Timeout, attempt {attempt+1}/{max_retries}")
            time.sleep(RETRY_DELAY)
        except requests.exceptions.ConnectionError:
            print(f"[RETRY] Connection error, attempt {attempt+1}/{max_retries}")
            time.sleep(RETRY_DELAY*2)
        except Exception as e:
            last_error = e
            print(f"[RETRY] Error: {e}, attempt {attempt+1}/{max_retries}")
            time.sleep(RETRY_DELAY)
    print(f"[ERROR] Gagal setelah {max_retries} kali: {last_error}")
    return None

def fetch_klines(symbol, interval, limit=100):
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    result = fetch_with_retry(url, params)
    if not result:
        if DEBUG: print(f"[DEBUG] fetch_klines gagal {symbol} {interval}, retry 1x...")
        time.sleep(2)
        result = fetch_with_retry(url, params)
    return result

def fetch_24h_ticker():
    return fetch_with_retry(f"{BASE_URL}/fapi/v1/ticker/24hr")

def fetch_open_interest_cached(symbol):
    if is_cache_valid(OI_CACHE, symbol):
        _, data = OI_CACHE[symbol]
        return data
    url = f"{BASE_URL}/fapi/v1/openInterest"
    params = {"symbol": symbol}
    result = fetch_with_retry(url, params)
    if result and isinstance(result, dict):
        try:
            float(result.get("openInterest", 0))
            OI_CACHE[symbol] = (time.time(), result)
            return result
        except:
            return None
    return None

def fetch_funding_rate_cached(symbol):
    if is_cache_valid(FUNDING_CACHE, symbol):
        _, data = FUNDING_CACHE[symbol]
        return data
    url = f"{BASE_URL}/fapi/v1/fundingRate"
    params = {"symbol": symbol, "limit": 1}
    result = fetch_with_retry(url, params)
    if isinstance(result, list) and len(result) > 0:
        latest = result[-1]
        try:
            float(latest.get("fundingRate", 0))
            FUNDING_CACHE[symbol] = (time.time(), latest)
            return latest
        except:
            return None
    return None

# ============================================================
# PARSING DATA KLINES
# ============================================================
def parse_klines(klines_data):
    if not klines_data: return []
    candles = []
    for k in klines_data:
        try:
            candles.append({
                "open_time": k[0], "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                "close": float(k[4]), "volume": float(k[5]), "close_time": k[6],
                "quote_volume": float(k[7]), "trades": k[8], "taker_buy_base": float(k[9]),
                "taker_buy_quote": float(k[10])
            })
        except (IndexError, ValueError, TypeError): continue
    return candles

def calculate_sma(closes, period):
    if len(closes) < period: return None
    return sum(closes[-period:]) / period

def calculate_ema_last(closes, period):
    if len(closes) < period: return None
    multiplier = 2/(period+1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]: ema = (price - ema) * multiplier + ema
    return ema

def calculate_ema_series(closes, period):
    if len(closes) < period: return []
    multiplier = 2/(period+1)
    ema = sum(closes[:period]) / period
    result = []
    for price in closes:
        ema = (price - ema) * multiplier + ema
        result.append(ema)
    return result

def calculate_atr(candles, period=14):
    if len(candles) < period+1: return None
    tr_list = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr = max(high-low, abs(high-prev_close), abs(low-prev_close))
        tr_list.append(tr)
    if len(tr_list) < period: return None
    return sum(tr_list[-period:]) / period

def calculate_rsi(closes, period=14):
    """RSI – returns None if insufficient data (no masking)."""
    if len(closes) < period + 1:
        return None

    gains = 0
    losses = 0
    for i in range(-period, 0):
        diff = closes[i] - closes[i-1]
        gains += max(diff, 0)
        losses += abs(min(diff, 0))

    if losses == 0:
        return 100.0

    rs = gains / losses
    return 100 - (100 / (1 + rs))

def calculate_macd(closes):
    """Fixed MACD – aligned EMA series, zero‑padding safety."""
    if len(closes) < 50:
        return None, None, None

    def ema(series, period):
        multiplier = 2 / (period + 1)
        ema_val = series[0]
        out = []
        for price in series:
            ema_val = (price - ema_val) * multiplier + ema_val
            out.append(ema_val)
        return out

    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)

    macd_line = []
    for i in range(len(closes)):
        if i < 26:
            macd_line.append(0)
        else:
            macd_line.append(ema12[i] - ema26[i])

    signal_line = ema(macd_line, 9)

    if len(signal_line) < 2:
        return None, None, None

    macd = macd_line[-1]
    signal = signal_line[-1]
    hist = macd - signal

    return macd, signal, hist

def calculate_percent_change(candles, lookback=20):
    if len(candles) < lookback: return 0.0
    cur = candles[-1]["close"]
    past = candles[-lookback]["close"]
    if past == 0: return 0.0
    return ((cur-past)/past)*100

def calculate_volume_trend(candles):
    if len(candles) < 20: return "neutral"
    recent = sum(c["volume"] for c in candles[-5:])
    older = sum(c["volume"] for c in candles[-15:-5])
    if older == 0: return "neutral"
    ratio = recent/older
    return "increasing" if ratio>1.3 else "decreasing" if ratio<0.7 else "neutral"

def get_momentum_dir(momentum):
    if not momentum: return None
    if "bullish" in momentum: return "bullish"
    if "bearish" in momentum: return "bearish"
    return None

# ============================================================
# ENGINE ANALISA (UPGRADED BTC CONTEXT, GUARDS)
# ============================================================
def analyze_btc_context(candles_4h, candles_1h, candles_15m):
    """Multi‑factor BTC context."""
    if not candles_4h or len(candles_4h) < 30:
        return "sideways"

    closes = [c["close"] for c in candles_4h]
    change = calculate_percent_change(candles_4h, 6)
    rsi = calculate_rsi(closes, 14)
    sma20 = calculate_sma(closes, 20)
    sma50 = calculate_sma(closes, 50)

    trend_bias = 0
    if sma20 and sma50:
        trend_bias = 1 if sma20 > sma50 else -1

    score = 0
    score += 1 if change > 3 else -1 if change < -5 else 0
    if rsi is not None:
        score += 1 if rsi > 55 else -1 if rsi < 45 else 0
    score += trend_bias

    if score >= 2:
        return "baik"
    elif score <= -2:
        return "buruk"
    return "sideways"

def trend_engine(candles_4h):
    if not candles_4h or len(candles_4h)<50: return "neutral"
    closes = [c["close"] for c in candles_4h]
    sma20 = calculate_sma(closes, 20)
    sma50 = calculate_sma(closes, 50)
    if sma20 is None or sma50 is None: return "neutral"
    if sma20 > sma50: return "bullish"
    elif sma20 < sma50: return "bearish"
    return "neutral"

def momentum_engine(candles_1h):
    if not candles_1h or len(candles_1h)<35: return "netral"
    closes = [c["close"] for c in candles_1h]
    rsi = calculate_rsi(closes, 14)
    if rsi is None:
        return "netral"   # honest None → skip bias
    macd_res = calculate_macd(closes)
    macd_hist = macd_res[2] if macd_res else 0.0
    vol_trend = calculate_volume_trend(candles_1h)
    score = 0
    if rsi > 60: score+=1
    elif rsi < 40: score-=1
    if macd_hist > 0: score+=1
    elif macd_hist < 0: score-=1
    if vol_trend=="increasing": score+=1
    elif vol_trend=="decreasing": score-=1
    if score>=2: return "kuat_bullish"
    elif score<=-2: return "kuat_bearish"
    elif score==1: return "lemah_bullish"
    elif score==-1: return "lemah_bearish"
    return "netral"

def volatility_engine(candles_4h):
    if not candles_4h or len(candles_4h)<15: return "normal"
    atr = calculate_atr(candles_4h, 14)
    price = candles_4h[-1]["close"]
    if not atr or price==0: return "normal"
    pct = (atr/price)*100
    return "tinggi" if pct>5.0 else "rendah" if pct<1.5 else "normal"

def oi_funding_filter(symbol):
    oi = fetch_open_interest_cached(symbol)
    if not oi: return "tidak_valid", 0.0
    funding = fetch_funding_rate_cached(symbol)
    if funding is None: return "tidak_valid", 0.0
    try: rate = float(funding.get("fundingRate", 0))
    except: return "tidak_valid", 0.0
    return "valid", rate

def scoring_engine(trend_result, momentum_result, volatility_result, oi_result, btc_context="", funding_rate=0.0):
    if trend_result is None or momentum_result is None:
        return "NO_TRADE"

    trend_score = 1 if trend_result=="bullish" else -1 if trend_result=="bearish" else 0
    mom_dir = get_momentum_dir(momentum_result)
    momentum_score = 1 if mom_dir=="bullish" else -1 if mom_dir=="bearish" else 0

    volatility_score = 0
    if volatility_result == "normal": volatility_score = 1
    elif volatility_result == "tinggi": volatility_score = 0.5

    oi_score = 1 if oi_result=="valid" else 0

    funding_score = 0
    if abs(funding_rate) < MAX_FUNDING_RATE: funding_score = 1
    elif funding_rate > 0: funding_score = -0.5
    else: funding_score = 0.5

    btc_weight = 0
    if btc_context == "baik": btc_weight = 1
    elif btc_context == "buruk": btc_weight = -1

    total_score = (trend_score * TREND_WEIGHT +
                   momentum_score * MOMENTUM_WEIGHT +
                   volatility_score * VOLATILITY_WEIGHT +
                   oi_score * OI_WEIGHT +
                   funding_score +
                   btc_weight)

    if total_score is None: return "NO_TRADE"

    if total_score >= 4: return "LONG"
    elif total_score <= -4: return "SHORT"
    return "NO_TRADE"

def tp_sl_engine(symbol, signal, candles_15m):
    if signal == "NO_TRADE": return None
    if not candles_15m or len(candles_15m) < 20: return None

    current_price = candles_15m[-1]["close"]
    atr_15m = calculate_atr(candles_15m, 14)
    if not atr_15m: return None

    subset = candles_15m[-3:] if len(candles_15m)>=3 else candles_15m
    last_high = max(c["high"] for c in subset)
    last_low = min(c["low"] for c in subset)
    candle_range = last_high - last_low
    entry_penalty = False
    if signal=="LONG" and current_price > (last_high - candle_range*0.2):
        entry_penalty = True
    if signal=="SHORT" and current_price < (last_low + candle_range*0.2):
        entry_penalty = True

    window = candles_15m[-50:] if len(candles_15m)>=50 else candles_15m
    if len(window)==0: return None
    swing_high = max(c["high"] for c in window)
    swing_low = min(c["low"] for c in window)

    sl_multiplier = 1.5
    risk_mult = 1.3 if entry_penalty else 1.0
    atr_sl = atr_15m * sl_multiplier * risk_mult

    if signal=="LONG":
        entry = current_price
        stop_loss = round(min(entry - atr_sl, swing_low - atr_15m*0.3), 4)
        take_profit_1 = round(entry + atr_15m*1.5, 4)
        take_profit_2 = round(entry + atr_15m*3.0, 4)
    else:
        entry = current_price
        stop_loss = round(max(entry + atr_sl, swing_high + atr_15m*0.3), 4)
        take_profit_1 = round(entry - atr_15m*1.5, 4)
        take_profit_2 = round(entry - atr_15m*3.0, 4)

    risk = abs(entry - stop_loss)
    if risk <= 0 or risk < 1e-8:
        return None
    reward = abs(take_profit_1 - entry)
    rr = round(reward/risk, 2)
    return {"entry": entry, "stop_loss": stop_loss, "take_profit_1": take_profit_1,
            "take_profit_2": take_profit_2, "risk_reward": rr, "atr": round(atr_15m,4)}

# ============================================================
# ANALISA PER PAIR (DENGAN EARLY TP/SL GATE)
# ============================================================
def analyze_pair(symbol, btc_context):
    print(f"\n[ANALISA] {symbol}")
    c4 = parse_klines(fetch_klines(symbol, TF_4H, limit=100))
    c1 = parse_klines(fetch_klines(symbol, TF_1H, limit=100))
    c15 = parse_klines(fetch_klines(symbol, TF_15M, limit=100))
    if not c4 or not c1 or not c15:
        print(f"[SKIP] {symbol}: Data tidak lengkap"); return None
    vol = sum(c["quote_volume"] for c in c4[-24:]) if len(c4)>=24 else 0
    if vol < MIN_VOLUME_USDT and symbol!="BTCUSDT":
        print(f"[SKIP] {symbol}: Volume rendah (${vol:,.0f})"); return None

    # Early TP/SL feasibility check (avoid ghost signals)
    if tp_sl_engine(symbol, "LONG", c15) is None and tp_sl_engine(symbol, "SHORT", c15) is None:
        print(f"[SKIP] {symbol}: TP/SL invalid early filter"); return None

    trend_res = trend_engine(c4)
    print(f"  Trend (4H): {trend_res}")
    mom_res = momentum_engine(c1)
    print(f"  Momentum (1H): {mom_res}")
    vol_res = volatility_engine(c4)
    print(f"  Volatility (4H): {vol_res}")
    oi_res, funding_rate = oi_funding_filter(symbol)
    print(f"  OI+Funding: OI={oi_res}, funding_rate={funding_rate}")

    signal = scoring_engine(trend_res, mom_res, vol_res, oi_res, btc_context, funding_rate)
    print(f"  Scoring: {signal}")

    tp = tp_sl_engine(symbol, signal, c15)
    if signal=="NO_TRADE" or not tp:
        if not tp: print(f"[FILTERED] {symbol}: TP/SL invalid")
        return None

    return {
        "symbol": symbol, "signal": signal, "trend": trend_res, "momentum": mom_res,
        "volatility": vol_res, "entry": tp["entry"], "stop_loss": tp["stop_loss"],
        "take_profit_1": tp["take_profit_1"], "take_profit_2": tp["take_profit_2"],
        "risk_reward": tp["risk_reward"], "atr_15m": tp["atr"], "btc_context": btc_context
    }

def safe_analyze_pair(symbol, btc_context):
    try: return analyze_pair(symbol, btc_context)
    except Exception as e:
        print(f"[PAIR ERROR] {symbol}: {e}"); return None

# ============================================================
# PAIR TRENDING (SPIKE TRAP FILTER)
# ============================================================
def get_top_gainers(exclude_pairs, limit=7):
    tickers = fetch_24h_ticker()
    if not tickers: return []
    pairs = []
    for t in tickers:
        symbol = t.get("symbol","")
        if symbol.endswith("USDT") and symbol not in exclude_pairs:
            try:
                chg = float(t.get("priceChangePercent",0))
                vol = float(t.get("quoteVolume",0))
                # Volume basic
                if vol < MIN_VOLUME_USDT: continue
                # Spike trap filter
                if chg > 8 and vol < MIN_VOLUME_USDT * 3:
                    continue
                pairs.append({"symbol": symbol, "price_change": chg, "volume": vol})
            except: continue
    pairs.sort(key=lambda x: x["price_change"], reverse=True)
    top = pairs[:limit]
    print(f"\n[TOP GAINERS 24J]")
    for i,p in enumerate(top,1): print(f"  {i}. {p['symbol']}: {p['price_change']:.2f}% (Vol: ${p['volume']:,.0f})")
    return [p["symbol"] for p in top]

# ============================================================
# PRODUCER (LOCK SAFETY)
# ============================================================
def run_analysis_engine(cycle_count):
    acquired = ANALYSIS_LOCK.acquire(blocking=False)
    if not acquired:
        print("[GUARD] Analysis sedang berjalan, skip siklus ini")
        return
    try:
        print(f"\n{'='*60}")
        print(f"[SIKLUS #{cycle_count}] Mulai: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")
        if cycle_count % SESSION_REFRESH_INTERVAL == 0: refresh_session()
        cleanup_cache(OI_CACHE); cleanup_cache(FUNDING_CACHE)

        print("\n[LANGKAH 1] Mencari 7 pair trending...")
        trending = get_top_gainers(PAIR_TETAP, 7)
        all_pairs = list(dict.fromkeys(PAIR_TETAP + trending))[:MAX_PAIR_ANALISA]
        print(f"\n[LANGKAH 2] Total pair: {len(all_pairs)}")
        print(f"  Tetap: {PAIR_TETAP}"); print(f"  Trending: {trending}")

        print(f"\n[LANGKAH 3] Analisa BTC Context...")
        btc4 = parse_klines(fetch_klines("BTCUSDT", TF_4H, limit=100))
        btc1 = parse_klines(fetch_klines("BTCUSDT", TF_1H, limit=100))
        btc15 = parse_klines(fetch_klines("BTCUSDT", TF_15M, limit=100))
        btc_ctx = analyze_btc_context(btc4, btc1, btc15)
        print(f"  BTC Context: {btc_ctx}")

        print(f"\n[LANGKAH 4] Analisa {len(all_pairs)} pair...")
        signals = []
        for pair in all_pairs:
            res = safe_analyze_pair(pair, btc_ctx)
            if res: signals.append(res)

        print(f"\n[LANGKAH 5] Mengirim sinyal ke Telegram (DSS FORMAT)...")
        print(f"  Total sinyal valid: {len(signals)}")
        save_all_outputs(signals, btc_ctx)
        save_signal_history(signals, btc_ctx)
        vip_distribution(signals, btc_ctx)
        free_distribution(signals, btc_ctx)
        print(f"\n[SIKLUS #{cycle_count}] Selesai: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    finally:
        if ANALYSIS_LOCK.locked():
            ANALYSIS_LOCK.release()

# ============================================================
# DATA LAYER
# ============================================================
def save_all_outputs(signals, btc_context):
    if signals is None: signals = []
    out = {"btc_context": btc_context, "signal_count": len(signals),
           "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "signals": signals}
    atomic_write_json(SIGNAL_FILE, out)
    print(f"[OUTPUT] {SIGNAL_FILE} tersimpan ({len(signals)} sinyal)")
    pub = [{"symbol": s["symbol"], "signal": s["signal"], "trend": s["trend"],
            "momentum": s["momentum"], "volatility": s["volatility"], "btc_context": s["btc_context"]} for s in signals]
    web = {"btc_context": btc_context, "signal_count": len(signals),
           "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "signals": pub}
    atomic_write_json(WEB_FILE, web)
    print(f"[WEB] {WEB_FILE} tersimpan ({len(pub)} sinyal)")

def save_signal_history(signals, btc_context):
    if signals is None: signals = []
    entry = {"timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             "btc_context": btc_context, "signal_count": len(signals), "signals": signals}
    history = safe_load_json(SIGNAL_HISTORY_FILE, [])
    history.insert(0, entry)
    if len(history) > MAX_HISTORY_ENTRIES: history = history[:MAX_HISTORY_ENTRIES]
    atomic_write_json(SIGNAL_HISTORY_FILE, history)

# ============================================================
# TELEGRAM (ERROR DETAIL)
# ============================================================
def log_telegram_failed(chat_id, reason):
    try:
        with open(TELEGRAM_FAILED_LOG, "a") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CHAT {chat_id}: {reason}\n")
    except: pass

def send_to_telegram(chat_id, message, parse_mode="HTML"):
    if not message: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": parse_mode}
    for attempt in range(MAX_RETRIES):
        try:
            resp = get_session().post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                print(f"[TELEGRAM] Pesan terkirim"); return True
            elif resp.status_code == 400:
                if payload["parse_mode"] != "":
                    payload["parse_mode"] = ""
                    continue
                log_telegram_failed(chat_id, "HTTP 400 parse error"); return False
            elif resp.status_code == 404:
                log_telegram_failed(chat_id, "HTTP 404 token/chat salah"); return False
            else:
                print(f"[TELEGRAM] HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"[TELEGRAM ERROR DETAIL] {str(e)}")
            time.sleep(RETRY_DELAY)
    log_telegram_failed(chat_id, f"Gagal setelah {MAX_RETRIES} kali")
    return False

# ============================================================
# FORMAT SINYAL
# ============================================================
def escape_html(text):
    if text is None: return ""
    return str(text).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def format_bias_emoji(signal):
    if signal == "LONG": return "🟢"
    elif signal == "SHORT": return "🔴"
    return "⚪"

def format_trend_emoji(trend):
    if trend == "bullish": return "BULLISH 🐂"
    elif trend == "bearish": return "BEARISH 🐻"
    return "NEUTRAL ➡️"

def format_momentum_label(momentum):
    if "kuat" in momentum and "bullish" in momentum: return "MENGUAT ⬆️"
    elif "kuat" in momentum and "bearish" in momentum: return "MELEMAH ⬇️"
    elif "lemah" in momentum and "bullish" in momentum: return "LEMAH NAIK ↗️"
    elif "lemah" in momentum and "bearish" in momentum: return "LEMAH TURUN ↘️"
    return "NETRAL ➡️"

def format_btc_context_label(ctx):
    if ctx == "baik": return "BULLISH 🟢"
    elif ctx == "buruk": return "BEARISH 🔴"
    return "SIDEWAYS 📊"

def format_signal_free(signal_data):
    symbol = escape_html(signal_data["symbol"])
    signal = escape_html(signal_data["signal"])
    trend = escape_html(signal_data["trend"])
    momentum = escape_html(signal_data["momentum"])
    btc = escape_html(signal_data["btc_context"])
    emoji = format_bias_emoji(signal)
    trend_label = format_trend_emoji(trend)
    mom_label = format_momentum_label(momentum)
    btc_label = format_btc_context_label(btc)
    return f"""<b>🔥 DSS MARKET ALERT</b>

🆓 <i>VERSION FREE</i>

<b>🪙 PAIR</b>       : <code>{symbol}</code>
<b>🎯 BIAS</b>       : <b>{emoji} {signal}</b>
<b>📈 TREND</b>      : <b>{trend_label}</b>
<b>⚡ MOMENTUM</b>   : <b>{mom_label}</b>
<b>₿ BTC CONTEXT</b> : {btc_label}

✨ <i>Watch for setup!</i>

<b>🔐 FULL ENTRY & TP/SL:</b>
<blockquote>⚠️ <b>VIP CHANNEL ONLY</b> ⚠️</blockquote>

<b>🏷️ #DSS</b>  <b>#{symbol}</b>"""

def format_signal_vip(signal_data):
    symbol = escape_html(signal_data["symbol"])
    signal = escape_html(signal_data["signal"])
    entry = escape_html(signal_data["entry"])
    sl = escape_html(signal_data["stop_loss"])
    tp1 = escape_html(signal_data["take_profit_1"])
    tp2 = escape_html(signal_data["take_profit_2"])
    rr = escape_html(signal_data["risk_reward"])
    trend = escape_html(signal_data["trend"])
    momentum = escape_html(signal_data["momentum"])
    emoji = format_bias_emoji(signal)
    trend_label = format_trend_emoji(trend)
    mom_label = format_momentum_label(momentum)
    return f"""<b>🔥 DSS VIP SIGNAL</b>

💎 <i>FULL ACCESS</i>

<b>🪙 PAIR</b>       : <code>{symbol}</code>
<b>🎯 BIAS</b>       : <b>{emoji} {signal}</b>
<b>📈 TREND</b>      : <b>{trend_label}</b>
<b>⚡ MOMENTUM</b>   : <b>{mom_label}</b>
<b>💰 ENTRY</b>      : <code>{entry}</code>
<b>🛑 STOP LOSS</b>  : <code>{sl}</code>
<b>✅ TP1</b>         : <code>{tp1}</code>
<b>✅ TP2</b>         : <code>{tp2}</code>
<b>📊 RISK/REWARD</b> : <b>{rr}</b>

🏷️ <b>#DSS #VIP</b>  <b>#{symbol}</b>"""

def format_summary(signals, btc_context, channel="FREE"):
    btc_label = format_btc_context_label(btc_context)
    cnt = len(signals) if signals else 0
    header = "<b>📊 DSS VIP SESSION</b>" if channel=="VIP" else "<b>📊 DSS MARKET SESSION</b>"
    tag = "#DSS #VIP" if channel=="VIP" else "#DSS"
    if cnt == 0:
        return f"""{header}

⏰ <i>Tidak ada sinyal valid</i>
₿ BTC: {btc_label}
✅ <i>Sistem tetap berjalan normal</i>

🏷️ <b>{tag}</b>"""
    s = f"""{header}

₿ BTC: {btc_label}
📨 Sinyal: <b>{cnt}</b>

"""
    for sig in signals:
        emoji = format_bias_emoji(sig["signal"])
        symbol = escape_html(sig["symbol"])
        signal = escape_html(sig["signal"])
        s += f"{emoji} <b>{symbol}</b>: {signal}\n"
    if channel=="FREE": s += "\n🔐 <i>Full entry di VIP Channel</i>"
    s += f"\n🏷️ <b>{tag}</b>"
    return s

# ============================================================
# DISTRIBUTION
# ============================================================
def free_distribution(signals, btc_context):
    if signals is None: signals = []
    summary = format_summary(signals, btc_context, "FREE")
    send_to_telegram(TELEGRAM_FREE_ID, summary)
    if signals:
        for s in signals:
            send_to_telegram(TELEGRAM_FREE_ID, format_signal_free(s))
            time.sleep(SEND_DELAY)

def vip_distribution(signals, btc_context):
    if signals is None: signals = []
    summary = format_summary(signals, btc_context, "VIP")
    send_to_telegram(TELEGRAM_VIP_ID, summary)
    if signals:
        for s in signals:
            send_to_telegram(TELEGRAM_VIP_ID, format_signal_vip(s))
            time.sleep(SEND_DELAY)

# ============================================================
# GITHUB SYNC
# ============================================================
def github_sync():
    repo = GIT_REPO_PATH
    if not os.path.exists(os.path.join(repo, ".git")):
        print("[GIT] Repo tidak ditemukan"); return
    try:
        r = subprocess.run(["git","status","--porcelain"], cwd=repo, capture_output=True, text=True)
        if not r.stdout.strip(): return
        a = subprocess.run(["git","add","."], cwd=repo, capture_output=True, text=True)
        if a.returncode!=0: print(f"[GIT] Add gagal: {a.stderr.strip()}"); return
        c = subprocess.run(["git","commit","-m","auto update signal"], cwd=repo, capture_output=True, text=True)
        if c.returncode!=0: print(f"[GIT] Commit gagal: {c.stderr.strip()}"); return
        p = subprocess.run(["git","push"], cwd=repo, capture_output=True, text=True)
        if p.returncode!=0: print(f"[GIT] Push gagal: {p.stderr.strip()}")
        else: print("[GIT] SYNC OK")
    except Exception as e: print(f"[GIT ERROR] {e}")

# ============================================================
# MAIN LOOP
# ============================================================
def main():
    get_session()
    print("="*60)
    print("DSS MARKET - SISTEM ANALISA SINYAL SWING INTERDAY")
    print(f"Versi: 3.2.0 | Siklus: {SIKLUS_DETIK//60} menit")
    print(f"Cache integrity: ON | MACD alignment: FIXED")
    print(f"Spike trap filter: ON | Early TP/SL gate: ON")
    print("="*60)
    cycle = 0
    while True:
        cycle += 1
        start = time.time()
        run_analysis_engine(cycle)
        elapsed = time.time()-start
        if elapsed > 40*60: print("[ABORT] Cycle overload")
        else: github_sync()
        remaining = max(0, SIKLUS_DETIK - elapsed)
        next_time = datetime.now() + timedelta(seconds=remaining)
        print(f"\n[INFO] Durasi siklus: {elapsed:.0f}s")
        print(f"[INFO] Siklus #{cycle+1} berikutnya: {next_time.strftime('%H:%M:%S')}")
        if remaining > 0: time.sleep(remaining)
        else: print("[WARNING] Siklus melebihi 45 menit, langsung lanjut.")

if __name__ == "__main__":
    main()
