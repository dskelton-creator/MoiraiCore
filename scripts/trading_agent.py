#!/usr/bin/env python3
"""
MoiraiCore — AI Trading Engine
Market data → Technical Analysis → Signal Generation → Trade Execution
Uses IG Markets API (trading-ig library) for AU-based trading.
"""

import json
import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

# ── Configuration ──────────────────────────────────────────────────────────

AGENT_OS_ROOT = Path(__file__).parent.parent
CONFIG_DIR = AGENT_OS_ROOT / "config" / "trading"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
LOG_FILE = CONFIG_DIR / "trades.log"
SIGNAL_LOG = CONFIG_DIR / "signals.json"

# ── Logging ────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("trading-agent")


# ── Config ─────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "account_type": "DEMO",  # DEMO or LIVE
    "username": "",
    "password": "",
    "api_key": "",
    "acc_number": "",
    "base_currency": "AUD",
    "risk_per_trade": 0.02,  # 2%
    "max_daily_loss": 0.03,  # 3%
    "max_open_positions": 3,
    "risk_reward_min": 2.0,
    "indicators_required": 3,  # of 5 must agree
    "markets": [
        "CS.D.EURUSD.MINI.IP",   # EUR/USD
        "CS.D.GBPUSD.MINI.IP",   # GBP/USD
        "CS.D.AUDUSD.MINI.IP",   # AUD/USD
        "IX.D.SPTRD.DAILY.IP",   # S&P 500
        "CC.D.GOLD.DIS.IP",       # Gold
    ],
    "timeframes": ["H4", "H1", "M15"],
    "paper_trading": True,  # Start with paper trading!
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG.copy()


def save_config(config: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# ── State ──────────────────────────────────────────────────────────────────

DEFAULT_STATE = {
    "account_balance": 10000.0,  # Default paper trading balance
    "daily_pnl": 0.0,
    "daily_trades": 0,
    "open_positions": [],
    "trade_history": [],
    "last_signal_time": None,
    "consecutive_losses": 0,
    "total_trades": 0,
    "winning_trades": 0,
    "losing_trades": 0,
}


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return {**DEFAULT_STATE, **json.load(f)}
    return DEFAULT_STATE.copy()


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ── Technical Indicators ───────────────────────────────────────────────────

def calc_rsi(prices: list[float], period: int = 14) -> float:
    """Calculate Relative Strength Index."""
    if len(prices) < period + 1:
        return 50.0  # neutral default
    deltas = np.diff(prices[-period - 1:])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains)
    avg_loss = np.mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_ema(prices: list[float], period: int) -> float:
    """Calculate Exponential Moving Average."""
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    multiplier = 2.0 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = (p - ema) * multiplier + ema
    return ema


def calc_macd(prices: list[float]) -> tuple[float, float, float]:
    """Calculate MACD, Signal, and Histogram."""
    if len(prices) < 26:
        return 0.0, 0.0, 0.0
    ema12 = calc_ema(prices, 12)
    ema26 = calc_ema(prices, 26)
    macd_line = ema12 - ema26
    # Simplified signal (9-period EMA of MACD)
    signal_line = macd_line * 0.8  # approximation
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calc_bollinger(prices: list[float], period: int = 20, std_dev: float = 2.0) -> tuple[float, float, float]:
    """Calculate Bollinger Bands (upper, middle, lower)."""
    if len(prices) < period:
        mid = prices[-1] if prices else 0.0
        return mid, mid, mid
    window = prices[-period:]
    middle = np.mean(window)
    std = np.std(window)
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def calc_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Calculate Average True Range for stop-loss sizing."""
    if len(highs) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return np.mean(trs[-period:]) if trs else 0.0


def calc_volume_avg(volumes: list[float], period: int = 20) -> float:
    """Calculate average volume."""
    if len(volumes) < period:
        return np.mean(volumes) if volumes else 0.0
    return np.mean(volumes[-period:])


# ── Signal Generation ──────────────────────────────────────────────────────

def generate_signal(
    prices: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
) -> dict:
    """
    Generate BUY/SELL/HOLD signal using multi-indicator confirmation.
    Returns signal dict with confidence score and reasoning.
    """
    if len(prices) < 30:
        return {"signal": "HOLD", "confidence": 0, "reasoning": "Insufficient data"}

    current_price = prices[-1]
    buy_score = 0
    sell_score = 0
    reasons = []

    # 1. RSI
    rsi = calc_rsi(prices)
    if rsi < 30:
        buy_score += 1
        reasons.append(f"RSI oversold ({rsi:.1f})")
    elif rsi > 70:
        sell_score += 1
        reasons.append(f"RSI overbought ({rsi:.1f})")
    elif 30 <= rsi <= 50:
        buy_score += 0.5
        reasons.append(f"RSI in buy zone ({rsi:.1f})")
    elif 50 <= rsi <= 70:
        sell_score += 0.5
        reasons.append(f"RSI in sell zone ({rsi:.1f})")

    # 2. MACD
    macd_line, signal_line, histogram = calc_macd(prices)
    if macd_line > signal_line and histogram > 0:
        buy_score += 1
        reasons.append("MACD bullish crossover")
    elif macd_line < signal_line and histogram < 0:
        sell_score += 1
        reasons.append("MACD bearish crossover")

    # 3. EMA Crossover
    ema9 = calc_ema(prices, 9)
    ema21 = calc_ema(prices, 21)
    if ema9 > ema21:
        buy_score += 1
        reasons.append(f"EMA9 ({ema9:.5f}) > EMA21 ({ema21:.5f})")
    else:
        sell_score += 1
        reasons.append(f"EMA9 ({ema9:.5f}) < EMA21 ({ema21:.5f})")

    # 4. Bollinger Bands
    upper, middle, lower = calc_bollinger(prices)
    if current_price <= lower:
        buy_score += 1
        reasons.append(f"Price at lower BB ({lower:.5f})")
    elif current_price >= upper:
        sell_score += 1
        reasons.append(f"Price at upper BB ({upper:.5f})")

    # 5. Volume
    if volumes:
        avg_vol = calc_volume_avg(volumes)
        if volumes[-1] > avg_vol * 1.2:
            if buy_score > sell_score:
                buy_score += 0.5
                reasons.append("Above-average volume confirms buy")
            elif sell_score > buy_score:
                sell_score += 0.5
                reasons.append("Above-average volume confirms sell")

    # Determine signal
    total_indicators = 5
    required = 3

    if buy_score >= required:
        signal = "BUY"
        confidence = min(int(buy_score), total_indicators)
    elif sell_score >= required:
        signal = "SELL"
        confidence = min(int(sell_score), total_indicators)
    else:
        signal = "HOLD"
        confidence = 0

    return {
        "signal": signal,
        "confidence": confidence,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "indicators": {
            "rsi": round(rsi, 2),
            "macd": round(macd_line, 6),
            "macd_signal": round(signal_line, 6),
            "ema9": round(ema9, 6),
            "ema21": round(ema21, 6),
            "bb_upper": round(upper, 6),
            "bb_middle": round(middle, 6),
            "bb_lower": round(lower, 6),
            "current_price": round(current_price, 6),
        },
        "reasoning": "; ".join(reasons) if reasons else "No clear setup",
    }


# ── Risk Management ────────────────────────────────────────────────────────

def calc_position_size(
    account_balance: float,
    risk_pct: float,
    stop_distance_pips: float,
    pip_value: float = 1.0,
) -> dict:
    """Calculate position size based on risk parameters."""
    risk_amount = account_balance * risk_pct
    if stop_distance_pips <= 0:
        return {"units": 0, "risk_amount": 0, "risk_pct": 0}
    units = risk_amount / (stop_distance_pips * pip_value)
    units = max(1, int(units))  # minimum 1 unit
    actual_risk = units * stop_distance_pips * pip_value
    actual_risk_pct = (actual_risk / account_balance) * 100 if account_balance > 0 else 0
    return {
        "units": units,
        "risk_amount": round(actual_risk, 2),
        "risk_pct": round(actual_risk_pct, 2),
    }


def check_risk_limits(state: dict, config: dict) -> tuple[bool, str]:
    """Check if trading is allowed based on risk limits."""
    # Daily loss limit
    daily_loss_pct = abs(state["daily_pnl"]) / state["account_balance"] if state["account_balance"] > 0 else 0
    if daily_loss_pct >= config["max_daily_loss"]:
        return False, f"Daily loss limit hit ({daily_loss_pct:.1%})"

    # Max open positions
    if len(state["open_positions"]) >= config["max_open_positions"]:
        return False, f"Max open positions ({config['max_open_positions']})"

    # Consecutive losses
    if state["consecutive_losses"] >= 3:
        return False, f"3 consecutive losses — pause trading"

    return True, "OK"


# ── IG Markets API Wrapper ─────────────────────────────────────────────────

class IGTradingAPI:
    """Wrapper for IG Markets API using trading-ig library."""

    def __init__(self, config: dict):
        self.config = config
        self.client = None
        self.connected = False

    def connect(self) -> bool:
        """Connect to IG Markets API."""
        try:
            from trading_ig import IGService, IGStreamService
            from trading_ig.config import config as ig_config

            self.client = IGService(
                self.config["username"],
                self.config["password"],
                self.config["api_key"],
                self.config["account_type"],
            )
            self.client.create_session()
            self.connected = True
            log.info("Connected to IG Markets API (%s)", self.config["account_type"])
            return True
        except ImportError:
            log.warning("trading-ig not installed. Run: pip install trading-ig")
            return False
        except Exception as e:
            log.error("IG connection failed: %s", e)
            return False

    def get_account_info(self) -> dict:
        """Get account balance and info."""
        if not self.connected:
            return {}
        try:
            accounts = self.client.fetch_accounts()
            for acc in accounts:
                if acc["accountData"].get("availableBalance") is not None:
                    return {
                        "balance": acc["accountData"]["availableBalance"],
                        "currency": acc["accountData"].get("currency", "AUD"),
                        "account_id": acc["accountId"],
                    }
        except Exception as e:
            log.error("Failed to fetch account: %s", e)
        return {}

    def get_market_data(self, epic: str, resolution: str = "H4", num_points: int = 100) -> dict:
        """Fetch historical price data for a market."""
        if not self.connected:
            return {}
        try:
            data = self.client.fetch_historical_prices_by_epic_and_num_points(
                epic, resolution, num_points
            )
            prices = [float(d["closePrice"]["bid"]) for d in data["prices"]]
            highs = [float(d["highPrice"]["bid"]) for d in data["prices"]]
            lows = [float(d["lowPrice"]["bid"]) for d in data["prices"]]
            volumes = [float(d["lastTradedVolume"]) for d in data["prices"]]
            return {"prices": prices, "highs": highs, "lows": lows, "volumes": volumes}
        except Exception as e:
            log.error("Failed to fetch market data for %s: %s", epic, e)
            return {}

    def place_trade(
        self, epic: str, direction: str, size: float, stop: float = None, limit: float = None
    ) -> dict:
        """Place a trade."""
        if not self.connected:
            return {"error": "Not connected"}
        try:
            order = {
                "epic": epic,
                "direction": direction,
                "size": size,
                "orderType": "MARKET",
                "currencyCode": self.config["base_currency"],
                "forceOpen": True,
            }
            if stop:
                order["stopDistance"] = str(stop)
            if limit:
                order["limitDistance"] = str(limit)

            result = self.client.create_open_position(order)
            log.info("Trade placed: %s %s %s → ref: %s", direction, size, epic, result.get("dealReference"))
            return result
        except Exception as e:
            log.error("Trade failed: %s", e)
            return {"error": str(e)}

    def close_position(self, deal_id: str) -> dict:
        """Close an open position."""
        if not self.connected:
            return {"error": "Not connected"}
        try:
            result = self.client.close_open_position(deal_id)
            log.info("Position closed: %s", deal_id)
            return result
        except Exception as e:
            log.error("Close failed: %s", e)
            return {"error": str(e)}

    def get_open_positions(self) -> list:
        """Get all open positions."""
        if not self.connected:
            return []
        try:
            positions = self.client.fetch_open_positions()
            return positions.get("positions", [])
        except Exception as e:
            log.error("Failed to fetch positions: %s", e)
            return []


# ── Paper Trading (Demo Mode) ──────────────────────────────────────────────

class YFinanceDataFeed:
    """Real market data from Yahoo Finance."""

    # Map IG epics to Yahoo Finance tickers
    EPIC_TO_YF = {
        "CS.D.EURUSD": "EURUSD=X",
        "CS.D.GBPUSD": "GBPUSD=X",
        "CS.D.AUDUSD": "AUDUSD=X",
        "IX.D.SPTRD": "^GSPC",
        "CC.D.GOLD": "GC=F",
    }

    # Map IG resolution to yfinance interval
    RESOLUTION_MAP = {
        "M15": "15m",
        "H1": "1h",
        "H4": "1h",  # yfinance doesn't have 4h, use 1h and resample
        "D1": "1d",
    }

    @classmethod
    def get_ticker(cls, epic: str) -> str | None:
        for prefix, ticker in cls.EPIC_TO_YF.items():
            if epic.startswith(prefix):
                return ticker
        return None

    @classmethod
    def fetch(cls, epic: str, resolution: str = "H4", num_points: int = 100) -> dict | None:
        """Fetch real market data. Returns None on failure."""
        import yfinance as yf

        ticker = cls.get_ticker(epic)
        if not ticker:
            log.warning("No yfinance mapping for %s", epic)
            return None

        interval = cls.RESOLUTION_MAP.get(resolution, "1h")
        # Request extra bars so we have enough after any filtering
        period_map = {"15m": "7d", "1h": "60d", "1d": "1y"}
        period = period_map.get(interval, "60d")

        try:
            data = yf.download(ticker, period=period, interval=interval, progress=False)
            if data.empty:
                log.warning("No data returned for %s (%s)", epic, ticker)
                return None

            # Flatten multi-index columns if present
            if hasattr(data.columns, 'levels'):
                data.columns = data.columns.get_level_values(0)

            # For H4, resample from 1h
            if resolution == "H4" and interval == "1h":
                data = data.resample("4h").agg({
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum",
                }).dropna()

            # Take the last num_points bars
            data = data.tail(num_points)

            prices = data["Close"].tolist()
            highs = data["High"].tolist()
            lows = data["Low"].tolist()
            volumes = data["Volume"].tolist() if "Volume" in data else [0.0] * len(prices)

            if len(prices) < 30:
                log.warning("Insufficient data for %s: %d bars", epic, len(prices))
                return None

            log.info("Fetched %d bars for %s (%s)", len(prices), epic, ticker)
            return {
                "prices": prices,
                "highs": highs,
                "lows": lows,
                "volumes": volumes,
                "source": "yfinance",
                "ticker": ticker,
            }
        except Exception as e:
            log.error("yfinance fetch failed for %s: %s", epic, e)
            return None


class PaperTradingAPI:
    """Simulated trading for testing strategies without real money."""

    def __init__(self, initial_balance: float = 10000.0, use_real_data: bool = False):
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.positions = []
        self.trade_log = []
        self.connected = True
        self.use_real_data = use_real_data
        mode = "real data (yfinance)" if use_real_data else "synthetic data"
        log.info("Paper trading mode (%s) — starting balance: $%.2f", mode, initial_balance)

    def get_account_info(self) -> dict:
        return {"balance": self.balance, "currency": "AUD", "account_id": "PAPER"}

    def get_market_data(self, epic: str, resolution: str = "H4", num_points: int = 100) -> dict:
        """Fetch market data — real via yfinance if available, else synthetic."""
        # Try real data first if enabled
        if self.use_real_data:
            real_data = YFinanceDataFeed.fetch(epic, resolution, num_points)
            if real_data:
                return real_data
            log.warning("Falling back to synthetic data for %s", epic)

        # Synthetic fallback
        return self._generate_synthetic(epic, num_points)

    @staticmethod
    def _generate_synthetic(epic: str, num_points: int = 100) -> dict:
        """Generate synthetic market data for testing."""
        np.random.seed(int(time.time() * 1000) % (2**31))
        base_price = {
            "CS.D.EURUSD": 1.0850,
            "CS.D.GBPUSD": 1.2650,
            "CS.D.AUDUSD": 0.6550,
            "IX.D.SPTRD": 5450.0,
            "CC.D.GOLD": 2350.0,
        }
        price_key = next((k for k in base_price if epic.startswith(k)), None)
        base = base_price[price_key] if price_key else 1.0
        returns = np.random.normal(0.0001, 0.005, num_points)
        prices = [base]
        for r in returns:
            prices.append(prices[-1] * (1 + r))
        highs = [p * (1 + abs(np.random.normal(0, 0.002))) for p in prices]
        lows = [p * (1 - abs(np.random.normal(0, 0.002))) for p in prices]
        volumes = [np.random.uniform(100, 500) for _ in prices]
        return {"prices": prices, "highs": highs, "lows": lows, "volumes": volumes, "source": "synthetic"}

    def place_trade(self, epic: str, direction: str, size: float, stop: float = None, limit: float = None) -> dict:
        """Simulate placing a trade."""
        deal_id = f"PAPER-{len(self.trade_log) + 1:06d}"
        position = {
            "dealReference": deal_id,
            "epic": epic,
            "direction": direction,
            "size": size,
            "stop": stop,
            "limit": limit,
            "open_time": datetime.now().isoformat(),
            "status": "OPEN",
        }
        self.positions.append(position)
        self.trade_log.append(position)
        log.info("[PAPER] %s %s %s units → %s", direction, size, epic, deal_id)
        return {"dealReference": deal_id, "dealStatus": "ACCEPTED"}

    def close_position(self, deal_id: str) -> dict:
        """Simulate closing a position."""
        for p in self.positions:
            if p["dealReference"] == deal_id:
                p["status"] = "CLOSED"
                self.positions.remove(p)
                log.info("[PAPER] Closed: %s", deal_id)
                return {"dealStatus": "ACCEPTED"}
        return {"error": "Position not found"}

    def get_open_positions(self) -> list:
        return self.positions


# ── Main Trading Loop ─────────────────────────────────────────────────────

def run_trading_cycle(config: dict, state: dict) -> dict:
    """Run one complete trading cycle: analyse all markets, generate signals, execute."""
    results = {
        "timestamp": datetime.now().isoformat(),
        "signals": [],
        "trades": [],
        "errors": [],
    }

    # Connect to API
    if config.get("paper_trading", True):
        use_real = config.get("use_real_data", False)
        api = PaperTradingAPI(
            initial_balance=state.get("account_balance", 10000.0),
            use_real_data=use_real,
        )
    else:
        api = IGTradingAPI(config)
        if not api.connect():
            results["errors"].append("Failed to connect to IG API")
            return results

    # Update account info
    account = api.get_account_info()
    if account:
        state["account_balance"] = account.get("balance", state["account_balance"])

    # Check risk limits
    can_trade, reason = check_risk_limits(state, config)
    if not can_trade:
        log.warning("Trading blocked: %s", reason)
        results["errors"].append(reason)
        return results

    # Analyse each market
    resolution = config.get("resolution", "H4")
    for epic in config["markets"]:
        try:
            data = api.get_market_data(epic, resolution=resolution)
            if not data or not data.get("prices"):
                continue

            signal = generate_signal(
                data["prices"], data["highs"], data["lows"], data["volumes"]
            )
            signal["epic"] = epic
            signal["timestamp"] = datetime.now().isoformat()
            results["signals"].append(signal)

            log.info("[%s] %s (confidence: %d/5) — %s",
                     epic, signal["signal"], signal["confidence"], signal["reasoning"])

            # Execute if signal is strong enough
            if signal["signal"] in ("BUY", "SELL") and signal["confidence"] >= config["indicators_required"]:
                atr = calc_atr(data["highs"], data["lows"], data["prices"])
                current_price = data["prices"][-1]

                # Calculate stop and target
                stop_distance = max(atr * 1.5, 0.0010)  # minimum 10 pips
                if signal["signal"] == "BUY":
                    stop_price = current_price - stop_distance
                    target_price = current_price + (stop_distance * config["risk_reward_min"])
                else:
                    stop_price = current_price + stop_distance
                    target_price = current_price - (stop_distance * config["risk_reward_min"])

                # Position sizing
                sizing = calc_position_size(
                    state["account_balance"],
                    config["risk_per_trade"],
                    stop_distance,
                )

                if sizing["units"] > 0:
                    trade = api.place_trade(
                        epic=epic,
                        direction=signal["signal"],
                        size=sizing["units"],
                        stop=round(stop_distance, 5),
                        limit=round(abs(target_price - current_price), 5),
                    )
                    trade["signal"] = signal
                    trade["sizing"] = sizing
                    results["trades"].append(trade)
                    state["daily_trades"] += 1
                    state["total_trades"] += 1

        except Exception as e:
            log.error("Error processing %s: %s", epic, e)
            results["errors"].append(f"{epic}: {str(e)}")

    # Save state
    save_state(state)

    # Log signals
    if SIGNAL_LOG.exists():
        with open(SIGNAL_LOG) as f:
            history = json.load(f)
    else:
        history = []
    history.append(results)
    with open(SIGNAL_LOG, "w") as f:
        json.dump(history[-100:], f, indent=2, default=str)  # keep last 100

    return results


def generate_report(state: dict, results: dict, config: dict | None = None) -> str:
    """Generate a human-readable trading report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cfg = config or {}
    data_source = "Real (yfinance)" if cfg.get("use_real_data") else "Synthetic"
    lines = [
        f"## Trading Report — {now} AEST",
        "",
        f"**Data Source**: {data_source}",
        f"**Account Balance**: ${state['account_balance']:,.2f}",
        f"**Daily P&L**: ${state['daily_pnl']:,.2f} ({state['daily_pnl']/state['account_balance']*100:.1f}%)" if state['account_balance'] > 0 else "",
        f"**Open Positions**: {len(state['open_positions'])}/{state.get('max_open_positions', 3)}",
        f"**Total Trades**: {state['total_trades']} (W:{state['winning_trades']} L:{state['losing_trades']})",
        "",
        "### Signals Generated",
    ]

    for sig in results.get("signals", []):
        lines.append(f"- **{sig['epic']}**: {sig['signal']} (confidence: {sig['confidence']}/5)")
        lines.append(f"  - {sig['reasoning']}")
        ind = sig.get("indicators", {})
        lines.append(f"  - RSI: {ind.get('rsi','—')} | MACD: {ind.get('macd','—')} | Price: {ind.get('current_price','—')}")

    if results.get("trades"):
        lines.append("")
        lines.append("### Trades Executed")
        for t in results["trades"]:
            lines.append(f"- {t['signal']['signal']} {t['sizing']['units']} units @ {t.get('dealReference','—')}")

    if results.get("errors"):
        lines.append("")
        lines.append("### ⚠️ Issues")
        for e in results["errors"]:
            lines.append(f"- {e}")

    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MoiraiCore Trading Agent")
    parser.add_argument("--config", help="Path to config JSON", default=str(CONFIG_FILE))
    parser.add_argument("--paper", action="store_true", help="Force paper trading mode")
    parser.add_argument("--report", action="store_true", help="Generate report only")
    parser.add_argument("--setup", action="store_true", help="Interactive setup wizard")
    parser.add_argument("--backtest", action="store_true", help="Run backtest via trader.dev")
    parser.add_argument("--backtest-symbol", default="BTCUSDT", help="Backtest symbol (default: BTCUSDT)")
    parser.add_argument("--backtest-tf", default="1h", help="Backtest timeframe (default: 1h)")
    parser.add_argument("--search-strategies", action="store_true", help="Search trader.dev public strategies")
    parser.add_argument("--real-data", action="store_true", help="Use real market data via yfinance (paper trading)")
    parser.add_argument("--resolution", default="H4", help="Data resolution: M15, H1, H4, D1 (default: H4)")
    args = parser.parse_args()

    config = load_config()
    state = load_state()

    if args.paper:
        config["paper_trading"] = True

    if args.real_data:
        config["use_real_data"] = True

    if args.resolution:
        config["resolution"] = args.resolution

    if args.setup:
        print("🤖 MoiraiCore Trading Agent — Setup Wizard")
        print("=" * 50)
        config["username"] = input("IG Username: ").strip() or config["username"]
        config["password"] = input("IG Password: ").strip() or config["password"]
        config["api_key"] = input("IG API Key: ").strip() or config["api_key"]
        config["acc_number"] = input("Account Number: ").strip() or config["acc_number"]
        config["account_type"] = input("Account type (DEMO/LIVE) [DEMO]: ").strip().upper() or "DEMO"
        balance = input("Starting balance for paper trading [10000]: ").strip()
        if balance:
            state["account_balance"] = float(balance)
        config["paper_trading"] = input("Paper trading? (y/n) [y]: ").strip().lower() != "n"
        save_config(config)
        save_state(state)
        print("✅ Config saved to", CONFIG_FILE)
        sys.exit(0)

    if args.report:
        print(generate_report(state, {"signals": [], "trades": [], "errors": []}))
        sys.exit(0)

    # ── Trader.dev backtesting ──────────────────────────────────────────────
    if args.search_strategies or args.backtest:
        try:
            from trader_dev_integration import TraderDevIntegration, SMA_CROSSOVER_PINE, RSI_STRATEGY_PINE
            td = TraderDevIntegration()
            if not td.connect():
                log.error("Failed to connect to trader.dev")
                sys.exit(1)

            if args.search_strategies:
                print("\n🔍 Searching trader.dev public strategies...")
                strategies = td.search_strategies(limit=10, sort="sharpe")
                print(f"\nTop {len(strategies)} strategies by Sharpe ratio:")
                for i, s in enumerate(strategies, 1):
                    name = s.get("name", "?")
                    symbol = s.get("symbol", "?")
                    tf = s.get("timeframe", "?")
                    res = s.get("result", {})
                    sharpe = res.get("sharpeRatio", "?")
                    win_rate = res.get("winRatePct", "?")
                    net_pct = res.get("netProfitPct", "?")
                    trades = res.get("totalTrades", "?")
                    print(f"  {i:2d}. {name}")
                    print(f"      {symbol} {tf} | Sharpe: {sharpe} | Win: {win_rate}% | Return: {net_pct}% | Trades: {trades}")
                    print(f"      ID: {s.get('id', '?')}")
                td.close()
                sys.exit(0)

            if args.backtest:
                print(f"\n📊 Running backtest via trader.dev: {args.backtest_symbol} {args.backtest_tf}")
                result = td.quick_backtest(
                    pine_source=SMA_CROSSOVER_PINE,
                    symbol=args.backtest_symbol,
                    timeframe=args.backtest_tf,
                )
                if isinstance(result, dict) and "error" in result:
                    print(f"❌ Backtest failed: {result['error']}")
                else:
                    print("\n✅ Backtest Results:")
                    print(json.dumps(result, indent=2, default=str)[:3000])
                td.close()
                sys.exit(0)
        except ImportError as e:
            log.error("trader.dev integration not available: %s", e)
            print("❌ trader.dev integration not available. Ensure trader_dev_integration.py is in scripts/")
            sys.exit(1)
        except Exception as e:
            log.error("trader.dev error: %s", e)
            print(f"❌ trader.dev error: {e}")
            sys.exit(1)

    # Run trading cycle
    log.info("Starting trading cycle (paper=%s)", config.get("paper_trading", True))
    results = run_trading_cycle(config, state)
    report = generate_report(state, results, config)
    print(report)

    # Save report
    report_file = CONFIG_DIR / f"report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    with open(report_file, "w") as f:
        f.write(report)
    log.info("Report saved to %s", report_file)
