#!/usr/bin/env python3
"""
kraken_institutional_bot.py - Production Version

1-minute polling για risk management & emergency exits
4-hour candle closes για new entries
"""

import os
import json
import time
import logging
import signal
import sys
from pathlib import Path
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any, Union
from enum import Enum
import threading

import requests
import numpy as np
import pandas as pd
import yfinance as yf

# =============================================================================
# CONFIGURATION
# =============================================================================

class Config:
    # Trading
    SYMBOL = os.getenv("BUD_SYMBOL", "XBTUSD")
    INTERVAL_MINUTES = int(os.getenv("BUD_INTERVAL", "240"))  # 4h for signals
    POLL_SECONDS = int(os.getenv("BUD_POLL_SECONDS", "60"))  # 1 minute for risk!
    
    # Emergency Thresholds (checked every minute)
    EMERGENCY_VIX_SPIKE = float(os.getenv("BUD_EMERG_VIX", "5.0"))  # VIX +5 points
    EMERGENCY_DXY_SPIKE = float(os.getenv("BUD_EMERG_DXY", "0.015"))  # DXY +1.5%
    EMERGENCY_PRICE_DROP = float(os.getenv("BUD_EMERG_PRICE", "0.03"))  # -3% from entry
    EMERGENCY_PRICE_RISE = float(os.getenv("BUD_EMERG_RISE", "0.08"))  # +8% fast rise (take profit)
    
    # Risk Management
    TARGET_VOLATILITY = float(os.getenv("BUD_TARGET_VOL", "0.015"))
    MAX_POSITION_PCT = float(os.getenv("BUD_MAX_POSITION", "0.20"))
    PORTFOLIO_HEAT_LIMIT = float(os.getenv("BUD_HEAT_LIMIT", "0.30"))
    KELLY_FRACTION = float(os.getenv("BUD_KELLY", "0.25"))
    TRAILING_STOP = float(os.getenv("BUD_TRAILING", "0.02"))  # 2% trailing
    
    # Execution
    MAX_SLIPPAGE_BPS = float(os.getenv("BUD_MAX_SLIPPAGE", "50"))
    
    # Health
    MAX_API_LATENCY_MS = int(os.getenv("BUD_MAX_LATENCY", "3000"))
    STALE_DATA_SECONDS = int(os.getenv("BUD_STALE_THRESHOLD", "180"))  # 3 min for 1min polling
    CIRCUIT_BREAKER_FAILURES = int(os.getenv("BUD_CB_FAILURES", "5"))
    CIRCUIT_BREAKER_TIMEOUT = int(os.getenv("BUD_CB_TIMEOUT", "300"))
    
    # Macro
    ENABLE_MACRO_FILTER = os.getenv("BUD_ENABLE_MACRO", "true").lower() == "true"
    VIX_THRESHOLD = float(os.getenv("BUD_VIX_THRESHOLD", "30.0"))
    DXY_IMPACT_THRESHOLD = float(os.getenv("BUD_DXY_THRESHOLD", "0.02"))
    
    # WunderTrading
    WUNDER_WEBHOOK_URL = os.getenv("WUNDER_WEBHOOK_URL", "")
    WUNDER_LONG_CODE = os.getenv("WUNDER_LONG_CODE", "")
    WUNDER_SHORT_CODE = os.getenv("WUNDER_SHORT_CODE", "")
    WUNDER_EXIT_CODE = os.getenv("WUNDER_EXIT_CODE", "")
    
    ORDER_TYPE = os.getenv("BUD_ORDER_TYPE", "market")
    AMOUNT_PER_TRADE_TYPE = os.getenv("BUD_AMOUNT_TYPE", "percents")
    BASE_LEVERAGE = float(os.getenv("BUD_LEVERAGE", "6"))
    STOP_LOSS_PCT = float(os.getenv("BUD_STOP_LOSS_PCT", "0.015"))
    TAKE_PROFIT_PCT = float(os.getenv("BUD_TAKE_PROFIT_PCT", "0.03"))
    
    DRY_RUN = os.getenv("BUD_DRY_RUN", "true").lower() == "true"
    STATE_FILE = Path(os.getenv("BUD_STATE_FILE", "institutional_bot_state.json"))
    LOG_LEVEL = os.getenv("BUD_LOG_LEVEL", "INFO")
    
    KRAKEN_API = "https://futures.kraken.com/derivatives/api/v3/tickers/PF_XBTUSD"


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", mode='a')
    ]
)
logger = logging.getLogger("institutional_bot")


# =============================================================================
# DATA STRUCTURES
# =============================================================================

class Signal(Enum):
    WAIT = "WAIT"
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"


class ExitReason(Enum):
    NONE = "none"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    EMERGENCY_VIX = "emergency_vix"
    EMERGENCY_DXY = "emergency_dxy"
    EMERGENCY_PRICE = "emergency_price"
    REGIME_CHANGE = "regime_change"
    SIGNAL_FLIP = "signal_flip"


@dataclass
class HealthMetrics:
    last_price_update: Optional[datetime] = None
    last_api_call: Optional[datetime] = None
    api_latency_ms: float = 0.0
    consecutive_failures: int = 0
    circuit_open: bool = False
    circuit_opened_at: Optional[datetime] = None
    
    def is_stale(self) -> bool:
        if not self.last_price_update:
            return False  # First run
        return (datetime.utcnow() - self.last_price_update).total_seconds() > Config.STALE_DATA_SECONDS


@dataclass
class PositionState:
    direction: str = ""  # LONG or SHORT
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    size: float = 0.0
    highest_price: float = 0.0  # For trailing stop
    lowest_price: float = float('inf')
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    
    def unrealized_pnl(self, current_price: float) -> float:
        if not self.direction:
            return 0.0
        if self.direction == "LONG":
            return (current_price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - current_price) / self.entry_price
    
    def update_trailing(self, current_price: float) -> Optional[ExitReason]:
        """Check trailing stop and return exit reason if triggered."""
        if not self.direction:
            return None
        
        # Update extremes
        if current_price > self.highest_price:
            self.highest_price = current_price
        if current_price < self.lowest_price:
            self.lowest_price = current_price
        
        # Check trailing stop
        if self.direction == "LONG":
            # Trail below highs
            trail_price = self.highest_price * (1 - Config.TRAILING_STOP)
            if current_price < trail_price and self.highest_price > self.entry_price * 1.02:
                return ExitReason.TRAILING_STOP
            # Check stop loss
            if current_price < self.stop_price:
                return ExitReason.STOP_LOSS
            # Check take profit
            if current_price > self.take_profit_price:
                return ExitReason.TAKE_PROFIT
        else:  # SHORT
            trail_price = self.lowest_price * (1 + Config.TRAILING_STOP)
            if current_price > trail_price and self.lowest_price < self.entry_price * 0.98:
                return ExitReason.TRAILING_STOP
            if current_price > self.stop_price:
                return ExitReason.STOP_LOSS
            if current_price < self.take_profit_price:
                return ExitReason.TAKE_PROFIT
        
        return None


@dataclass
class EmergencyContext:
    """Real-time emergency monitoring."""
    vix_current: float = 0.0
    vix_previous: float = 0.0
    dxy_current: float = 0.0
    dxy_previous: float = 0.0
    price_current: float = 0.0
    price_previous: float = 0.0
    
    def check_emergency(self, position: Optional[PositionState]) -> Tuple[bool, ExitReason, str]:
        """Check all emergency conditions."""
        # VIX spike
        vix_change = self.vix_current - self.vix_previous
        if vix_change > Config.EMERGENCY_VIX_SPIKE:
            return True, ExitReason.EMERGENCY_VIX, f"VIX spike: {self.vix_previous:.1f} → {self.vix_current:.1f} (+{vix_change:.1f})"
        
        # DXY spike
        dxy_change = (self.dxy_current - self.dxy_previous) / self.dxy_previous if self.dxy_previous else 0
        if abs(dxy_change) > Config.EMERGENCY_DXY_SPIKE:
            direction = "up" if dxy_change > 0 else "down"
            return True, ExitReason.EMERGENCY_DXY, f"DXY spike {direction}: {dxy_change:.2%}"
        
        # Price emergency (if in position)
        if position and position.entry_price > 0:
            pnl = position.unrealized_pnl(self.price_current)
            
            if pnl < -Config.EMERGENCY_PRICE_DROP:
                return True, ExitReason.EMERGENCY_PRICE, f"Price crash: {pnl:.2%} loss"
            
            if pnl > Config.EMERGENCY_PRICE_RISE:
                return True, ExitReason.TAKE_PROFIT, f"Fast profit: {pnl:.2%} gain"
        
        return False, ExitReason.NONE, ""


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitBreaker:
    def __init__(self, health: HealthMetrics):
        self.health = health
    
    def record_success(self):
        self.health.consecutive_failures = 0
    
    def record_failure(self) -> bool:
        self.health.consecutive_failures += 1
        if self.health.consecutive_failures >= Config.CIRCUIT_BREAKER_FAILURES:
            self.open_circuit()
            return True
        return False
    
    def open_circuit(self):
        logger.error(f"🔴 CIRCUIT BREAKER OPENED")
        self.health.circuit_open = True
        self.health.circuit_opened_at = datetime.utcnow()
    
    def is_open(self) -> bool:
        if not self.health.circuit_open:
            return False
        
        if self.health.circuit_opened_at:
            elapsed = (datetime.utcnow() - self.health.circuit_opened_at).total_seconds()
            if elapsed > Config.CIRCUIT_BREAKER_TIMEOUT:
                logger.info("🟢 Circuit breaker recovery")
                self.health.circuit_open = False
                self.health.consecutive_failures = 0
                return False
        return True


# =============================================================================
# STATE MANAGER
# =============================================================================

class StateManager:
    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.backup_path = filepath.with_suffix('.backup.json')
        self.lock = threading.Lock()
    
    def load(self) -> Dict:
        defaults = {
            'last_candle_time': None,
            'last_4h_eval_time': None,
            'active_position': None,
            'position_state': None,
            'macro_baseline': {'vix': None, 'dxy': None, 'timestamp': None},
            'trade_history': [],
            'daily_pnl': 0.0
        }
        
        for path in [self.filepath, self.backup_path]:
            if path.exists():
                try:
                    with open(path, 'r') as f:
                        data = json.load(f)
                        for key, val in defaults.items():
                            if key not in data:
                                data[key] = val
                        # Restore position state
                        if data.get('position_state'):
                            data['position_state'] = PositionState(**data['position_state'])
                        return data
                except Exception as e:
                    logger.error(f"Failed to load {path}: {e}")
        
        return defaults
    
    def save(self, state: Dict) -> bool:
        with self.lock:
            try:
                # Convert position state to dict for JSON
                save_state = state.copy()
                if save_state.get('position_state'):
                    save_state['position_state'] = asdict(save_state['position_state'])
                
                tmp = self.filepath.with_suffix('.tmp')
                with open(tmp, 'w') as f:
                    json.dump(save_state, f, indent=2, default=str)
                
                if self.filepath.exists():
                    self.filepath.rename(self.backup_path)
                tmp.rename(self.filepath)
                return True
            except Exception as e:
                logger.error(f"Save failed: {e}")
                return False


# =============================================================================
# API CLIENTS
# =============================================================================

class KrakenClient:
    def __init__(self, health: HealthMetrics):
        self.health = health
    
    def _request(self, endpoint: str, params: Dict, retries: int = 3) -> Optional[Dict]:
        url = f"{Config.KRAKEN_API}/{endpoint}"
        start = time.time()
        
        for attempt in range(retries):
            try:
                r = requests.get(url, params=params, timeout=10)
                self.health.api_latency_ms = (time.time() - start) * 1000
                self.health.last_api_call = datetime.utcnow()
                
                r.raise_for_status()
                data = r.json()
                if data.get('error'):
                    raise RuntimeError(data['error'])
                
                self.health.consecutive_failures = 0
                return data['result']
                
            except Exception as e:
                logger.warning(f"Attempt {attempt+1}/{retries} failed: {e}")
                time.sleep(2 ** attempt)
        
        self.health.consecutive_failures += 1
        return None
    
    def get_price(self) -> Optional[float]:
        """Get current price - fast for emergency checks."""
        result = self._request("Ticker", {"pair": Config.SYMBOL})
        if result:
            pair_key = list(result.keys())[0]
            return float(result[pair_key]['c'][0])
        return None
    
    def get_ohlc(self, interval: int = 240, limit: int = 100) -> Optional[pd.DataFrame]:
        """Get candles for signal generation."""
        result = self._request("OHLC", {
            "pair": Config.SYMBOL,
            "interval": interval,
            "since": 0
        })
        
        if not result:
            return None
        
        pair_key = list(result.keys())[0]
        raw = result[pair_key]
        
        df = pd.DataFrame(raw, columns=[
            "time", "open", "high", "low", "close", "vwap", "volume", "count"
        ])
        
        for col in ["open", "high", "low", "close", "vwap", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        
        df["close_time"] = pd.to_datetime(df["time"], unit="s", utc=True) + \
                          pd.Timedelta(minutes=interval)
        
        df = df.iloc[:-1]  # Drop incomplete
        
        self.health.last_price_update = datetime.utcnow()
        return df.reset_index(drop=True)


class MacroDataFetcher:
    """Fetch macro data with caching."""
    
    def __init__(self):
        self.cache = {}
        self.last_fetch = None
        self.cache_ttl = 300  # 5 minutes
    
    def _is_cached(self) -> bool:
        if not self.last_fetch:
            return False
        return (datetime.utcnow() - self.last_fetch).seconds < self.cache_ttl
    
    def get_vix(self) -> float:
        try:
            if not self._is_cached() or 'vix' not in self.cache:
                vix = yf.Ticker("^VIX")
                hist = vix.history(period="2d")
                self.cache['vix'] = float(hist['Close'].iloc[-1])
                self.cache['vix_prev'] = float(hist['Close'].iloc[-2]) if len(hist) > 1 else self.cache['vix']
                self.last_fetch = datetime.utcnow()
            return self.cache['vix'], self.cache.get('vix_prev', self.cache['vix'])
        except Exception as e:
            logger.warning(f"VIX fetch failed: {e}")
            return 20.0, 20.0
    
    def get_dxy(self) -> Tuple[float, float]:
        try:
            if not self._is_cached() or 'dxy' not in self.cache:
                dxy = yf.Ticker("DX-Y.NYB")
                hist = dxy.history(period="2d")
                self.cache['dxy'] = float(hist['Close'].iloc[-1])
                self.cache['dxy_prev'] = float(hist['Close'].iloc[-2]) if len(hist) > 1 else self.cache['dxy']
                self.last_fetch = datetime.utcnow()
            return self.cache['dxy'], self.cache.get('dxy_prev', self.cache['dxy'])
        except Exception as e:
            logger.warning(f"DXY fetch failed: {e}")
            return 100.0, 100.0
    
    def get_emergency_context(self, price: float, prev_price: float) -> EmergencyContext:
        vix, vix_prev = self.get_vix()
        dxy, dxy_prev = self.get_dxy()
        
        return EmergencyContext(
            vix_current=vix,
            vix_previous=vix_prev,
            dxy_current=dxy,
            dxy_previous=dxy_prev,
            price_current=price,
            price_previous=prev_price
        )


# =============================================================================
# SIGNAL ENGINE (4H only)
# =============================================================================

class SignalEngine:
    def __init__(self):
        pass
    
    def ema(self, s: pd.Series, span: int) -> pd.Series:
        return s.ewm(span=span, adjust=False).mean()
    
    def rsi(self, s: pd.Series, period: int = 14) -> pd.Series:
        d = s.diff()
        gain = d.clip(lower=0)
        loss = -d.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))
    
    def generate_4h_signal(self, df: pd.DataFrame) -> str:
        """Generate signal only on 4h closes."""
        if len(df) < 55:
            return "WAIT"
        
        close = df['close']
        
        # Trend
        ema9 = self.ema(close, 9).iloc[-1]
        ema21 = self.ema(close, 21).iloc[-1]
        ema55 = self.ema(close, 55).iloc[-1]
        
        trend = 0
        if ema9 > ema21 > ema55:
            trend = 1
        elif ema9 < ema21 < ema55:
            trend = -1
        
        # RSI
        rsi = self.rsi(close).iloc[-1]
        rsi_sig = 1 if rsi > 60 else (-1 if rsi < 40 else 0)
        
        # Momentum
        mom = (close.iloc[-1] / close.iloc[-10]) - 1
        mom_sig = 1 if mom > 0.03 else (-1 if mom < -0.03 else 0)
        
        votes = [trend, rsi_sig, mom_sig]
        nonzero = [v for v in votes if v != 0]
        
        if len(nonzero) < 2:
            return "WAIT"
        
        score = sum(nonzero)
        agree = sum(1 for v in nonzero if v == (1 if score > 0 else -1))
        
        if agree >= 2:
            return "LONG" if score > 0 else "SHORT"
        return "WAIT"


# =============================================================================
# EXECUTION
# =============================================================================

class ExecutionEngine:
    def send_exit(self, reason: ExitReason):
        logger.info(f"[EXIT] {reason.value}")
        if Config.DRY_RUN:
            return True
        
        payload = {
            "code": Config.WUNDER_EXIT_CODE,
            "reduceOnly": True
        }
        
        try:
            r = requests.post(Config.WUNDER_WEBHOOK_URL, json=payload, timeout=10)
            return r.status_code == 200
        except Exception as e:
            logger.error(f"Exit failed: {e}")
            return False
    
    def send_entry(self, direction: str, price: float):
        logger.info(f"[ENTRY] {direction} @ ${price:,.2f}")
        if Config.DRY_RUN:
            return True
        
        code = Config.WUNDER_LONG_CODE if direction == "LONG" else Config.WUNDER_SHORT_CODE
        
        payload = {
            "code": code,
            "orderType": Config.ORDER_TYPE,
            "stopLoss": {"priceDeviation": Config.STOP_LOSS_PCT},
            "takeProfits": [{"priceDeviation": Config.TAKE_PROFIT_PCT, "portfolio": 1}]
        }
        
        try:
            r = requests.post(Config.WUNDER_WEBHOOK_URL, json=payload, timeout=10)
            return r.status_code == 200
        except Exception as e:
            logger.error(f"Entry failed: {e}")
            return False


# =============================================================================
# MAIN BOT
# =============================================================================

class InstitutionalBot:
    def __init__(self):
        self.health = HealthMetrics()
        self.circuit = CircuitBreaker(self.health)
        self.kraken = KrakenClient(self.health)
        self.macro = MacroDataFetcher()
        self.signals = SignalEngine()
        self.execution = ExecutionEngine()
        self.state_mgr = StateManager(Config.STATE_FILE)
        
        self.state = self.state_mgr.load()
        self.position = self.state.get('position_state')
        if self.position:
            self.position = PositionState(**self.position) if isinstance(self.position, dict) else self.position
        
        self.last_price = None
        self.running = False
        
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)
    
    def _shutdown(self, *args):
        logger.info("Shutting down...")
        self.running = False
    
    def check_emergency_exit(self, price: float) -> bool:
        """Check emergency conditions every minute."""
        if not self.position:
            return False
        
        # Get emergency context
        emergency = self.macro.get_emergency_context(price, self.last_price or price)
        
        # Check trailing stops and emergency conditions
        exit_triggered, reason, msg = emergency.check_emergency(self.position)
        
        if not exit_triggered:
            # Check trailing stop
            reason = self.position.update_trailing(price)
            if reason:
                exit_triggered = True
                msg = f"Trailing stop: {reason.value}"
        
        if exit_triggered:
            logger.warning(f"🚨 EMERGENCY EXIT: {msg}")
            if self.execution.send_exit(reason):
                self.position = None
                self.state['position_state'] = None
                self.state['active_position'] = None
                self.state_mgr.save(self.state)
            return True
        
        return False
    
    def evaluate_4h_signal(self):
        """Evaluate new 4h signal (only on candle close)."""
        df = self.kraken.get_ohlc(interval=240)
        if df is None or len(df) < 20:
            return
        
        last_close = df['close_time'].iloc[-1].isoformat()
        
        # Skip if already evaluated this candle
        if self.state.get('last_4h_eval_time') == last_close:
            return
        
        self.state['last_4h_eval_time'] = last_close
        current_price = df['close'].iloc[-1]
        
        logger.info(f"📊 4H Candle closed @ ${current_price:,.2f}")
        
        # Generate signal
        signal = self.signals.generate_4h_signal(df)
        
        # Reconfirmation logic
        pending = self.state.get('pending_signal')
        count = self.state.get('pending_count', 0)
        
        if signal == pending:
            count += 1
        else:
            pending = signal
            count = 1
        
        self.state['pending_signal'] = pending
        self.state['pending_count'] = count
        
        logger.info(f"Signal: {signal} (confirmation {count}/{Config.RECONFIRM_BARS})")
        
        # Execute if confirmed
        if pending in ('LONG', 'SHORT') and count >= Config.RECONFIRM_BARS:
            if self.state.get('active_position') != pending:
                # Exit existing first
                if self.position:
                    self.execution.send_exit(ExitReason.SIGNAL_FLIP)
                
                # Enter new
                if self.execution.send_entry(pending, current_price):
                    self.position = PositionState(
                        direction=pending,
                        entry_price=current_price,
                        entry_time=datetime.utcnow(),
                        highest_price=current_price,
                        lowest_price=current_price,
                        stop_price=current_price * (1 - Config.STOP_LOSS_PCT) if pending == "LONG" \
                                  else current_price * (1 + Config.STOP_LOSS_PCT),
                        take_profit_price=current_price * (1 + Config.TAKE_PROFIT_PCT) if pending == "LONG" \
                                          else current_price * (1 - Config.TAKE_PROFIT_PCT)
                    )
                    self.state['position_state'] = self.position
                    self.state['active_position'] = pending
        
        self.state_mgr.save(self.state)
    
    def run_cycle(self):
        """Main cycle - runs every minute."""
        # Get current price (fast)
        price = self.kraken.get_price()
        if price is None:
            if self.circuit.record_failure():
                logger.error("Circuit open - too many failures")
            return
        
        self.circuit.record_success()
        self.last_price = price
        
        # 1. EMERGENCY CHECK (every minute)
        if self.check_emergency_exit(price):
            return  # Exited, skip rest
        
        # 2. 4H SIGNAL EVALUATION (only on new candles)
        self.evaluate_4h_signal()
        
        # Log status
        if self.position:
            pnl = self.position.unrealized_pnl(price)
            logger.info(f"Position: {self.position.direction} | "
                       f"Entry: ${self.position.entry_price:,.2f} | "
                       f"Current: ${price:,.2f} | "
                       f"P&L: {pnl:+.2%}")
        else:
            logger.debug(f"No position | Price: ${price:,.2f}")
    
    def run(self):
        logger.info("=" * 60)
        logger.info("🏦 Institutional Bot Starting")
        logger.info(f"Symbol: {Config.SYMBOL}")
        logger.info(f"Risk Check: Every {Config.POLL_SECONDS}s")
        logger.info(f"Signal Eval: Every 4H on candle close")
        logger.info(f"Dry Run: {Config.DRY_RUN}")
        logger.info("=" * 60)
        
        self.running = True
        
        while self.running:
            try:
                self.run_cycle()
            except Exception as e:
                logger.exception(f"Cycle error: {e}")
            
            # Sleep until next minute
            time.sleep(Config.POLL_SECONDS)


if __name__ == "__main__":
    bot = InstitutionalBot()
    bot.run()
