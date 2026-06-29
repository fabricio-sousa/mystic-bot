"""
Thin wrapper around Alpaca's TradingClient (alpaca-py).

Isolates every Alpaca call so the rest of the bot deals in plain floats and
dicts. All money values from Alpaca arrive as strings, so they're cast here.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

import config


@dataclass
class Account:
    equity: float          # total account value (cash + positions)
    last_equity: float     # equity at previous close (for "today" change)
    cash: float            # settled + unsettled cash
    buying_power: float
    market_open: bool


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    cost_basis: float
    unrealized_pl: float
    unrealized_plpc: float   # as a fraction, e.g. 0.0512 = +5.12%


class Broker:
    def __init__(self):
        key, secret = config.load_credentials()
        self.client = TradingClient(key, secret, paper=config.PAPER)

    # -- reads ------------------------------------------------------------- #
    def get_account(self) -> Account:
        a = self.client.get_account()
        clock = self.client.get_clock()
        return Account(
            equity=float(a.equity),
            last_equity=float(a.last_equity),
            cash=float(a.cash),
            buying_power=float(a.buying_power),
            market_open=bool(clock.is_open),
        )

    def get_positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for p in self.client.get_all_positions():
            out[p.symbol] = Position(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price),
                market_value=float(p.market_value),
                cost_basis=float(p.cost_basis),
                unrealized_pl=float(p.unrealized_pl),
                unrealized_plpc=float(p.unrealized_plpc),
            )
        return out

    def open_order_symbols(self) -> set[str]:
        """Symbols that currently have an unfilled order. Used to avoid
        deploying the same cash twice while orders are still working."""
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        return {o.symbol for o in self.client.get_orders(filter=req)}

    # -- writes ------------------------------------------------------------ #
    def buy_notional(self, symbol: str, usd: float):
        """Submit a fractional market buy for a dollar amount."""
        order = MarketOrderRequest(
            symbol=symbol,
            notional=round(usd, 2),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        return self.client.submit_order(order_data=order)
