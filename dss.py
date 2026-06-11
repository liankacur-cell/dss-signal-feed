#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║  DSS MARKET - SISTEM ANALISA SINYAL SWING INTERDAY      ║
║  Platform: Termux Android | Binance Futures             ║
║  Library: Hanya requests                                ║
║  Siklus: 45 menit (anti-drift)                          ║
║                                                        ║
║  VERSI: 2.3.0 (2026-06-12) — ENGINE OPTIMIZATION       ║
║  ARSITEKTUR: PRODUCER → signals.json → ROUTER          ║
║              ├── FREE Telegram (ringkasan + sinyal)     ║
║              ├── VIP Telegram (full entry/SL/TP)        ║
║              ├── web.json (data publik)                 ║
║              └── GitHub Sync (auto push)                ║
║                                                        ║
║  OPTIMIZATIONS (4):                                    ║
║  • Momentum safe structure (helper)                    ║
║  • BTC filter soft penalty (bukan hard block)          ║
║  • Swing window 50 candle (lebih stabil)               ║
║  • Scoring weighted confidence (bukan voting)          ║
╚══════════════════════════════════════════════════════════╝
"""

import requests
import json
import time
import os
import subprocess
import threading
from datetime import datetime, timedelta

# ============================================================
# THREAD-SAFE ANALYSIS LOCK
# ============================================================

ANALYSIS_LOCK = threading.Lock()

# ============================================================
# DEBUG FLAG
# ============================================================

DEBUG = False

# ============================================================
# KONFIGURASI
# ============================================================

# Pair Tetap (7)
PAIR_TETAP = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "SUIUSDT",
    "DOGEUSDT",
    "UNIUSDT",
    "ZECUSDT"
]

# Timeframe
TF_15M = "15m"
TF_1H = "1h"
TF_4H = "4h"

# Siklus (detik)
SIKLUS_DETIK = 45 * 60

# Telegram Routing Config
TELEGRAM_BOT_TOKEN = "8440657002:AAEqJIJziZ37HVRKOd0e3TcXyEAb3PclrwQ"
TELEGRAM_FREE_ID = "-1004295086287"
TELEGRAM_VIP_ID = "-1003913950288"

# Threshold
MIN_TOTAL_SUPPORT = 4
MIN_VOLUME_USDT = 5_000_000
MAX_PAIR_ANALISA = 14

# Kondisi BTC
BTC_SIDEWAYS_RANGE = 3.0
BTC_BEARISH_THRESHOLD = -5.0
BTC_BULLISH_THRESHOLD = 3.0

# Funding rate filter (RAW — tidak dikali 100)
MAX_FUNDING_RATE = 0.0005

# Scoring weights
TREND_WEIGHT = 2
MOMENTUM_WEIGHT = 2
VOLATILITY_WEIGHT = 1
OI_WEIGHT = 1

# Retry config
MAX_RETRIES = 3
RETRY_DELAY = 3
REQUEST_TIMEOUT = 15

# Output files
SIGNAL_FILE = "signals.json"
WEB_FILE = "web.json"
SIGNAL_HISTORY_FILE = "signal_history.json"
TELEGRAM_FAILED_LOG = "telegram_failed.log"

# History limit
MAX_HISTORY_ENTRIES = 1000

# Session refresh setiap N siklus
SESSION_REFRESH_INTERVAL = 10

# Telegram delay
SEND_DELAY = 0.3

# Cache TTL (detik)
CACHE_TTL = 300

# Git Repo Path
GIT_REPO_PATH = os.path.expanduser("~/Dss_Web2")

# Global Cache dengan TTL
OI_CACHE = {}
FUNDING_CACHE = {}

# ============================================================
# SAFE JSON LOADER
# ============================================================

def safe_load_json(path, default=None):
    """Safe JSON loader — anti corruption."""
    if default is None:
        default = {}
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r") as f:
            data = json.load(f)
            return data if data else default
    except:
        return default


def atomic_write_json(filepath, data):
    """Tulis JSON dengan atomic write (temp → rename)."""
    temp_file = filepath + ".tmp"
    try:
        with open(temp_file, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_file, filepath)
        return True
    except Exception as e:
        print(f"[ERROR] Gagal menulis {filepath}: {e}")
        return False


# ============================================================
# CACHE DENGAN TTL
# ============================================================

def is_cache_valid(cache, symbol):
    """Cek apakah cache masih valid berdasarkan TTL."""
    if symbol not in cache:
        return False
    ts, _ = cache[symbol]
    return (time.time() - ts) < CACHE_TTL


def cleanup_cache(cache):
    """Hapus entry cache yang sudah expired — race-safe."""
    now = time.time()
    expired = []

    for k, v in cache.items():
        if now - v[0] > CACHE_TTL:
            expired.append(k)

    for k in expired:
        cache.pop(k, None)


# ============================================================
# BINANCE FUTURES API
# ============================================================

BASE_URL = "https://fapi.binance.com"

session = None


def get_session():
    global session
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Android; Termux)",
            "Accept": "application/json"
        })
    return session


def refresh_session():
    global session
    if session:
        try:
            session.close()
        except:
            pass
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Android; Termux)",
        "Accept": "application/json"
    })
    print("[SESSION] Refreshed")


def fetch_with_retry(url, params=None, max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT):
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = get_session().get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                wait = RETRY_DELAY * (attempt + 1) * 2
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
            time.sleep(RETRY_DELAY * 2)
        except Exception as e:
            last_error = e
            print(f"[RETRY] Error: {e}, attempt {attempt+1}/{max_retries}")
            time.sleep(RETRY_DELAY)
    print(f"[ERROR] Gagal setelah {max_retries} kali: {last_error}")
    return None


def fetch_klines(symbol, interval, limit=100):
    """Fetch klines dengan fallback retry 1x."""
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    result = fetch_with_retry(url, params=params)
    if not result:
        if DEBUG:
            print(f"[DEBUG] fetch_klines gagal untuk {symbol} {interval}, retry 1x...")
        time.sleep(2)
        result = fetch_with_retry(url, params=params)
    return result


def fetch_24h_ticker():
    url = f"{BASE_URL}/fapi/v1/ticker/24hr"
    return fetch_with_retry(url)


def fetch_open_interest_cached(symbol):
    if is_cache_valid(OI_CACHE, symbol):
        _, data = OI_CACHE[symbol]
        return data
    url = f"{BASE_URL}/fapi/v1/openInterest"
    params = {"symbol": symbol}
    result = fetch_with_retry(url, params=params)
    if result:
        OI_CACHE[symbol] = (time.time(), result)
    return result


def fetch_funding_rate_cached(symbol):
    if is_cache_valid(FUNDING_CACHE, symbol):
        _, data = FUNDING_CACHE[symbol]
        return data

    url = f"{BASE_URL}/fapi/v1/fundingRate"
    params = {"symbol": symbol, "limit": 1}

    result = fetch_with_retry(url, params=params)

    try:
        if isinstance(result, list) and len(result) > 0:
            latest = result[-1]
            FUNDING_CACHE[symbol] = (time.time(), latest)
            return latest
    except:
        return None

    return None


# ============================================================
# PARSING DATA KLINES
# ============================================================

def parse_klines(klines_data):
    """Safe parsing — tahan terhadap IndexError."""
    if not klines_data:
        return []
    candles = []
    for k in klines_data:
        try:
            candles.append({
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": k[6],
                "quote_volume": float(k[7]),
                "trades": k[8],
                "taker_buy_base": float(k[9]),
                "taker_buy_quote": float(k[10])
            })
        except (IndexError, ValueError, TypeError):
            continue
    return candles


def calculate_sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calculate_ema(closes, period):
    if len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calculate_ema_series(closes, period):
    """EMA series — aligned calculation."""
    if len(closes) < period:
        return []
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    result = []

    for price in closes:
        ema = (price - ema) * multiplier + ema
        result.append(ema)

    return result


def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    tr_list = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    if len(tr_list) < period:
        return None
    return sum(tr_list[-period:]) / period


def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    if len(gains) < period or len(losses) < period:
        return None
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_macd(closes):
    """MACD — validated EMA series + signal series safety."""
    if len(closes) < 35:
        return None
    ema_12_series = calculate_ema_series(closes, 12)
    ema_26_series = calculate_ema_series(closes, 26)
    if len(ema_12_series) < 26 or len(ema_26_series) < 26:
        return None
    min_len = min(len(ema_12_series), len(ema_26_series))
    macd_line_series = [
        ema_12_series[i] - ema_26_series[i]
        for i in range(min_len)
    ]
    if len(macd_line_series) < 9:
        return None
    signal_line_series = calculate_ema_series(macd_line_series, 9)
    if len(signal_line_series) == 0:
        return None
    macd_line = macd_line_series[-1]
    signal_line = signal_line_series[-1]
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_percent_change(candles, lookback=20):
    if len(candles) < lookback:
        return 0.0
    current_close = candles[-1]["close"]
    past_close = candles[-lookback]["close"]
    if past_close == 0:
        return 0.0
    return ((current_close - past_close) / past_close) * 100


def calculate_volume_trend(candles):
    if len(candles) < 20:
        return "neutral"
    recent_vol = sum(c["volume"] for c in candles[-5:])
    older_vol = sum(c["volume"] for c in candles[-15:-5])
    if older_vol == 0:
        return "neutral"
    ratio = recent_vol / older_vol
    if ratio > 1.3:
        return "increasing"
    elif ratio < 0.7:
        return "decreasing"
    else:
        return "neutral"


# ============================================================
# HELPER: MOMENTUM DIRECTION (SAFE STRUCTURE)
# ============================================================

def get_momentum_dir(momentum):
    """Safe momentum direction parser — hindari string bias."""
    if not momentum:
        return None
    if "bullish" in momentum:
        return "bullish"
    if "bearish" in momentum:
        return "bearish"
    return None


# ============================================================
# ENGINE ANALISA
# ============================================================

def analyze_btc_context(candles_4h, candles_1h, candles_15m):
    if not candles_4h or len(candles_4h) < 24:
        return "sideways"
    change_4h = calculate_percent_change(candles_4h, lookback=6)
    closes_4h = [c["close"] for c in candles_4h]
    rsi_4h = calculate_rsi(closes_4h, 14)
    if change_4h < BTC_BEARISH_THRESHOLD:
        return "buruk"
    elif change_4h > BTC_BULLISH_THRESHOLD:
        return "baik"
    elif abs(change_4h) <= BTC_SIDEWAYS_RANGE:
        if rsi_4h and 40 <= rsi_4h <= 60:
            return "sideways"
        else:
            return "baik" if change_4h > 0 else "buruk"
    else:
        return "baik" if change_4h > 0 else "buruk"


def trend_engine(candles_4h, candles_1h, candles_15m):
    score = 0
    closes_4h = [c["close"] for c in candles_4h] if candles_4h else []
    sma_20_4h = calculate_sma(closes_4h, 20) if len(closes_4h) >= 20 else None
    sma_50_4h = calculate_sma(closes_4h, 50) if len(closes_4h) >= 50 else None
    if sma_20_4h and sma_50_4h:
        if sma_20_4h > sma_50_4h:
            score += 2
        elif sma_20_4h < sma_50_4h:
            score -= 2
    closes_1h = [c["close"] for c in candles_1h] if candles_1h else []
    sma_20_1h = calculate_sma(closes_1h, 20) if len(closes_1h) >= 20 else None
    current_price_1h = closes_1h[-1] if closes_1h else None
    if sma_20_1h and current_price_1h:
        if current_price_1h > sma_20_1h:
            score += 1
        else:
            score -= 1
    change_15m = calculate_percent_change(candles_15m, lookback=10) if candles_15m else 0
    if change_15m > 0.5:
        score += 1
    elif change_15m < -0.5:
        score -= 1
    if score >= 2:
        return "bullish"
    elif score <= -2:
        return "bearish"
    else:
        return "neutral"


def momentum_engine(candles_4h, candles_1h, candles_15m):
    score = 0
    closes_1h = [c["close"] for c in candles_1h] if candles_1h else []
    rsi_1h = calculate_rsi(closes_1h, 14) if len(closes_1h) >= 15 else None
    if rsi_1h:
        if rsi_1h > 60:
            score += 1
        elif rsi_1h < 40:
            score -= 1
    macd_result = calculate_macd(closes_1h) if len(closes_1h) >= 35 else None
    if macd_result:
        _, _, histogram = macd_result
    else:
        histogram = None
    if histogram is not None:
        if histogram > 0:
            score += 1
        elif histogram < 0:
            score -= 1
    vol_trend = calculate_volume_trend(candles_15m) if candles_15m else "neutral"
    if vol_trend == "increasing":
        score += 1
    elif vol_trend == "decreasing":
        score -= 1
    if score >= 2:
        return "kuat_bullish"
    elif score <= -2:
        return "kuat_bearish"
    elif score == 1:
        return "lemah_bullish"
    elif score == -1:
        return "lemah_bearish"
    else:
        return "netral"


def volatility_engine(candles_4h, candles_1h, candles_15m):
    atr_4h = calculate_atr(candles_4h, 14) if candles_4h and len(candles_4h) >= 15 else None
    current_price = candles_4h[-1]["close"] if candles_4h else 0
    if not atr_4h or current_price == 0:
        return "normal"
    atr_percent = (atr_4h / current_price) * 100
    if atr_percent > 5.0:
        return "tinggi"
    elif atr_percent < 1.5:
        return "rendah"
    else:
        return "normal"


def oi_funding_filter(symbol):
    oi_data = fetch_open_interest_cached(symbol)
    if not oi_data:
        return "tidak_valid"
    funding_data = fetch_funding_rate_cached(symbol)
    if not funding_data:
        return "tidak_valid"
    try:
        funding_rate = float(funding_data.get("fundingRate", 0))
    except (ValueError, TypeError):
        return "tidak_valid"
    if abs(funding_rate) > MAX_FUNDING_RATE:
        return "tidak_valid"
    return "valid"


def scoring_engine(trend_result, momentum_result, volatility_result, oi_result, btc_context=""):
    if trend_result is None or momentum_result is None:
        return "NO_TRADE"
    
    trend_bullish = trend_result == "bullish"
    trend_bearish = trend_result == "bearish"
    
    mom_dir = get_momentum_dir(momentum_result)
    
    volatility_ok = volatility_result in ["normal", "tinggi"]
    oi_ok = oi_result == "valid"
    
    # BTC soft penalty / bonus
    btc_penalty = (btc_context == "buruk")
    btc_bonus = (btc_context == "baik")
    
    # Weighted scoring
    trend_score = 1 if trend_bullish else -1 if trend_bearish else 0
    momentum_score = 1 if mom_dir == "bullish" else -1 if mom_dir == "bearish" else 0
    
    directional_bullish = 0
    directional_bearish = 0
    
    directional_bullish += max(0, trend_score) * TREND_WEIGHT
    directional_bearish += max(0, -trend_score) * TREND_WEIGHT
    
    directional_bullish += max(0, momentum_score) * MOMENTUM_WEIGHT
    directional_bearish += max(0, -momentum_score) * MOMENTUM_WEIGHT
    
    if volatility_ok:
        directional_bullish += VOLATILITY_WEIGHT
        directional_bearish += VOLATILITY_WEIGHT
    
    if oi_ok:
        directional_bullish += OI_WEIGHT
        directional_bearish += OI_WEIGHT
    
    # BTC influence
    if btc_penalty:
        directional_bullish -= 1
        directional_bearish -= 1
    
    if btc_bonus:
        directional_bullish += 1
        directional_bearish += 1
    
    if directional_bullish >= MIN_TOTAL_SUPPORT:
        return "LONG"
    if directional_bearish >= MIN_TOTAL_SUPPORT:
        return "SHORT"
    
    return "NO_TRADE"


def tp_sl_engine(symbol, signal, candles_4h, candles_1h, candles_15m):
    if signal == "NO_TRADE":
        return None
    
    if not candles_4h or not candles_1h or not candles_15m:
        return None
    
    if len(candles_1h) < 20:
        return None
    if len(candles_15m) < 3:
        return None
    
    current_price = candles_15m[-1]["close"]
    
    atr_4h = calculate_atr(candles_4h, 14)
    atr_1h = calculate_atr(candles_1h, 14)
    
    if not atr_4h or not atr_1h:
        return None
    
    # === 1. ENTRY QUALITY FILTER (ANTI SPIKE ENTRY — null-safe) ===
    subset = candles_15m[-3:] if len(candles_15m) >= 3 else candles_15m
    
    last_high = max(c["high"] for c in subset)
    last_low = min(c["low"] for c in subset)
    candle_range = last_high - last_low
    
    if signal == "LONG" and current_price > (last_high - candle_range * 0.2):
        return None
    
    if signal == "SHORT" and current_price < (last_low + candle_range * 0.2):
        return None
    
    # === 2. MOMENTUM CONTINUATION CHECK ===
    closes_1h = [c["close"] for c in candles_1h]
    
    if len(closes_1h) < 35:
        return None
    
    momentum_strength = 0
    
    rsi = calculate_rsi(closes_1h, 14)
    volume_trend = calculate_volume_trend(candles_15m)
    macd_result = calculate_macd(closes_1h)
    macd_hist = macd_result[2] if macd_result else None
    
    if signal == "LONG":
        if rsi and rsi > 55:
            momentum_strength += 1
        if volume_trend == "increasing":
            momentum_strength += 1
        if macd_hist and macd_hist > 0:
            momentum_strength += 1
    elif signal == "SHORT":
        if rsi and rsi < 45:
            momentum_strength += 1
        if volume_trend == "increasing":
            momentum_strength += 1
        if macd_hist and macd_hist < 0:
            momentum_strength += 1
    
    if momentum_strength == 0:
        momentum_strength = 1
    
    sl_multiplier = 1.5
    
    # === 3. HYBRID STOP LOSS (ATR + SWING PROTECTION — 50 candle window) ===
    window = candles_1h[-50:] if len(candles_1h) >= 50 else candles_1h
    
    swing_high = max(c["high"] for c in window)
    swing_low = min(c["low"] for c in window)
    atr_sl = atr_1h * sl_multiplier
    
    if signal == "LONG":
        entry = current_price
        stop_loss = round(min(entry - atr_sl, swing_low - (atr_1h * 0.3)), 4)
    else:
        entry = current_price
        stop_loss = round(max(entry + atr_sl, swing_high + (atr_1h * 0.3)), 4)
    
    # === 4. ADAPTIVE TP MULTIPLIER ===
    if momentum_strength == 3:
        tp1_mult = 1.8
        tp2_mult = 3.5
    elif momentum_strength == 2:
        tp1_mult = 1.5
        tp2_mult = 3.0
    else:
        tp1_mult = 1.2
        tp2_mult = 2.0
    
    if signal == "LONG":
        take_profit_1 = round(entry + (atr_4h * tp1_mult), 4)
        take_profit_2 = round(entry + (atr_4h * tp2_mult), 4)
    else:
        take_profit_1 = round(entry - (atr_4h * tp1_mult), 4)
        take_profit_2 = round(entry - (atr_4h * tp2_mult), 4)
    
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return None
    reward = abs(take_profit_1 - entry)
    risk_reward = round(reward / risk, 2) if risk > 0 else 0
    
    return {
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "risk_reward": risk_reward,
        "atr_4h": round(atr_4h, 4),
        "atr_1h": round(atr_1h, 4)
    }


# ============================================================
# ANALISA PER PAIR
# ============================================================

def analyze_pair(symbol, btc_context):
    print(f"\n[ANALISA] {symbol}")
    
    candles_4h = parse_klines(fetch_klines(symbol, TF_4H, limit=100))
    candles_1h = parse_klines(fetch_klines(symbol, TF_1H, limit=100))
    candles_15m = parse_klines(fetch_klines(symbol, TF_15M, limit=100))
    
    if not candles_4h or not candles_1h or not candles_15m:
        print(f"[SKIP] {symbol}: Data tidak lengkap")
        return None
    
    total_volume = sum(c["quote_volume"] for c in candles_4h[-24:]) if len(candles_4h) >= 24 else 0
    if total_volume < MIN_VOLUME_USDT and symbol != "BTCUSDT":
        print(f"[SKIP] {symbol}: Volume rendah (${total_volume:,.0f})")
        return None
    
    trend_result = trend_engine(candles_4h, candles_1h, candles_15m)
    print(f"  Trend Engine: {trend_result}")
    
    momentum_result = momentum_engine(candles_4h, candles_1h, candles_15m)
    print(f"  Momentum Engine: {momentum_result}")
    
    volatility_result = volatility_engine(candles_4h, candles_1h, candles_15m)
    print(f"  Volatility Engine: {volatility_result}")
    
    oi_result = oi_funding_filter(symbol)
    print(f"  OI+Funding Filter: {oi_result}")
    
    signal = scoring_engine(trend_result, momentum_result, volatility_result, oi_result, btc_context)
    print(f"  Scoring Engine: {signal}")
    
    tp_sl = tp_sl_engine(symbol, signal, candles_4h, candles_1h, candles_15m)
    
    if signal == "NO_TRADE" or not tp_sl:
        if not tp_sl:
            print(f"[FILTERED] {symbol}: TP/SL invalid")
        return None
    
    return {
        "symbol": symbol,
        "signal": signal,
        "trend": trend_result,
        "momentum": momentum_result,
        "volatility": volatility_result,
        "entry": tp_sl["entry"],
        "stop_loss": tp_sl["stop_loss"],
        "take_profit_1": tp_sl["take_profit_1"],
        "take_profit_2": tp_sl["take_profit_2"],
        "risk_reward": tp_sl["risk_reward"],
        "atr_4h": tp_sl["atr_4h"],
        "atr_1h": tp_sl["atr_1h"],
        "btc_context": btc_context
    }


def safe_analyze_pair(symbol, btc_context):
    """Safe wrapper — cegah silent crash."""
    try:
        return analyze_pair(symbol, btc_context)
    except Exception as e:
        print(f"[PAIR ERROR] {symbol}: {e}")
        return None


# ============================================================
# PAIR TRENDING
# ============================================================

def get_top_gainers(exclude_pairs, limit=7):
    tickers = fetch_24h_ticker()
    if not tickers:
        return []
    usdt_pairs = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if symbol.endswith("USDT") and symbol not in exclude_pairs:
            try:
                price_change = float(t.get("priceChangePercent", 0))
                volume = float(t.get("quoteVolume", 0))
                if volume >= MIN_VOLUME_USDT:
                    usdt_pairs.append({
                        "symbol": symbol,
                        "price_change": price_change,
                        "volume": volume
                    })
            except (ValueError, TypeError):
                continue
    usdt_pairs.sort(key=lambda x: x["price_change"], reverse=True)
    top_7 = usdt_pairs[:limit]
    print(f"\n[TOP GAINERS 24J]")
    for i, p in enumerate(top_7, 1):
        print(f"  {i}. {p['symbol']}: {p['price_change']:.2f}% (Vol: ${p['volume']:,.0f})")
    return [p["symbol"] for p in top_7]


# ============================================================
# PRODUCER: ANALYSIS ENGINE
# ============================================================

def run_analysis_engine(cycle_count):
    if not ANALYSIS_LOCK.acquire(blocking=False):
        print("[GUARD] Analysis sedang berjalan, skip siklus ini")
        return
    
    try:
        print(f"\n{'='*60}")
        print(f"[SIKLUS #{cycle_count}] Mulai: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")
        
        if cycle_count % SESSION_REFRESH_INTERVAL == 0:
            refresh_session()
        
        cleanup_cache(OI_CACHE)
        cleanup_cache(FUNDING_CACHE)
        
        print("\n[LANGKAH 1] Mencari 7 pair trending...")
        trending_pairs = get_top_gainers(exclude_pairs=PAIR_TETAP, limit=7)
        
        all_pairs = list(PAIR_TETAP) + trending_pairs
        all_pairs = list(dict.fromkeys(all_pairs))
        all_pairs = all_pairs[:MAX_PAIR_ANALISA]
        
        print(f"\n[LANGKAH 2] Total pair: {len(all_pairs)}")
        print(f"  Tetap: {PAIR_TETAP}")
        print(f"  Trending: {trending_pairs}")
        
        print(f"\n[LANGKAH 3] Analisa BTC Context...")
        btc_candles_4h = parse_klines(fetch_klines("BTCUSDT", TF_4H, limit=100))
        btc_candles_1h = parse_klines(fetch_klines("BTCUSDT", TF_1H, limit=100))
        btc_candles_15m = parse_klines(fetch_klines("BTCUSDT", TF_15M, limit=100))
        
        btc_context = analyze_btc_context(btc_candles_4h, btc_candles_1h, btc_candles_15m)
        print(f"  BTC Context: {btc_context}")
        
        print(f"\n[LANGKAH 4] Analisa {len(all_pairs)} pair...")
        signals = []
        
        for pair in all_pairs:
            result = safe_analyze_pair(pair, btc_context)
            if result:
                signals.append(result)
        
        print(f"\n[LANGKAH 5] Mengirim sinyal ke Telegram (DSS FORMAT)...")
        print(f"  Total sinyal valid: {len(signals)}")
        
        save_all_outputs(signals, btc_context)
        save_signal_history(signals, btc_context)
        
        vip_distribution(signals, btc_context)
        free_distribution(signals, btc_context)
        
        print(f"\n[SIKLUS #{cycle_count}] Selesai: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    finally:
        ANALYSIS_LOCK.release()


# ============================================================
# DATA LAYER
# ============================================================

def save_all_outputs(signals, btc_context):
    if signals is None:
        signals = []
    
    output = {
        "btc_context": btc_context,
        "signal_count": len(signals),
        "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "signals": signals
    }
    atomic_write_json(SIGNAL_FILE, output)
    print(f"[OUTPUT] {SIGNAL_FILE} tersimpan ({len(signals)} sinyal)")
    
    public_signals = []
    for s in signals:
        public_signals.append({
            "symbol": s["symbol"],
            "signal": s["signal"],
            "trend": s["trend"],
            "momentum": s["momentum"],
            "volatility": s["volatility"],
            "btc_context": s["btc_context"]
        })
    web_data = {
        "btc_context": btc_context,
        "signal_count": len(signals),
        "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "signals": public_signals
    }
    atomic_write_json(WEB_FILE, web_data)
    print(f"[WEB] {WEB_FILE} tersimpan ({len(public_signals)} sinyal)")


def save_signal_history(signals, btc_context):
    if signals is None:
        signals = []
    
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "btc_context": btc_context,
        "signal_count": len(signals),
        "signals": signals
    }

    history = safe_load_json(SIGNAL_HISTORY_FILE, [])
    history.insert(0, entry)

    if len(history) > MAX_HISTORY_ENTRIES:
        history = history[:MAX_HISTORY_ENTRIES]

    atomic_write_json(SIGNAL_HISTORY_FILE, history)


# ============================================================
# TELEGRAM SENDER
# ============================================================

def log_telegram_failed(chat_id, reason):
    try:
        with open(TELEGRAM_FAILED_LOG, "a") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CHAT {chat_id}: {reason}\n")
    except:
        pass


def send_to_telegram(chat_id, message, parse_mode="HTML"):
    if not message:
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode
    }
    for attempt in range(MAX_RETRIES):
        try:
            resp = get_session().post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                print(f"[TELEGRAM] Pesan terkirim")
                return True
            elif resp.status_code == 400:
                payload["parse_mode"] = ""
                resp2 = get_session().post(url, json=payload, timeout=REQUEST_TIMEOUT)
                if resp2.status_code == 200:
                    print(f"[TELEGRAM] Pesan terkirim")
                    return True
                else:
                    log_telegram_failed(chat_id, "HTTP 400 parse error")
                    return False
            elif resp.status_code == 404:
                log_telegram_failed(chat_id, "HTTP 404 token/chat salah")
                return False
            else:
                print(f"[TELEGRAM] HTTP {resp.status_code}, attempt {attempt+1}")
                time.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"[TELEGRAM] Error: {e}, attempt {attempt+1}")
            time.sleep(RETRY_DELAY)
    log_telegram_failed(chat_id, f"Gagal setelah {MAX_RETRIES} kali")
    return False


# ============================================================
# FORMAT SINYAL
# ============================================================

def escape_html(text):
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_bias_emoji(signal):
    if signal == "LONG":
        return "🟢"
    elif signal == "SHORT":
        return "🔴"
    return "⚪"


def format_trend_emoji(trend):
    if trend == "bullish":
        return "BULLISH 🐂"
    elif trend == "bearish":
        return "BEARISH 🐻"
    return "NEUTRAL ➡️"


def format_momentum_label(momentum):
    if "kuat" in momentum and "bullish" in momentum:
        return "MENGUAT ⬆️"
    elif "kuat" in momentum and "bearish" in momentum:
        return "MELEMAH ⬇️"
    elif "lemah" in momentum and "bullish" in momentum:
        return "LEMAH NAIK ↗️"
    elif "lemah" in momentum and "bearish" in momentum:
        return "LEMAH TURUN ↘️"
    return "NETRAL ➡️"


def format_btc_context_label(btc_context):
    if btc_context == "baik":
        return "BULLISH 🟢"
    elif btc_context == "buruk":
        return "BEARISH 🔴"
    return "SIDEWAYS 📊"


def format_signal_free(signal_data):
    symbol = escape_html(signal_data["symbol"])
    signal = escape_html(signal_data["signal"])
    trend = escape_html(signal_data["trend"])
    momentum = escape_html(signal_data["momentum"])
    btc_context = escape_html(signal_data["btc_context"])
    bias_emoji = format_bias_emoji(signal)
    trend_label = format_trend_emoji(trend)
    momentum_label = format_momentum_label(momentum)
    btc_label = format_btc_context_label(btc_context)
    message = f"""<b>🔥 DSS MARKET ALERT</b>

🆓 <i>VERSION FREE</i>

<b>🪙 PAIR</b>       : <code>{symbol}</code>
<b>🎯 BIAS</b>       : <b>{bias_emoji} {signal}</b>
<b>📈 TREND</b>      : <b>{trend_label}</b>
<b>⚡ MOMENTUM</b>   : <b>{momentum_label}</b>
<b>₿ BTC CONTEXT</b> : {btc_label}

✨ <i>Watch for setup!</i>

<b>🔐 FULL ENTRY & TP/SL:</b>
<blockquote>⚠️ <b>VIP CHANNEL ONLY</b> ⚠️</blockquote>

<b>🏷️ #DSS</b>  <b>#{symbol}</b>"""
    return message


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
    bias_emoji = format_bias_emoji(signal)
    trend_label = format_trend_emoji(trend)
    momentum_label = format_momentum_label(momentum)
    message = f"""<b>🔥 DSS VIP SIGNAL</b>

💎 <i>FULL ACCESS</i>

<b>🪙 PAIR</b>       : <code>{symbol}</code>
<b>🎯 BIAS</b>       : <b>{bias_emoji} {signal}</b>
<b>📈 TREND</b>      : <b>{trend_label}</b>
<b>⚡ MOMENTUM</b>   : <b>{momentum_label}</b>
<b>💰 ENTRY</b>      : <code>{entry}</code>
<b>🛑 STOP LOSS</b>  : <code>{sl}</code>
<b>✅ TP1</b>         : <code>{tp1}</code>
<b>✅ TP2</b>         : <code>{tp2}</code>
<b>📊 RISK/REWARD</b> : <b>{rr}</b>

🏷️ <b>#DSS #VIP</b>  <b>#{symbol}</b>"""
    return message


def format_summary(signals, btc_context, channel="FREE"):
    btc_label = format_btc_context_label(btc_context)
    signal_count = len(signals) if signals else 0
    
    if channel == "VIP":
        header = "<b>📊 DSS VIP SESSION</b>"
        tag = "#DSS #VIP"
    else:
        header = "<b>📊 DSS MARKET SESSION</b>"
        tag = "#DSS"
    
    if signal_count == 0:
        return f"""{header}

⏰ <i>Tidak ada sinyal valid</i>
₿ BTC: {btc_label}
✅ <i>Sistem tetap berjalan normal</i>

🏷️ <b>{tag}</b>"""
    
    summary = f"""{header}

₿ BTC: {btc_label}
📨 Sinyal: <b>{signal_count}</b>

"""
    for s in signals:
        emoji = format_bias_emoji(s["signal"])
        symbol = escape_html(s["symbol"])
        signal = escape_html(s["signal"])
        summary += f"{emoji} <b>{symbol}</b>: {signal}\n"
    
    if channel == "FREE":
        summary += f"\n🔐 <i>Full entry di VIP Channel</i>"
    summary += f"\n🏷️ <b>{tag}</b>"
    
    return summary


# ============================================================
# DISTRIBUTION
# ============================================================

def free_distribution(signals, btc_context):
    if signals is None:
        signals = []
    summary = format_summary(signals, btc_context, "FREE")
    send_to_telegram(TELEGRAM_FREE_ID, summary)
    if signals:
        for s in signals:
            message = format_signal_free(s)
            send_to_telegram(TELEGRAM_FREE_ID, message)
            time.sleep(SEND_DELAY)


def vip_distribution(signals, btc_context):
    if signals is None:
        signals = []
    summary = format_summary(signals, btc_context, "VIP")
    send_to_telegram(TELEGRAM_VIP_ID, summary)
    if signals:
        for s in signals:
            message = format_signal_vip(s)
            send_to_telegram(TELEGRAM_VIP_ID, message)
            time.sleep(SEND_DELAY)


# ============================================================
# GITHUB SYNC
# ============================================================

def github_sync():
    repo_path = GIT_REPO_PATH
    
    if not os.path.exists(os.path.join(repo_path, ".git")):
        print("[GIT] Repo tidak ditemukan")
        return
    
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True
        )
        
        if not result.stdout.strip():
            return
        
        subprocess.run(["git", "add", "."], cwd=repo_path, check=False)
        subprocess.run(["git", "commit", "-m", "auto update signal"], cwd=repo_path, check=False)
        subprocess.run(["git", "push"], cwd=repo_path, check=False)
        print("[GIT] SYNC OK")
    except Exception as e:
        print(f"[GIT ERROR] {e}")


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    get_session()
    
    print("=" * 60)
    print("DSS MARKET - SISTEM ANALISA SINYAL SWING INTERDAY")
    print("Platform: Termux Android | Binance Futures")
    print(f"Versi: 2.3.0 | Siklus: {SIKLUS_DETIK // 60} menit")
    print(f"Pair: 7 tetap + 7 trending | Maks: {MAX_PAIR_ANALISA}")
    print(f"Scoring: Weighted (T={TREND_WEIGHT} M={MOMENTUM_WEIGHT} V={VOLATILITY_WEIGHT} O={OI_WEIGHT})")
    print(f"Threshold: {MIN_TOTAL_SUPPORT} | BTC: Soft Penalty")
    print(f"Distribusi: FREE + VIP + WEB + GIT")
    print("=" * 60)
    
    cycle_count = 0
    
    while True:
        cycle_count += 1
        cycle_start = time.time()
        
        run_analysis_engine(cycle_count)
        
        elapsed = time.time() - cycle_start
        
        if elapsed > 40 * 60:
            print("[ABORT] Cycle overload — melebihi 40 menit")
        else:
            github_sync()
        
        remaining = max(0, SIKLUS_DETIK - elapsed)
        
        next_cycle_time = datetime.now() + timedelta(seconds=remaining)
        print(f"\n[INFO] Durasi siklus: {elapsed:.0f}s")
        print(f"[INFO] Siklus #{cycle_count+1} berikutnya: {next_cycle_time.strftime('%H:%M:%S')}")
        
        if remaining > 0:
            time.sleep(remaining)
        else:
            print("[WARNING] Siklus melebihi 45 menit, langsung lanjut.")


if __name__ == "__main__":
    main()
