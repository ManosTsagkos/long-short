#!/usr/bin/env python3
"""
kraken_institutional_bot.py

Production-grade BTC trading bot for Kraken with institutional risk management.
Features: volatility targeting, macro regime filters, smart execution, portfolio heat tracking.
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
from typing import Optional, Dict, List, Tuple, Any
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
    """Centralized configuration with validation."""
    
    # Trading Parameters
    SYMBOL = os.getenv("BUD_SYMBOL", "XBTUSD")
    INTERVAL_MINUTES = int(os.getenv("BUD_INTERVAL", "240"))
    RECONFIRM_BARS = int(os.getenv("BUD_RECONFIRM_BARS", "1"))
    
    # Risk Management - Volatility Targeting
    TARGET_VOLATILITY = float(os.getenv("BUD_TARGET_VOL", "0.015"))  # 1.5% daily vol target
    MAX_POSITION_PCT = float(os.getenv("BUD_MAX_POSITION", "0.20"))   # 20% max single position
    PORTFOLIO_HEAT_LIMIT = float(os.getenv("BUD_HEAT_LIMIT", "0.30"))  # 30% total heat
    KELLY_FRACTION = float(os.getenv("BUD_KELLY", "0.25"))  # Conservative quarter-kelly
    
    # Execution
    MAX_SLIPPAGE_BPS = float(os.getenv("BUD_MAX_SLIPPAGE", "50"))  # 0.5%
    TWAP_SLICES = int(os.getenv("BUD_TWAP_SLICES", "3"))
    EXECUTION_TIMEOUT = int(os.getenv("BUD_EXEC_TIMEOUT", "300"))
    
    # Health & Monitoring
    MAX_API_LATENCY_MS = int(os.getenv("BUD_MAX_LATENCY", "3000"))
    STALE_DATA_SECONDS = int(os.getenv("BUD_STALE_THRESHOLD", "300"))
    CIRCUIT_BREAKER_FAILURES = int(os.getenv("BUD_CB_FAILURES", "5"))
    CIRCUIT_BREAKER_TIMEOUT = int(os.getenv("BUD_CB_TIMEOUT", "900"))
    
    # Macro Filters
    ENABLE_MACRO_FILTER = os.getenv("BUD_ENABLE_MACRO", "true").lower() == "true"
    VIX_THRESHOLD = float(os.getenv("BUD_VIX_THRESHOLD", "30.0"))
    DXY_IMPACT_THRESHOLD = float(os.getenv("BUD_DXY_THRESHOLD", "0.02"))
    
    # WunderTrading
    WUNDER_WEBHOOK_URL = os.getenv("WUNDER_WEBHOOK_URL", "")
    WUNDER_LONG_CODE = os.getenv("WUNDER_LONG_CODE", "")
    WUNDER_SHORT_CODE = os.getenv("WUNDER_SHORT_CODE", "")
    WUNDER_EXIT_CODE = os.getenv("WUNDER_EXIT_CODE", "")
    
    # Position Parameters
    ORDER_TYPE = os.getenv("BUD_ORDER_TYPE", "market")
    AMOUNT_PER_TRADE_TYPE = os.getenv("BUD_AMOUNT_TYPE", "percents")
    BASE_LEVERAGE = float(os.getenv("BUD_LEVERAGE", "6"))
    STOP_LOSS_PCT = float(os.getenv("BUD_STOP_LOSS_PCT", "0.015"))
    TAKE_PROFIT_PCT = float(os.getenv("BUD_TAKE_PROFIT_PCT", "0.03"))
    
    # Runtime
    DRY_RUN = os.getenv("BUD_DRY_RUN", "true").lower() == "true"
    STATE_FILE = Path(os.getenv("BUD_STATE_FILE", "institutional_bot_state.json"))
    LOG_LEVEL = os.getenv("BUD_LOG_LEVEL", "INFO")
    
    # API
    KRAKEN_API = "https://api.kraken.com/0/public"


# =============================================================================
# LOGGING SETUP
# =============================================================================

logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
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


class Regime(Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"
    CRASH = "crash"
    UNKNOWN = "unknown"


@dataclass
class HealthMetrics:
    """Real-time system health tracking."""
    last_price_update: Optional[datetime] = None
    last_api_call: Optional[datetime] = None
    api_latency_ms: float = 0.0
    consecutive_failures: int = 0
    circuit_open: bool = False
    circuit_opened_at: Optional[datetime] = None
    daily_api_calls: int = 0
    last_api_reset: Optional[datetime] = None
    
    def is_stale(self) -> bool:
        if not self.last_price_update:
            return True
        return (datetime.utcnow() - self.last_price_update).total_seconds() > Config.STALE_DATA_SECONDS
    
    def should_reset_counter(self) -> bool:
        if not self.last_api_reset:
            self.last_api_reset = datetime.utcnow()
            return False
        elapsed = (datetime.utcnow() - self.last_api_reset).total_seconds()
        if elapsed > 86400:  # 24 hours
            self.daily_api_calls = 0
            self.last_api_reset = datetime.utcnow()
            return True
        return False
    
    def can_execute(self) -> Tuple[bool, str]:
        """Check if system is healthy enough to trade."""
        if self.is_stale():
            return False, "Data feed stale"
        if self.circuit_open:
            if self.circuit_opened_at:
                elapsed = (datetime.utcnow() - self.circuit_opened_at).total_seconds()
                if elapsed > Config.CIRCUIT_BREAKER_TIMEOUT:
                    logger.info("Circuit breaker auto-recovery")
                    self.circuit_open = False
                    self.consecutive_failures = 0
                else:
                    return False, f"Circuit breaker open ({int(elapsed)}s remaining)"
        if self.api_latency_ms > Config.MAX_API_LATENCY_MS:
            logger.warning(f"High latency: {self.api_latency_ms:.0f}ms")
        return True, "OK"

# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitBreaker:
    """Prevent cascade failures by stopping trading after consecutive errors."""
    
    def __init__(self, health: HealthMetrics):
        self.health = health
    
    def record_success(self):
        """Reset failure counter on success."""
        if self.health.consecutive_failures > 0:
            logger.debug(f"Resetting failure counter from {self.health.consecutive_failures}")
        self.health.consecutive_failures = 0
    
    def record_failure(self) -> bool:
        """Record failure and return True if circuit should open."""
        self.health.consecutive_failures += 1
        logger.warning(f"Failure recorded ({self.health.consecutive_failures}/{Config.CIRCUIT_BREAKER_FAILURES})")
        
        if self.health.consecutive_failures >= Config.CIRCUIT_BREAKER_FAILURES:
            self.open_circuit()
            return True
        return False
    
    def open_circuit(self):
        """Open circuit breaker."""
        logger.error(f"🔴 CIRCUIT BREAKER OPENED after {self.health.consecutive_failures} failures")
        self.health.circuit_open = True
        self.health.circuit_opened_at = datetime.utcnow()
    
    def is_open(self) -> bool:
        """Check if circuit is open (with auto-recovery)."""
        if not self.health.circuit_open:
            return False
        
        # Check auto-recovery
        if self.health.circuit_opened_at:
            elapsed = (datetime.utcnow() - self.health.circuit_opened_at).total_seconds()
            if elapsed > Config.CIRCUIT_BREAKER_TIMEOUT:
                logger.info("🟢 Circuit breaker auto-recovery triggered")
                self.health.circuit_open = False
                self.health.consecutive_failures = 0
                self.health.circuit_opened_at = None
                return False
        
        return True

@dataclass
class PositionSizing:
    """Detailed position sizing calculation."""
    base_size: float = 0.0
    volatility_scalar: float = 1.0
    confidence_adjustment: float = 1.0
    macro_adjustment: float = 1.0
    final_size: float = 0.0
    notional_value: float = 0.0
    max_position_hit: bool = False
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass 
class PortfolioState:
    """Track open positions and portfolio heat."""
    positions: Dict[str, Dict] = field(default_factory=dict)
    total_heat: float = 0.0
    available_capacity: float = 1.0
    unrealized_pnl: float = 0.0
    
    def calculate_heat(self) -> float:
        """Calculate portfolio heat as sum of (size * stop_distance)."""
        heat = 0.0
        for symbol, pos in self.positions.items():
            heat += pos.get('size', 0) * pos.get('stop_distance', Config.STOP_LOSS_PCT)
        self.total_heat = heat
        self.available_capacity = max(0, Config.PORTFOLIO_HEAT_LIMIT - heat)
        return heat


@dataclass
class ExecutionPlan:
    """TWAP execution plan."""
    total_size: float = 0.0
    slices: int = 1
    slice_size: float = 0.0
    delay_between_slices: int = 60
    max_slippage_bps: float = 50.0
    fallback_to_market: bool = True
    
    def generate(self) -> List[Dict]:
        """Generate execution slices."""
        if self.slices <= 1:
            return [{'size': self.total_size, 'type': 'market'}]
        
        slices = []
        remaining = self.total_size
        base_slice = self.total_size / self.slices
        
        for i in range(self.slices):
            size = min(base_slice, remaining) if i < self.slices - 1 else remaining
            slices.append({
                'slice_num': i + 1,
                'size': size,
                'type': 'limit' if i == 0 else 'market',  # First slice limit, rest market
                'delay_after': self.delay_between_slices if i < self.slices - 1 else 0
            })
            remaining -= size
        
        return slices


@dataclass
class SignalContext:
    """Complete context for signal generation."""
    timestamp: datetime
    price: float
    momentum_score: int = 0
    pressure_score: int = 0
    liquidity_score: int = 0
    composite_signal: Signal = Signal.WAIT
    confidence: float = 0.0
    regime: Regime = Regime.UNKNOWN
    macro_score: int = 0
    vix_level: float = 0.0
    dxy_trend: float = 0.0
    atr_14: float = 0.0
    volatility_regime: str = "normal"
    
    def to_dict(self) -> Dict:
        return {
            'timestamp': self.timestamp.isoformat(),
            'price': self.price,
            'momentum': self.momentum_score,
            'pressure': self.pressure_score,
            'liquidity': self.liquidity_score,
            'signal': self.composite_signal.value,
            'confidence': self.confidence,
            'regime': self.regime.value,
            'macro_score': self.macro_score,
            'vix': self.vix_level,
            'dxy_trend': self.dxy_trend,
            'atr': self.atr_14
        }


# =============================================================================
# STATE PERSISTENCE
# =============================================================================

class StateManager:
    """Atomic state management with backup."""
    
    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.backup_path = filepath.with_suffix('.backup.json')
        self.lock = threading.Lock()
    
    def load(self) -> Dict[str, Any]:
        """Load state with fallback to backup."""
        for path in [self.filepath, self.backup_path]:
            if path.exists():
                try:
                    with open(path, 'r') as f:
                        data = json.load(f)
                        logger.info(f"State loaded from {path}")
                        return data
                except (json.JSONDecodeError, IOError) as e:
                    logger.error(f"Failed to load {path}: {e}")
        
        logger.warning("No valid state file found, using defaults")
        return self._default_state()
    
    def save(self, state: Dict[str, Any]) -> bool:
        """Atomic save with backup."""
        with self.lock:
            tmp_path = self.filepath.with_suffix('.tmp')
            try:
                # Write to temp first
                with open(tmp_path, 'w') as f:
                    json.dump(state, f, indent=2, default=str)
                
                # Backup existing
                if self.filepath.exists():
                    self.filepath.rename(self.backup_path)
                
                # Atomic move
                tmp_path.rename(self.filepath)
                return True
                
            except IOError as e:
                logger.error(f"State save failed: {e}")
                return False
    
    def _default_state(self) -> Dict[str, Any]:
        return {
            'last_candle_time': None,
            'pending_signal': None,
            'pending_count': 0,
            'active_position': None,
            'entry_price': None,
            'position_size': 0,
            'portfolio': {'positions': {}, 'total_heat': 0.0},
            'trade_history': [],
            'daily_stats': {'trades': 0, 'pnl': 0.0, 'date': datetime.utcnow().date().isoformat()},
            'health': asdict(HealthMetrics())
        }


# =============================================================================
# KRAKEN API CLIENT
# =============================================================================

class KrakenClient:
    """Robust Kraken API client with health tracking."""
    
    def __init__(self, health: HealthMetrics):
        self.health = health
        self.base_url = Config.KRAKEN_API
    
    def _request(self, endpoint: str, params: Dict, retries: int = 3) -> Optional[Dict]:
        """Make request with full error handling and latency tracking."""
        url = f"{self.base_url}/{endpoint}"
        start = time.time()
        
        for attempt in range(retries):
            try:
                response = requests.get(url, params=params, timeout=10)
                latency = (time.time() - start) * 1000
                self.health.api_latency_ms = latency
                self.health.last_api_call = datetime.utcnow()
                self.health.daily_api_calls += 1
                
                response.raise_for_status()
                data = response.json()
                
                if data.get('error'):
                    raise RuntimeError(f"Kraken API error: {data['error']}")
                
                self.health.consecutive_failures = 0
                return data['result']
                
            except requests.RequestException as e:
                logger.warning(f"Request failed (attempt {attempt + 1}/{retries}): {e}")
                self.health.consecutive_failures += 1
                time.sleep(2 ** attempt)  # Exponential backoff
        
        logger.error(f"All {retries} attempts failed for {endpoint}")
        return None
    
    def get_ohlc(self, limit: int = 200) -> Optional[pd.DataFrame]:
        """Fetch candle data."""
        result = self._request("OHLC", {
            "pair": Config.SYMBOL,
            "interval": Config.INTERVAL_MINUTES,
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
        
        df["open_time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df["close_time"] = df["open_time"] + pd.Timedelta(minutes=Config.INTERVAL_MINUTES)
        
        # CRITICAL: Drop incomplete candle
        df = df.iloc[:-1]
        
        if len(df) > limit:
            df = df.iloc[-limit:]
        
        self.health.last_price_update = datetime.utcnow()
        
        return df.reset_index(drop=True)
    
    def get_order_book(self, depth: int = 100) -> Optional[Tuple[pd.DataFrame, pd.DataFrame]]:
        """Fetch order book for liquidity analysis."""
        result = self._request("Depth", {"pair": Config.SYMBOL, "count": depth})
        
        if not result:
            return None
        
        pair_key = list(result.keys())[0]
        data = result[pair_key]
        
        bids = pd.DataFrame(data["bids"], columns=["price", "volume", "timestamp"])
        asks = pd.DataFrame(data["asks"], columns=["price", "volume", "timestamp"])
        
        for col in ["price", "volume"]:
            bids[col] = pd.to_numeric(bids[col], errors="coerce")
            asks[col] = pd.to_numeric(asks[col], errors="coerce")
        
        return bids, asks
    
    def get_ticker(self) -> Optional[Dict]:
        """Get current price."""
        result = self._request("Ticker", {"pair": Config.SYMBOL})
        if not result:
            return None
        pair_key = list(result.keys())[0]
        return {
            'bid': float(result[pair_key]['b'][0]),
            'ask': float(result[pair_key]['a'][0]),
            'last': float(result[pair_key]['c'][0]),
            'volume': float(result[pair_key]['v'][1])
        }


# =============================================================================
# TECHNICAL INDICATORS
# =============================================================================

class Indicators:
    """Vectorized technical indicators."""
    
    @staticmethod
    def ema(series: pd.Series, span: int) -> pd.Series:
        return series.ewm(span=span, adjust=False).mean()
    
    @staticmethod
    def rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))
    
    @staticmethod
    def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
        macd_line = Indicators.ema(series, fast) - Indicators.ema(series, slow)
        signal_line = Indicators.ema(macd_line, signal)
        return macd_line, signal_line, macd_line - signal_line
    
    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def calculate_volatility(df: pd.DataFrame, lookback: int = 20) -> float:
        """Annualized volatility estimate."""
        returns = df['close'].pct_change().dropna()
        if len(returns) < lookback:
            return 0.0
        return returns.tail(lookback).std() * np.sqrt(365)  # Annualized
    
    @staticmethod
    def bollinger_bands(series: pd.Series, period: int = 20, std: float = 2.0):
        sma = series.rolling(period).mean()
        band = series.rolling(period).std() * std
        return sma, sma + band, sma - band


# =============================================================================
# MACRO DATA FETCHER
# =============================================================================

class MacroDataFetcher:
    """Fetch macro indicators from Yahoo Finance."""
    
    def __init__(self):
        self.cache: Dict[str, Any] = {}
        self.cache_time: Dict[str, datetime] = {}
        self.cache_duration = timedelta(minutes=15)
    
    def _get_cached(self, key: str, fetch_func) -> Any:
        """Cache macro data to avoid rate limits."""
        now = datetime.utcnow()
        if key in self.cache_time and (now - self.cache_time[key]) < self.cache_duration:
            return self.cache[key]
        
        try:
            data = fetch_func()
            self.cache[key] = data
            self.cache_time[key] = now
            return data
        except Exception as e:
            logger.error(f"Failed to fetch {key}: {e}")
            return self.cache.get(key)  # Return stale if available
    
    def get_vix(self) -> float:
        """Get VIX level."""
        def fetch():
            vix = yf.Ticker("^VIX")
            hist = vix.history(period="2d")
            return float(hist['Close'].iloc[-1]) if not hist.empty else 20.0
        
        return self._get_cached('vix', fetch) or 20.0
    
    def get_dxy_trend(self) -> float:
        """Get DXY 5-day trend."""
        def fetch():
            dxy = yf.Ticker("DX-Y.NYB")
            hist = dxy.history(period="5d")
            if len(hist) < 2:
                return 0.0
            return (hist['Close'].iloc[-1] / hist['Close'].iloc[0]) - 1
        
        return self._get_cached('dxy', fetch) or 0.0
    
    def get_sp500_trend(self) -> float:
        """Get S&P 500 trend for correlation."""
        def fetch():
            spy = yf.Ticker("SPY")
            hist = spy.history(period="5d")
            if len(hist) < 2:
                return 0.0
            return (hist['Close'].iloc[-1] / hist['Close'].iloc[0]) - 1
        
        return self._get_cached('spy', fetch) or 0.0
    
    def get_macro_context(self) -> Dict[str, float]:
        """Get complete macro context."""
        return {
            'vix': self.get_vix(),
            'dxy_trend': self.get_dxy_trend(),
            'spy_trend': self.get_sp500_trend()
        }


# =============================================================================
# SIGNAL GENERATION
# =============================================================================

class SignalEngine:
    """Multi-factor signal generation with regime detection."""
    
    def __init__(self, macro_fetcher: MacroDataFetcher):
        self.macro = macro_fetcher
        self.indicators = Indicators()
    
    def detect_regime(self, df: pd.DataFrame) -> Regime:
        """Detect market regime using volatility and trend."""
        if len(df) < 50:
            return Regime.UNKNOWN
        
        # Volatility check
        atr = self.indicators.atr(df).iloc[-1]
        atr_sma = self.indicators.atr(df).rolling(20).mean().iloc[-1]
        vol_ratio = atr / atr_sma if atr_sma > 0 else 1.0
        
        # Trend check
        price_sma20 = df['close'].rolling(20).mean().iloc[-1]
        price_sma50 = df['close'].rolling(50).mean().iloc[-1]
        
        if vol_ratio > 2.0:
            return Regime.CRASH
        elif vol_ratio > 1.5:
            return Regime.VOLATILE
        elif price_sma20 > price_sma50 * 1.02:
            return Regime.TRENDING
        elif abs(price_sma20 - price_sma50) / price_sma50 < 0.01:
            return Regime.RANGING
        else:
            return Regime.TRENDING  # Default to trending
    
    def calculate_macro_score(self) -> Tuple[int, Dict]:
        """Calculate macro filter score."""
        if not Config.ENABLE_MACRO_FILTER:
            return 0, {}
        
        context = self.macro.get_macro_context()
        score = 0
        
        # VIX check - high VIX = risk off
        if context['vix'] > Config.VIX_THRESHOLD:
            score -= 2
            logger.info(f"High VIX detected: {context['vix']:.1f}")
        elif context['vix'] < 15:
            score += 1  # Low vol environment favorable
        
        # DXY trend - strong dollar = crypto headwind
        if context['dxy_trend'] > Config.DXY_IMPACT_THRESHOLD:
            score -= 1
        elif context['dxy_trend'] < -Config.DXY_IMPACT_THRESHOLD:
            score += 1
        
        return score, context
    
    def momentum_vote(self, df: pd.DataFrame) -> int:
        """Multi-factor momentum."""
        close = df["close"]
        
        # EMA alignment
        ema9 = self.indicators.ema(close, 9).iloc[-1]
        ema21 = self.indicators.ema(close, 21).iloc[-1]
        ema55 = self.indicators.ema(close, 55).iloc[-1]
        
        ema_vote = 0
        if ema9 > ema21 > ema55:
            ema_vote = 1
        elif ema9 < ema21 < ema55:
            ema_vote = -1
        
        # MACD
        _, _, hist = self.indicators.macd(close)
        macd_vote = 1 if hist.iloc[-1] > 0 else -1
        
        # RSI with dynamic thresholds based on regime
        rsi_val = self.indicators.rsi(close).iloc[-1]
        if rsi_val > 65:
            rsi_vote = 1
        elif rsi_val < 35:
            rsi_vote = -1
        else:
            rsi_vote = 0
        
        votes = [ema_vote, macd_vote, rsi_vote]
        nonzero = [v for v in votes if v != 0]
        
        if len(nonzero) < 2:
            return 0
        
        return 1 if sum(nonzero) > 0 else -1 if sum(nonzero) < 0 else 0
    
    def pressure_vote(self, df: pd.DataFrame, ob_imbalance: float) -> int:
        """Volume and order flow pressure."""
        votes = []
        
        # Price momentum
        ret_20d = (df['close'].iloc[-1] / df['close'].iloc[-20]) - 1
        if ret_20d > 0.05:
            votes.append(1)
        elif ret_20d < -0.05:
            votes.append(-1)
        else:
            votes.append(0)
        
        # Volume trend
        vol_sma = df['volume'].rolling(20).mean().iloc[-1]
        recent_vol = df['volume'].iloc[-5:].mean()
        vol_trend = 1 if recent_vol > vol_sma else -1
        votes.append(vol_trend if abs(ret_20d) > 0.02 else 0)
        
        # Order book
        if abs(ob_imbalance) > 0.15:
            votes.append(1 if ob_imbalance > 0 else -1)
        else:
            votes.append(0)
        
        nonzero = [v for v in votes if v != 0]
        if len(nonzero) < 2:
            return 0
        
        score = sum(nonzero)
        agree = sum(1 for v in nonzero if v == (1 if score > 0 else -1))
        return (1 if score > 0 else -1) if agree / len(nonzero) >= 0.6 else 0
    
    def liquidity_vote(self, df: pd.DataFrame) -> int:
        """Support/resistance and volatility regime."""
        votes = []
        
        # Bollinger Band position
        sma, upper, lower = self.indicators.bollinger_bands(df['close'])
        last = df['close'].iloc[-1]
        
        if last > upper.iloc[-1]:
            votes.append(-1)  # Overbought
        elif last < lower.iloc[-1]:
            votes.append(1)   # Oversold
        else:
            votes.append(0)
        
        # Volatility regime
        atr = self.indicators.atr(df).iloc[-1]
        atr_sma = self.indicators.atr(df).rolling(20).mean().iloc[-1]
        
        if atr > atr_sma * 1.8:
            votes.append(0)  # Too volatile
        
        nonzero = [v for v in votes if v != 0]
        if not nonzero:
            return 0
        
        return 1 if sum(nonzero) > 0 else -1 if sum(nonzero) < 0 else 0
    
    def generate_signal(self, df: pd.DataFrame, ob_imbalance: float) -> SignalContext:
        """Generate complete signal with all factors."""
        context = SignalContext(
            timestamp=datetime.utcnow(),
            price=df['close'].iloc[-1]
        )
        
        # Technical pillars
        context.momentum_score = self.momentum_vote(df)
        context.pressure_score = self.pressure_vote(df, ob_imbalance)
        context.liquidity_score = self.liquidity_vote(df)
        
        # Regime detection
        context.regime = self.detect_regime(df)
        
        # Macro filter
        context.macro_score, macro_data = self.calculate_macro_score()
        context.vix_level = macro_data.get('vix', 0)
        context.dxy_trend = macro_data.get('dxy_trend', 0)
        
        # Volatility metrics
        context.atr_14 = self.indicators.atr(df).iloc[-1]
        vol = self.indicators.calculate_volatility(df)
        context.volatility_regime = "high" if vol > 0.8 else "low" if vol < 0.3 else "normal"
        
        # Composite signal
        votes = [context.momentum_score, context.pressure_score, context.liquidity_score]
        
        # Apply macro filter - require macro alignment for strong signals
        if context.macro_score <= -2:
            votes = [v * 0.5 for v in votes]  # Reduce conviction in bad macro
        
        nonzero = [v for v in votes if v != 0]
        
        if len(nonzero) >= 2:
            score = sum(nonzero)
            agree = sum(1 for v in nonzero if v == (1 if score > 0 else -1))
            
            if agree >= 2:
                context.composite_signal = Signal.LONG if score > 0 else Signal.SHORT
        
        # Calculate confidence (0.0 to 1.0)
        if context.composite_signal != Signal.WAIT:
            context.confidence = min(agree / len(nonzero) + 0.1 * abs(context.macro_score), 1.0)
        
        return context


# =============================================================================
# RISK MANAGEMENT
# =============================================================================

class RiskManager:
    """Institutional risk management."""
    
    def __init__(self, portfolio: PortfolioState):
        self.portfolio = portfolio
        self.indicators = Indicators()
    
    def calculate_position_size(
        self,
        signal: SignalContext,
        account_equity: float
    ) -> PositionSizing:
        """
        Volatility-targeted position sizing with Kelly Criterion.
        """
        sizing = PositionSizing()
        
        # Base size from Kelly
        win_rate = 0.55  # Assumed from backtesting
        avg_win = Config.TAKE_PROFIT_PCT
        avg_loss = Config.STOP_LOSS_PCT
        
        kelly = win_rate - ((1 - win_rate) / (avg_win / avg_loss)) if avg_loss > 0 else 0
        kelly = max(0, min(kelly, 0.5))  # Cap at 50%
        
        sizing.base_size = account_equity * kelly * Config.KELLY_FRACTION
        
        # Volatility scaling
        current_vol = signal.atr_14 / signal.price  # ATR as % of price
        target_vol = Config.TARGET_VOLATILITY
        
        if current_vol > 0:
            sizing.volatility_scalar = target_vol / current_vol
            sizing.volatility_scalar = max(0.25, min(sizing.volatility_scalar, 2.0))  # Cap scaling
        
        # Confidence adjustment
        sizing.confidence_adjustment = signal.confidence if signal.confidence > 0 else 0.5
        
        # Macro adjustment
        if signal.macro_score < -1:
            sizing.macro_adjustment = 0.5  # Reduce size in bad macro
        elif signal.macro_score > 0:
            sizing.macro_adjustment = 1.2  # Increase slightly in good macro
        
        # Calculate final
        sizing.final_size = (
            sizing.base_size * 
            sizing.volatility_scalar * 
            sizing.confidence_adjustment * 
            sizing.macro_adjustment
        )
        
        # Apply hard limits
        max_position = account_equity * Config.MAX_POSITION_PCT
        if sizing.final_size > max_position:
            sizing.final_size = max_position
            sizing.max_position_hit = True
        
        # Check portfolio heat
        position_heat = sizing.final_size * Config.STOP_LOSS_PCT
        available_heat = Config.PORTFOLIO_HEAT_LIMIT - self.portfolio.total_heat
        
        if position_heat > available_heat:
            scaling = available_heat / position_heat if position_heat > 0 else 0
            sizing.final_size *= scaling
            logger.warning(f"Position scaled due to heat limit: {scaling:.2%}")
        
        sizing.notional_value = sizing.final_size * signal.price
        
        return sizing
    
    def check_portfolio_heat(self) -> bool:
        """Check if we can add new positions."""
        self.portfolio.calculate_heat()
        return self.portfolio.total_heat < Config.PORTFOLIO_HEAT_LIMIT


# =============================================================================
# EXECUTION ENGINE
# =============================================================================

class ExecutionEngine:
    """Smart order execution with TWAP and slippage control."""
    
    def __init__(self, client: KrakenClient, health: HealthMetrics):
        self.client = client
        self.health = health
    
    def estimate_slippage(self, size: float, book: Tuple[pd.DataFrame, pd.DataFrame]) -> float:
        """Estimate slippage in basis points."""
        bids, asks = book
        mid = (bids['price'].iloc[0] + asks['price'].iloc[0]) / 2
        
        # Calculate weighted average fill price
        if size > 0:  # Buying
            cumulative = 0
            weighted_sum = 0
            for _, row in asks.iterrows():
                take = min(row['volume'], size - cumulative)
                weighted_sum += take * row['price']
                cumulative += take
                if cumulative >= size:
                    break
        else:  # Selling
            cumulative = 0
            weighted_sum = 0
            for _, row in bids.iterrows():
                take = min(row['volume'], abs(size) - cumulative)
                weighted_sum += take * row['price']
                cumulative += take
                if cumulative >= abs(size):
                    break
        
        if cumulative == 0:
            return 1000  # High slippage if no liquidity
        
        avg_price = weighted_sum / cumulative
        slippage = abs(avg_price - mid) / mid * 10000  # Convert to bps
        
        return slippage
    
    def create_execution_plan(self, direction: Signal, sizing: PositionSizing) -> ExecutionPlan:
        """Create TWAP execution plan."""
        plan = ExecutionPlan()
        plan.total_size = sizing.final_size
        plan.max_slippage_bps = Config.MAX_SLIPPAGE_BPS
        
        # Determine slices based on size
        if sizing.final_size > 100000:  # >$100k
            plan.slices = Config.TWAP_SLICES * 2
        elif sizing.final_size > 50000:  # >$50k
            plan.slices = Config.TWAP_SLICES
        else:
            plan.slices = 1
        
        plan.slice_size = plan.total_size / plan.slices
        plan.delay_between_slices = Config.EXECUTION_TIMEOUT // plan.slices
        
        return plan
    
    def execute_signal(
        self,
        direction: Signal,
        sizing: PositionSizing,
        context: SignalContext
    ) -> bool:
        """Execute with smart order routing."""
        if Config.DRY_RUN:
            logger.info(f"[DRY RUN] Would execute {direction.value} "
                       f"size=${sizing.final_size:,.2f} "
                       f"vol_scalar={sizing.volatility_scalar:.2f}")
            return True
        
        # Get order book for slippage estimation
        book = self.client.get_order_book()
        if not book:
            logger.error("Cannot execute: no order book data")
            return False
        
        # Check slippage
        slippage = self.estimate_slippage(sizing.final_size, book)
        if slippage > Config.MAX_SLIPPAGE_BPS:
            logger.warning(f"Slippage too high: {slippage:.0f} bps, aborting")
            return False
        
        # Create execution plan
        plan = self.create_execution_plan(direction, sizing)
        slices = plan.generate()
        
        logger.info(f"Executing {plan.slices} slices over {Config.EXECUTION_TIMEOUT}s")
        
        # Send to WunderTrading (simplified - in reality would use their API)
        return self._send_to_wunder(direction, sizing, context)
    
    def _send_to_wunder(
        self,
        direction: Signal,
        sizing: PositionSizing,
        context: SignalContext
    ) -> bool:
        """Send signal to WunderTrading webhook."""
        code_map = {
            Signal.LONG: Config.WUNDER_LONG_CODE,
            Signal.SHORT: Config.WUNDER_SHORT_CODE,
            Signal.EXIT: Config.WUNDER_EXIT_CODE
        }
        
        code = code_map.get(direction, "")
        if not code:
            logger.error(f"No webhook code for {direction}")
            return False
        
        # Adjust leverage based on volatility
        adjusted_leverage = Config.BASE_LEVERAGE * sizing.volatility_scalar
        adjusted_leverage = max(1, min(adjusted_leverage, 10))  # Cap at 10x
        
        payload = {
            "code": code,
            "orderType": Config.ORDER_TYPE,
            "amountPerTradeType": Config.AMOUNT_PER_TRADE_TYPE,
            "amountPerTrade": sizing.final_size / context.price,  # Convert to BTC
            "leverage": adjusted_leverage,
            "metadata": {
                "vol_scalar": sizing.volatility_scalar,
                "confidence": sizing.confidence_adjustment,
                "regime": context.regime.value,
                "atr": context.atr_14
            }
        }
        
        if direction in (Signal.LONG, Signal.SHORT):
            payload["stopLoss"] = {"priceDeviation": Config.STOP_LOSS_PCT}
            payload["takeProfits"] = [
                {"priceDeviation": Config.TAKE_PROFIT_PCT * 0.5, "portfolio": 0.5},
                {"priceDeviation": Config.TAKE_PROFIT_PCT, "portfolio": 0.5}
            ]
        
        try:
            response = requests.post(
                Config.WUNDER_WEBHOOK_URL,
                json=payload,
                timeout=10
            )
            response.raise_for_status()
            logger.info(f"Webhook success: {response.status_code}")
            return True
            
        except requests.RequestException as e:
            logger.error(f"Webhook failed: {e}")
            return False


# =============================================================================
# MAIN BOT
# =============================================================================

class InstitutionalBot:
    """Main trading bot orchestrating all components."""
    
    def __init__(self):
        self.health = HealthMetrics()
        self.circuit = CircuitBreaker(self.health)
        self.client = KrakenClient(self.health)
        self.state_mgr = StateManager(Config.STATE_FILE)
        self.macro = MacroDataFetcher()
        self.signal_engine = SignalEngine(self.macro)
        
        self.state = self.state_mgr.load()
        self.portfolio = PortfolioState(**self.state.get('portfolio', {}))
        self.risk_mgr = RiskManager(self.portfolio)
        self.execution = ExecutionEngine(self.client, self.health)
        
        self.running = False
        
        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.running = False
    
    def calculate_sleep_time(self) -> float:
        """Calculate seconds until next candle close."""
        now = datetime.utcnow()
        minutes_since_midnight = now.hour * 60 + now.minute
        minutes_into_interval = minutes_since_midnight % Config.INTERVAL_MINUTES
        minutes_until_close = Config.INTERVAL_MINUTES - minutes_into_interval
        
        # Add small randomization to avoid API thundering herd
        jitter = np.random.uniform(5, 15)
        sleep_seconds = minutes_until_close * 60 - now.second + jitter
        
        return max(sleep_seconds, 5)
    
    def update_portfolio_state(self, signal: Signal, sizing: PositionSizing, price: float):
        """Track positions and heat."""
        if signal in (Signal.LONG, Signal.SHORT):
            self.portfolio.positions[Config.SYMBOL] = {
                'direction': signal.value,
                'size': sizing.final_size,
                'entry_price': price,
                'stop_distance': Config.STOP_LOSS_PCT,
                'heat': sizing.final_size * Config.STOP_LOSS_PCT
            }
        elif signal == Signal.EXIT:
            if Config.SYMBOL in self.portfolio.positions:
                del self.portfolio.positions[Config.SYMBOL]
        
        self.portfolio.calculate_heat()
        self.state['portfolio'] = asdict(self.portfolio)
    
    def run_cycle(self) -> bool:
        """Execute one trading cycle."""
        # Health check
        can_trade, reason = self.health.can_execute()
        if not can_trade:
            logger.error(f"Cannot trade: {reason}")
            return False
        
        # Fetch data
        df = self.client.get_ohlc(limit=200)
        if df is None or len(df) < 55:
            if self.circuit.record_failure():
                logger.error("Circuit breaker triggered due to data failures")
            return False
        
        self.circuit.record_success()
        
        # Check if new candle
        last_close = df['close_time'].iloc[-1].isoformat()
        if self.state.get('last_candle_time') == last_close:
            return True  # Already processed
        
        logger.info(f"New candle @ {last_close} | Price: ${df['close'].iloc[-1]:,.2f}")
        
        # Get order book
        book = self.client.get_order_book()
        ob_imbalance = 0.0
        if book:
            bids, asks = book
            bid_vol = bids['volume'].sum()
            ask_vol = asks['volume'].sum()
            total = bid_vol + ask_vol
            ob_imbalance = (bid_vol - ask_vol) / total if total > 0 else 0
        
        # Generate signal
        context = self.signal_engine.generate_signal(df, ob_imbalance)
        
        logger.info(
            f"Signal: {context.composite_signal.value} | "
            f"Confidence: {context.confidence:.2%} | "
            f"Regime: {context.regime.value} | "
            f"Macro: {context.macro_score:+d} | "
            f"ATR: {context.atr_14:.2f}"
        )
        
        # Reconfirmation logic
        pending = self.state.get('pending_signal')
        pending_count = self.state.get('pending_count', 0)
        
        if context.composite_signal.value == pending:
            pending_count += 1
        else:
            pending = context.composite_signal.value
            pending_count = 1
        
        self.state['pending_signal'] = pending
        self.state['pending_count'] = pending_count
        
        # Execute if confirmed
        if pending in ('LONG', 'SHORT') and pending_count >= Config.RECONFIRM_BARS:
            if self.state.get('active_position') != pending:
                # Calculate position size
                account_equity = 100000  # TODO: Fetch from API
                sizing = self.risk_mgr.calculate_position_size(context, account_equity)
                
                logger.info(
                    f"Position sizing: ${sizing.final_size:,.2f} "
                    f"(vol_scalar={sizing.volatility_scalar:.2f}, "
                    f"conf={sizing.confidence_adjustment:.2f})"
                )
                
                if sizing.final_size > 0:
                    success = self.execution.execute_signal(
                        Signal(pending), sizing, context
                    )
                    if success:
                        self.state['active_position'] = pending
                        self.state['entry_price'] = context.price
                        self.update_portfolio_state(Signal(pending), sizing, context.price)
        
        elif pending == 'WAIT' and self.state.get('active_position') and pending_count >= Config.RECONFIRM_BARS:
            # Exit signal
            exit_sizing = PositionSizing(final_size=0)
            self.execution.execute_signal(Signal.EXIT, exit_sizing, context)
            self.state['active_position'] = None
            self.update_portfolio_state(Signal.EXIT, exit_sizing, context.price)
        
        self.state['last_candle_time'] = last_close
        self.state_mgr.save(self.state)
        
        return True
    
    def run(self):
        """Main loop."""
        logger.info("=" * 60)
        logger.info("Institutional Kraken Bot Starting")
        logger.info(f"Symbol: {Config.SYMBOL} | Interval: {Config.INTERVAL_MINUTES}m")
        logger.info(f"Dry Run: {Config.DRY_RUN} | Macro Filter: {Config.ENABLE_MACRO_FILTER}")
        logger.info("=" * 60)
        
        self.running = True
        
        while self.running:
            try:
                success = self.run_cycle()
                
                if not success:
                    time.sleep(60)
                    continue
                
                # Smart sleep
                sleep_time = self.calculate_sleep_time()
                next_check = datetime.utcnow() + timedelta(seconds=sleep_time)
                logger.info(f"Sleeping {sleep_time/60:.1f}m until next check @ {next_check:%H:%M:%S}")
                
                # Sleep in chunks to allow graceful shutdown
                slept = 0
                while slept < sleep_time and self.running:
                    time.sleep(min(10, sleep_time - slept))
                    slept += 10
                    
            except Exception as e:
                logger.exception(f"Error in main loop: {e}")
                if self.circuit.record_failure():
                    logger.error("Circuit breaker opened, pausing 15 minutes")
                    time.sleep(900)
                else:
                    time.sleep(60)
        
        logger.info("Bot shutdown complete")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    bot = InstitutionalBot()
    bot.run()
