"""
main.py - Kraken Version (Fixed)

BTC composite signal bot for WunderTrading with Kraken API.
Fixes: incomplete candle evaluation, inefficient polling, error handling.
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

SYMBOL = os.getenv("BUD_SYMBOL", "XBTUSD")
INTERVAL_MINUTES = int(os.getenv("BUD_INTERVAL", "240"))
RECONFIRM_BARS = int(os.getenv("BUD_RECONFIRM_BARS", "1"))
STATE_FILE = Path(os.getenv("BUD_STATE_FILE", "bud_bot_state.json"))

WUNDER_WEBHOOK_URL = os.getenv("WUNDER_WEBHOOK_URL", "https://wtalerts.com/bot/trading_view")
WUNDER_LONG_CODE = os.getenv("WUNDER_LONG_CODE")
WUNDER_SHORT_CODE = os.getenv("WUNDER_SHORT_CODE")
WUNDER_EXIT_CODE = os.getenv("WUNDER_EXIT_CODE")

ORDER_TYPE = os.getenv("BUD_ORDER_TYPE", "market")
AMOUNT_PER_TRADE_TYPE = os.getenv("BUD_AMOUNT_TYPE", "percents")
AMOUNT_PER_TRADE = float(os.getenv("BUD_AMOUNT_PER_TRADE", "0.1"))
LEVERAGE = float(os.getenv("BUD_LEVERAGE", "6"))
STOP_LOSS_PCT = float(os.getenv("BUD_STOP_LOSS_PCT", "0.015"))
TAKE_PROFIT_PCT = float(os.getenv("BUD_TAKE_PROFIT_PCT", "0.03"))

DRY_RUN = os.getenv("BUD_DRY_RUN", "true").lower() == "true"

KRAKEN_API = "https://api.kraken.com/0/public"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("kraken_bot")


# --------------------------------------------------------------------------
# DATA FETCHING (Kraken)
# --------------------------------------------------------------------------

def _get(endpoint: str, params: dict, retries: int = 3):
    """Make GET request to Kraken API with retry logic."""
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


def get_klines(limit: int = 200) -> pd.DataFrame:
    """
    Fetch OHLC data from Kraken.
    
    IMPORTANT: Kraken returns the currently-forming candle as the last entry.
    We drop it to avoid evaluating incomplete candles.
    """
    result = _get("OHLC", {"pair": SYMBOL, "interval": INTERVAL_MINUTES, "since": 0})
    
    # Kraken uses different pair keys (e.g., "XXBTZUSD" for XBTUSD)
    pair_key = list(result.keys())[0]
    raw = result[pair_key]
    
    # Convert to DataFrame
    df = pd.DataFrame(raw, columns=[
        "time", "open", "high", "low", "close", "vwap", "volume", "count"
    ])
    
    # Convert types
    for col in ["open", "high", "low", "close", "vwap", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    
    df["open_time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df["close_time"] = df["open_time"] + pd.Timedelta(minutes=INTERVAL_MINUTES)
    
    # CRITICAL FIX: Drop the last candle (currently forming/incomplete)
    df = df.iloc[:-1]
    
    # Now apply limit
    if len(df) > limit:
        df = df.iloc[-limit:]
    
    return df.reset_index(drop=True)


def get_order_book_imbalance(depth: int = 100, band_pct: float = 0.005) -> float:
    """
    Calculate order book imbalance within a band around mid price.
    Returns: -1 (heavy asks) to +1 (heavy bids), 0 = balanced
    """
    try:
        result = _get("Depth", {"pair": SYMBOL, "count": depth})
        pair_key = list(result.keys())[0]
        data = result[pair_key]
        
        bids = pd.DataFrame(data["bids"], columns=["price", "volume", "timestamp"])
        asks = pd.DataFrame(data["asks"], columns=["price", "volume", "timestamp"])
        
        for col in ["price", "volume"]:
            bids[col] = pd.to_numeric(bids[col], errors="coerce")
            asks[col] = pd.to_numeric(asks[col], errors="coerce")
        
        mid = (bids["price"].iloc[0] + asks["price"].iloc[0]) / 2
        band = mid * band_pct
        
        bid_depth = bids[bids["price"] >= mid - band]["volume"].sum()
        ask_depth = asks[asks["price"] <= mid + band]["volume"].sum()
        total = bid_depth + ask_depth
        
        return (bid_depth - ask_depth) / total if total > 0 else 0.0
        
    except Exception as e:
        log.warning(f"Order book fetch failed: {e}")
        return 0.0


# --------------------------------------------------------------------------
# INDICATORS (vectorized pandas operations)
# --------------------------------------------------------------------------

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range for volatility measurement."""
    high, low, close = df["high"], df["low"], df["close"]
    
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


# --------------------------------------------------------------------------
# SIGNAL LOGIC (3-Pillar Composite)
# --------------------------------------------------------------------------

def momentum_vote(df: pd.DataFrame) -> int:
    """
    Technical momentum: EMA trend, MACD histogram, RSI.
    Requires 2 of 3 indicators to agree.
    """
    close = df["close"]
    
    # EMA trend (21 vs 55)
    ema_fast = ema(close, 21).iloc[-1]
    ema_slow = ema(close, 55).iloc[-1]
    ema_vote = 1 if ema_fast > ema_slow else -1
    
    # MACD histogram
    _, _, hist = macd(close)
    macd_vote = 1 if hist.iloc[-1] > 0 else -1
    
    # RSI with thresholds
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


def pressure_vote(df: pd.DataFrame, ob_imbalance: float) -> int:
    """
    Market pressure: price momentum, volume confirmation, order book.
    """
    votes = []
    
    # Price momentum (20-period change)
    price_change = df["close"].iloc[-1] / df["close"].iloc[-20] - 1
    if price_change > 0.02:
        votes.append(1)
    elif price_change < -0.02:
        votes.append(-1)
    else:
        votes.append(0)
    
    # Volume confirmation (trending volume)
    vol_sma = df["volume"].rolling(20).mean().iloc[-1]
    recent_vol = df["volume"].iloc[-3:].mean()
    vol_trend = 1 if recent_vol > vol_sma else -1
    votes.append(vol_trend if abs(price_change) > 0.01 else 0)
    
    # Order book imbalance (significant only)
    if abs(ob_imbalance) > 0.1:
        votes.append(1 if ob_imbalance > 0 else -1)
    else:
        votes.append(0)
    
    nonzero = [v for v in votes if v != 0]
    if len(nonzero) < 2:
        return 0
    
    score = sum(nonzero)
    agree = sum(1 for v in nonzero if v == (1 if score > 0 else -1))
    
    return (1 if score > 0 else -1) if agree / len(nonzero) >= 0.6 else 0


def liquidity_vote(df: pd.DataFrame) -> int:
    """
    Liquidity analysis: support/resistance proximity, volatility regime.
    """
    votes = []
    
    # Proximity to 30-period range extremes
    lookback = df.tail(30)
    swing_high, swing_low = lookback["high"].max(), lookback["low"].min()
    last_close = df["close"].iloc[-1]
    rng = swing_high - swing_low
    
    if rng > 0:
        pos = (last_close - swing_low) / rng
        # Near resistance = bearish, near support = bullish
        if pos > 0.85:
            votes.append(-1)
        elif pos < 0.15:
            votes.append(1)
        else:
            votes.append(0)
    else:
        votes.append(0)
    
    # Volatility check (avoid high volatility = no edge)
    atr_val = atr(df).iloc[-1]
    atr_sma = atr(df).rolling(20).mean().iloc[-1]
    if atr_val > atr_sma * 1.5:
        votes.append(0)  # Too volatile
    
    nonzero = [v for v in votes if v != 0]
    if not nonzero:
        return 0
    
    return 1 if sum(nonzero) > 0 else -1


def composite_signal(momentum: int, pressure: int, liquidity: int) -> str:
    """Require 2 of 3 pillars to agree for a signal."""
    votes = [momentum, pressure, liquidity]
    nonzero = [v for v in votes if v != 0]
    
    if len(nonzero) < 2:
        return "WAIT"
    
    score = sum(nonzero)
    agree = sum(1 for v in nonzero if v == (1 if score > 0 else -1))
    
    if agree >= 2:
        return "LONG" if score > 0 else "SHORT"
    return "WAIT"


# --------------------------------------------------------------------------
# STATE & WEBHOOK
# --------------------------------------------------------------------------

def load_state() -> dict:
    """Load bot state from file or return defaults."""
    defaults = {
        "last_closed_candle": None,
        "pending_direction": None,
        "pending_count": 0,
        "active_position": None
    }
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text())
            return {**defaults, **loaded}  # merge with defaults for new fields
        except (json.JSONDecodeError, IOError) as e:
            log.error(f"Failed to load state: {e}")
    return defaults


def save_state(state: dict):
    """Atomically save state to file."""
    tmp_file = STATE_FILE.with_suffix(".tmp")
    try:
        tmp_file.write_text(json.dumps(state, indent=2))
        tmp_file.replace(STATE_FILE)  # atomic rename
    except IOError as e:
        log.error(f"Failed to save state: {e}")


def send_wunder_signal(direction: str):
    """Send signal to WunderTrading webhook."""
    code_map = {
        "LONG": WUNDER_LONG_CODE,
        "SHORT": WUNDER_SHORT_CODE,
        "EXIT": WUNDER_EXIT_CODE
    }
    code = code_map.get(direction)
    
    if not code:
        log.error(f"No webhook code configured for {direction}")
        return
    
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
    
    log.info(f"[{'DRY RUN' if DRY_RUN else 'LIVE'}] Sending {direction} signal")
    
    if DRY_RUN:
        log.info(f"Payload: {json.dumps(payload, indent=2)}")
        return
    
    try:
        r = requests.post(WUNDER_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        log.info(f"WunderTrading response [{r.status_code}]: {r.text[:200]}")
    except requests.RequestException as e:
        log.error(f"Webhook failed: {e}")


# --------------------------------------------------------------------------
# MAIN LOOP
# --------------------------------------------------------------------------

def calculate_sleep_time(next_candle_close: pd.Timestamp) -> float:
    """Calculate seconds to sleep until next candle close + small buffer."""
    now = pd.Timestamp.now(tz="UTC")
    sleep_seconds = (next_candle_close - now).total_seconds()
    return max(sleep_seconds + 2, 5)  # minimum 5s, add 2s buffer


def run_once(state: dict) -> dict:
    """Execute one iteration of the trading loop."""
    try:
        df = get_klines(limit=200)
    except Exception as e:
        log.error(f"Failed to fetch klines: {e}")
        return state
    
    if len(df) < 55:  # Need enough data for indicators
        log.warning(f"Insufficient data: {len(df)} candles")
        return state
    
    # Get the most recently closed candle's close time
    last_close_time = df["close_time"].iloc[-1]
    last_close_iso = last_close_time.isoformat()
    
    # Skip if already evaluated this candle
    if state["last_closed_candle"] == last_close_iso:
        return state
    
    log.info(f"New closed {INTERVAL_MINUTES}m candle @ {last_close_iso} — evaluating...")
    
    # Fetch order book
    ob_imb = get_order_book_imbalance()
    
    # Calculate votes
    m = momentum_vote(df)
    p = pressure_vote(df, ob_imb)
    l = liquidity_vote(df)
    direction = composite_signal(m, p, l)
    
    current_price = df["close"].iloc[-1]
    log.info(f"momentum={m:+d} pressure={p:+d} liquidity={l:+d} -> {direction} "
              f"(close={current_price:.2f}, ob_imb={ob_imb:+.3f})")
    
    # Reconfirmation logic
    if direction == state["pending_direction"]:
        state["pending_count"] += 1
    else:
        state["pending_direction"] = direction
        state["pending_count"] = 1
    
    # Execute if confirmed
    if direction in ("LONG", "SHORT") and state["pending_count"] >= RECONFIRM_BARS:
        if state["active_position"] != direction:
            send_wunder_signal(direction)
            state["active_position"] = direction
            
    elif direction == "WAIT" and state["active_position"] and state["pending_count"] >= RECONFIRM_BARS:
        send_wunder_signal("EXIT")
        state["active_position"] = None
    
    state["last_closed_candle"] = last_close_iso
    save_state(state)
    return state


def main():
    """Main entry point with smart polling."""
    log.info(f"Starting Kraken bot: {SYMBOL} {INTERVAL_MINUTES}m "
              f"(reconfirm={RECONFIRM_BARS}, dry_run={DRY_RUN})")
    
    # Validate config
    if not DRY_RUN and not all([WUNDER_LONG_CODE, WUNDER_SHORT_CODE, WUNDER_EXIT_CODE]):
        log.error("Webhook codes not configured! Set WUNDER_*_CODE env vars.")
        return
    
    state = load_state()
    
    while True:
        try:
            state = run_once(state)
            
            # Calculate next candle close time for smart sleep
            now = pd.Timestamp.now(tz="UTC")
            minutes_since_epoch = now.timestamp() // 60
            minutes_into_interval = minutes_since_epoch % INTERVAL_MINUTES
            minutes_until_close = INTERVAL_MINUTES - minutes_into_interval
            
            # Sleep until next candle close
            sleep_seconds = minutes_until_close * 60 + 5  # +5s buffer
            
            if sleep_seconds > 300:  # Log if waiting more than 5 minutes
                next_check = now + pd.Timedelta(seconds=sleep_seconds)
                log.info(f"Sleeping {sleep_seconds/60:.1f}m until next check @ {next_check:%H:%M:%S}")
            
            time.sleep(sleep_seconds)
            
        except Exception as e:
            log.exception(f"Error in main loop: {e}")
            time.sleep(60)  # Recover with 1m sleep on error


if __name__ == "__main__":
    main()
