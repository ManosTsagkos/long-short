"""
bud_style_btc_signal_bot.py

An ORIGINAL, independently-built BTC composite signal bot for WunderTrading.

IMPORTANT: This is NOT a copy of budsignal.io's proprietary algorithm.
Their landing page is marketing copy. It discloses the *categories* of data
they say they use (order-flow "pressure", technical "momentum", liquidation
"liquidity"), but not their exact formulas, weights, lookbacks, or
thresholds -- that's the paid product, and it isn't published anywhere.
There is nothing to "copy" from a page that doesn't show its math.

What follows is a comparable, from-scratch implementation of the same
*category* of system (a multi-factor composite that needs independent
signals to agree before firing), built entirely on public Binance Futures
data, with a real, documented integration into WunderTrading's Signal Bot.

PILLARS (each returns -1 / 0 / +1):
    1. Momentum  - EMA21 vs EMA55 trend, MACD histogram sign, RSI vs 50
    2. Pressure  - Open-interest/price divergence, taker buy/sell
                   aggression, whale (top-trader) vs retail (global account)
                   positioning delta, order-book depth imbalance
    3. Liquidity - Funding-rate extremes (crowded-side liquidation risk)
                   and proximity to recent swing high/low (where stops and
                   liquidations cluster)

COMPOSITE RULE:
    - At least 2 of the 3 pillars must agree on direction.
    - That agreement must hold for RECONFIRM_BARS consecutive closed
      candles before a signal actually fires (reduces chop/whipsaw).
    - Otherwise the engine outputs WAIT and sends nothing.

On a confirmed state change, the bot POSTs JSON to your WunderTrading
Signal Bot webhook (default: https://wtalerts.com/bot/custom), using the
field names from WunderTrading's own JSON schema (code, orderType,
amountPerTrade, leverage, stopLoss, takeProfits, etc.).

THIS IS NOT FINANCIAL ADVICE. Backtest and paper-trade extensively before
risking real capital. No combination of indicators predicts price; this
only formalizes a rule set so it can run unattended.
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
# CONFIG  (override via environment variables / a .env file)
# --------------------------------------------------------------------------

SYMBOL = os.getenv("BUD_SYMBOL", "BTCUSDT")
INTERVAL = os.getenv("BUD_INTERVAL", "4h")            # 4H matches the swing-trade framing
RECONFIRM_BARS = int(os.getenv("BUD_RECONFIRM_BARS", "1"))
POLL_SECONDS = int(os.getenv("BUD_POLL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("BUD_STATE_FILE", "bud_bot_state.json"))

WUNDER_WEBHOOK_URL = os.getenv("WUNDER_WEBHOOK_URL", "https://wtalerts.com/bot/custom")
WUNDER_LONG_CODE = os.getenv("WUNDER_LONG_CODE", "CHANGE-THIS-ENTER-LONG-COMMENT")
WUNDER_SHORT_CODE = os.getenv("WUNDER_SHORT_CODE", "CHANGE-THIS-ENTER-SHORT-COMMENT")
WUNDER_EXIT_CODE = os.getenv("WUNDER_EXIT_CODE", "CHANGE-THIS-EXIT-ALL-COMMENT")

ORDER_TYPE = os.getenv("BUD_ORDER_TYPE", "market")               # "market" | "limit"
AMOUNT_PER_TRADE_TYPE = os.getenv("BUD_AMOUNT_TYPE", "percents")  # "percents"|"quote"|"contracts"|"base"
AMOUNT_PER_TRADE = float(os.getenv("BUD_AMOUNT_PER_TRADE", "0.1"))  # 0.1 = 10%
LEVERAGE = float(os.getenv("BUD_LEVERAGE", "2"))
STOP_LOSS_PCT = float(os.getenv("BUD_STOP_LOSS_PCT", "0.015"))     # 1.5%
TAKE_PROFIT_PCT = float(os.getenv("BUD_TAKE_PROFIT_PCT", "0.03"))  # 3%

DRY_RUN = os.getenv("BUD_DRY_RUN", "true").lower() == "true"  # log only, don't POST

BINANCE_FAPI = "https://api.kraken.com/0/public/OHLC?pair=BTCUSD&interval=240"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bud_style_bot")

# --------------------------------------------------------------------------
# DATA FETCHING (Binance USDT-M Futures public endpoints — no API key needed)
# --------------------------------------------------------------------------


def _get(path, params, retries=3):
    url = f"{BINANCE_FAPI}{path}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning(f"GET {path} failed ({e}); retry {attempt + 1}/{retries}")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {path} after {retries} retries")


def get_klines(limit=200):
    raw = _get("/fapi/v1/klines", {"symbol": SYMBOL, "interval": INTERVAL, "limit": limit})
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def get_open_interest_hist(period="4h", limit=200):
    raw = _get("/futures/data/openInterestHist", {"symbol": SYMBOL, "period": period, "limit": limit})
    df = pd.DataFrame(raw)
    df["sumOpenInterest"] = df["sumOpenInterest"].astype(float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def get_taker_long_short_ratio(period="4h", limit=200):
    raw = _get("/futures/data/takerlongshortratio", {"symbol": SYMBOL, "period": period, "limit": limit})
    df = pd.DataFrame(raw)
    df["buySellRatio"] = df["buySellRatio"].astype(float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def get_top_trader_position_ratio(period="4h", limit=200):
    """Proxy for 'whale' positioning: long/short ratio of top traders by position size."""
    raw = _get("/futures/data/topLongShortPositionRatio", {"symbol": SYMBOL, "period": period, "limit": limit})
    df = pd.DataFrame(raw)
    df["longShortRatio"] = df["longShortRatio"].astype(float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def get_global_account_ratio(period="4h", limit=200):
    """Proxy for 'retail' positioning: long/short ratio across all accounts."""
    raw = _get("/futures/data/globalLongShortAccountRatio", {"symbol": SYMBOL, "period": period, "limit": limit})
    df = pd.DataFrame(raw)
    df["longShortRatio"] = df["longShortRatio"].astype(float)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def get_funding_rate(limit=50):
    raw = _get("/fapi/v1/fundingRate", {"symbol": SYMBOL, "limit": limit})
    df = pd.DataFrame(raw)
    df["fundingRate"] = df["fundingRate"].astype(float)
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    return df


def get_order_book_imbalance(depth_limit=100, band_pct=0.005):
    raw = _get("/fapi/v1/depth", {"symbol": SYMBOL, "limit": depth_limit})
    bids = pd.DataFrame(raw["bids"], columns=["price", "qty"]).astype(float)
    asks = pd.DataFrame(raw["asks"], columns=["price", "qty"]).astype(float)
    mid = (bids["price"].iloc[0] + asks["price"].iloc[0]) / 2
    band = mid * band_pct
    bid_depth = bids[bids["price"] >= mid - band]["qty"].sum()
    ask_depth = asks[asks["price"] <= mid + band]["qty"].sum()
    total = bid_depth + ask_depth
    return 0.0 if total == 0 else (bid_depth - ask_depth) / total  # range: -1..+1


# --------------------------------------------------------------------------
# INDICATORS (no external TA library required)
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


# --------------------------------------------------------------------------
# PILLAR VOTES  (-1 bearish, 0 neutral, +1 bullish)
# --------------------------------------------------------------------------


def momentum_vote(df):
    close = df["close"]
    ema_fast = ema(close, 21).iloc[-1]
    ema_slow = ema(close, 55).iloc[-1]
    _, _, hist = macd(close)
    rsi_val = rsi(close).iloc[-1]

    votes = [1 if ema_fast > ema_slow else -1, 1 if hist.iloc[-1] > 0 else -1]
    if rsi_val > 55:
        votes.append(1)
    elif rsi_val < 45:
        votes.append(-1)
    else:
        votes.append(0)

    nonzero = [v for v in votes if v != 0]
    if len(nonzero) < 2:
        return 0
    score = sum(nonzero)
    agree = sum(1 for v in nonzero if v == (1 if score > 0 else -1))
    return (1 if score > 0 else -1) if agree >= 2 else 0


def pressure_vote(df_price, df_oi, df_taker, df_top, df_global, ob_imbalance):
    votes = []

    oi_chg = df_oi["sumOpenInterest"].iloc[-1] - df_oi["sumOpenInterest"].iloc[-3]
    price_chg = df_price["close"].iloc[-1] - df_price["close"].iloc[-3]
    if oi_chg > 0 and price_chg > 0:
        votes.append(1)          # rising OI + rising price -> bullish continuation
    elif oi_chg > 0 and price_chg < 0:
        votes.append(-1)         # rising OI + falling price -> bearish continuation
    else:
        votes.append(0)          # falling OI -> conviction unwinding, no read

    buy_sell = df_taker["buySellRatio"].iloc[-1]
    votes.append(1 if buy_sell > 1.0 else -1)

    whale = df_top["longShortRatio"].iloc[-1]
    retail = df_global["longShortRatio"].iloc[-1]
    delta = whale - retail
    votes.append((1 if delta > 0 else -1) if abs(delta) > 0.05 else 0)

    votes.append((1 if ob_imbalance > 0 else -1) if abs(ob_imbalance) > 0.1 else 0)

    nonzero = [v for v in votes if v != 0]
    if len(nonzero) < 2:
        return 0
    score = sum(nonzero)
    agree = sum(1 for v in nonzero if v == (1 if score > 0 else -1))
    return (1 if score > 0 else -1) if agree / len(nonzero) >= 0.6 else 0


def liquidity_vote(df_price, df_funding):
    votes = []

    funding_now = df_funding["fundingRate"].iloc[-1]
    if funding_now > 0.0004:
        votes.append(-1)   # crowded longs paying shorts -> downside liq. risk skew
    elif funding_now < -0.0004:
        votes.append(1)    # crowded shorts -> upside squeeze risk skew
    else:
        votes.append(0)

    lookback = df_price.tail(30)
    swing_high, swing_low = lookback["high"].max(), lookback["low"].min()
    last_close = df_price["close"].iloc[-1]
    rng = swing_high - swing_low
    if rng > 0:
        pos = (last_close - swing_low) / rng
        votes.append(-1 if pos > 0.85 else (1 if pos < 0.15 else 0))
    else:
        votes.append(0)

    nonzero = [v for v in votes if v != 0]
    if not nonzero:
        return 0
    score = sum(nonzero)
    return 1 if score > 0 else (-1 if score < 0 else 0)


def composite_signal(m, p, l):
    nonzero = [v for v in (m, p, l) if v != 0]
    if len(nonzero) < 2:
        return "WAIT"
    score = sum(nonzero)
    agree = sum(1 for v in nonzero if v == (1 if score > 0 else -1))
    if agree >= 2:
        return "LONG" if score > 0 else "SHORT"
    return "WAIT"


# --------------------------------------------------------------------------
# STATE  (so we don't re-fire on every poll, and can require reconfirmation)
# --------------------------------------------------------------------------


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_closed_candle": None, "pending_direction": None, "pending_count": 0, "active_position": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# WUNDERTRADING WEBHOOK
# --------------------------------------------------------------------------


def send_wunder_signal(direction):
    """
    direction: "LONG" | "SHORT" | "EXIT"
    Builds a JSON payload matching WunderTrading's Signal Bot JSON schema
    and POSTs it to the bot's webhook URL.
    """
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
    df = get_klines(limit=200)
    closed = df.iloc[:-1]  # drop the still-forming candle
    last_closed_time = closed["close_time"].iloc[-1].isoformat()

    if state["last_closed_candle"] == last_closed_time:
        return state  # already evaluated this candle

    log.info(f"New closed {INTERVAL} candle @ {last_closed_time} — evaluating pillars...")

    df_oi = get_open_interest_hist()
    df_taker = get_taker_long_short_ratio()
    df_top = get_top_trader_position_ratio()
    df_global = get_global_account_ratio()
    df_funding = get_funding_rate()
    ob_imb = get_order_book_imbalance()

    m = momentum_vote(closed)
    p = pressure_vote(closed, df_oi, df_taker, df_top, df_global, ob_imb)
    l = liquidity_vote(closed, df_funding)
    direction = composite_signal(m, p, l)

    log.info(f"momentum={m:+d} pressure={p:+d} liquidity={l:+d} -> {direction} "
              f"(close={closed['close'].iloc[-1]:.1f})")

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
    log.info(f"Starting composite {SYMBOL} {INTERVAL} signal bot "
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
