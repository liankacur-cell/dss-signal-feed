#!/usr/bin/env python3
"""
SISTEM ANALISA SINYAL SWING INTERDAY - BINANCE FUTURES
Platform: Termux Android
Library: Hanya requests
Siklus: 45 menit
Arsitektur: PRODUCER → signals.json → ROUTER → (FREE / VIP / WEB) → GIT SYNC
Repo: ~/Dss_Web2
"""

import requests
import json
import time
import os
import sys
import traceback
import subprocess
from datetime import datetime, timedelta

# ============================================================
# ENV CONFIG (ANTI HARDCODE)
# ============================================================

def load_env_file(path=".env"):
    config = {}
    try:
        with open(path, "r") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    config[k.strip()] = v.strip()
    except:
        pass
    return config


ENV = load_env_file()

TELEGRAM_BOT_TOKEN = ENV.get("8440657002:AAEqJIJziZ37HVRKOd0e3TcXyEAb3PclrwQ")
TELEGRAM_FREE_ID = ENV.get("-1003624661217")
TELEGRAM_VIP_ID = ENV.get("-1003765702878")

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

# Threshold
MIN_TOTAL_SUPPORT = 3
MIN_VOLUME_USDT = 5_000_000
MAX_PAIR_ANALISA = 14

# Kondisi BTC
BTC_SIDEWAYS_RANGE = 3.0
BTC_BEARISH_THRESHOLD = -5.0
BTC_BULLISH_THRESHOLD = 3.0

# Funding rate filter
MAX_FUNDING_RATE = 0.05

# Retry config
MAX_RETRIES = 3
RETRY_DELAY = 3
REQUEST_TIMEOUT = 15

# File paths
SIGNAL_FILE = "signals.json"
WEB_FILE = "web.json"
LOCK_FILE = "dss.lock"
ERROR_LOG = "error.log"
CYCLE_LOG = "cycle.log"

# Git Repo Path
GIT_REPO_PATH = os.path.expanduser("~/Dss_Web2")

# Global Cache
CACHE = {}

# ============================================================
# BINANCE FUTURES API
# ============================================================

BASE_URL = "https://fapi.binance.com"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Android; Termux)",
    "Accept": "application/json"
})


def fetch_with_retry(url, params=None, max_retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT):
    """Request dengan retry otomatis."""
    last_error = None
    
    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, timeout=timeout)
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
    """Ambil data klines dari Binance Futures."""
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    return fetch_with_retry(url, params=params)


def fetch_24h_ticker():
    """Ambil 24h ticker semua USDT pairs."""
    url = f"{BASE_URL}/fapi/v1/ticker/24hr"
    return fetch_with_retry(url)


def fetch_open_interest(symbol):
    """Ambil Open Interest."""
    url = f"{BASE_URL}/fapi/v1/openInterest"
    params = {"symbol": symbol}
    return fetch_with_retry(url, params=params)


def fetch_funding_rate(symbol):
    """Ambil Funding Rate."""
    url = f"{BASE_URL}/fapi/v1/fundingRate"
    params = {"symbol": symbol, "limit": 1}
    result = fetch_with_retry(url, params=params)
    if result and isinstance(result, list) and len(result) > 0:
        return result[0]
    return None


# ============================================================
# SAFE API WRAPPER
# ============================================================

def safe_fetch_klines(symbol, tf):
    """Safe wrapper: selalu return list kosong jika gagal."""
    data = fetch_klines(symbol, tf)
    if not data:
        return []
    return parse_klines(data)


def get_cached(symbol, tf):
    """Cache-aware fetch."""
    key = f"{symbol}_{tf}"
    if key in CACHE:
        return CACHE[key]
    data = safe_fetch_klines(symbol, tf)
    CACHE[key] = data
    return data


# ============================================================
# PARSING DATA KLINES
# ============================================================

def parse_klines(klines_data):
    """Parse data klines mentah ke list of dict."""
    if not klines_data:
        return []
    
    candles = []
    for k in klines_data:
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
    return candles


def calculate_sma(closes, period):
    """Hitung Simple Moving Average."""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calculate_ema(closes, period):
    """Hitung Exponential Moving Average."""
    if len(closes) < period:
        return None
    
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    
    return ema


def calculate_ema_series(closes, period):
    """Hitung EMA full series."""
    if len(closes) < period:
        return []
    
    multiplier = 2 / (period + 1)
    ema_list = []
    
    first_ema = sum(closes[:period]) / period
    ema_list.append(first_ema)
    
    for i in range(period, len(closes)):
        ema = (closes[i] - ema_list[-1]) * multiplier + ema_list[-1]
        ema_list.append(ema)
    
    return ema_list


def calculate_atr(candles, period=14):
    """Hitung Average True Range."""
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
    """Hitung Relative Strength Index."""
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
    
    if len(gains) < period:
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
    """Hitung MACD (12, 26, 9)."""
    if len(closes) < 26 + 9:
        return None, None, None
    
    ema_12_series = calculate_ema_series(closes, 12)
    ema_26_series = calculate_ema_series(closes, 26)
    
    if not ema_12_series or not ema_26_series:
        return None, None, None
    
    offset = len(ema_12_series) - len(ema_26_series)
    
    macd_line_series = []
    for i in range(len(ema_26_series)):
        macd_val = ema_12_series[i + offset] - ema_26_series[i]
        macd_line_series.append(macd_val)
    
    signal_line_series = calculate_ema_series(macd_line_series, 9)
    
    if not signal_line_series or len(signal_line_series) == 0:
        return None, None, None
    
    macd_line = macd_line_series[-1]
    signal_line = signal_line_series[-1]
    histogram = macd_line - signal_line
    
    return macd_line, signal_line, histogram


def calculate_percent_change(candles, lookback=20):
    """Hitung persentase perubahan harga."""
    if len(candles) < lookback:
        return 0.0
    
    current_close = candles[-1]["close"]
    past_close = candles[-lookback]["close"]
    
    if past_close == 0:
        return 0.0
    
    return ((current_close - past_close) / past_close) * 100


def calculate_volume_trend(candles):
    """Hitung tren volume."""
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
# ENGINE ANALISA
# ============================================================

def analyze_btc_context(candles_4h, candles_1h, candles_15m):
    """Analisa BTC sebagai market konteks."""
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
    """Trend Engine: Arah trend berdasarkan multi-TF."""
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
    """Momentum Engine: Kekuatan pergerakan saat ini."""
    score = 0
    
    closes_1h = [c["close"] for c in candles_1h] if candles_1h else []
    rsi_1h = calculate_rsi(closes_1h, 14) if len(closes_1h) >= 15 else None
    
    if rsi_1h:
        if rsi_1h > 60:
            score += 1
        elif rsi_1h < 40:
            score -= 1
    
    macd_line, signal_line, histogram = calculate_macd(closes_1h) if len(closes_1h) >= 35 else (None, None, None)
    
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
    """Volatility Engine: Kondisi volatilitas market."""
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
    """OI + Funding Filter."""
    oi_data = fetch_open_interest(symbol)
    if not oi_data:
        return "tidak_valid"
    
    funding_data = fetch_funding_rate(symbol)
    if not funding_data:
        return "tidak_valid"
    
    try:
        funding_rate = float(funding_data.get("fundingRate", 0)) * 100
    except (ValueError, TypeError):
        return "tidak_valid"
    
    if abs(funding_rate) > MAX_FUNDING_RATE:
        return "tidak_valid"
    
    return "valid"


def scoring_engine(trend_result, momentum_result, volatility_result, oi_result):
    """Scoring Engine."""
    trend_bullish = trend_result == "bullish"
    trend_bearish = trend_result == "bearish"
    
    momentum_bullish = "bullish" in momentum_result
    momentum_bearish = "bearish" in momentum_result
    
    directional_bullish = 0
    directional_bearish = 0
    
    if trend_bullish:
        directional_bullish += 1
    if trend_bearish:
        directional_bearish += 1
    if momentum_bullish:
        directional_bullish += 1
    if momentum_bearish:
        directional_bearish += 1
    
    volatility_ok = volatility_result in ["normal", "tinggi"]
    oi_ok = oi_result == "valid"
    
    if directional_bullish == 0 and directional_bearish == 0:
        return "NO_TRADE"
    
    if directional_bullish >= 2:
        total_support = directional_bullish
        if volatility_ok:
            total_support += 1
        if oi_ok:
            total_support += 1
        
        if total_support >= MIN_TOTAL_SUPPORT:
            return "LONG"
    
    if directional_bearish >= 2:
        total_support = directional_bearish
        if volatility_ok:
            total_support += 1
        if oi_ok:
            total_support += 1
        
        if total_support >= MIN_TOTAL_SUPPORT:
            return "SHORT"
    
    if directional_bullish > directional_bearish:
        return "LONG"
    elif directional_bearish > directional_bullish:
        return "SHORT"
    
    return "NO_TRADE"


def tp_sl_engine(symbol, signal, candles_4h, candles_1h, candles_15m):
    """TP/SL Engine."""
    if signal == "NO_TRADE":
        return None
    
    if not candles_4h or not candles_1h or not candles_15m:
        return None
    
    current_price = candles_15m[-1]["close"]
    
    atr_4h = calculate_atr(candles_4h, 14)
    atr_1h = calculate_atr(candles_1h, 14)
    
    if not atr_4h or not atr_1h:
        return None
    
    sl_multiplier = 1.5
    
    if signal == "LONG":
        entry = current_price
        stop_loss = round(entry - (atr_1h * sl_multiplier), 4)
        take_profit_1 = round(entry + (atr_4h * 1.5), 4)
        take_profit_2 = round(entry + (atr_4h * 3.0), 4)
    else:
        entry = current_price
        stop_loss = round(entry + (atr_1h * sl_multiplier), 4)
        take_profit_1 = round(entry - (atr_4h * 1.5), 4)
        take_profit_2 = round(entry - (atr_4h * 3.0), 4)
    
    risk = abs(entry - stop_loss)
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
    """Analisa satu pair lengkap sesuai pipeline."""
    print(f"\n[ANALISA] {symbol}")
    
    candles_4h = get_cached(symbol, TF_4H)
    candles_1h = get_cached(symbol, TF_1H)
    candles_15m = get_cached(symbol, TF_15M)
    
    if not candles_4h or not candles_1h or not candles_15m:
        print(f"[SKIP] {symbol}: Data tidak lengkap")
        return None
    
    total_volume = sum(c["quote_volume"] for c in candles_4h[-24:]) if len(candles_4h) >= 24 else 0
    if total_volume < MIN_VOLUME_USDT and symbol != "BTCUSDT":
        print(f"[SKIP] {symbol}: Volume rendah (${total_volume:,.0f})")
        return None
    
    if symbol != "BTCUSDT" and btc_context == "buruk":
        print(f"[SKIP] {symbol}: BTC context buruk, altcoin di-skip")
        return None
    
    try:
        trend_result = trend_engine(candles_4h, candles_1h, candles_15m)
    except:
        trend_result = "neutral"
    print(f"  Trend Engine: {trend_result}")
    
    try:
        momentum_result = momentum_engine(candles_4h, candles_1h, candles_15m)
    except:
        momentum_result = "netral"
    print(f"  Momentum Engine: {momentum_result}")
    
    try:
        volatility_result = volatility_engine(candles_4h, candles_1h, candles_15m)
    except:
        volatility_result = "normal"
    print(f"  Volatility Engine: {volatility_result}")
    
    try:
        oi_result = oi_funding_filter(symbol)
    except:
        oi_result = "valid"
    print(f"  OI+Funding Filter: {oi_result}")
    
    signal = scoring_engine(trend_result, momentum_result, volatility_result, oi_result)
    print(f"  Scoring Engine: {signal}")
    
    try:
        tp_sl = tp_sl_engine(symbol, signal, candles_4h, candles_1h, candles_15m)
    except:
        tp_sl = None
    
    if signal == "NO_TRADE" or not tp_sl:
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


# ============================================================
# PAIR TRENDING
# ============================================================

def get_top_gainers(exclude_pairs, limit=7):
    """Ambil 7 pair dengan kenaikan 24 jam tertinggi."""
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
    """PRODUCER: Analisa market lengkap 1x per siklus."""
    global CACHE
    CACHE = {}
    
    print(f"\n{'='*60}")
    print(f"[PRODUCER] SIKLUS #{cycle_count} - Mulai: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    print("\n[LANGKAH 1] Mencari 7 pair trending...")
    trending_pairs = get_top_gainers(exclude_pairs=PAIR_TETAP, limit=7)
    
    all_pairs = list(PAIR_TETAP) + trending_pairs
    all_pairs = list(dict.fromkeys(all_pairs))
    all_pairs = all_pairs[:MAX_PAIR_ANALISA]
    
    print(f"\n[LANGKAH 2] Total pair: {len(all_pairs)}")
    print(f"  Tetap: {PAIR_TETAP}")
    print(f"  Trending: {trending_pairs}")
    
    print(f"\n[LANGKAH 3] Analisa BTC Context...")
    btc_candles_4h = get_cached("BTCUSDT", TF_4H)
    btc_candles_1h = get_cached("BTCUSDT", TF_1H)
    btc_candles_15m = get_cached("BTCUSDT", TF_15M)
    
    btc_context = analyze_btc_context(btc_candles_4h, btc_candles_1h, btc_candles_15m)
    print(f"  BTC Context: {btc_context}")
    
    print(f"\n[LANGKAH 4] Analisa {len(all_pairs)} pair...")
    signals = []
    
    for pair in all_pairs:
        try:
            result = analyze_pair(pair, btc_context)
            if result:
                signals.append(result)
        except Exception as e:
            print(f"[ERROR] Gagal analisa {pair}: {e}")
            continue
    
    print(f"\n[LANGKAH 5] Menyimpan hasil ke {SIGNAL_FILE}...")
    print(f"  Total sinyal valid: {len(signals)}")
    
    save_signals_json(signals, btc_context)
    
    print(f"\n[PRODUCER] SIKLUS #{cycle_count} Selesai: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


# ============================================================
# DATA LAYER (Atomic Write)
# ============================================================

def save_signals_json(signals, btc_context):
    """Simpan signals.json dengan atomic write (anti-corruption)."""
    output = {
        "btc_context": btc_context,
        "signal_count": len(signals),
        "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "signals": signals
    }
    try:
        # Atomic write: tulis ke temp file, lalu rename
        temp_file = SIGNAL_FILE + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(output, f, indent=2)
        os.replace(temp_file, SIGNAL_FILE)
        print(f"[OUTPUT] {SIGNAL_FILE} tersimpan ({len(signals)} sinyal)")
        return True
    except Exception as e:
        print(f"[ERROR] Gagal menyimpan {SIGNAL_FILE}: {e}")
        return False


def load_signals_json():
    """Membaca data dari signals.json."""
    try:
        with open(SIGNAL_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Gagal membaca {SIGNAL_FILE}: {e}")
        return None


# ============================================================
# TELEGRAM SENDER (dengan Batching)
# ============================================================

def send_to_telegram_batch(chat_id, messages, parse_mode="HTML"):
    """Kirim multiple pesan ke Telegram dengan jeda aman."""
    for i, message in enumerate(messages):
        success = send_to_telegram_single(chat_id, message, parse_mode)
        if success and i < len(messages) - 1:
            time.sleep(0.5)  # 500ms jeda, lebih aman untuk mobile
    return True


def send_to_telegram_single(chat_id, message, parse_mode="HTML"):
    """Kirim satu pesan ke Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode
    }
    
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                print(f"[TELEGRAM] Pesan terkirim ke {chat_id}")
                return True
            elif resp.status_code == 400:
                payload["parse_mode"] = ""
                resp2 = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
                if resp2.status_code == 200:
                    print(f"[TELEGRAM] Pesan terkirim (plain text) ke {chat_id}")
                    return True
                else:
                    print(f"[TELEGRAM] Plain text gagal: HTTP {resp2.status_code}")
                    return False
            else:
                print(f"[TELEGRAM] HTTP {resp.status_code}, attempt {attempt+1}")
                time.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"[TELEGRAM] Error: {e}, attempt {attempt+1}")
            time.sleep(RETRY_DELAY)
    
    print(f"[TELEGRAM] Gagal kirim setelah {MAX_RETRIES} kali ke {chat_id}")
    return False


# ============================================================
# FORMAT SINYAL
# ============================================================

def escape_html(text):
    """Escape karakter HTML."""
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_bias_emoji(signal):
    """Konversi sinyal ke emoji."""
    if signal == "LONG":
        return "🟢"
    elif signal == "SHORT":
        return "🔴"
    return "⚪"


def format_trend_emoji(trend):
    """Konversi trend ke tampilan."""
    if trend == "bullish":
        return "BULLISH 🐂"
    elif trend == "bearish":
        return "BEARISH 🐻"
    return "NEUTRAL ➡️"


def format_momentum_label(momentum):
    """Konversi momentum ke label."""
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
    """Konversi BTC context ke label."""
    if btc_context == "baik":
        return "BULLISH 🟢"
    elif btc_context == "buruk":
        return "BEARISH 🔴"
    return "SIDEWAYS 📊"


def format_signal_free(signal_data):
    """Format sinyal FREE (tanpa entry/TP/SL)."""
    symbol = escape_html(signal_data["symbol"])
    signal = signal_data["signal"]
    trend = signal_data["trend"]
    momentum = signal_data["momentum"]
    btc_context = signal_data["btc_context"]
    
    bias_emoji = format_bias_emoji(signal)
    trend_label = format_trend_emoji(trend)
    momentum_label = format_momentum_label(momentum)
    btc_label = format_btc_context_label(btc_context)
    
    message = f"""<b>🔥 DSS MARKET ALERT 🔥</b>

🆓 <i>VERSION FREE</i>

<b>🪙 PAIR</b>       : <code>{symbol}</code>
<b>🎯 BIAS</b>       : <b>{bias_emoji} {signal}</b>
<b>📈 TREND</b>      : <b>{trend_label}</b>
<b>⚡ MOMENTUM</b>   : <b>{momentum_label}</b>

─────────────────
<b>₿ BTC CONTEXT</b> : {btc_label}
<b>🎯 SIGNAL</b>      : <b>{signal}</b>
─────────────────

✨ <i>Watch for setup!</i> ✨

<b>🔐 FULL ENTRY & TP/SL:</b>
<blockquote>⚠️ <b>VIP CHANNEL ONLY</b> ⚠️</blockquote>

<b>🏷️ #DSS</b>  <b>#{symbol}</b>"""
    return message


def format_signal_vip(signal_data):
    """Format sinyal VIP (full detail)."""
    symbol = escape_html(signal_data["symbol"])
    signal = signal_data["signal"]
    entry = signal_data["entry"]
    sl = signal_data["stop_loss"]
    tp1 = signal_data["take_profit_1"]
    tp2 = signal_data["take_profit_2"]
    rr = signal_data["risk_reward"]
    trend = signal_data["trend"]
    momentum = signal_data["momentum"]
    
    bias_emoji = format_bias_emoji(signal)
    trend_label = format_trend_emoji(trend)
    momentum_label = format_momentum_label(momentum)
    
    message = f"""<b>🔥 DSS VIP SIGNAL 🔥</b>

💎 <i>FULL ACCESS</i>

<b>🪙 PAIR</b>       : <code>{symbol}</code>
<b>🎯 BIAS</b>       : <b>{bias_emoji} {signal}</b>
<b>📈 TREND</b>      : <b>{trend_label}</b>
<b>⚡ MOMENTUM</b>   : <b>{momentum_label}</b>

─────────────────
<b>💰 ENTRY</b>      : <code>{entry}</code>
<b>🛑 STOP LOSS</b>  : <code>{sl}</code>
<b>✅ TP1</b>         : <code>{tp1}</code>
<b>✅ TP2</b>         : <code>{tp2}</code>
<b>📊 RISK/REWARD</b> : <b>{rr}</b>
─────────────────

🏷️ <b>#DSS #VIP</b>  <b>#{symbol}</b>"""
    return message


def format_summary_free(signals, btc_context):
    """Format ringkasan sinyal FREE."""
    btc_label = format_btc_context_label(btc_context)
    signal_count = len(signals)
    
    if signal_count == 0:
        return f"""<b>📊 DSS MARKET SESSION</b>

⏰ <i>No valid signals</i>
₿ BTC: {btc_label}

🏷️ <b>#DSS</b>"""
    
    summary = f"""<b>📊 DSS MARKET SESSION</b>

₿ BTC: {btc_label}
📨 Sinyal: <b>{signal_count}</b>

"""
    for s in signals:
        emoji = format_bias_emoji(s["signal"])
        symbol = escape_html(s["symbol"])
        signal = s["signal"]
        summary += f"{emoji} <b>{symbol}</b>: {signal}\n"
    
    summary += f"\n🔐 <i>Full entry di VIP Channel</i>"
    summary += f"\n🏷️ <b>#DSS</b>"
    
    return summary


# ============================================================
# DISTRIBUTION LAYERS (dengan batching)
# ============================================================

def free_distribution(signals, btc_context):
    """Kirim sinyal FREE ke Telegram dengan batching."""
    if not signals:
        print("[FREE] Tidak ada sinyal, kirim notifikasi kosong...")
        send_to_telegram_single(TELEGRAM_FREE_ID, format_summary_free([], btc_context))
        return
    
    print(f"[FREE] Mengirim {len(signals)} sinyal ke FREE group...")
    
    messages = [format_summary_free(signals, btc_context)]
    for s in signals:
        messages.append(format_signal_free(s))
    
    send_to_telegram_batch(TELEGRAM_FREE_ID, messages)
    print("[FREE] Distribusi selesai")


def vip_distribution(signals, btc_context):
    """Kirim sinyal VIP ke Telegram dengan batching."""
    if not signals:
        print("[VIP] Tidak ada sinyal, skip...")
        return
    
    print(f"[VIP] Mengirim {len(signals)} sinyal ke VIP group...")
    
    messages = []
    for s in signals:
        messages.append(format_signal_vip(s))
    
    send_to_telegram_batch(TELEGRAM_VIP_ID, messages)
    print("[VIP] Distribusi selesai")


def web_distribution(data):
    """Generate web.json untuk Netlify."""
    if data is None:
        print("[WEB] Tidak ada data, skip...")
        return
    
    public_signals = []
    for s in data.get("signals", []):
        public_signals.append({
            "symbol": s["symbol"],
            "signal": s["signal"],
            "trend": s["trend"],
            "momentum": s["momentum"],
            "volatility": s["volatility"],
            "btc_context": s["btc_context"]
        })
    
    web_data = {
        "btc_context": data["btc_context"],
        "signal_count": data["signal_count"],
        "last_update": data["last_update"],
        "signals": public_signals
    }
    
    try:
        temp_file = WEB_FILE + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(web_data, f, indent=2)
        os.replace(temp_file, WEB_FILE)
        print(f"[WEB] {WEB_FILE} tersimpan ({len(public_signals)} sinyal)")
    except Exception as e:
        print(f"[WEB] Gagal menyimpan {WEB_FILE}: {e}")


# ============================================================
# ROUTER
# ============================================================

def router():
    """ROUTER: Baca signals.json, distribusikan ke semua channel."""
    print(f"\n[ROUTER] Membaca {SIGNAL_FILE}...")
    data = load_signals_json()
    
    if data is None:
        print("[ROUTER] Gagal membaca signals.json, distribusi dibatalkan")
        return
    
    signals = data.get("signals", [])
    btc_context = data.get("btc_context", "unknown")
    
    print(f"[ROUTER] BTC: {btc_context} | Sinyal: {len(signals)}")
    print(f"[ROUTER] Mendistribusikan ke: FREE + VIP + WEB")
    
    vip_distribution(signals, btc_context)
    free_distribution(signals, btc_context)
    web_distribution(data)
    
    print(f"[ROUTER] Distribusi selesai")


# ============================================================
# WATCHDOG
# ============================================================

def crash_handler(e):
    """Tangkap crash, log ke error.log."""
    err = traceback.format_exc()
    print("[FATAL ERROR]", err)
    
    try:
        with open(ERROR_LOG, "a") as f:
            f.write(f"[{datetime.now()}] {err}\n\n")
    except:
        pass


def safe_run(func, *args):
    """Wrapper anti crash total."""
    try:
        return func(*args)
    except Exception as e:
        crash_handler(e)
        return None


# ============================================================
# STRUCTURE LOG
# ============================================================

def log_cycle(cycle, message):
    """Catat log siklus."""
    try:
        with open(CYCLE_LOG, "a") as f:
            f.write(f"[{datetime.now()}] CYCLE {cycle}: {message}\n")
    except:
        pass


# ============================================================
# LOCK SYSTEM
# ============================================================

def acquire_lock():
    """Cegah double run."""
    if os.path.exists(LOCK_FILE):
        print("[LOCK] System already running. Exit.")
        exit()
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    print("[LOCK] Acquired")


def release_lock():
    """Lepas lock."""
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)
        print("[LOCK] Released")


# ============================================================
# GITHUB SYNC
# ============================================================

def github_sync():
    """SAFE GIT SYNC: validasi, cek perubahan, push."""
    repo_path = GIT_REPO_PATH
    
    if not os.path.exists(os.path.join(repo_path, ".git")):
        print("[GIT] INVALID REPO — .git tidak ditemukan")
        print(f"[GIT] Path: {repo_path}")
        return
    
    try:
        if os.path.exists("signals.json"):
            subprocess.run(["cp", "signals.json", repo_path])
            print("[GIT] signals.json dicopy ke repo")
        
        if os.path.exists("web.json"):
            subprocess.run(["cp", "web.json", repo_path])
            print("[GIT] web.json dicopy ke repo")
        
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True)
        
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            capture_output=True,
            text=True
        )
        
        if not status.stdout.strip():
            print("[GIT] No changes, skip commit")
            return
        
        commit_msg = f"update {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_path, check=True)
        
        subprocess.run(["git", "push", "origin", "main"], cwd=repo_path, check=True)
        
        print("[GIT] SYNC OK")
    
    except subprocess.CalledProcessError as e:
        print(f"[GIT ERROR] Command gagal: {e}")
    except FileNotFoundError:
        print("[GIT ERROR] Git tidak terinstall")
    except Exception as e:
        print("[GIT ERROR]", e)


# ============================================================
# MAIN LOOP (Anti Drift Scheduler)
# ============================================================

def main():
    """Main loop - PRODUCER → signals.json → ROUTER → (FREE / VIP / WEB) → GIT SYNC"""
    
    acquire_lock()
    
    print("=" * 60)
    print("DSS MARKET - SISTEM ANALISA SINYAL SWING INTERDAY")
    print("Platform: Termux Android | Binance Futures")
    print(f"Arsitektur: PRODUCER → signals.json → ROUTER → GIT SYNC")
    print(f"Siklus: {SIKLUS_DETIK // 60} menit (anti-drift)")
    print(f"Repo: {GIT_REPO_PATH}")
    print("=" * 60)
    
    cycle_count = 0
    next_run = time.time() + SIKLUS_DETIK
    
    try:
        while True:
            cycle_count += 1
            cycle_start = time.time()
            
            log_cycle(cycle_count, "START")
            
            safe_run(run_analysis_engine, cycle_count)
            
            print(f"\n[CONSUMER] Mulai distribusi...")
            safe_run(router)
            
            safe_run(github_sync)
            
            elapsed = time.time() - cycle_start
            next_run = next_run + SIKLUS_DETIK
            sleep_time = max(0, next_run - time.time())
            
            if sleep_time > SIKLUS_DETIK:
                sleep_time = SIKLUS_DETIK
            
            print(f"\n[INFO] Cycle time: {elapsed:.0f}s")
            print(f"[INFO] Next run in: {sleep_time:.0f}s")
            
            log_cycle(cycle_count, f"DONE in {elapsed:.0f}s")
            
            time.sleep(sleep_time)
    
    finally:
        release_lock()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
