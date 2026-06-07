#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║  DSS MARKET - SISTEM ANALISA SINYAL SWING INTERDAY      ║
║  Platform: Termux Android | Binance Futures             ║
║  Library: Hanya requests                                ║
║  Siklus: 45 menit (anti-drift)                          ║
║                                                        ║
║  VERSI: 2.0.0 (2026-06-02)                             ║
║  ARSITEKTUR: PRODUCER → signals.json → ROUTER          ║
║              ├── FREE Telegram (ringkasan + sinyal)     ║
║              ├── VIP Telegram (full entry/SL/TP)        ║
║              ├── web.json (data publik)                 ║
║              └── GitHub Sync (auto push)                ║
║                                                        ║
║  ENGINE (ASLI - TIDAK DIUBAH):                         ║
║  BTC Context → Trend → Momentum → Volatility           ║
║  → OI + Funding Filter → Scoring → TP/SL               ║
║                                                        ║
║  PAIR TETAP (7):                                       ║
║  BTCUSDT, ETHUSDT, SOLUSDT, SUIUSDT,                   ║
║  DOGEUSDT, UNIUSDT, ZECUSDT                            ║
║  + 7 Pair Trending 24 Jam (dinamis)                    ║
║                                                        ║
║  DISTRIBUSI:                                           ║
║  • FREE: Sinyal tanpa entry/TP/SL                      ║
║  • VIP: Sinyal lengkap (entry, SL, TP1, TP2, RR)       ║
║  • WEB: Data publik untuk Netlify dashboard             ║
║  • GIT: Auto commit + push ke GitHub                   ║
║                                                        ║
║  SCORING (ASLI):                                       ║
║  • Trend + Momentum HARUS searah (2 poin)              ║
║  • Gate: Volatility (normal/tinggi) + Funding valid    ║
║  • Minimal total dukungan: 3                           ║
║  • Tanpa fallback — ketat sesuai desain awal           ║
║                                                        ║
║  BEHAVIOR:                                             ║
║  • Tidak ada sinyal → semua channel tetap notifikasi   ║
║  • Ada sinyal → FREE dapat ringkasan, VIP dapat full   ║
║  • signals.json + web.json diperbarui setiap siklus    ║
║  • signal_history.json mencatat semua histori sinyal   ║
║  • Git push otomatis setelah setiap siklus             ║
╚══════════════════════════════════════════════════════════╝
"""

import requests
import json
import time
import os
from datetime import datetime, timedelta

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

# Output files
SIGNAL_FILE = "signals.json"
WEB_FILE = "web.json"
SIGNAL_HISTORY_FILE = "signal_history.json"

# Git Repo Path
GIT_REPO_PATH = os.path.expanduser("~/Dss_Web2")

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
    url = f"{BASE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    return fetch_with_retry(url, params=params)


def fetch_24h_ticker():
    url = f"{BASE_URL}/fapi/v1/ticker/24hr"
    return fetch_with_retry(url)


def fetch_open_interest(symbol):
    url = f"{BASE_URL}/fapi/v1/openInterest"
    params = {"symbol": symbol}
    return fetch_with_retry(url, params=params)


def fetch_funding_rate(symbol):
    url = f"{BASE_URL}/fapi/v1/fundingRate"
    params = {"symbol": symbol, "limit": 1}
    result = fetch_with_retry(url, params=params)
    if result and isinstance(result, list) and len(result) > 0:
        return result[0]
    return None


# ============================================================
# PARSING DATA KLINES
# ============================================================

def parse_klines(klines_data):
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
# ENGINE ANALISA (ASLI - TIDAK DIUBAH)
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
    if directional_bullish < 2 and directional_bearish < 2:
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
    return "NO_TRADE"


def tp_sl_engine(symbol, signal, candles_4h, candles_1h, candles_15m):
    if signal == "NO_TRADE":
        return None
    
    if not candles_4h or not candles_1h or not candles_15m:
        return None
    
    current_price = candles_15m[-1]["close"]
    
    atr_4h = calculate_atr(candles_4h, 14)
    atr_1h = calculate_atr(candles_1h, 14)
    
    if not atr_4h or not atr_1h:
        return None
    
    # === 1. ENTRY QUALITY FILTER (ANTI SPIKE ENTRY) ===
    last_high = max(c["high"] for c in candles_15m[-3:])
    last_low = min(c["low"] for c in candles_15m[-3:])
    candle_range = last_high - last_low
    
    if signal == "LONG" and current_price > (last_high - candle_range * 0.2):
        return None
    
    if signal == "SHORT" and current_price < (last_low + candle_range * 0.2):
        return None
    
    # === 2. MOMENTUM CONTINUATION CHECK ===
    momentum_strength = 0
    
    rsi = calculate_rsi([c["close"] for c in candles_1h], 14)
    volume_trend = calculate_volume_trend(candles_15m)
    _, _, macd_hist = calculate_macd([c["close"] for c in candles_1h])
    
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
    
    # === 5. FILTER WEAK CONTINUATION ===
    if momentum_strength == 0:
        return None
    
    sl_multiplier = 1.5
    
    # === 2. HYBRID STOP LOSS (ATR + SWING PROTECTION) ===
    swing_high = max(c["high"] for c in candles_1h[-20:])
    swing_low = min(c["low"] for c in candles_1h[-20:])
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
    
    if symbol != "BTCUSDT" and btc_context == "buruk":
        print(f"[SKIP] {symbol}: BTC context buruk, altcoin di-skip")
        return None
    
    trend_result = trend_engine(candles_4h, candles_1h, candles_15m)
    print(f"  Trend Engine: {trend_result}")
    
    momentum_result = momentum_engine(candles_4h, candles_1h, candles_15m)
    print(f"  Momentum Engine: {momentum_result}")
    
    volatility_result = volatility_engine(candles_4h, candles_1h, candles_15m)
    print(f"  Volatility Engine: {volatility_result}")
    
    oi_result = oi_funding_filter(symbol)
    print(f"  OI+Funding Filter: {oi_result}")
    
    signal = scoring_engine(trend_result, momentum_result, volatility_result, oi_result)
    print(f"  Scoring Engine: {signal}")
    
    tp_sl = tp_sl_engine(symbol, signal, candles_4h, candles_1h, candles_15m)
    
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
    print(f"\n{'='*60}")
    print(f"[SIKLUS #{cycle_count}] Mulai: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
    btc_candles_4h = parse_klines(fetch_klines("BTCUSDT", TF_4H, limit=100))
    btc_candles_1h = parse_klines(fetch_klines("BTCUSDT", TF_1H, limit=100))
    btc_candles_15m = parse_klines(fetch_klines("BTCUSDT", TF_15M, limit=100))
    
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
    
    print(f"\n[LANGKAH 5] Mengirim sinyal ke Telegram (DSS FORMAT)...")
    print(f"  Total sinyal valid: {len(signals)}")
    
    save_signals_json(signals, btc_context)
    save_signal_history(signals, btc_context)
    
    vip_distribution(signals, btc_context)
    free_distribution(signals, btc_context)
    web_distribution(signals, btc_context)
    
    print(f"\n[SIKLUS #{cycle_count}] Selesai: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


# ============================================================
# DATA LAYER
# ============================================================

def save_signals_json(signals, btc_context):
    output = {
        "btc_context": btc_context,
        "signal_count": len(signals),
        "last_update": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "signals": signals
    }
    try:
        with open(SIGNAL_FILE, "w") as f:
            json.dump(output, f, indent=2)
        print(f"[OUTPUT] {SIGNAL_FILE} tersimpan ({len(signals)} sinyal)")
    except Exception as e:
        print(f"[ERROR] Gagal menyimpan {SIGNAL_FILE}: {e}")


def save_signal_history(signals, btc_context):
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "btc_context": btc_context,
        "signal_count": len(signals),
        "signals": signals
    }

    history = []

    if os.path.exists(SIGNAL_HISTORY_FILE):
        try:
            with open(SIGNAL_HISTORY_FILE, "r") as f:
                history = json.load(f)
        except:
            history = []

    history.insert(0, entry)

    with open(SIGNAL_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


# ============================================================
# TELEGRAM SENDER
# ============================================================

def send_to_telegram(chat_id, message, parse_mode="HTML"):
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
                print(f"[TELEGRAM] Pesan terkirim")
                return True
            elif resp.status_code == 400:
                payload["parse_mode"] = ""
                resp2 = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
                if resp2.status_code == 200:
                    print(f"[TELEGRAM] Pesan terkirim")
                    return True
                else:
                    return False
            else:
                time.sleep(RETRY_DELAY)
        except:
            time.sleep(RETRY_DELAY)
    return False


# ============================================================
# FORMAT SINYAL (TANPA GARIS - CLEAN DESIGN)
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
        return "🐂"
    elif trend == "bearish":
        return "🐻"
    return "➡️"


def format_momentum_label(momentum):
    if "kuat" in momentum and "bullish" in momentum:
        return "KUAT NAIK"
    elif "kuat" in momentum and "bearish" in momentum:
        return "KUAT TURUN"
    elif "lemah" in momentum and "bullish" in momentum:
        return "Lemah Naik"
    elif "lemah" in momentum and "bearish" in momentum:
        return "Lemah Turun"
    return "Netral"


def format_btc_context_label(btc_context):
    if btc_context == "baik":
        return "BULLISH"
    elif btc_context == "buruk":
        return "BEARISH"
    return "SIDEWAYS"


def format_signal_free(signal_data):
    symbol = signal_data["symbol"]
    signal = signal_data["signal"]
    trend = signal_data["trend"]
    momentum = signal_data["momentum"]
    btc_context = signal_data["btc_context"]
    
    bias_emoji = format_bias_emoji(signal)
    trend_label = trend.upper()
    momentum_label = format_momentum_label(momentum)
    btc_label = format_btc_context_label(btc_context)
    
    message = f"""🔥 DSS MARKET ALERT

{bias_emoji} {symbol}  •  {signal}
📈 Trend: {trend_label} ({format_trend_emoji(trend)})
⚡ Momentum: {momentum_label}
₿ BTC: {btc_label}

✨ Watch for setup!

🔐 Full Entry & TP/SL: VIP Only
🏷️ #DSS #{symbol}"""
    return message


def format_signal_vip(signal_data):
    symbol = signal_data["symbol"]
    signal = signal_data["signal"]
    entry = signal_data["entry"]
    sl = signal_data["stop_loss"]
    tp1 = signal_data["take_profit_1"]
    tp2 = signal_data["take_profit_2"]
    rr = signal_data["risk_reward"]
    trend = signal_data["trend"]
    momentum = signal_data["momentum"]
    
    bias_emoji = format_bias_emoji(signal)
    trend_label = trend.upper()
    momentum_label = format_momentum_label(momentum)
    
    message = f"""🔥 DSS VIP SIGNAL

{bias_emoji} {symbol}  •  {signal}
📈 Trend: {trend_label} ({format_trend_emoji(trend)})
⚡ Momentum: {momentum_label}

💰 Entry: {entry}
🛑 SL: {sl}
✅ TP1: {tp1}
✅ TP2: {tp2}
📊 RR: {rr}

🏷️ #DSS #{symbol}"""
    return message


def format_summary(signals, btc_context, channel="FREE"):
    btc_label = format_btc_context_label(btc_context)
    signal_count = len(signals)
    
    if channel == "VIP":
        header = "📊 DSS VIP SESSION"
        tag = "#DSS #VIP"
    else:
        header = "📊 DSS MARKET SESSION"
        tag = "#DSS"
    
    if signal_count == 0:
        return f"""{header}

⏰ Tidak ada sinyal valid
₿ BTC: {btc_label}
✅ Sistem tetap berjalan normal

🏷️ {tag}"""
    
    summary = f"""{header}

₿ BTC: {btc_label}
📨 Sinyal: {signal_count}

"""
    for s in signals:
        emoji = format_bias_emoji(s["signal"])
        symbol = s["symbol"]
        signal = s["signal"]
        summary += f"{emoji} {symbol}: {signal}\n"
    
    if channel == "FREE":
        summary += f"\n🔐 Full entry di VIP Channel"
    summary += f"\n🏷️ {tag}"
    
    return summary


# ============================================================
# DISTRIBUTION
# ============================================================

def free_distribution(signals, btc_context):
    summary = format_summary(signals, btc_context, "FREE")
    send_to_telegram(TELEGRAM_FREE_ID, summary)
    if signals:
        for s in signals:
            message = format_signal_free(s)
            send_to_telegram(TELEGRAM_FREE_ID, message)
            time.sleep(1)


def vip_distribution(signals, btc_context):
    summary = format_summary(signals, btc_context, "VIP")
    send_to_telegram(TELEGRAM_VIP_ID, summary)
    if signals:
        for s in signals:
            message = format_signal_vip(s)
            send_to_telegram(TELEGRAM_VIP_ID, message)
            time.sleep(1)


def web_distribution(signals, btc_context):
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
    try:
        with open(WEB_FILE, "w") as f:
            json.dump(web_data, f, indent=2)
        print(f"[WEB] {WEB_FILE} tersimpan ({len(public_signals)} sinyal)")
    except Exception as e:
        print(f"[WEB] Gagal menyimpan {WEB_FILE}: {e}")


# ============================================================
# GITHUB SYNC
# ============================================================

def github_sync():
    os.system("git add .")
    os.system('git commit -m "auto update signal"')
    os.system("git push origin system1")


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("=" * 60)
    print("DSS MARKET - SISTEM ANALISA SINYAL SWING INTERDAY")
    print("Platform: Termux Android | Binance Futures")
    print(f"Versi: 2.0.0 | Siklus: {SIKLUS_DETIK // 60} menit")
    print(f"Pair: 7 tetap + 7 trending | Maks: {MAX_PAIR_ANALISA}")
    print(f"Scoring: Asli (MIN_SUPPORT={MIN_TOTAL_SUPPORT})")
    print(f"Distribusi: FREE + VIP + WEB + GIT")
    print("=" * 60)
    
    cycle_count = 0
    
    while True:
        cycle_count += 1
        cycle_start = time.time()
        
        run_analysis_engine(cycle_count)
        github_sync()
        
        elapsed = time.time() - cycle_start
        remaining = SIKLUS_DETIK - elapsed
        
        next_cycle = datetime.now() + timedelta(seconds=remaining)
        print(f"\n[INFO] Durasi siklus: {elapsed:.0f}s")
        print(f"[INFO] Siklus #{cycle_count+1} berikutnya: {next_cycle.strftime('%H:%M:%S')} (tepat {SIKLUS_DETIK//60} menit dari mulai)")
        
        if remaining > 0:
            time.sleep(remaining)
        else:
            print("[WARNING] Siklus melebihi 45 menit, langsung lanjut.")


if __name__ == "__main__":
    main()
