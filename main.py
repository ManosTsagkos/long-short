"""
main.py - Kraken Version

BTC composite signal bot για WunderTrading με Kraken API.
Το Kraken δεν έχει Futures data (OI, funding, etc.), 
οπότε χρησιμοποιούμε μόνο price-based indicators.
"""

import os
import json
import time
import logging
import datetime as dt
from pathlib import Path

import requests
import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

SYMBOL = os.getenv("BUD_SYMBOL", "XBTUSD")  # Kraken uses XBTUSD
INTERVAL = os.getenv("BUD_INTERVAL", "240")  # 240 minutes = 4h
RECONFIRM_BARS = int(os.getenv("BUD_RECONFIRM_BARS", "1"))
POLL_SECONDS = int(os.getenv("BUD_POLL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("BUD_STATE_FILE", "bud_bot_state.json"))

WUNDER_WEBHOOK_URL = os.getenv("WUNDER_WEBHOOK_URL", "https://wtalerts.com/bot/trading_view")
WUNDER_LONG_CODE = os.getenv("WUNDER_LONG_CODE", "CHANGE-THIS-ENTER-LONG-COMMENT")
WUNDER_SHORT_CODE = os.getenv("WUNDER_SHORT_CODE", "CHANGE-THIS-ENTER-SHORT-COMMENT")
WUNDER_EXIT_CODE = os.getenv("WUNDER_EXIT_CODE", "CHANGE-THIS-EXIT-ALL-COMMENT")

ORDER_TYPE = os.getenv("BUD_ORDER_TYPE", "market")
AMOUNT_PER_TRADE_TYPE = os.getenv("BUD_AMOUNT_TYPE", "percents")
AMOUNT_PER_TRADE = float(os.getenv("BUD_AMOUNT_PER_TRADE", "0.1"))
LEVERAGE = float(os.getenv("BUD_LEVERAGE", "2"))
STOP_LOSS_PCT = float(os.getenv("BUD_STOP_LOSS_PCT", "0.015"))
TAKE_PROFIT_PCT = float(os.getenv("BUD_TAKE_PROFIT_PCT", "0.03"))

DRY_RUN = os.getenv("BUD_DRY_RUN", "true").lower() == "true"

KRAKEN_API = "https://api.kraken.com/0/public"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("kraken_bot")


# --------------------------------------------------------------------------
# DATA FETCHING (Kraken)
# --------------------------------------------------------------------------

def _get(endpoint, params, retries=3):
    url = f"{KRAKEN_API}/{endpoint}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("error"):
                raise RuntimeError(f"Kraken API error: {data['error']}")
            return data["result"]
        except requests.RequestException as e:
            log.warning(f"GET {endpoint} failed ({e}); retry {attempt + 1}/{retries}")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {endpoint} after {retries} retries")


def get_klines(limit=200):
    """
    Kraken OHLC format: [[time, open, high, low, close, vwap, volume, count], ...]
    """
    result = _get("OHLC", {"pair": SYMBOL, "interval": INTERVAL, "since": 0})
    
    # Get the data array (key is the pair name, e.g., "XXBTZUSD" or "XBTUSD")
    pair_key = list(result.keys())[0]
    raw = result[pair_key]
    
    # Take last 'limit' records
    raw = raw[-limit:] if len(raw) > limit else raw
    
    df = pd.DataFrame(raw, columns=[
        "time", "open", "high", "low", "close", "vwap", "volume", "count"
    ])
    
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    
    df["open_time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df["close_time"] = df["open_time"] + pd.Timedelta(minutes=int(INTERVAL))
    
    return df


def get_order_book_imbalance():
    """
    Get order book depth from Kraken and calculate imbalance.
    Returns: -1 to +1 (negative = more asks, positive = more bids)
    """
    result = _get("Depth", {"pair": SYMBOL, "count": 100})
    pair_key = list(result.keys())[0]
    data = result[pair_key]
    
    bids = pd.DataFrame(data["bids"], columns=["price", "volume", "timestamp"])
    asks = pd.DataFrame(data["asks"], columns=["price", "volume", "timestamp"])
    
    bids["price"] = bids["price"].astype(float)
    bids["volume"] = bids["volume"].astype(float)
    asks["price"] = asks["price"].astype(float)
    asks["volume"] = asks["volume"].astype(float)
    
    mid = (bids["price"].iloc[0] + asks["price"].iloc[0]) / 2
    band_pct = 0.005  # 0.5% band
    band = mid * band_pct
    
    bid_depth = bids[bids["price"] >= mid - band]["volume"].sum()
    ask_depth = asks[asks["price"] <= mid + band]["volume"].sum()
    total = bid_depth + ask_depth
    
    return 0.0 if total == 0 else (bid_depth - ask_depth) / total


# --------------------------------------------------------------------------
# INDICATORS
# --------------------------------------------------------------------------

def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series, fast=12, slow=26, signal=9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df, period=14):
    """Average True Range for volatility"""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


# --------------------------------------------------------------------------
# PILLAR VOTES (Simplified for Kraken - no Futures data)
# --------------------------------------------------------------------------

def momentum_vote(df):
    """
    Technical momentum indicators only.
    Returns: -1 (bearish), 0 (neutral), +1 (bullish)
    """
    close = df["close"]
    
    # EMA trend
    ema_fast = ema(close, 21).iloc[-1]
    ema_slow = ema(close, 55).iloc[-1]
    ema_vote = 1 if ema_fast > ema_slow else -1
    
    # MACD histogram
    _, _, hist = macd(close)
    macd_vote = 1 if hist.iloc[-1] > 0 else -1
    
    # RSI
    rsi_val = rsi(close).iloc[-1]
    if rsi_val > 60:
        rsi_vote = 1
    elif rsi_val < 40:
        rsi_vote = -1
    else:
        rsi_vote = 0
    
    votes = [ema_vote, macd_vote, rsi_vote]
    nonzero = [v for v in votes if v != 0]
    
    if len(nonzero) < 2:
        return 0
    
    score = sum(nonzero)
    agree = sum(1 for v in nonzero if v == (1 if score > 0 else -1))
    
    return (1 if score > 0 else -1) if agree >= 2 else 0


def pressure_vote(df, ob_imbalance):
    """
    Price-based pressure + order book imbalance.
    No OI or taker data on Kraken spot.
    """
    votes = []
    
    # Volume trend (increasing volume on trend = confirmation)
    vol_sma = df["volume"].rolling(20).mean()
    recent_vol = df["volume"].iloc[-3:].mean()
    vol_trend = 1 if recent_vol > vol_sma.iloc[-1] else -1
    
    # Price trend direction
    price_change = (df["close"].iloc[-1] - df["close"].iloc[-20]) / df["close"].iloc[-20]
    if price_change > 0.02:  # +2%
        votes.append(1)
    elif price_change < -0.02:  # -2%
        votes.append(-1)
    else:
        votes.append(0)
    
    # Volume confirmation
    votes.append(vol_trend if abs(price_change) > 0.01 else 0)
    
    # Order book imbalance
    votes.append((1 if ob_imbalance > 0 else -1) if abs(ob_imbalance) > 0.1 else 0)
    
    nonzero = [v for v in votes if v != 0]
    if len(nonzero) < 2:
        return 0
    
    score = sum(nonzero)
    agree = sum(1 for v in nonzero if v == (1 if score > 0 else -1))
    
    return (1 if score > 0 else -1) if agree / len(nonzero) >= 0.6 else 0


def liquidity_vote(df):
    """
    Price-based liquidity analysis (support/resistance levels).
    No funding rate on Kraken spot.
    """
    votes = []
    
    # Proximity to recent swing high/low
    lookback = df.tail(30)
    swing_high, swing_low = lookback["high"].max(), lookback["low"].min()
    last_close = df["close"].iloc[-1]
    rng = swing_high - swing_low
    
    if rng > 0:
        pos = (last_close - swing_low) / rng
        # Near resistance = bearish (potential rejection)
        # Near support = bullish (potential bounce)
        if pos > 0.85:
            votes.append(-1)
        elif pos < 0.15:
            votes.append(1)
        else:
            votes.append(0)
    else:
        votes.append(0)
    
    # Volatility regime (ATR)
    atr_val = atr(df).iloc[-1]
    atr_sma = atr(df).rolling(20).mean().iloc[-1]
    # High volatility = uncertainty = neutral/avoid
    if atr_val > atr_sma * 1.5:
        votes.append(0)  # Too volatile, no edge
    else:
        votes.append(0)
    
    nonzero = [v for v in votes if v != 0]
    if not nonzero:
        return 0
    
    score = sum(nonzero)
    return 1 if score > 0 else (-1 if score < 0 else 0)


def composite_signal(m, p, l):
    """Require 2 of 3 pillars to agree."""
    nonzero = [v for v in (m, p, l) if v != 0]
    if len(nonzero) < 2:
        return "WAIT"
    
    score = sum(nonzero)
    agree = sum(1 for v in nonzero if v == (1 if score > 0 else -1))
    
    if agree >= 2:
        return "LONG" if score > 0 else "SHORT"
    return "WAIT"


# --------------------------------------------------------------------------
# STATE & WEBHOOK (unchanged)
# --------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_closed_candle": None, "pending_direction": None, "pending_count": 0, "active_position": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_wunder_signal(direction):
    if direction == "LONG":
        code = WUNDER_LONG_CODE
    elif direction == "SHORT":
        code = WUNDER_SHORT_CODE
    else:
        code = WUNDER_EXIT_CODE

    payload = {
        "code": code,
        "orderType": ORDER_TYPE,
        "amountPerTradeType": AMOUNT_PER_TRADE_TYPE,
        "amountPerTrade": AMOUNT_PER_TRADE,
        "leverage": LEVERAGE,
    }

    if direction in ("LONG", "SHORT"):
        payload["stopLoss"] = {"priceDeviation": STOP_LOSS_PCT}
        payload["takeProfits"] = [{"priceDeviation": TAKE_PROFIT_PCT, "portfolio": 1}]
        payload["reduceOnly"] = False
    else:
        payload["reduceOnly"] = True

    log.info(f"[{'DRY RUN' if DRY_RUN else 'LIVE'}] {direction} payload: {json.dumps(payload)}")

    if DRY_RUN:
        return

    try:
        r = requests.post(WUNDER_WEBHOOK_URL, json=payload, timeout=10)
        log.info(f"WunderTrading response [{r.status_code}]: {r.text}")
    except requests.RequestException as e:
        log.error(f"Failed to reach WunderTrading webhook: {e}")


# --------------------------------------------------------------------------
# MAIN LOOP
# --------------------------------------------------------------------------

def run_once(state):
    # Fetch klines from Kraken
    try:
        closed = get_klines(limit=200)
        log.info(f"Fetched {len(closed)} candles from Kraken")
    except Exception as e:
        log.error(f"Failed to fetch klines: {e}")
        return state
    
    if closed is None or len(closed) == 0:
        log.warning("No closed data available")
        return state
    
    last_closed_time = closed["close_time"].iloc[-1].isoformat()

    if state["last_closed_candle"] == last_closed_time:
        return state  # already evaluated this candle

    log.info(f"New closed {INTERVAL}m candle @ {last_closed_time} — evaluating pillars...")

    # Get order book data
    try:
        ob_imb = get_order_book_imbalance()
    except Exception as e:
        log.warning(f"Failed to get order book: {e}, using 0")
        ob_imb = 0.0

    m = momentum_vote(closed)
    p = pressure_vote(closed, ob_imb)
    l = liquidity_vote(closed)
    direction = composite_signal(m, p, l)

    log.info(f"momentum={m:+d} pressure={p:+d} liquidity={l:+d} -> {direction} "
              f"(close={closed['close'].iloc[-1]:.2f}, ob_imb={ob_imb:+.3f})")

    if direction == state["pending_direction"]:
        state["pending_count"] += 1
    else:
        state["pending_direction"] = direction
        state["pending_count"] = 1

    if direction in ("LONG", "SHORT") and state["pending_count"] >= RECONFIRM_BARS:
        if state["active_position"] != direction:
            send_wunder_signal(direction)
            state["active_position"] = direction
    elif direction == "WAIT" and state["active_position"] and state["pending_count"] >= RECONFIRM_BARS:
        send_wunder_signal("EXIT")
        state["active_position"] = None

    state["last_closed_candle"] = last_closed_time
    save_state(state)
    return state


def main():
    log.info(f"Starting Kraken composite {SYMBOL} {INTERVAL}m signal bot "
              f"(reconfirm_bars={RECONFIRM_BARS}, dry_run={DRY_RUN})")
    state = load_state()
    while True:
        try:
            state = run_once(state)
        except Exception as e:
            log.exception(f"Error in main loop: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
