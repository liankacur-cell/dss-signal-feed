#!/usr/bin/env python3
"""
DSS SWING INTERDAY - CRYPTO ONLY
Decision Support System - Manual Trading Only
Termux Ready - Single File - No Heavy Libraries

STABLE v7.7 - PRIORITY FIXES
- EMA200 15m: fetch 250 candles
- WAIT_PULLBACK: clear action label
- SL: liquidity primary, ATR as minimum validation
"""

import os
import sys
import json
import time
import math
import logging
import traceback
import threading
import base64
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Any
from enum import Enum
from threading import Lock

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================
# CONFIGURATION
# ============================================
TELEGRAM_FREE_TOKEN = "8440657002:AAEqJIJziZ37HVRKOd0e3TcXyEAb3PclrwQ"
TELEGRAM_FREE_CHAT_ID = "-1003624661217"
TELEGRAM_VIP_TOKEN = "8440657002:AAEqJIJziZ37HVRKOd0e3TcXyEAb3PclrwQ"
TELEGRAM_VIP_CHAT_ID = "-1003765702878"
GITHUB_TOKEN = ""
GITHUB_REPO = "liankacur-cell/dss-signal-feed"
GITHUB_BRANCH = "main"

# ============================================
# ENUMS
# ============================================
class MarketType(Enum):
    CRYPTO = "crypto"

class SignalDir(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NO_SIGNAL"

class TrendDir(Enum):
    UP = "bullish"
    DOWN = "bearish"
    FLAT = "neutral"

class StructType(Enum):
    BULL = "valid_bullish"
    BEAR = "valid_bearish"
    BROKEN = "broken"
    NONE = "no_structure"

class VolRegime(Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

# ============================================
# LOGGING
# ============================================
class Logger:
    def __init__(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s | %(levelname)s | %(message)s',
            handlers=[
                logging.FileHandler('dss_swing.log', mode='a'),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.log = logging.getLogger('DSS')
        self.err_count = 0
        self.sig_count = 0
        self.skip_count = 0

    def info(self, m): self.log.info(m); print(f"[*] {m}")
    def warn(self, m): self.log.warning(m); print(f"[!] {m}")
    def err(self, m, e=None):
        self.err_count += 1; self.log.error(m)
        print(f"[ERROR] {m}")
        if e: traceback.print_exc()
    def sig(self, pair, d, score):
        self.sig_count += 1
        m = f"SIGNAL: {pair} -> {d} (Score:{score:.1f})"
        self.log.info(m); print(f"[>>>] {m}")
    def nosig(self, pair, reason):
        m = f"NO SIGNAL [{pair}]: {reason}"
        self.log.info(m); print(f"[---] {m}")
    def skip(self, pair, reason):
        self.skip_count += 1
        m = f"SKIP [{pair}]: {reason}"
        self.log.warning(m); print(f"[SKIP] {m}")

logger = Logger()

# ============================================
# MATH UTILS
# ============================================
class MathLib:
    @staticmethod
    def mean(arr): return sum(arr)/len(arr) if arr else 0.0
    @staticmethod
    def std(arr):
        if not arr or len(arr) < 2: return 0.0
        m = MathLib.mean(arr)
        v = sum((x-m)**2 for x in arr)/(len(arr)-1)
        return math.sqrt(max(v, 0))
    @staticmethod
    def max_val(arr): return max(arr) if arr else 0.0
    @staticmethod
    def min_val(arr): return min(arr) if arr else 0.0
    @staticmethod
    def safe_div(a, b, d=0.0): return a/b if b != 0 else d
    @staticmethod
    def clamp(v, lo, hi): return max(lo, min(hi, v))

# ============================================
# FASE B: DATA FETCHER
# ============================================
class DataFetcher:
    def __init__(self):
        self.ses = requests.Session()
        retry = Retry(total=5, backoff_factor=0.8,
                     status_forcelist=[429, 500, 502, 503, 504],
                     allowed_methods=["GET", "POST"])
        adapter = HTTPAdapter(max_retries=retry)
        self.ses.mount("https://", adapter)
        self.ses.mount("http://", adapter)
        self.ses.headers.update({'User-Agent': 'DSS-Swing/1.0'})
        self.cache = {}
        self.cache_ttl = 600
        self.lock = Lock()

    def _cached(self, key):
        with self.lock:
            if key in self.cache:
                ts, data = self.cache[key]
                if time.time() - ts < self.cache_ttl: return data
        return None

    def _set_cache(self, key, data):
        with self.lock:
            self.cache[key] = (time.time(), data)

    def fetch_binance(self, symbol, interval, limit=100):
        ck = f"bn_{symbol}_{interval}"
        cached = self._cached(ck)
        if cached: return cached
        try:
            url = "https://fapi.binance.com/fapi/v1/klines"
            params = {'symbol': symbol, 'interval': interval, 'limit': limit}
            resp = self.ses.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                logger.warn(f"Binance {symbol} {interval}: HTTP {resp.status_code}")
                return []
            data = resp.json()
            if not data or not isinstance(data, list):
                logger.warn(f"Binance {symbol} {interval}: invalid response")
                return []
            out = []
            for k in data:
                if len(k) >= 6:
                    out.append({'ts':float(k[0])/1000,'o':float(k[1]),'h':float(k[2]),
                               'l':float(k[3]),'c':float(k[4]),'v':float(k[5])})
            if out: self._set_cache(ck, out)
            else: logger.skip(symbol, f"Empty dataset {interval}")
            return out
        except requests.exceptions.Timeout:
            logger.err(f"Binance timeout: {symbol} {interval}")
            return []
        except requests.exceptions.ConnectionError:
            logger.err(f"Binance connection error: {symbol} {interval}")
            return []
        except Exception as e:
            logger.err(f"Binance fetch: {symbol} {interval}", e)
            return []

    def fetch_crypto_all(self, symbol):
        data = {}
        # Fetch 15m dengan 250 candle untuk EMA200
        for tf in ['1m','5m','1h']:
            candles = self.fetch_binance(symbol, tf, 100)
            if candles: data[tf] = candles

        # 15m fetch 250 candle
        candles_15m = self.fetch_binance(symbol, '15m', 250)
        if candles_15m:
            data['15m'] = candles_15m
        else:
            candles_15m = self.fetch_binance(symbol, '15m', 100)
            if candles_15m:
                data['15m'] = candles_15m

        if not data: logger.skip(symbol, "Empty dataset")
        return data

    def fetch_trending_binance(self):
        ck = "trending_bn"
        cached = self._cached(ck)
        if cached: return cached
        try:
            url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
            resp = self.ses.get(url, timeout=10)
            if resp.status_code != 200:
                logger.warn(f"Trending API: HTTP {resp.status_code}")
                return []
            all_d = resp.json()
            if not isinstance(all_d, list):
                logger.warn("Trending API: invalid response")
                return []
            usdt = [d for d in all_d if isinstance(d,dict) and d.get('symbol','').endswith('USDT')]
            usdt.sort(key=lambda x: abs(float(x.get('priceChangePercent',0))), reverse=True)
            exclude = {'BTCUSDT','ETHUSDT','SOLUSDT','SUIUSDT','DOGEUSDT','UNIUSDT','ZECUSDT'}
            trending = []
            for d in usdt:
                sym = d.get('symbol','')
                if sym and sym not in exclude: trending.append(sym)
                if len(trending) >= 7: break
            if trending: self._set_cache(ck, trending)
            return trending
        except Exception as e:
            logger.err("Trending fetch failed", e)
            return []

    def fetch_all(self):
        result = {'crypto':{},'ts':datetime.now().isoformat()}
        logger.info("=== FETCHING CRYPTO (Binance) ===")
        for sym in ['BTCUSDT','ETHUSDT','SOLUSDT','SUIUSDT','DOGEUSDT','UNIUSDT','ZECUSDT']:
            d = self.fetch_crypto_all(sym)
            if d: result['crypto'][sym] = d; logger.info(f"  OK {sym}")
            else: logger.warn(f"  FAIL {sym}")
        for sym in self.fetch_trending_binance()[:7]:
            if sym not in result['crypto']:
                d = self.fetch_crypto_all(sym)
                if d: result['crypto'][sym] = d
        return result

# ============================================
# FASE C: MARKET STRUCTURE ENGINE
# ============================================
class StructureEngine:
    def analyze(self, candles_1h):
        if not candles_1h or len(candles_1h) < 20:
            return {'type':StructType.NONE,'valid':False,'hh':False,'hl':False,
                   'lh':False,'ll':False,'swing_h':[],'swing_l':[],
                   'liq_above':0,'liq_below':0,'score':0.0,'reason':'Insufficient 1h data'}
        highs = [c['h'] for c in candles_1h]
        lows = [c['l'] for c in candles_1h]
        swing_h = []; swing_l = []
        n = len(candles_1h)
        for i in range(3, n-3):
            if highs[i] >= max(highs[i-3:i+4]) * 0.995: swing_h.append(highs[i])
            if lows[i] <= min(lows[i-3:i+4]) * 1.005: swing_l.append(lows[i])
        liq_above = MathLib.max_val(highs[-10:])
        liq_below = MathLib.min_val(lows[-10:])
        if len(swing_h) < 2 or len(swing_l) < 2:
            return {'type':StructType.NONE,'valid':False,'hh':False,'hl':False,
                   'lh':False,'ll':False,'swing_h':swing_h,'swing_l':swing_l,
                   'liq_above':liq_above,'liq_below':liq_below,
                   'score':30.0,'reason':'Weak structure (fallback)'}
        hh = swing_h[-1] > swing_h[-2]; hl = swing_l[-1] > swing_l[-2]
        lh = swing_h[-1] < swing_h[-2]; ll = swing_l[-1] < swing_l[-2]
        if hh and hl: st = StructType.BULL; valid = True; reason = "Bullish: HH+HL"
        elif lh and ll: st = StructType.BEAR; valid = True; reason = "Bearish: LH+LL"
        elif (hh and not hl) or (ll and not lh): st = StructType.BROKEN; valid = False; reason = "Structure broken"
        else: st = StructType.NONE; valid = False; reason = "No clear structure"
        if valid: struct_score = 100.0
        elif st == StructType.BROKEN: struct_score = 50.0
        else: struct_score = 30.0
        return {'type':st,'valid':valid,'hh':hh,'hl':hl,'lh':lh,'ll':ll,
               'swing_h':swing_h[-5:],'swing_l':swing_l[-5:],
               'liq_above':liq_above,'liq_below':liq_below,
               'score':struct_score,'reason':reason}

# ============================================
# FASE D: TREND ENGINE
# ============================================
class TrendEngine:
    @staticmethod
    def ema(prices, period):
        if not prices or len(prices) < period: return []
        mult = 2.0 / (period + 1.0)
        sma = MathLib.mean(prices[:period])
        ema_values = [0.0] * len(prices)
        ema_values[period - 1] = sma
        for i in range(period, len(prices)):
            ema_values[i] = (prices[i] - ema_values[i-1]) * mult + ema_values[i-1]
        for i in range(period - 1): ema_values[i] = ema_values[period - 1]
        return ema_values

    def analyze(self, data):
        trends = {}; ema9v = {}; ema21v = {}
        for tf in ['1m','5m','15m','1h']:
            if tf in data and len(data[tf]) >= 21:
                closes = [c['c'] for c in data[tf]]
                e9 = self.ema(closes, 9); e21 = self.ema(closes, 21)
                if not e9 or not e21:
                    trends[tf] = TrendDir.FLAT; ema9v[tf] = 0; ema21v[tf] = 0
                    continue
                ema9v[tf] = e9[-1]; ema21v[tf] = e21[-1]
                if e9[-1] > e21[-1]: trends[tf] = TrendDir.UP
                elif e9[-1] < e21[-1]: trends[tf] = TrendDir.DOWN
                else: trends[tf] = TrendDir.FLAT
            else: trends[tf] = TrendDir.FLAT; ema9v[tf] = 0; ema21v[tf] = 0
        tv = list(trends.values())
        up = tv.count(TrendDir.UP); down = tv.count(TrendDir.DOWN)
        aligned = up >= 3 or down >= 3
        if up > down: d = TrendDir.UP; strength = up/4.0
        elif down > up: d = TrendDir.DOWN; strength = down/4.0
        else: d = TrendDir.FLAT; strength = 0.5
        trend_score = strength * 100.0
        if aligned: trend_score = min(trend_score + 20.0, 100.0)
        return {'dir':d,'strength':strength,'trends':trends,'aligned':aligned,
               'ema9':ema9v,'ema21':ema21v,'score':trend_score,
               'reason':f"Trend {d.value} (strength:{strength:.2f}, aligned:{aligned})"}

# ============================================
# FASE D: MOMENTUM ENGINE
# ============================================
class MomentumEngine:
    @staticmethod
    def rsi(prices, period=14):
        if not prices or len(prices) < period + 1: return 50.0
        if period == 0: return 50.0
        if len(prices) < period * 2: period = max(5, len(prices) // 4)
        gains = []; losses = []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i-1]
            gains.append(max(diff, 0)); losses.append(max(-diff, 0))
        if len(gains) < period: return 50.0
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        if avg_loss == 0.0: return 100.0
        for i in range(period, len(gains)):
            avg_gain = (avg_gain*(period-1)+gains[i])/period
            avg_loss = (avg_loss*(period-1)+losses[i])/period
        rs = MathLib.safe_div(avg_gain, avg_loss, 0.0)
        return 100.0 - (100.0/(1.0+rs))

    @staticmethod
    def macd_bullish(prices):
        if not prices or len(prices) < 26: return False
        ema12 = TrendEngine.ema(prices, 12); ema26 = TrendEngine.ema(prices, 26)
        if not ema12 or not ema26: return False
        macd_vals = [a - b for a, b in zip(ema12, ema26)]
        if len(macd_vals) < 9: return False
        signal = TrendEngine.ema(macd_vals, 9)
        if not signal: return False
        return macd_vals[-1] > signal[-1]

    def analyze(self, data):
        rsis = {}; macds = {}
        tf_weights = {'1m': 0.10, '5m': 0.20, '15m': 0.30, '1h': 0.40}
        for tf in ['1m','5m','15m','1h']:
            if tf in data and len(data[tf]) >= 26:
                closes = [c['c'] for c in data[tf]]
                rsis[tf] = self.rsi(closes)
                macds[tf] = self.macd_bullish(closes)
            else: rsis[tf] = 50.0; macds[tf] = False
        score = 0.0
        for tf in ['1m','5m','15m','1h']:
            w = tf_weights.get(tf, 0.25)
            r = rsis.get(tf, 50.0)
            m = macds.get(tf, False)
            if r > 60: score += 0.25 * w * 4
            elif r < 35: score -= 0.25 * w * 4
            if m: score += 0.25 * w * 4
            else: score -= 0.10 * w * 4
        score = MathLib.clamp(score, -1.0, 1.0)
        score = (score + 1.0) * 50.0
        score = MathLib.clamp(score, 0.0, 100.0)
        return {'rsi':rsis,'macd':macds,'score':score,'reason':f"Momentum score: {score:.0f}"}

# ============================================
# FASE D: VOLATILITY ENGINE
# ============================================
class VolatilityEngine:
    @staticmethod
    def atr(candles, period=14):
        if not candles or len(candles) < period+1: return 0.0
        trs = []
        for i in range(1, len(candles)):
            h=candles[i].get('h',0); l=candles[i].get('l',0); pc=candles[i-1].get('c',0)
            trs.append(max(h-l, abs(h-pc), abs(l-pc)))
        return MathLib.mean(trs[-period:]) if trs else 0.0

    @staticmethod
    def bollinger_pos(prices, period=20):
        if not prices or len(prices) < period: return "unknown"
        window = prices[-period:]
        sma = MathLib.mean(window); std = MathLib.std(window)
        if std == 0.0: return "at_middle"
        upper = sma+2.0*std; lower = sma-2.0*std
        current = prices[-1]
        if current > upper: return "above_upper"
        if current < lower: return "below_lower"
        if current > sma: return "above_mid"
        return "below_mid"

    def analyze(self, data):
        atrs = {}; bb_pos = {}
        for tf in ['1m','5m','15m','1h']:
            if tf in data and len(data[tf]) >= 20:
                candles = data[tf]
                atrs[tf] = self.atr(candles)
                closes = [c['c'] for c in candles]
                bb_pos[tf] = self.bollinger_pos(closes)
            else: atrs[tf] = 0.0; bb_pos[tf] = "unknown"
        atr_1h = atrs.get('1h',0.0); atr_1m = atrs.get('1m',0.0)
        if atr_1m > 0 and atr_1h > atr_1m*2.0: regime = VolRegime.HIGH
        elif atr_1m > 0 and atr_1h < atr_1m*1.5: regime = VolRegime.LOW
        else: regime = VolRegime.NORMAL
        expansion = MathLib.safe_div(atr_1h, atr_1m, 1.0)
        if regime == VolRegime.HIGH: vol_score = 70.0
        elif regime == VolRegime.LOW: vol_score = 30.0
        else: vol_score = 50.0
        return {'atr':atrs,'bb':bb_pos,'regime':regime,'expansion':expansion,
               'score':vol_score,'reason':f"Volatility: {regime.value} (exp={expansion:.2f})"}

# ============================================
# FASE E: LIQUIDITY ENGINE
# ============================================
class LiquidityEngine:
    def analyze(self, candles, structure):
        if not candles or len(candles) < 10:
            return {'zones_above':[],'zones_below':[],'sweep_above':False,
                   'sweep_below':False,'resting':'unknown','score':50.0,'reason':'No data'}
        highs = [c['h'] for c in candles]; lows = [c['l'] for c in candles]
        closes = [c['c'] for c in candles]; vols = [c.get('v',0) for c in candles]
        current = closes[-1] if closes else 0
        avg_vol = MathLib.mean(vols); std_highs = MathLib.std(highs)
        high_vol_zones = []
        for i, v in enumerate(vols):
            if v > avg_vol*1.5 and abs(highs[i]-lows[i]) > std_highs:
                high_vol_zones.append(closes[i])
        za = sorted([z for z in high_vol_zones if z > current]) if current > 0 else []
        zb = sorted([z for z in high_vol_zones if z < current], reverse=True) if current > 0 else []
        za = [z for z in za if z and z > 0]
        zb = [z for z in zb if z and z > 0]
        liq_above_struct = structure.get('liq_above', MathLib.max_val(highs[-10:]))
        liq_below_struct = structure.get('liq_below', MathLib.min_val(lows[-10:]))
        threshold_pct = 0.05
        if (not za or len(za) == 0) and liq_above_struct > 0 and current > 0:
            if abs(liq_above_struct - current) / current < threshold_pct:
                za = [liq_above_struct]
        if (not zb or len(zb) == 0) and liq_below_struct > 0 and current > 0:
            if abs(liq_below_struct - current) / current < threshold_pct:
                zb = [liq_below_struct]
        recent_low = MathLib.min_val(lows[-3:]); recent_high = MathLib.max_val(highs[-3:])
        sweep_threshold = 0.008
        sweep_above = any(z and z > 0 and MathLib.safe_div(abs(recent_high-z),z,999)<sweep_threshold for z in za)
        sweep_below = any(z and z > 0 and MathLib.safe_div(abs(recent_low-z),z,999)<sweep_threshold for z in zb)
        avg_above = MathLib.mean(za) if za else 0; avg_below = MathLib.mean(zb) if zb else 0
        if avg_above > 0 and current > avg_above: resting = "above_liquidity"
        elif avg_below > 0 and current < avg_below: resting = "below_liquidity"
        else: resting = "inside_range"
        liq_score = 50.0
        if sweep_above: liq_score += 25.0
        if sweep_below: liq_score += 25.0
        return {'zones_above':za[:3],'zones_below':zb[:3],
               'sweep_above':sweep_above,'sweep_below':sweep_below,
               'resting':resting,'score':MathLib.clamp(liq_score,0.0,100.0),
               'reason':f"Liquidity resting: {resting}"}

# ============================================
# FASE E: MONEY FLOW ENGINE
# ============================================
class MoneyFlowEngine:
    def analyze(self, data):
        if '1h' not in data or len(data['1h']) < 10:
            return {'obv_trend':'flat','accumulation':False,'distribution':False,'score':50.0,'reason':'No data'}
        candles = data['1h'][-20:]
        closes = [c['c'] for c in candles]; vols = [c.get('v',0) for c in candles]
        obv = 0.0; obv_vals = []
        for i in range(len(candles)):
            if i == 0: obv_vals.append(obv); continue
            if closes[i] > closes[i-1]: obv += vols[i]
            elif closes[i] < closes[i-1]: obv -= vols[i]
            obv_vals.append(obv)
        if len(obv_vals) >= 5:
            recent = obv_vals[-5:]
            rising = all(recent[i]>=recent[i-1] for i in range(1,len(recent)))
            falling = all(recent[i]<=recent[i-1] for i in range(1,len(recent)))
            obv_trend = "rising" if rising else "falling" if falling else "mixed"
        else: obv_trend = "flat"
        price_up = closes[-1] > closes[0] if len(closes)>=2 else False
        vol_clean = [v for v in vols if isinstance(v,(int,float)) and v > 0]
        vol_increasing = False
        if len(vol_clean) >= 2:
            vol_increasing = vol_clean[-1] > MathLib.mean(vol_clean[:-1])
        accumulation = price_up and vol_increasing
        distribution = (not price_up) and vol_increasing
        mf_score = 50.0
        if obv_trend == "rising": mf_score += 25.0
        elif obv_trend == "falling": mf_score -= 25.0
        if accumulation: mf_score += 25.0
        if distribution: mf_score -= 25.0
        return {'obv_trend':obv_trend,'accumulation':accumulation,'distribution':distribution,
               'score':MathLib.clamp(mf_score,0.0,100.0),
               'reason':f"OBV: {obv_trend}, Acc:{accumulation}, Dist:{distribution}"}

# ============================================
# FASE E: SQUEEZE ENGINE
# ============================================
class SqueezeEngine:
    def analyze(self, data, volatility):
        if '1h' not in data or len(data['1h']) < 40:
            return {'squeezing':False,'type':'none','energy':0.0,'breakout_dir':'none',
                   'score':0.0,'reason':'No data (need 40+ candles)'}
        candles = data['1h']; closes = [c['c'] for c in candles]
        window = closes[-20:]
        sma = MathLib.mean(window); std = MathLib.std(window)
        bb_width = MathLib.safe_div(2.0*std, sma, 999.0)
        if len(closes) >= 40:
            prev_window = closes[-40:-20]
            prev_sma = MathLib.mean(prev_window); prev_std = MathLib.std(prev_window)
            prev_bb_width = MathLib.safe_div(2.0*prev_std, prev_sma, 999.0)
        else: prev_bb_width = bb_width
        is_squeezing = False; energy = 0.0; breakout_dir = "none"
        sq_score = 0.0
        if prev_bb_width > 0 and bb_width < prev_bb_width*0.8:
            is_squeezing = True
            energy = MathLib.clamp(1.0-MathLib.safe_div(bb_width,prev_bb_width,1.0),0.0,1.0)
            ranges = []
            for i in range(5, len(closes)):
                win = closes[i-5:i]
                if len(win) == 5:
                    r = MathLib.max_val(win)-MathLib.min_val(win)
                    if r > 0: ranges.append(r)
            if ranges:
                avg_range = MathLib.mean(ranges)
                recent_range = MathLib.max_val(window[-5:])-MathLib.min_val(window[-5:])
                contraction = MathLib.safe_div(recent_range, avg_range, 0)
                if contraction > 3.2:
                    is_squeezing = False; energy = 0.0
                    breakout_dir = "fake_breakout"
                    sq_score = 15.0
            if is_squeezing:
                breakout_dir = "up" if closes[-1] > sma else "down"
                sq_score = energy * 100.0
        return {'squeezing':is_squeezing,'type':'volatility_squeeze' if is_squeezing else 'none',
               'energy':energy,'breakout_dir':breakout_dir,'score':sq_score,
               'reason':f"Squeeze: {is_squeezing}, Energy: {energy:.2f}"}

# ============================================
# FASE F: RISK ENGINE (SL liquidity primary, ATR minimum)
# ============================================
class RiskEngine:
    def calculate(self, direction, current_price, structure, liquidity, volatility):
        if current_price <= 0: return None
        if not isinstance(volatility, dict): return None

        atr_data = volatility.get('atr', {})
        atr_1h = atr_data.get('1h', 0.0) if isinstance(atr_data, dict) else 0.0

        if not atr_1h or atr_1h <= 0 or atr_1h > current_price * 0.5:
            atr_1h = current_price * 0.008

        atr_min = atr_1h * 1.0  # ATR sebagai validasi jarak minimum, bukan pengali lebar
        atr_max = atr_1h * 3.0  # ATR sebagai batas maksimum

        za = liquidity.get('zones_above',[]); zb = liquidity.get('zones_below',[])
        liq_above = structure.get('liq_above', 0)
        liq_below = structure.get('liq_below', 0)

        if direction == SignalDir.LONG:
            entry = current_price

            # SL utama: liquidity below
            if zb and zb[0] > 0:
                sl = zb[0] * 0.995
            elif liq_below > 0:
                sl = liq_below * 0.995
            else:
                sl = entry - atr_1h * 1.5

            # Validasi jarak SL minimum dan maksimum
            sl_dist = entry - sl
            if sl_dist < atr_min:
                sl = entry - atr_min
            elif sl_dist > atr_max:
                sl = entry - atr_max

            # TP utama: liquidity above
            if za and za[0] > 0:
                tp = za[0] * 0.995
            elif liq_above > 0:
                tp = liq_above * 0.995
            else:
                tp = entry + atr_1h * 2.0

            risk = entry - sl; reward = tp - entry

        elif direction == SignalDir.SHORT:
            entry = current_price

            # SL utama: liquidity above
            if za and za[0] > 0:
                sl = za[0] * 1.005
            elif liq_above > 0:
                sl = liq_above * 1.005
            else:
                sl = entry + atr_1h * 1.5

            # Validasi jarak SL minimum dan maksimum
            sl_dist = sl - entry
            if sl_dist < atr_min:
                sl = entry + atr_min
            elif sl_dist > atr_max:
                sl = entry + atr_max

            # TP utama: liquidity below
            if zb and zb[0] > 0:
                tp = zb[0] * 1.005
            elif liq_below > 0:
                tp = liq_below * 1.005
            else:
                tp = entry - atr_1h * 2.0

            risk = sl - entry; reward = entry - tp

        else:
            return None

        if risk <= 0 or reward <= 0: return None
        rr = MathLib.safe_div(reward, risk, 0.0)
        if rr < 1.0: return None

        regime = volatility.get('regime', VolRegime.NORMAL)
        if regime == VolRegime.HIGH: pos_factor = 0.5
        elif regime == VolRegime.LOW: pos_factor = 1.5
        else: pos_factor = 1.0
        if rr >= 3.0: pos_factor *= 1.2
        elif rr < 1.5: pos_factor *= 0.8

        return {'entry':entry,'sl':sl,'tp':tp,'risk':risk,'reward':reward,'rr':rr,
               'pos_factor':pos_factor,
               'sl_reason':'Liquidity primary, ATR validated' if direction==SignalDir.LONG else 'Liquidity primary, ATR validated',
               'tp_reason':'Before liquidity target','reason':f"RR=1:{rr:.2f}"}

# ============================================
# ENTRY TIMING FILTER (EMA200 with 250 candles)
# ============================================
class EntryTimingFilter:
    def filter(self, price_data, direction):
        if '15m' not in price_data or len(price_data['15m']) < 200:
            return {'status':'NO_DATA','reason':'Insufficient 15m data','distance':0}

        closes = [c['c'] for c in price_data['15m']]
        price = closes[-1]

        ema21 = TrendEngine.ema(closes, 21)
        ema50 = TrendEngine.ema(closes, 50)
        ema200 = TrendEngine.ema(closes, 200)

        if not ema21 or not ema50 or not ema200:
            return {'status':'NO_DATA','reason':'EMA calculation failed','distance':0}

        e21 = ema21[-1]
        e50 = ema50[-1]
        e200 = ema200[-1]

        if direction == SignalDir.LONG:
            if e50 < e200:
                return {'status':'WEAK','reason':'EMA50 below EMA200','distance':0}

            distance = ((price - e21) / e21) * 100

            if price >= e21 and distance <= 2.5:
                return {'status':'GOOD','reason':f'Price {distance:.1f}% above EMA21','distance':distance}
            elif price > e21 and distance <= 5:
                return {'status':'WAIT_PULLBACK','reason':f'Price {distance:.1f}% above EMA21, wait pullback','distance':distance}
            elif price < e21:
                return {'status':'WAIT_PULLBACK','reason':'Price below EMA21, wait confirmation','distance':distance}
            else:
                return {'status':'LATE','reason':f'Price extended {distance:.1f}% above EMA21','distance':distance}

        elif direction == SignalDir.SHORT:
            if e50 > e200:
                return {'status':'WEAK','reason':'EMA50 above EMA200','distance':0}

            distance = ((e21 - price) / e21) * 100

            if price <= e21 and distance <= 2.5:
                return {'status':'GOOD','reason':f'Price {distance:.1f}% below EMA21','distance':distance}
            elif price < e21 and distance <= 5:
                return {'status':'WAIT_PULLBACK','reason':f'Price {distance:.1f}% below EMA21, wait pullback','distance':distance}
            elif price > e21:
                return {'status':'WAIT_PULLBACK','reason':'Price above EMA21, wait rejection','distance':distance}
            else:
                return {'status':'LATE','reason':f'Price extended {distance:.1f}% below EMA21','distance':distance}

        return {'status':'NO_DATA','reason':'Unknown direction','distance':0}

# ============================================
# FASE G: TELEGRAM OUTPUT
# ============================================
class TelegramOutput:
    def __init__(self):
        self.free_token = TELEGRAM_FREE_TOKEN; self.free_chat = TELEGRAM_FREE_CHAT_ID
        self.vip_token = TELEGRAM_VIP_TOKEN; self.vip_chat = TELEGRAM_VIP_CHAT_ID
        self.msg_count = 0; self.msg_minute_start = time.time()

    def _rate_limit(self):
        self.msg_count += 1
        if self.msg_count >= 20:
            elapsed = time.time() - self.msg_minute_start
            if elapsed < 60: time.sleep(60 - elapsed)
            self.msg_count = 0; self.msg_minute_start = time.time()

    def _send(self, token, chat, text):
        if not token or not chat: return False
        if "ISI_TOKEN" in token: return False
        self._rate_limit()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            parts = []; buf = ""
            for line in text.split("\n"):
                if len(buf.encode('utf-8')) + len(line.encode('utf-8')) > 3500:
                    parts.append(buf)
                    buf = line + "\n"
                else:
                    buf += line + "\n"
            if buf: parts.append(buf)
            for part in parts:
                for attempt in range(2):
                    try:
                        r = requests.post(url, json={"chat_id":chat,"text":part,
                                    "parse_mode":"HTML","disable_web_page_preview":True}, timeout=10)
                        if r.status_code == 200: break
                    except requests.exceptions.RequestException:
                        if attempt == 0: time.sleep(2)
                time.sleep(0.5)
            return True
        except Exception as e:
            logger.err("Telegram send failed", e)
            return False

    def send_free(self, signals):
        if not self.free_token or not self.free_chat: return
        if "ISI_TOKEN" in self.free_token: return
        all_s = signals[:5]
        if not all_s:
            msg = f"<b>📭 NO VALID SIGNAL FOUND</b>\n\n⏰ Next: <b>{(datetime.now()+timedelta(hours=1)).strftime('%H:%M')}</b>"
            self._send(self.free_token, self.free_chat, msg); return
        msg = f"<b>🎯 DSS SWING SIGNALS</b>\n⏰ <b>{datetime.now().strftime('%H:%M | %d %b %Y')}</b>\n\n"
        msg += "<b>₿ CRYPTO</b>\n"
        for s in all_s:
            emoji = "🟢" if s['dir']=='LONG' else "🔴"
            msg += f"{emoji} <b>{s['pair']}</b>  ⭐<b>{s['score']:.0f}</b> | <b>{s['dir']}</b> | 📊<b>{s.get('struct','?')}</b>\n"
        msg += "\n"
        best = all_s[0] if all_s else None
        if best:
            msg += f"🔥 <b>TOP:</b> <b>{best['pair']}</b> | <b>{best['dir']}</b> | <b>{best['score']:.0f}</b>/100\n"
            if best.get('risk') is not None:
                msg += f"   Est. RR: <b>1:{best['risk']['rr']:.2f}</b>\n"
        msg += f"\n🔒 <b>VIP:</b> Entry • SL • TP • Full Analysis\n💬 Contact admin\n\n⏰ Next: <b>{(datetime.now()+timedelta(hours=1)).strftime('%H:%M')}</b>"
        self._send(self.free_token, self.free_chat, msg)
        logger.info(f"Sent {len(all_s)} FREE signals")

    def send_vip(self, signals):
        if not self.vip_token or not self.vip_chat: return
        if "ISI_TOKEN" in self.vip_token: return
        all_s = signals[:5]
        if not all_s:
            msg = f"<b>🔍 DSS VIP</b>\n\n<b>📭 NO SIGNAL</b>\n\n⏰ Next: <b>{(datetime.now()+timedelta(hours=1)).strftime('%H:%M')}</b>"
            self._send(self.vip_token, self.vip_chat, msg); return
        msg = f"<b>🔥 DSS VIP SIGNALS</b>\n⏰ <b>{datetime.now().strftime('%H:%M | %d %b %Y')}</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        self._send(self.vip_token, self.vip_chat, msg)
        msg = "<b>₿ CRYPTO</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        for s in all_s:
            emoji = "🟢" if s['dir']=='LONG' else "🔴"
            msg += f"{emoji} <b>{s['pair']}</b> | <b>{s['dir']}</b> | ⭐<b>{s['score']:.0f}</b>/100\n\n"
            if s.get('risk') is not None:
                r = s['risk']
                if r and 'tp' in r and 'entry' in r:
                    if s['dir'] == 'LONG':
                        tp2 = r['tp'] + (r['tp'] - r['entry']) * 0.5
                    else:
                        tp2 = r['tp'] - (r['entry'] - r['tp']) * 0.5

                    price = r['entry']
                    fmt = '{:.8f}' if (price < 0.01 and price > 0) else '{:.4f}'

                    msg += (
                        f"📍 <b>ENTRY:</b> <b>{fmt.format(r['entry'])}</b>\n"
                        f"🛑 <b>SL:</b> <b>{fmt.format(r['sl'])}</b>\n"
                        f"🎯 <b>TP1:</b> <b>{fmt.format(r['tp'])}</b>\n"
                        f"🎯 <b>TP2:</b> <b>{fmt.format(tp2)}</b>\n"
                        f"💰 <b>RR:</b> <b>1:{r['rr']:.2f}</b>\n\n"
                    )
            if s.get('entry_status'):
                status = s.get('entry_status','?')
                if status == 'GOOD':
                    msg += f"⏳ <b>Entry:</b> GOOD\n   Boleh mencari entry sekarang\n\n"
                elif status == 'WAIT_PULLBACK':
                    msg += f"⏳ <b>Entry:</b> WAIT_PULLBACK\n📌 <b>ACTION:</b> JANGAN ENTRY SEKARANG\n🎯 <b>WAIT:</b> {s.get('entry_reason','?')}\n\n"
                elif status == 'WEAK':
                    msg += f"⏳ <b>Entry:</b> WEAK\n   {s.get('entry_reason','?')}\n\n"
                else:
                    msg += f"⏳ <b>Entry:</b> {status}\n   {s.get('entry_reason','?')}\n\n"
            msg += f"📊 <b>{s.get('struct_reason','?')}</b>\n📈 <b>{s.get('trend_reason','?')}</b>\n⚡ <b>{s.get('mom_reason','?')}</b>\n🌊 <b>{s.get('vol_reason','?')}</b>\n💧 <b>{s.get('liq_reason','?')}</b>\n💰 <b>{s.get('mf_reason','?')}</b>\n🔨 <b>{s.get('sq_reason','?')}</b>\n\n"
        self._send(self.vip_token, self.vip_chat, msg)
        logger.info(f"Sent {len(all_s)} VIP signals")

# ============================================
# FASE G: GITHUB SYNC
# ============================================
class GitHubSync:
    def __init__(self):
        self.token = GITHUB_TOKEN; self.repo = GITHUB_REPO; self.branch = GITHUB_BRANCH

    def sync(self, signals):
        if not self.token or not self.repo: return
        if "ISI_GITHUB" in self.token: return
        output = {'timestamp':datetime.now().isoformat(),'system':'DSS-Swing-Interday-Crypto',
                 'total_signals':len(signals),'signals':[]}
        for s in signals:
            output['signals'].append({'pair':s.get('pair'),'direction':s.get('dir'),
                     'score':s.get('score'),'structure':s.get('struct'),
                     'trend':s.get('trend'),'risk':s.get('risk'),
                     'reasoning':s.get('struct_reason','')})
        try:
            with open('dss_signals.json','w') as f: json.dump(output, f, indent=2)
            logger.info("Saved local JSON")
        except Exception as e: logger.err("Local JSON save failed", e); return
        for attempt in range(2):
            try:
                content = json.dumps(output, indent=2)
                content_b64 = base64.b64encode(content.encode()).decode()
                url = f"https://api.github.com/repos/{self.repo}/contents/signals/dss_signals.json"
                headers = {'Authorization':f'Bearer {self.token}','Accept':'application/vnd.github.v3+json'}
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code in [403, 429]:
                    logger.warn(f"GitHub rate limited, retry after sleep")
                    time.sleep(10)
                    continue
                payload = {'message':f'DSS Update {datetime.now().isoformat()}',
                          'content':content_b64,'branch':self.branch}
                sha = None
                if resp.status_code == 200: sha = resp.json().get('sha')
                if sha: payload['sha'] = sha
                put_resp = requests.put(url, headers=headers, json=payload, timeout=10)
                if put_resp.status_code in [200, 201]:
                    logger.info("GitHub push OK"); return
                elif put_resp.status_code == 409:
                    logger.warn("GitHub conflict, retrying")
                    time.sleep(3)
                else:
                    logger.warn(f"GitHub push failed: {put_resp.status_code}")
            except Exception as e:
                logger.err(f"GitHub attempt {attempt+1} failed", e)
                if attempt == 0: time.sleep(3)

# ============================================
# DSS MAIN SYSTEM
# ============================================
class DSSSystem:
    def __init__(self):
        self.fetcher = DataFetcher()
        self.struct_eng = StructureEngine()
        self.trend_eng = TrendEngine()
        self.mom_eng = MomentumEngine()
        self.vol_eng = VolatilityEngine()
        self.liq_eng = LiquidityEngine()
        self.mf_eng = MoneyFlowEngine()
        self.sq_eng = SqueezeEngine()
        self.risk_eng = RiskEngine()
        self.entry_filter = EntryTimingFilter()
        self.tg = TelegramOutput()
        self.gh = GitHubSync()

    def save_signal_history(self, signals):
        history_file = "signal_history.json"
        try:
            if os.path.exists(history_file):
                with open(history_file, "r") as f:
                    history = json.load(f)
            else:
                history = []
            history.extend(signals)
            with open(history_file, "w") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            logger.err("History save failed", e)

    def analyze_asset(self, pair, price_data):
        if not price_data: logger.skip(pair, "No price data"); return None
        if '1h' not in price_data or len(price_data['1h']) < 4: logger.skip(pair, "Missing 1h data"); return None

        struct = self.struct_eng.analyze(price_data['1h'])
        trend = self.trend_eng.analyze(price_data)
        mom = self.mom_eng.analyze(price_data)
        vol = self.vol_eng.analyze(price_data)
        liq = self.liq_eng.analyze(price_data.get('1h',[]), struct)
        mf = self.mf_eng.analyze(price_data)
        sq = self.sq_eng.analyze(price_data, vol)

        struct_w=0.25; trend_w=0.25; mom_w=0.20; liq_w=0.10; mf_w=0.10; vol_w=0.05; sq_w=0.05
        score = (struct.get('score',0.0)*struct_w + trend.get('score',0.0)*trend_w +
                mom.get('score',50.0)*mom_w + liq.get('score',50.0)*liq_w +
                mf.get('score',50.0)*mf_w + vol.get('score',50.0)*vol_w +
                sq.get('score',0.0)*sq_w)

        if struct['valid']:
            if (struct['type']==StructType.BULL and trend['dir']==TrendDir.UP): score += 5.0
            elif (struct['type']==StructType.BEAR and trend['dir']==TrendDir.DOWN): score += 5.0
            else: score -= 8.0

        score = MathLib.clamp(score, 0.0, 100.0)
        if score < 40: return None

        threshold = 62
        if score >= threshold and trend['dir']==TrendDir.UP and struct['type']==StructType.BULL:
            direction = SignalDir.LONG
        elif score >= threshold and trend['dir']==TrendDir.DOWN and struct['type']==StructType.BEAR:
            direction = SignalDir.SHORT
        else:
            direction = SignalDir.NONE

        if direction == SignalDir.NONE:
            return None

        # Entry timing filter
        entry_timing = self.entry_filter.filter(price_data, direction)

        if entry_timing['status'] == 'LATE':
            logger.nosig(pair, f"Late entry: {entry_timing['reason']}")
            return None

        # Entry price dari 15m
        current_price = price_data['15m'][-1]['c'] if price_data.get('15m') else 0

        risk = None
        if current_price > 0:
            risk = self.risk_eng.calculate(direction, current_price, struct, liq, vol)
            if risk is None: direction = SignalDir.NONE
        if direction == SignalDir.NONE: return None

        return {'pair':pair,'market':'crypto','dir':direction.value,'score':score,
               'struct':struct['type'].value,'struct_reason':struct['reason'],
               'trend':trend['dir'].value,'trend_reason':trend['reason'],
               'mom_reason':mom['reason'],'vol_reason':vol['reason'],
               'liq_reason':liq['reason'],'mf_reason':mf['reason'],
               'sq_reason':sq['reason'],'risk':risk,
               'entry_status':entry_timing['status'],
               'entry_reason':entry_timing['reason'],
               'ts':datetime.now().isoformat()}

    def run_cycle(self):
        logger.skip_count=0; logger.err_count=0; logger.sig_count=0
        logger.info("="*50); logger.info(f"CYCLE START: {datetime.now()}"); logger.info("="*50)
        market_data = self.fetcher.fetch_all()
        logger.info(f"CRYPTO COUNT: {len(market_data['crypto'])}")
        all_signals = []; cycle_start = time.time()

        logger.info("--- Analyzing CRYPTO ---")
        for sym, data in market_data['crypto'].items():
            if time.time()-cycle_start > 90: logger.warn("Timeout"); break
            sig = self.analyze_asset(sym, data)
            if sig: all_signals.append(sig); logger.sig(sym, sig['dir'], sig['score'])
            else: logger.nosig(sym, "No setup")

        all_signals.sort(key=lambda x: x['score'], reverse=True)

        self.save_signal_history(all_signals)

        logger.info(f"=== {len(all_signals)} signals | {logger.skip_count} skipped | {logger.err_count} errors ===")
        self.tg.send_free(all_signals); self.tg.send_vip(all_signals); self.gh.sync(all_signals)
        try:
            with open('dss_signals.json','w') as f: json.dump({'timestamp':datetime.now().isoformat(),'total':len(all_signals),'signals':all_signals}, f, indent=2)
        except Exception as e: logger.err("Local save failed", e)

        # Git auto commit & push
        try:
            os.system("cd /data/data/com.termux/files/home/Dss_System2 && git add .")
            os.system("cd /data/data/com.termux/files/home/Dss_System2 && git commit -m 'auto update signal'")
            os.system("cd /data/data/com.termux/files/home/Dss_System2 && git push origin system2")
            logger.info("Git auto push OK")
        except Exception as e:
            logger.err("Git auto push failed", e)

        logger.info("CYCLE COMPLETE"); logger.info("="*50)

# ============================================
# FASE A: MAIN LOOP
# ============================================
def main():
    print("""╔══════════════════════════════════╗
║ DSS SWING INTERDAY v7.7          ║
║ CRYPTO ONLY + PRIORITY FIX       ║
╚══════════════════════════════════╝""")
    print("[*] Running every 1 hour...\n")
    dss = DSSSystem()
    try: dss.run_cycle()
    except Exception as e: logger.err("Initial cycle failed", e)
    while True:
        try:
            next_run = datetime.now() + timedelta(hours=1)
            print(f"\n[.] Next: {next_run.strftime('%H:%M')}")
            time.sleep(3600); dss.run_cycle()
        except KeyboardInterrupt: print("\n[*] Done"); break
        except Exception as e: logger.err("Cycle error", e); time.sleep(300)

if __name__ == "__main__":
    main()
