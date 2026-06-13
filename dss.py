#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║  DSS MARKET v8 — FINAL FINAL                            ║
║  Platform: Termux Android | Binance Futures             ║
║  Library: Hanya requests                                ║
║  Siklus: 45 menit (anti-drift)                          ║
║  Retention: 90 hari rolling                             ║
║                                                        ║
║  ARSITEKTUR:                                           ║
║  Layer 1: Config                                       ║
║  Layer 2: Data Fetcher                                 ║
║  Layer 3: Analysis Engines                             ║
║    - Structure (4H)                                    ║
║    - Trend (4H+1H)                                     ║
║    - Momentum (1H)                                     ║
║    - Liquidity (Swing+Sweep)                           ║
║    - Money Flow (OBV)                                  ║
║    - Squeeze (Bollinger)                               ║
║    - Open Interest (History+Klasifikasi)               ║
║    - Funding Rate (Sentiment)                          ║
║    - BTC Market Regime                                 ║
║  Layer 4: Scoring Engine                               ║
║  Layer 5: Risk Engine                                  ║
║  Layer 6: Output Engine                                ║
║                                                        ║
║  SCORING:                                              ║
║  Structure 22% | Trend 22% | Momentum 15%              ║
║  Liquidity 12% | OI 10% | Funding 5%                  ║
║  MoneyFlow 6% | Squeeze 3% | Volatility 5%            ║
║                                                        ║
║  RULES:                                                ║
║  • Structure != Trend → NO_TRADE                       ║
║  • LONG + BTC STRONG_BEAR → NO_TRADE                  ║
║  • SHORT + BTC STRONG_BULL → NO_TRADE                 ║
╚══════════════════════════════════════════════════════════╝
"""

import requests, json, time, os, subprocess, threading
from datetime import datetime, timedelta

# ============================================================
# LAYER 1: CONFIG
# ============================================================
ANALYSIS_LOCK = threading.Lock()

PAIR_TETAP = ["BTCUSDT","ETHUSDT","SOLUSDT","SUIUSDT","DOGEUSDT","UNIUSDT","ZECUSDT"]
TF_15M, TF_1H, TF_4H = "15m", "1h", "4h"
SIKLUS_DETIK = 45 * 60

TELEGRAM_BOT_TOKEN = "8440657002:AAEqJIJziZ37HVRKOd0e3TcXyEAb3PclrwQ"
TELEGRAM_FREE_ID = "-1004295086287"
TELEGRAM_VIP_ID = "-1003913950288"

MIN_VOLUME_USDT = 5_000_000
MAX_PAIR_ANALISA = 14

SCORE_THRESHOLD = 62
SWING_LOOKBACK = 10

MAX_RETRIES = 3
RETRY_DELAY = 3
REQUEST_TIMEOUT = 15

SIGNAL_FILE = "signals.json"
WEB_FILE = "web.json"
SIGNAL_HISTORY_FILE = "signal_history.json"
TELEGRAM_FAILED_LOG = "telegram_failed.log"
ROLLOVER_STATE_FILE = "rollover_state.json"
LAST_SIGNAL_FILE = "last_signal.json"
OI_HISTORY_FILE = "oi_history.json"
MAX_HISTORY_ENTRIES = 1000
RETENTION_DAYS = 90
SESSION_REFRESH_INTERVAL = 10
SEND_DELAY = 0.3
CACHE_TTL = 300
GIT_REPO_PATH = os.path.expanduser("~/Dss_Web2")

OI_CACHE = {}
FUNDING_CACHE = {}

# ============================================================
# LAYER 2: DATA FETCHER
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

def fetch_with_retry(url, params=None, max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT):
    for attempt in range(max_retries):
        try:
            resp = get_session().get(url, params=params, timeout=timeout)
            if resp.status_code == 200: return resp.json()
            elif resp.status_code == 429: time.sleep(RETRY_DELAY * (attempt+1)*2)
            else: time.sleep(RETRY_DELAY)
        except: time.sleep(RETRY_DELAY)
    return None

def fetch_klines(symbol, interval, limit=100):
    url = f"{BASE_URL}/fapi/v1/klines"
    result = fetch_with_retry(url, {"symbol": symbol, "interval": interval, "limit": limit})
    if not result:
        time.sleep(2)
        result = fetch_with_retry(url, {"symbol": symbol, "interval": interval, "limit": limit})
    return result

def fetch_24h_ticker():
    return fetch_with_retry(f"{BASE_URL}/fapi/v1/ticker/24hr")

def is_cache_valid(cache, symbol):
    if symbol not in cache: return False
    ts, data = cache[symbol]
    if (time.time() - ts) >= CACHE_TTL: return False
    if not isinstance(data, dict): return False
    try: float(data.get("openInterest", 0)); return True
    except: return False

def fetch_open_interest_cached(symbol):
    if is_cache_valid(OI_CACHE, symbol): return OI_CACHE[symbol][1]
    result = fetch_with_retry(f"{BASE_URL}/fapi/v1/openInterest", {"symbol": symbol})
    if result and isinstance(result, dict):
        try:
            float(result.get("openInterest", 0))
            OI_CACHE[symbol] = (time.time(), result)
            return result
        except: pass
    return None

def fetch_funding_rate_cached(symbol):
    if is_cache_valid(FUNDING_CACHE, symbol): return FUNDING_CACHE[symbol][1]
    result = fetch_with_retry(f"{BASE_URL}/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})
    if isinstance(result, list) and len(result) > 0:
        latest = result[-1]
        try:
            float(latest.get("fundingRate", 0))
            FUNDING_CACHE[symbol] = (time.time(), latest)
            return latest
        except: pass
    return None

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
        except: continue
    return candles

# ============================================================
# SHARED INDICATORS
# ============================================================
def calculate_sma(closes, period):
    if len(closes) < period: return None
    return sum(closes[-period:]) / period

def calculate_ema(closes, period):
    if len(closes) < period: return None
    multiplier = 2/(period+1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]: ema = (price - ema) * multiplier + ema
    return ema

def calculate_atr(candles, period=14):
    if len(candles) < period+1: return None
    tr_list = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        tr_list.append(max(h-l, abs(h-pc), abs(l-pc)))
    if len(tr_list) < period: return None
    return sum(tr_list[-period:]) / period

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains = 0; losses = 0
    for i in range(-period, 0):
        diff = closes[i] - closes[i-1]
        gains += max(diff, 0)
        losses += abs(min(diff, 0))
    if losses == 0: return 100.0
    return 100 - (100 / (1 + gains/losses))

def calculate_macd(closes):
    if len(closes) < 50: return None, None, None
    def ema(series, period):
        m = 2/(period+1); e = series[0]; out = []
        for p in series: e = (p-e)*m + e; out.append(e)
        return out
    e12 = ema(closes, 12); e26 = ema(closes, 26)
    macd = [0]*26 + [e12[i]-e26[i] for i in range(26, len(closes))]
    sig = ema(macd, 9)
    if len(sig) < 2: return None, None, None
    return macd[-1], sig[-1], macd[-1]-sig[-1]

def calculate_obv(closes, volumes):
    if len(closes) < 2 or len(volumes) < 2: return None
    obv = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]: obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i-1]: obv.append(obv[-1] - volumes[i])
        else: obv.append(obv[-1])
    return obv

def calculate_bollinger(closes, period=20, std_dev=2):
    if len(closes) < period: return None, None, None, None
    sma = sum(closes[-period:]) / period
    variance = sum((x - sma)**2 for x in closes[-period:]) / period
    std = variance**0.5
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    bandwidth = ((upper - lower) / sma) * 100 if sma > 0 else 0
    return sma, upper, lower, bandwidth

# ============================================================
# SAFE JSON & RETENTION
# ============================================================
def safe_load_json(path, default=None):
    if default is None: default = {}
    try:
        if not os.path.exists(path): return default
        with open(path) as f: return json.load(f) or default
    except: return default

def atomic_write_json(filepath, data):
    temp = filepath + ".tmp"
    try:
        with open(temp, "w") as f: json.dump(data, f, indent=2)
        os.replace(temp, filepath)
    except: pass

def apply_retention(filepath, days):
    if not os.path.exists(filepath): return
    try:
        if filepath.endswith(".json"):
            data = safe_load_json(filepath, [])
            if isinstance(data, list):
                cutoff = datetime.now() - timedelta(days=days)
                new_data = [e for e in data if datetime.strptime(e.get("timestamp","2000-01-01 00:00:00"), "%Y-%m-%d %H:%M:%S") > cutoff]
                if len(new_data) != len(data): atomic_write_json(filepath, new_data)
        elif filepath.endswith(".log"):
            cutoff = datetime.now() - timedelta(days=days)
            with open(filepath, "r") as f: lines = f.readlines()
            new_lines = [l for l in lines if datetime.strptime(l[1:20], "%Y-%m-%d %H:%M:%S") > cutoff]
            if len(new_lines) != len(lines):
                with open(filepath, "w") as f: f.writelines(new_lines)
    except: pass

def check_rollover():
    state = safe_load_json(ROLLOVER_STATE_FILE, {"start_date": datetime.now().strftime("%Y-%m-%d")})
    try:
        if (datetime.now() - datetime.strptime(state.get("start_date", ""), "%Y-%m-%d")).days >= RETENTION_DAYS:
            print("[ROLLOVER] 90 hari tercapai — reset history")
            for f in [SIGNAL_HISTORY_FILE, TELEGRAM_FAILED_LOG, OI_HISTORY_FILE]:
                if os.path.exists(f): atomic_write_json(f, [])
            state["start_date"] = datetime.now().strftime("%Y-%m-%d")
            atomic_write_json(ROLLOVER_STATE_FILE, state)
    except: pass

def is_duplicate_signal(symbol, signal):
    last = safe_load_json(LAST_SIGNAL_FILE, {})
    return last.get(f"{symbol}_{signal}") == signal

def update_last_signal(symbol, signal):
    last = safe_load_json(LAST_SIGNAL_FILE, {})
    last[f"{symbol}_{signal}"] = signal
    atomic_write_json(LAST_SIGNAL_FILE, last)

# ============================================================
# LAYER 3: ANALYSIS ENGINES
# ============================================================

# --- BTC MARKET REGIME ---
def btc_market_regime_engine(candles_4h, candles_1h):
    if not candles_4h or not candles_1h: return "BEAR"
    t4 = trend_engine(candles_4h)
    t1 = trend_engine(candles_1h)
    m = momentum_engine(candles_1h)
    score = 0
    if t4["direction"] == "bullish": score += 3
    elif t4["direction"] == "bearish": score -= 3
    if t1["direction"] == t4["direction"] and t1["direction"] != "netral": score += 2
    if m["direction"] == t4["direction"] and m["direction"] != "netral": score += 1
    if score >= 4: return "STRONG_BULL"
    elif score >= 2: return "BULL"
    elif score <= -4: return "STRONG_BEAR"
    elif score <= -2: return "BEAR"
    return "BEAR"

# --- STRUCTURE ENGINE ---
def structure_engine(candles_4h):
    if not candles_4h or len(candles_4h) < 30:
        return {"score": 50, "direction": "netral", "label": "DATA_KURANG",
                "swing_highs": [], "swing_lows": []}
    highs = [c["high"] for c in candles_4h]
    lows = [c["low"] for c in candles_4h]
    sh, sl = [], []
    for i in range(SWING_LOOKBACK, len(candles_4h)-SWING_LOOKBACK):
        if highs[i] == max(highs[i-SWING_LOOKBACK:i+SWING_LOOKBACK+1]): sh.append({"price": highs[i], "index": i})
        if lows[i] == min(lows[i-SWING_LOOKBACK:i+SWING_LOOKBACK+1]): sl.append({"price": lows[i], "index": i})
    if len(sh) < 2 or len(sl) < 2:
        return {"score": 50, "direction": "netral", "label": "SWING_KURANG",
                "swing_highs": sh, "swing_lows": sl}
    rsh, rsl = sh[-4:], sl[-4:]
    hh = sum(1 for i in range(1,len(rsh)) if rsh[i]["price"] > rsh[i-1]["price"])
    lh = sum(1 for i in range(1,len(rsh)) if rsh[i]["price"] < rsh[i-1]["price"])
    hl = sum(1 for i in range(1,len(rsl)) if rsl[i]["price"] > rsl[i-1]["price"])
    ll = sum(1 for i in range(1,len(rsl)) if rsl[i]["price"] < rsl[i-1]["price"])
    bull = hh + hl; bear = lh + ll
    if bull > bear:
        if hh >= 2 and hl >= 1: return {"score": 85, "direction": "bullish", "label": "HH-HL", "swing_highs": sh, "swing_lows": sl}
        elif hh >= 1: return {"score": 70, "direction": "bullish", "label": "HH", "swing_highs": sh, "swing_lows": sl}
        else: return {"score": 60, "direction": "bullish", "label": "HL", "swing_highs": sh, "swing_lows": sl}
    elif bear > bull:
        if ll >= 2 and lh >= 1: return {"score": 85, "direction": "bearish", "label": "LL-LH", "swing_highs": sh, "swing_lows": sl}
        elif ll >= 1: return {"score": 70, "direction": "bearish", "label": "LL", "swing_highs": sh, "swing_lows": sl}
        else: return {"score": 60, "direction": "bearish", "label": "LH", "swing_highs": sh, "swing_lows": sl}
    return {"score": 50, "direction": "netral", "label": "RANGE", "swing_highs": sh, "swing_lows": sl}

# --- TREND ENGINE ---
def trend_engine(candles_4h, candles_1h=None):
    if not candles_4h or len(candles_4h) < 50:
        return {"score": 50, "direction": "netral"}
    closes_4h = [c["close"] for c in candles_4h]
    e9 = calculate_ema(closes_4h, 9); e21 = calculate_ema(closes_4h, 21)
    if e9 is None or e21 is None: return {"score": 50, "direction": "netral"}
    cp = closes_4h[-1]
    if e9 > e21 and cp > e9: direction, base = "bullish", 70
    elif e9 > e21: direction, base = "bullish", 60
    elif e9 < e21 and cp < e9: direction, base = "bearish", 70
    elif e9 < e21: direction, base = "bearish", 60
    else: direction, base = "netral", 50
    diff_pct = abs((e9-e21)/e21)*100
    if diff_pct > 2.0: bonus = 20
    elif diff_pct > 1.0: bonus = 12
    elif diff_pct > 0.5: bonus = 6
    elif diff_pct > 0.2: bonus = 3
    else: bonus = 0
    conf = 0
    if candles_1h and len(candles_1h) >= 21:
        c1 = [c["close"] for c in candles_1h]
        e9_1 = calculate_ema(c1, 9); e21_1 = calculate_ema(c1, 21)
        if e9_1 and e21_1:
            if direction == "bullish" and e9_1 > e21_1: conf = 10
            elif direction == "bearish" and e9_1 < e21_1: conf = 10
            elif direction != "netral": conf = -5
    return {"score": max(5, min(98, base + bonus + conf)), "direction": direction}

# --- MOMENTUM ENGINE ---
def momentum_engine(candles_1h):
    if not candles_1h or len(candles_1h) < 50:
        return {"score": 50, "direction": "netral"}
    closes = [c["close"] for c in candles_1h]
    rsi = calculate_rsi(closes, 14)
    if rsi is None: return {"score": 50, "direction": "netral"}
    macd_line, signal_line, histogram = calculate_macd(closes)
    if macd_line is None: return {"score": 50, "direction": "netral"}
    if rsi >= 70: rsi_s = 85
    elif rsi >= 60: rsi_s = 70
    elif rsi >= 50: rsi_s = 55
    elif rsi >= 40: rsi_s = 45
    elif rsi >= 30: rsi_s = 30
    else: rsi_s = 15
    if macd_line > signal_line and histogram > 0: macd_s = 80
    elif macd_line > signal_line: macd_s = 65
    elif macd_line < signal_line and histogram < 0: macd_s = 20
    elif macd_line < signal_line: macd_s = 35
    else: macd_s = 50
    score = rsi_s*0.50 + macd_s*0.50
    direction = "bullish" if histogram > 0 else "bearish" if histogram < 0 else "netral"
    return {"score": round(max(5, min(98, score)), 1), "direction": direction}

# --- LIQUIDITY ENGINE (Swing + Sweep) ---
def liquidity_engine(structure_data, candles_4h):
    if not candles_4h or len(candles_4h) < 30:
        return {"score": 50, "sweep_type": "none", "liquidity_level": 0}
    sh = structure_data.get("swing_highs", [])
    sl = structure_data.get("swing_lows", [])
    if len(sh) < 2 or len(sl) < 2:
        return {"score": 50, "sweep_type": "none", "liquidity_level": 0}
    # Equal High / Equal Low
    eq_high = None; eq_low = None
    for i in range(len(sh)-1):
        if abs(sh[i]["price"] - sh[i+1]["price"]) / sh[i]["price"] < 0.005:
            eq_high = sh[i]["price"]
    for i in range(len(sl)-1):
        if abs(sl[i]["price"] - sl[i+1]["price"]) / sl[i]["price"] < 0.005:
            eq_low = sl[i]["price"]
    # Liquidity level
    if eq_high: liquidity_level = eq_high; side = "SELL_SIDE"
    elif eq_low: liquidity_level = eq_low; side = "BUY_SIDE"
    else:
        recent_high = max(s["price"] for s in sh[-3:])
        recent_low = min(s["price"] for s in sl[-3:])
        closes = [c["close"] for c in candles_4h]
        if closes[-1] > (recent_high+recent_low)/2:
            liquidity_level = recent_high; side = "SELL_SIDE"
        else:
            liquidity_level = recent_low; side = "BUY_SIDE"
    # Sweep detection
    closes = [c["close"] for c in candles_4h]
    highs = [c["high"] for c in candles_4h]
    lows = [c["low"] for c in candles_4h]
    sweep_type = "none"; score = 50
    for i in range(-10, 0):
        if side == "SELL_SIDE" and highs[i] > liquidity_level and closes[i] < liquidity_level:
            sweep_type = "SELL_SIDE_SWEEP"; score = 20
        if side == "BUY_SIDE" and lows[i] < liquidity_level and closes[i] > liquidity_level:
            sweep_type = "BUY_SIDE_SWEEP"; score = 85
    if sweep_type == "none":
        score = 65 if side == "BUY_SIDE" else 35
    return {"score": score, "sweep_type": sweep_type, "liquidity_level": round(liquidity_level, 4)}

# --- MONEY FLOW ENGINE ---
def money_flow_engine(candles_1h):
    if not candles_1h or len(candles_1h) < 30:
        return {"score": 50, "state": "netral"}
    closes = [c["close"] for c in candles_1h]
    volumes = [c["volume"] for c in candles_1h]
    obv = calculate_obv(closes, volumes)
    if obv is None or len(obv) < 20: return {"score": 50, "state": "netral"}
    obv_rising = obv[-1] > obv[-10]
    price_rising = closes[-1] > closes[-20] if len(closes) >= 20 else False
    if obv_rising and price_rising: score, state = 75, "accumulation"
    elif obv_rising and not price_rising: score, state = 65, "bullish_divergence"
    elif not obv_rising and not price_rising: score, state = 25, "distribution"
    elif not obv_rising and price_rising: score, state = 35, "bearish_divergence"
    else: score, state = 50, "netral"
    return {"score": max(5, min(98, score)), "state": state}

# --- SQUEEZE ENGINE ---
def squeeze_engine(candles_4h):
    if not candles_4h or len(candles_4h) < 30: return {"score": 50}
    closes = [c["close"] for c in candles_4h]
    sma, upper, lower, bandwidth = calculate_bollinger(closes, 20, 2)
    if sma is None: return {"score": 50}
    if len(closes) >= 30: _, _, _, bandwidth_prev = calculate_bollinger(closes[:-10], 20, 2)
    else: bandwidth_prev = bandwidth
    score = 50
    if bandwidth and bandwidth_prev:
        if bandwidth < 3: score += 20
        elif bandwidth < 5: score += 10
        if bandwidth > bandwidth_prev * 1.2: score += 15
        elif bandwidth < bandwidth_prev * 0.8: score += 5
    cp = closes[-1]
    if cp > upper: score += 10
    elif cp < lower: score -= 10
    elif cp > sma: score += 5
    else: score -= 5
    return {"score": max(5, min(98, score))}

# --- OPEN INTEREST ENGINE (History + Klasifikasi) ---
def open_interest_engine(symbol, current_price):
    oi = fetch_open_interest_cached(symbol)
    if not oi: return {"score": 50, "value": 0, "prev_value": 0, "change_pct": 0, "state": "NO_DATA"}
    try: current_oi = float(oi.get("openInterest", 0))
    except: return {"score": 50, "value": 0, "prev_value": 0, "change_pct": 0, "state": "NO_DATA"}
    history = safe_load_json(OI_HISTORY_FILE, {})
    pair_history = history.get(symbol, [])
    prev_oi = pair_history[-1]["oi"] if pair_history else current_oi
    prev_price = pair_history[-1]["price"] if pair_history else current_price
    oi_change_pct = ((current_oi - prev_oi) / prev_oi * 100) if prev_oi > 0 else 0
    price_change = current_price - prev_price
    if oi_change_pct > 0.5 and price_change > 0: state, score = "LONG_BUILDUP", 85
    elif oi_change_pct > 0.5 and price_change < 0: state, score = "SHORT_BUILDUP", 20
    elif oi_change_pct < -0.5 and price_change > 0: state, score = "SHORT_COVERING", 65
    elif oi_change_pct < -0.5 and price_change < 0: state, score = "LONG_LIQUIDATION", 35
    else: state, score = "STABLE", 50
    pair_history.append({"oi": current_oi, "price": current_price, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    if len(pair_history) > 50: pair_history = pair_history[-50:]
    history[symbol] = pair_history
    atomic_write_json(OI_HISTORY_FILE, history)
    return {"score": score, "value": current_oi, "prev_value": prev_oi, "change_pct": round(oi_change_pct, 2), "state": state}

# --- FUNDING RATE ENGINE ---
def funding_engine(symbol, signal=None):
    funding = fetch_funding_rate_cached(symbol)
    if not funding: return {"score": 50, "rate": 0, "state": "NO_DATA"}
    try: rate = float(funding.get("fundingRate", 0))
    except: return {"score": 50, "rate": 0, "state": "NO_DATA"}
    if signal == "LONG":
        if rate < 0: score, state = 80, "LONG_BIAS"
        elif rate < 0.0002: score, state = 65, "NEUTRAL"
        else: score, state = 40, "CROWDED_LONG"
    elif signal == "SHORT":
        if rate > 0: score, state = 80, "SHORT_BIAS"
        elif rate > -0.0002: score, state = 65, "NEUTRAL"
        else: score, state = 40, "CROWDED_SHORT"
    else:
        if rate < -0.0002: score, state = 70, "BULLISH"
        elif rate > 0.0002: score, state = 30, "BEARISH"
        else: score, state = 50, "NETRAL"
    return {"score": score, "rate": rate, "state": state}

# ============================================================
# LAYER 4: SCORING ENGINE
# ============================================================
def scoring_engine(structure_data, trend_data, momentum_data, liquidity_data, money_flow_data, squeeze_data, oi_data, funding_data, btc_regime, signal_direction=None):
    if structure_data is None or trend_data is None:
        return "NO_TRADE", {"total_score": 0}

    s_dir = structure_data.get("direction", "netral")
    t_dir = trend_data.get("direction", "netral")

    # RULE: Structure != Trend → NO_TRADE
    if s_dir != t_dir or s_dir == "netral":
        return "NO_TRADE", {"total_score": 0, "structure_dir": s_dir, "trend_dir": t_dir, "gate": "STRUCTURE_TREND_MISMATCH"}

    # RULE: LONG + BTC STRONG_BEAR → NO_TRADE
    if signal_direction == "LONG" and btc_regime == "STRONG_BEAR":
        return "NO_TRADE", {"total_score": 0, "gate": "BTC_STRONG_BEAR"}
    # RULE: SHORT + BTC STRONG_BULL → NO_TRADE
    if signal_direction == "SHORT" and btc_regime == "STRONG_BULL":
        return "NO_TRADE", {"total_score": 0, "gate": "BTC_STRONG_BULL"}

    s_score = structure_data.get("score", 50)
    t_score = trend_data.get("score", 50)
    m_score = momentum_data.get("score", 50)
    l_score = liquidity_data.get("score", 50)
    mf_score = money_flow_data.get("score", 50)
    sq_score = squeeze_data.get("score", 50)
    oi_score = oi_data.get("score", 50)
    f_score = funding_data.get("score", 50)

    # Volatility dari ATR
    v_score = 50  # default, akan di-overwrite dari luar

    total = (s_score*0.22 + t_score*0.22 + m_score*0.15 +
             l_score*0.12 + oi_score*0.10 + f_score*0.05 +
             mf_score*0.06 + sq_score*0.03 + v_score*0.05)

    if btc_regime == "STRONG_BULL": total += 5
    elif btc_regime == "BULL": total += 2
    elif btc_regime == "STRONG_BEAR": total -= 5
    elif btc_regime == "BEAR": total -= 2

    total = max(0, min(100, total))

    audit = {
        "total_score": round(total, 1),
        "structure_score": s_score, "trend_score": t_score,
        "momentum_score": m_score, "liquidity_score": l_score,
        "money_flow_score": mf_score, "squeeze_score": sq_score,
        "oi_score": oi_score, "funding_score": f_score,
        "volatility_score": v_score,
        "structure_dir": s_dir, "trend_dir": t_dir,
        "btc_regime": btc_regime,
        "gate": "PASS" if total >= 62 else ("BELOW_GATE" if total < 40 else "BELOW_THRESHOLD")
    }

    if total < 40: return "NO_TRADE", audit
    if total < 62: return "NO_TRADE", audit

    return s_dir.upper(), audit

# ============================================================
# LAYER 5: RISK ENGINE
# ============================================================
def risk_engine(symbol, signal, candles_15m, liquidity_level=0):
    if signal == "NO_TRADE": return None
    if not candles_15m or len(candles_15m) < 20: return None
    current_price = candles_15m[-1]["close"]
    atr_15m = calculate_atr(candles_15m, 14)
    if not atr_15m: return None
    subset = candles_15m[-3:] if len(candles_15m)>=3 else candles_15m
    last_high = max(c["high"] for c in subset)
    last_low = min(c["low"] for c in subset)
    candle_range = last_high - last_low
    entry_penalty = (signal=="LONG" and current_price > last_high - candle_range*0.2) or \
                    (signal=="SHORT" and current_price < last_low + candle_range*0.2)
    window = candles_15m[-50:] if len(candles_15m)>=50 else candles_15m
    if len(window)==0: return None
    swing_high = max(c["high"] for c in window)
    swing_low = min(c["low"] for c in window)
    risk_mult = 1.3 if entry_penalty else 1.0
    if signal == "LONG":
        entry = current_price
        stop_loss = round(min(entry - atr_15m*2*risk_mult, (liquidity_level if liquidity_level>0 and liquidity_level<entry else swing_low) - atr_15m*0.3), 4)
        take_profit_1 = round(max(entry + atr_15m*3, liquidity_level if liquidity_level>entry else 0), 4)
        take_profit_2 = round(take_profit_1 + (take_profit_1-entry)*0.5, 4)
    else:
        entry = current_price
        stop_loss = round(max(entry + atr_15m*2*risk_mult, (liquidity_level if liquidity_level>0 and liquidity_level>entry else swing_high) + atr_15m*0.3), 4)
        take_profit_1 = round(min(entry - atr_15m*3, liquidity_level if liquidity_level<entry else 0), 4)
        take_profit_2 = round(take_profit_1 - (entry-take_profit_1)*0.5, 4)
    risk = abs(entry - stop_loss)
    if risk <= 0 or risk < 1e-8: return None
    rr = round(abs(take_profit_1-entry)/risk, 2)
    if rr < 1.50:
        print(f"[FILTER] RR rendah ({rr}) untuk {symbol}")
        return None
    return {"entry": entry, "stop_loss": stop_loss, "take_profit_1": take_profit_1, "take_profit_2": take_profit_2, "risk_reward": rr}

# ============================================================
# ANALISA PER PAIR
# ============================================================
def analyze_pair(symbol, btc_regime):
    print(f"\n[ANALISA] {symbol}")
    c4 = parse_klines(fetch_klines(symbol, TF_4H, limit=100))
    c1 = parse_klines(fetch_klines(symbol, TF_1H, limit=100))
    c15 = parse_klines(fetch_klines(symbol, TF_15M, limit=100))
    if not c4 or not c1 or not c15:
        print(f"[SKIP] {symbol}: Data tidak lengkap"); return None
    vol = sum(c["quote_volume"] for c in c4[-24:]) if len(c4)>=24 else 0
    if vol < MIN_VOLUME_USDT and symbol!="BTCUSDT":
        print(f"[SKIP] {symbol}: Volume rendah (${vol:,.0f})"); return None

    current_price = c15[-1]["close"]

    # Engines
    struct = structure_engine(c4)
    trend = trend_engine(c4, c1)
    momentum = momentum_engine(c1)
    liquidity = liquidity_engine(struct, c4)
    moneyflow = money_flow_engine(c1)
    squeeze = squeeze_engine(c4)
    oi = open_interest_engine(symbol, current_price)

    s_dir = struct["direction"]; t_dir = trend["direction"]
    prelim_direction = s_dir.upper() if s_dir == t_dir and s_dir != "netral" else None
    funding = funding_engine(symbol, prelim_direction)

    # Volatility score
    atr_4h = calculate_atr(c4, 14)
    v_score = 50
    if atr_4h and c4[-1]["close"] > 0:
        atr_pct = (atr_4h / c4[-1]["close"]) * 100
        if atr_pct < 1.5: v_score = 40
        elif atr_pct > 5.0: v_score = 50
        else: v_score = 70

    # Audit log
    print(f"  Structure : {struct['score']} ({struct['direction']}) {struct['label']}")
    print(f"  Trend     : {trend['score']} ({trend['direction']})")
    print(f"  Momentum  : {momentum['score']} ({momentum['direction']})")
    print(f"  Liquidity : {liquidity['score']} (sweep={liquidity['sweep_type']}, level={liquidity['liquidity_level']})")
    print(f"  MoneyFlow : {moneyflow['score']} ({moneyflow['state']})")
    print(f"  Squeeze   : {squeeze['score']}")
    print(f"  OI        : {oi['value']:,.0f} (prev={oi['prev_value']:,.0f}, chg={oi['change_pct']}%, state={oi['state']}, score={oi['score']})")
    print(f"  Funding   : {funding['rate']} (state={funding['state']}, score={funding['score']})")
    print(f"  Volatility: {v_score}")
    print(f"  BTC Regime: {btc_regime}")

    # Scoring (pakai v_score dari atas)
    temp_audit = {}
    signal, audit = scoring_engine(struct, trend, momentum, liquidity, moneyflow, squeeze, oi, funding, btc_regime, prelim_direction)
    # Inject volatility score
    if "volatility_score" in audit: audit["volatility_score"] = v_score

    print(f"  Total Score: {audit.get('total_score', 0)} | Gate: {audit.get('gate', '?')}")
    print(f"  Decision   : {signal}")

    if signal == "NO_TRADE": return None

    tp = risk_engine(symbol, signal, c15, liquidity["liquidity_level"])
    if not tp:
        print(f"[FILTERED] {symbol}: TP/SL invalid")
        return None

    return {
        "symbol": symbol, "signal": signal,
        "entry": tp["entry"], "stop_loss": tp["stop_loss"],
        "take_profit_1": tp["take_profit_1"], "take_profit_2": tp["take_profit_2"],
        "risk_reward": tp["risk_reward"], "atr_15m": tp["atr"], "btc_regime": btc_regime,
        "audit": audit,
        "structure": struct, "trend": trend, "momentum": momentum,
        "liquidity": liquidity, "moneyflow": moneyflow, "squeeze": squeeze, "oi": oi, "funding": funding
    }

def safe_analyze_pair(symbol, btc_regime):
    try: return analyze_pair(symbol, btc_regime)
    except Exception as e:
        print(f"[PAIR ERROR] {symbol}: {e}"); return None

# ============================================================
# PAIR TRENDING
# ============================================================
def get_top_futures_pairs(exclude_pairs, limit=7):
    tickers = fetch_24h_ticker()
    if not tickers: return []
    pairs = []
    for t in tickers:
        symbol = t.get("symbol","")
        if symbol.endswith("USDT") and symbol not in exclude_pairs:
            try:
                vol = float(t.get("quoteVolume", 0))
                if vol < MIN_VOLUME_USDT * 2: continue
                chg = float(t.get("priceChangePercent", 0))
                if chg > 8 and vol < MIN_VOLUME_USDT * 3: continue
                pairs.append({"symbol": symbol, "volume": vol, "price_change": chg})
            except: continue
    pairs.sort(key=lambda x: x["volume"], reverse=True)
    top = pairs[:limit]
    print(f"\n[TOP FUTURES BY VOLUME]")
    for i,p in enumerate(top,1): print(f"  {i}. {p['symbol']}: ${p['volume']:,.0f} ({p['price_change']:.2f}%)")
    return [p["symbol"] for p in top]

# ============================================================
# PRODUCER
# ============================================================
def run_analysis_engine(cycle_count):
    acquired = ANALYSIS_LOCK.acquire(blocking=False)
    if not acquired:
        print("[GUARD] Analysis sedang berjalan, skip siklus ini"); return
    try:
        print(f"\n{'='*60}")
        print(f"[SIKLUS #{cycle_count}] Mulai: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")
        if cycle_count % SESSION_REFRESH_INTERVAL == 0: refresh_session()

        if cycle_count % 24 == 0:
            check_rollover()
            apply_retention(SIGNAL_HISTORY_FILE, RETENTION_DAYS)
            apply_retention(TELEGRAM_FAILED_LOG, RETENTION_DAYS)
            apply_retention(OI_HISTORY_FILE, RETENTION_DAYS)

        print("\n[LANGKAH 1] Mencari 7 pair trending...")
        trending = get_top_futures_pairs(PAIR_TETAP, 7)
        all_pairs = list(dict.fromkeys(PAIR_TETAP + trending))[:MAX_PAIR_ANALISA]
        print(f"\n[LANGKAH 2] Total pair: {len(all_pairs)}")

        print(f"\n[LANGKAH 3] Analisa BTC Regime...")
        btc4 = parse_klines(fetch_klines("BTCUSDT", TF_4H, limit=100))
        btc1 = parse_klines(fetch_klines("BTCUSDT", TF_1H, limit=100))
        btc_regime = btc_market_regime_engine(btc4, btc1)
        print(f"  BTC Regime: {btc_regime}")

        print(f"\n[LANGKAH 4] Analisa {len(all_pairs)} pair...")
        signals = []
        for pair in all_pairs:
            res = safe_analyze_pair(pair, btc_regime)
            if res: signals.append(res)

        print(f"\n[LANGKAH 5] Mengirim sinyal ke Telegram (DSS FORMAT)...")
        print(f"  Total sinyal valid: {len(signals)}")
        save_all_outputs(signals, btc_regime)
        save_signal_history(signals, btc_regime)
        vip_distribution(signals, btc_regime)
        free_distribution(signals, btc_regime)
        print(f"\n[SIKLUS #{cycle_count}] Selesai: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    finally:
        if ANALYSIS_LOCK.locked():
            ANALYSIS_LOCK.release()

# ============================================================
# LAYER 6: OUTPUT ENGINE
# ============================================================
def save_all_outputs(signals, btc_regime):
    if signals is None: signals = []
    out = {"btc_regime": btc_regime, "signal_count": len(signals),
           "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "signals": signals}
    atomic_write_json(SIGNAL_FILE, out)
    print(f"[OUTPUT] {SIGNAL_FILE} tersimpan ({len(signals)} sinyal)")
    pub = [{"symbol": s["symbol"], "signal": s["signal"],
            "entry": s["entry"], "stop_loss": s["stop_loss"],
            "take_profit_1": s["take_profit_1"], "take_profit_2": s["take_profit_2"],
            "risk_reward": s["risk_reward"], "btc_regime": s["btc_regime"]} for s in signals]
    web = {"btc_regime": btc_regime, "signal_count": len(signals),
           "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "signals": pub}
    atomic_write_json(WEB_FILE, web)
    print(f"[WEB] {WEB_FILE} tersimpan ({len(pub)} sinyal)")

def save_signal_history(signals, btc_regime):
    if signals is None: signals = []
    entry = {"timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             "btc_regime": btc_regime, "signal_count": len(signals), "signals": signals}
    history = safe_load_json(SIGNAL_HISTORY_FILE, [])
    history.insert(0, entry)
    if len(history) > MAX_HISTORY_ENTRIES: history = history[:MAX_HISTORY_ENTRIES]
    atomic_write_json(SIGNAL_HISTORY_FILE, history)

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
                    payload["parse_mode"] = ""; continue
                log_telegram_failed(chat_id, "HTTP 400 parse error"); return False
            elif resp.status_code == 404:
                log_telegram_failed(chat_id, "HTTP 404 token/chat salah"); return False
            else: time.sleep(RETRY_DELAY)
        except: time.sleep(RETRY_DELAY)
    log_telegram_failed(chat_id, f"Gagal setelah {MAX_RETRIES} kali")
    return False

def escape_html(text):
    if text is None: return ""
    return str(text).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def format_bias_emoji(signal):
    return "🟢" if signal=="LONG" else "🔴" if signal=="SHORT" else "⚪"

def format_signal_free(signal_data):
    symbol = escape_html(signal_data["symbol"]); signal = escape_html(signal_data["signal"])
    btc = escape_html(signal_data["btc_regime"]); emoji = format_bias_emoji(signal)
    return f"""<b>🔥 DSS MARKET ALERT</b>

🆓 <i>VERSION FREE</i>

<b>🪙 PAIR</b>       : <code>{symbol}</code>
<b>🎯 BIAS</b>       : <b>{emoji} {signal}</b>
<b>₿ BTC REGIME</b>  : {btc}

✨ <i>Watch for setup!</i>

<b>🔐 FULL ENTRY & TP/SL:</b>
<blockquote>⚠️ <b>VIP CHANNEL ONLY</b> ⚠️</blockquote>

<b>🏷️ #DSS</b>  <b>#{symbol}</b>"""

def format_signal_vip(signal_data):
    symbol = escape_html(signal_data["symbol"]); signal = escape_html(signal_data["signal"])
    entry = escape_html(signal_data["entry"]); sl = escape_html(signal_data["stop_loss"])
    tp1 = escape_html(signal_data["take_profit_1"]); tp2 = escape_html(signal_data["take_profit_2"])
    rr = escape_html(signal_data["risk_reward"]); btc = escape_html(signal_data["btc_regime"])
    emoji = format_bias_emoji(signal)
    return f"""<b>🔥 DSS VIP SIGNAL</b>

💎 <i>FULL ACCESS</i>

<b>🪙 PAIR</b>       : <code>{symbol}</code>
<b>🎯 BIAS</b>       : <b>{emoji} {signal}</b>
<b>₿ BTC REGIME</b>  : {btc}
<b>💰 ENTRY</b>      : <code>{entry}</code>
<b>🛑 STOP LOSS</b>  : <code>{sl}</code>
<b>✅ TP1</b>         : <code>{tp1}</code>
<b>✅ TP2</b>         : <code>{tp2}</code>
<b>📊 RISK/REWARD</b> : <b>{rr}</b>

🏷️ <b>#DSS #VIP</b>  <b>#{symbol}</b>"""

def format_summary(signals, btc_regime, channel="FREE"):
    cnt = len(signals) if signals else 0
    header = "<b>📊 DSS VIP SESSION</b>" if channel=="VIP" else "<b>📊 DSS MARKET SESSION</b>"
    tag = "#DSS #VIP" if channel=="VIP" else "#DSS"
    if cnt == 0:
        return f"""{header}

⏰ <i>Tidak ada sinyal valid</i>
₿ BTC: {btc_regime}
✅ <i>Sistem tetap berjalan normal</i>

🏷️ <b>{tag}</b>"""
    s = f"""{header}

₿ BTC Regime: {btc_regime}
📨 Sinyal: <b>{cnt}</b>

"""
    for sig in signals:
        emoji = format_bias_emoji(sig["signal"]); symbol = escape_html(sig["symbol"])
        signal = escape_html(sig["signal"])
        s += f"{emoji} <b>{symbol}</b>: {signal}\n"
    if channel=="FREE": s += "\n🔐 <i>Full entry di VIP Channel</i>"
    s += f"\n🏷️ <b>{tag}</b>"
    return s

def free_distribution(signals, btc_regime):
    if signals is None: signals = []
    summary = format_summary(signals, btc_regime, "FREE")
    send_to_telegram(TELEGRAM_FREE_ID, summary)
    if signals:
        for s in signals:
            if not is_duplicate_signal(s['symbol'], s['signal']):
                send_to_telegram(TELEGRAM_FREE_ID, format_signal_free(s))
                update_last_signal(s['symbol'], s['signal'])
                time.sleep(SEND_DELAY)

def vip_distribution(signals, btc_regime):
    if signals is None: signals = []
    summary = format_summary(signals, btc_regime, "VIP")
    send_to_telegram(TELEGRAM_VIP_ID, summary)
    if signals:
        for s in signals:
            if not is_duplicate_signal(s['symbol'], s['signal']):
                send_to_telegram(TELEGRAM_VIP_ID, format_signal_vip(s))
                update_last_signal(s['symbol'], s['signal'])
                time.sleep(SEND_DELAY)

def github_sync():
    repo = GIT_REPO_PATH
    if not os.path.exists(os.path.join(repo, ".git")):
        print("[GIT] Repo tidak ditemukan"); return
    try:
        r = subprocess.run(["git","status","--porcelain"], cwd=repo, capture_output=True, text=True)
        if not r.stdout.strip(): return
        subprocess.run(["git","add","."], cwd=repo, check=False)
        subprocess.run(["git","commit","-m","auto update signal"], cwd=repo, check=False)
        subprocess.run(["git","push"], cwd=repo, check=False)
        print("[GIT] SYNC OK")
    except: pass

# ============================================================
# MAIN LOOP
# ============================================================
def main():
    get_session()
    print("="*60)
    print("DSS MARKET v8 — FINAL")
    print(f"Siklus: {SIKLUS_DETIK//60} menit | Retention: {RETENTION_DAYS} hari")
    print(f"Scoring: 0-100 | Threshold: {SCORE_THRESHOLD}")
    print(f"Rules: Structure!=Trend→NO_TRADE | LONG+STRONG_BEAR→NO_TRADE | SHORT+STRONG_BULL→NO_TRADE")
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
