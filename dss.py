#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║  DSS MARKET v8 — FULL v7.5 CLONE (TF BESAR)            ║
║  Platform: Termux Android | Binance Futures             ║
║  Library: Hanya requests                                ║
║  Siklus: 45 menit (anti-drift)                          ║
║  Retention: 90 hari rolling                             ║
║                                                        ║
║  7 ENGINE: Structure + Trend + Momentum                 ║
║  + Volatility + Liquidity + Money Flow + Squeeze       ║
║  Scoring: Core 30/20/15 + Extras 35%                   ║
║  Alignment: +5/-8 | Gate: >=40 | Threshold: 62          ║
║  RR Filter: >= 1.10 | Git Lock: ON                     ║
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
SCORE_GATE = 40

MAX_RETRIES = 3
RETRY_DELAY = 3
REQUEST_TIMEOUT = 15

SIGNAL_FILE = "signals.json"
WEB_FILE = "web.json"
SIGNAL_HISTORY_FILE = "signal_history.json"
TELEGRAM_FAILED_LOG = "telegram_failed.log"
ROLLOVER_STATE_FILE = "rollover_state.json"
LAST_SIGNAL_FILE = "last_signal.json"
MAX_HISTORY_ENTRIES = 1000
RETENTION_DAYS = 90
SESSION_REFRESH_INTERVAL = 10
SEND_DELAY = 0.3
CACHE_TTL = 300
GIT_REPO_PATH = os.path.expanduser("~/Dss_Web2")

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

# ============================================================
# SAFE JSON & RETENTION
# ============================================================
def safe_load_json(path, default=None):
    if default is None: default = []
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
                new_data = []
                for e in data:
                    try:
                        dt = datetime.strptime(e.get("timestamp","2000-01-01 00:00:00"), "%Y-%m-%d %H:%M:%S")
                        if dt > cutoff: new_data.append(e)
                    except: continue
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
            for f in [SIGNAL_HISTORY_FILE, TELEGRAM_FAILED_LOG]:
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
# LAYER 3: 7 ANALYSIS ENGINES
# ============================================================

# --- STRUCTURE ENGINE ---
def structure_engine(candles_4h):
    if not candles_4h or len(candles_4h) < 20:
        return {"score": 50, "direction": "netral", "label": "DATA_KURANG"}
    highs = [c["high"] for c in candles_4h]
    lows = [c["low"] for c in candles_4h]
    sh, sl = [], []
    for i in range(3, len(candles_4h)-3):
        if highs[i] == max(highs[i-3:i+4]): sh.append(highs[i])
        if lows[i] == min(lows[i-3:i+4]): sl.append(lows[i])
    if len(sh) < 2 or len(sl) < 2:
        last = candles_4h[-1]
        prev = candles_4h[-10] if len(candles_4h) > 10 else candles_4h[0]
        direction = "bullish" if last["close"] > prev["close"] else "bearish"
        return {"score": 60, "direction": direction, "label": "FALLBACK_BIAS"}
    hh = sum(1 for i in range(1, len(sh)) if sh[i] > sh[i-1])
    ll = sum(1 for i in range(1, len(sl)) if sl[i] < sl[i-1])
    if hh > ll: return {"score": 75, "direction": "bullish", "label": "HH-BIAS"}
    elif ll > hh: return {"score": 75, "direction": "bearish", "label": "LL-BIAS"}
    return {"score": 65, "direction": "bullish", "label": "RANGE_BIAS"}

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

# --- VOLATILITY ENGINE ---
def volatility_engine(candles_4h):
    if not candles_4h or len(candles_4h) < 20:
        return {"score": 50, "state": "neutral"}
    atr = calculate_atr(candles_4h, 14)
    closes = [c["close"] for c in candles_4h]
    if atr is None: return {"score": 50, "state": "neutral"}
    avg_price = sum(closes[-20:]) / 20
    vol_pct = (atr / avg_price) * 100
    if vol_pct > 3: return {"score": 80, "state": "expansion"}
    elif vol_pct > 1.5: return {"score": 65, "state": "normal"}
    else: return {"score": 40, "state": "squeeze"}

# --- LIQUIDITY ENGINE ---
def liquidity_engine(candles_4h):
    if len(candles_4h) < 20: return {"score": 50, "state": "neutral"}
    highs = [c["high"] for c in candles_4h[-20:]]
    lows = [c["low"] for c in candles_4h[-20:]]
    recent_high = max(highs); recent_low = min(lows)
    last_close = candles_4h[-1]["close"]
    if last_close > recent_high * 0.99: return {"score": 80, "state": "sell_liquidity_sweep"}
    elif last_close < recent_low * 1.01: return {"score": 80, "state": "buy_liquidity_sweep"}
    return {"score": 60, "state": "neutral"}

# --- MONEY FLOW ENGINE ---
def money_flow_engine(candles_1h):
    if len(candles_1h) < 20: return {"score": 50, "state": "neutral"}
    buy_volume = sum(c["taker_buy_quote"] for c in candles_1h[-20:])
    total_volume = sum(c["quote_volume"] for c in candles_1h[-20:])
    if total_volume == 0: return {"score": 50, "state": "neutral"}
    ratio = buy_volume / total_volume
    if ratio > 0.6: return {"score": 80, "state": "accumulation"}
    elif ratio < 0.4: return {"score": 30, "state": "distribution"}
    return {"score": 55, "state": "balanced"}

# --- SQUEEZE ENGINE ---
def squeeze_engine(candles_4h):
    if len(candles_4h) < 20: return {"score": 50, "state": "neutral"}
    highs = [c["high"] for c in candles_4h[-20:]]
    lows = [c["low"] for c in candles_4h[-20:]]
    range_size = max(highs) - min(lows)
    avg_price = candles_4h[-1]["close"]
    compression = range_size / avg_price if avg_price > 0 else 0
    if compression < 0.01: return {"score": 80, "state": "squeeze_on"}
    elif compression < 0.03: return {"score": 60, "state": "pre_squeeze"}
    return {"score": 40, "state": "no_squeeze"}

# ============================================================
# LAYER 4: SCORING ENGINE
# ============================================================
def scoring_engine(structure_data, trend_data, momentum_data,
                   vol_data, liq_data, flow_data, squeeze_data):

    if structure_data is None or trend_data is None:
        return "NO_TRADE", {"total_score": 0}

    s = structure_data["score"]
    t = trend_data["score"]
    m = momentum_data["score"]
    v = vol_data["score"]
    l = liq_data["score"]
    f = flow_data["score"]
    q = squeeze_data["score"]

    core = s*0.30 + t*0.20 + m*0.15
    extras = (v + l + f + q) / 4 * 0.35

    total = core + extras

    s_dir = structure_data.get("direction", "netral")
    t_dir = trend_data.get("direction", "netral")
    m_dir = momentum_data.get("direction", "netral")

    if s_dir == t_dir and s_dir != "netral":
        total += 5
    elif s_dir != t_dir and s_dir != "netral" and t_dir != "netral":
        total -= 8

    if s_dir == m_dir and s_dir != "netral":
        total += 3
    elif s_dir != m_dir and s_dir != "netral" and m_dir != "netral":
        total -= 4

    total = max(0, min(100, total))

    audit = {
        "total_score": round(total, 1),
        "structure_score": s, "trend_score": t, "momentum_score": m,
        "volatility_score": v, "liquidity_score": l,
        "money_flow_score": f, "squeeze_score": q,
        "structure_dir": s_dir, "trend_dir": t_dir, "momentum_dir": m_dir,
        "gate": "PASS" if total >= SCORE_THRESHOLD else ("BELOW_GATE" if total < SCORE_GATE else "BELOW_THRESHOLD")
    }

    if total < SCORE_GATE:
        return "NO_TRADE", audit

    if total >= SCORE_THRESHOLD:
        direction = s_dir.upper()
        if direction == "NETRAL":
            direction = t_dir.upper()
        if direction != "NETRAL":
            return direction, audit

    return "NO_TRADE", audit

# ============================================================
# LAYER 5: RISK ENGINE
# ============================================================
def risk_engine(symbol, signal, candles_15m):
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
        stop_loss = round(min(entry - atr_15m*2*risk_mult, swing_low - atr_15m*0.3), 4)
        take_profit_1 = round(entry + atr_15m*3, 4)
        take_profit_2 = round(take_profit_1 + (take_profit_1-entry)*0.5, 4)
    else:
        entry = current_price
        stop_loss = round(max(entry + atr_15m*2*risk_mult, swing_high + atr_15m*0.3), 4)
        take_profit_1 = round(entry - atr_15m*3, 4)
        take_profit_2 = round(take_profit_1 - (entry-take_profit_1)*0.5, 4)
    risk = abs(entry - stop_loss)
    if risk <= 0 or risk < 1e-8: return None
    rr = round(abs(take_profit_1-entry)/risk, 2)
    if rr < 1.10:
        print(f"[FILTER] RR rendah ({rr}) untuk {symbol}")
        return None
    return {
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "risk_reward": rr,
        "atr_15m": atr_15m
    }

# ============================================================
# ANALISA PER PAIR
# ============================================================
def analyze_pair(symbol, btc_regime=""):
    print(f"\n[ANALISA] {symbol}")
    c4 = parse_klines(fetch_klines(symbol, TF_4H, limit=100))
    c1 = parse_klines(fetch_klines(symbol, TF_1H, limit=100))
    c15 = parse_klines(fetch_klines(symbol, TF_15M, limit=100))
    if not c4 or not c1 or not c15:
        print(f"[SKIP] {symbol}: Data tidak lengkap"); return None
    vol = sum(c["quote_volume"] for c in c4[-24:]) if len(c4)>=24 else 0
    if vol < MIN_VOLUME_USDT and symbol!="BTCUSDT":
        print(f"[SKIP] {symbol}: Volume rendah (${vol:,.0f})"); return None

    struct = structure_engine(c4)
    trend = trend_engine(c4, c1)
    momentum = momentum_engine(c1)
    vol_data = volatility_engine(c4)
    liq_data = liquidity_engine(c4)
    flow_data = money_flow_engine(c1)
    sqz_data = squeeze_engine(c4)

    print(f"  Structure : {struct['score']} ({struct['direction']}) {struct['label']}")
    print(f"  Trend     : {trend['score']} ({trend['direction']})")
    print(f"  Momentum  : {momentum['score']} ({momentum['direction']})")
    print(f"  Volatility: {vol_data['score']} ({vol_data['state']})")
    print(f"  Liquidity : {liq_data['score']} ({liq_data['state']})")
    print(f"  MoneyFlow : {flow_data['score']} ({flow_data['state']})")
    print(f"  Squeeze   : {sqz_data['score']} ({sqz_data['state']})")
    if btc_regime: print(f"  BTC       : {btc_regime}")

    signal, audit = scoring_engine(struct, trend, momentum, vol_data, liq_data, flow_data, sqz_data)

    if signal == "BULLISH": signal = "LONG"
    elif signal == "BEARISH": signal = "SHORT"

    print(f"  Total Score: {audit.get('total_score', 0)} | Gate: {audit.get('gate', '?')}")
    print(f"  Decision   : {signal}")

    if signal == "NO_TRADE": return None

    tp = risk_engine(symbol, signal, c15)
    if not tp:
        print(f"[FILTERED] {symbol}: TP/SL invalid")
        return None

    return {
        "symbol": symbol, "signal": signal,
        "entry": tp["entry"], "stop_loss": tp["stop_loss"],
        "take_profit_1": tp["take_profit_1"], "take_profit_2": tp["take_profit_2"],
        "risk_reward": tp["risk_reward"], "atr_15m": tp["atr_15m"], "btc_regime": btc_regime,
        "audit": audit,
        "structure": struct, "trend": trend, "momentum": momentum,
        "volatility": vol_data, "liquidity": liq_data,
        "money_flow": flow_data, "squeeze": sqz_data
    }

def safe_analyze_pair(symbol, btc_regime=""):
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

        print("\n[LANGKAH 1] Mencari 7 pair trending...")
        trending = get_top_futures_pairs(PAIR_TETAP, 7)
        all_pairs = list(dict.fromkeys(PAIR_TETAP + trending))[:MAX_PAIR_ANALISA]
        print(f"\n[LANGKAH 2] Total pair: {len(all_pairs)}")

        btc4 = parse_klines(fetch_klines("BTCUSDT", TF_4H, limit=100))
        btc1 = parse_klines(fetch_klines("BTCUSDT", TF_1H, limit=100))
        btc_struct = structure_engine(btc4)
        btc_trend = trend_engine(btc4, btc1)
        btc_regime = f"BTC: {btc_struct['direction']}/{btc_trend['direction']}"
        print(f"\n[LANGKAH 3] BTC Context: {btc_regime}")

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
    out = {"btc_context": btc_regime, "signal_count": len(signals),
           "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "signals": signals}
    atomic_write_json(SIGNAL_FILE, out)
    print(f"[OUTPUT] {SIGNAL_FILE} tersimpan ({len(signals)} sinyal)")
    pub = [{"symbol": s["symbol"], "signal": s["signal"],
            "entry": s["entry"], "stop_loss": s["stop_loss"],
            "take_profit_1": s["take_profit_1"], "take_profit_2": s["take_profit_2"],
            "risk_reward": s["risk_reward"]} for s in signals]
    web = {"btc_context": btc_regime, "signal_count": len(signals),
           "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), "signals": pub}
    atomic_write_json(WEB_FILE, web)
    print(f"[WEB] {WEB_FILE} tersimpan ({len(pub)} sinyal)")

def save_signal_history(signals, btc_regime):
    if signals is None: signals = []
    entry = {"timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             "btc_context": btc_regime, "signal_count": len(signals), "signals": signals}
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
    if not chat_id: return False
    if not message or not message.strip(): return False
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
    signal = signal_data["signal"]
    symbol = escape_html(signal_data["symbol"])
    emoji = format_bias_emoji(signal)
    return f"""<b>🔥 DSS MARKET ALERT</b>

🆓 <i>VERSION FREE</i>

<b>🪙 PAIR</b>       : <code>{symbol}</code>
<b>🎯 BIAS</b>       : <b>{emoji} {signal}</b>

✨ <i>Watch for setup!</i>

<b>🔐 FULL ENTRY & TP/SL:</b>
<blockquote>⚠️ <b>VIP CHANNEL ONLY</b> ⚠️</blockquote>

<b>🏷️ #DSS</b>  <b>#{symbol}</b>"""

def format_signal_vip(signal_data):
    signal = signal_data["signal"]
    symbol = escape_html(signal_data["symbol"])
    entry = escape_html(signal_data["entry"]); sl = escape_html(signal_data["stop_loss"])
    tp1 = escape_html(signal_data["take_profit_1"]); tp2 = escape_html(signal_data["take_profit_2"])
    rr = escape_html(signal_data["risk_reward"])
    emoji = format_bias_emoji(signal)
    return f"""<b>🔥 DSS VIP SIGNAL</b>

💎 <i>FULL ACCESS</i>

<b>🪙 PAIR</b>       : <code>{symbol}</code>
<b>🎯 BIAS</b>       : <b>{emoji} {signal}</b>
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
{btc_regime}
✅ <i>Sistem tetap berjalan normal</i>

🏷️ <b>{tag}</b>"""
    s = f"""{header}

{btc_regime}
📨 Sinyal: <b>{cnt}</b>

"""
    for sig in signals:
        signal = sig["signal"]
        emoji = format_bias_emoji(signal)
        symbol = escape_html(sig["symbol"])
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

# ============================================================
# GITHUB SYNC
# ============================================================
def github_sync():
    if os.path.exists(".git_lock"): return
    open(".git_lock", "w").close()
    try:
        repo = GIT_REPO_PATH
        if not os.path.exists(os.path.join(repo, ".git")):
            print("[GIT] Repo tidak ditemukan"); return
        r = subprocess.run(["git","status","--porcelain"], cwd=repo, capture_output=True, text=True)
        if not r.stdout.strip():
            if os.path.exists(".git_lock"): os.remove(".git_lock")
            return
        subprocess.run(["git","add","."], cwd=repo, check=False)
        subprocess.run(["git","commit","-m","auto update signal"], cwd=repo, check=False)
        subprocess.run(["git","push"], cwd=repo, check=False)
        print("[GIT] SYNC OK")
    except: pass
    if os.path.exists(".git_lock"): os.remove(".git_lock")

# ============================================================
# MAIN LOOP
# ============================================================
def main():
    get_session()
    print("="*60)
    print("DSS MARKET v8 — FULL v7.5 CLONE (TF BESAR)")
    print(f"Siklus: {SIKLUS_DETIK//60} menit | Retention: {RETENTION_DAYS} hari")
    print(f"7 Engines | Alignment: +5/-8 | Gate: >= {SCORE_GATE} | Threshold: {SCORE_THRESHOLD}")
    print(f"RR Filter: >= 1.10 | Git Lock: ON")
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
