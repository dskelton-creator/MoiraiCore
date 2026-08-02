#!/usr/bin/env python3
"""
MoiraiCore — Trader.dev Integration Module
Provides backtesting and strategy management via trader.dev MCP.
"""

import json
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from trader_dev_client import TraderDevClient

log = logging.getLogger("trader-dev-integration")


class TraderDevIntegration:
    """High-level integration with trader.dev for MoiraiCore."""

    def __init__(self, api_key: str = None):
        from trader_dev_client import DEFAULT_API_KEY
        self.api_key = api_key or DEFAULT_API_KEY
        self.client = TraderDevClient(self.api_key)

    def connect(self) -> bool:
        return self.client.connect()

    def get_credits(self) -> dict:
        """Check credit balance."""
        result = self.client.call_tool("get_credits", {})
        return self._parse_result(result)

    def search_strategies(self, limit: int = 10, sort: str = "sharpe") -> list:
        """Search public strategies."""
        result = self.client.call_tool("search_strategies", {
            "limit": limit,
            "sort": sort,
        })
        data = self._parse_result(result)
        return data.get("results", [])

    def create_strategy(self, name: str, pine_source: str, description: str = "") -> dict:
        """Create a new strategy."""
        result = self.client.call_tool("create_strategy", {
            "name": name,
            "pineSource": pine_source,
            "description": description,
        })
        return self._parse_result(result)

    def run_backtest(self, strategy_id: str, symbol: str = "BTCUSDT",
                     timeframe: str = "1h", from_date: str = None,
                     to_date: str = None) -> dict:
        """Queue a backtest for a saved strategy."""
        params = {
            "strategyId": strategy_id,
            "symbol": symbol,
            "timeframe": timeframe,
        }
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = self.client.call_tool("run_backtest", params)
        return self._parse_result(result)

    def quick_backtest(self, pine_source: str, symbol: str = "BTCUSDT",
                       timeframe: str = "1h", from_date: str = None,
                       to_date: str = None) -> dict:
        """Run a synchronous backtest with Pine Script source."""
        params = {
            "pineSource": pine_source,
            "symbol": symbol,
            "timeframe": timeframe,
        }
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        result = self.client.call_tool("quick_backtest", params)
        return self._parse_result(result)

    def get_backtest_result(self, job_id: str, wait: bool = True,
                            timeout: float = 60) -> dict:
        """Get backtest result by job ID."""
        result = self.client.call_tool("get_backtest_result", {
            "jobId": job_id,
            "wait": wait,
            "timeout": timeout,
        })
        return self._parse_result(result)

    def list_strategies(self, mode: str = "all") -> list:
        """List user's saved strategies."""
        result = self.client.call_tool("list_strategies", {"mode": mode})
        data = self._parse_result(result)
        return data.get("strategies", data if isinstance(data, list) else [])

    def fork_strategy(self, strategy_id: str) -> dict:
        """Fork a public strategy."""
        result = self.client.call_tool("fork_strategy", {"strategyId": strategy_id})
        return self._parse_result(result)

    def promote_strategy(self, strategy_id: str) -> dict:
        """Promote a strategy to deployed (live alerts)."""
        result = self.client.call_tool("promote_strategy", {"strategyId": strategy_id})
        return self._parse_result(result)

    def _parse_result(self, result: dict) -> dict:
        """Parse MCP tool result content."""
        if "error" in result:
            return {"error": result["error"]}
        if "content" in result:
            for item in result["content"]:
                if item.get("type") == "text":
                    try:
                        return json.loads(item["text"])
                    except json.JSONDecodeError:
                        return {"text": item["text"]}
        return result

    def close(self):
        self.client.close()


# ── Default Pine Script strategies ──────────────────────────────────────────

SMA_CROSSOVER_PINE = """//@version=5
strategy('SMA Crossover', overlay=true, default_qty_type=strategy.percent_of_equity, default_qty_value=10)
fastLength = input.int(10, 'Fast Length')
slowLength = input.int(30, 'Slow Length')
fast = ta.sma(close, fastLength)
slow = ta.sma(close, slowLength)
plot(fast, 'Fast', color=color.blue)
plot(slow, 'Slow', color=color.red)
if ta.crossover(fast, slow)
    strategy.entry('Long', strategy.long)
if ta.crossunder(fast, slow)
    strategy.close('Long')
"""

RSI_STRATEGY_PINE = """//@version=5
strategy('RSI Mean Reversion', overlay=true, default_qty_type=strategy.percent_of_equity, default_qty_value=10)
rsiLength = input.int(14, 'RSI Length')
overbought = input.int(70, 'Overbought')
oversold = input.int(30, 'Oversold')
rsi = ta.rsi(close, rsiLength)
if ta.crossover(rsi, oversold)
    strategy.entry('Long', strategy.long)
if ta.crossunder(rsi, overbought)
    strategy.close('Long')
"""


def main():
    """Demo: connect, check credits, search strategies, run a quick backtest."""
    integration = TraderDevIntegration()
    try:
        print("Connecting to trader.dev...")
        if not integration.connect():
            print("❌ Failed to connect")
            sys.exit(1)
        print("✅ Connected\n")

        # Check credits
        credits = integration.get_credits()
        print(f"Credits: {json.dumps(credits, indent=2)}\n")

        # Search top strategies
        strategies = integration.search_strategies(limit=5, sort="sharpe")
        print(f"Top {len(strategies)} strategies by Sharpe:")
        for s in strategies:
            name = s.get("name", "?")
            sharpe = s.get("result", {}).get("sharpeRatio", "?")
            win_rate = s.get("result", {}).get("winRatePct", "?")
            symbol = s.get("symbol", "?")
            tf = s.get("timeframe", "?")
            print(f"  • {name} ({symbol} {tf}): Sharpe={sharpe}, WinRate={win_rate}%")
        print()

        # Run a quick backtest
        print("Running SMA Crossover backtest on BTCUSDT 1h...")
        result = integration.quick_backtest(
            pine_source=SMA_CROSSOVER_PINE,
            symbol="BTCUSDT",
            timeframe="1h",
        )
        if "error" in result:
            print(f"❌ Backtest failed: {result['error']}")
        else:
            print(f"✅ Backtest complete:")
            print(json.dumps(result, indent=2)[:2000])

    finally:
        integration.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()
