"""
Alpaca broker: place/cancel orders, get positions, account equity.
Uses alpaca-py TradingClient. Credentials from config (.env).
"""

from typing import Optional

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False

# Project root on path when run from strategy_ide or parent
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER, require_alpaca_credentials


def get_client() -> "TradingClient":
    """Return TradingClient; raise if credentials missing or alpaca-py not installed."""
    if not _ALPACA_AVAILABLE:
        raise ImportError(
            "alpaca-py is required for execution. Install: pip install alpaca-py"
        )
    require_alpaca_credentials()
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)


def get_account_equity() -> float:
    """Return current account equity (cash + positions)."""
    client = get_client()
    account = client.get_account()
    return float(account.equity)


def get_positions() -> list[dict]:
    """
    Return list of open positions. Each dict: symbol, qty, side, market_value, unrealized_pl, etc.
    """
    client = get_client()
    positions = client.get_all_positions()
    out = []
    for p in positions:
        out.append({
            "symbol": p.symbol,
            "qty": float(p.qty),
            "side": str(p.side),
            "market_value": float(p.market_value) if p.market_value else 0.0,
            "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else 0.0,
            "current_price": float(p.current_price) if p.current_price else 0.0,
            "avg_entry_price": float(p.avg_entry_price) if p.avg_entry_price else 0.0,
        })
    return out


def place_market_order(
    symbol: str,
    qty: float,
    side: str,
    time_in_force: str = "day",
) -> dict:
    """
    Place a market order. side: 'buy' | 'sell'.

    Returns
    -------
    dict
        Order info (id, status, filled_qty, etc.) or raises on error.
    """
    client = get_client()
    side_enum = OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL
    tif = TimeInForce.DAY if time_in_force.lower() == "day" else TimeInForce.GTC
    req = MarketOrderRequest(
        symbol=symbol,
        qty=abs(qty),
        side=side_enum,
        time_in_force=tif,
    )
    order = client.submit_order(req)
    return {
        "id": str(order.id),
        "symbol": order.symbol,
        "qty": float(order.qty) if order.qty else 0,
        "side": str(order.side),
        "status": str(order.status),
        "filled_qty": float(order.filled_qty) if order.filled_qty else 0,
        "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
    }


def place_limit_order(
    symbol: str,
    qty: float,
    side: str,
    limit_price: float,
    time_in_force: str = "day",
) -> dict:
    """Place a limit order. Returns order info dict."""
    client = get_client()
    side_enum = OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL
    tif = TimeInForce.DAY if time_in_force.lower() == "day" else TimeInForce.GTC
    req = LimitOrderRequest(
        symbol=symbol,
        qty=abs(qty),
        side=side_enum,
        limit_price=limit_price,
        time_in_force=tif,
    )
    order = client.submit_order(req)
    return {
        "id": str(order.id),
        "symbol": order.symbol,
        "qty": float(order.qty) if order.qty else 0,
        "side": str(order.side),
        "limit_price": limit_price,
        "status": str(order.status),
    }


def cancel_order(order_id: str) -> bool:
    """Cancel an open order. Returns True if cancelled."""
    client = get_client()
    client.cancel_order_by_id(order_id)
    return True


def get_open_orders(symbol: Optional[str] = None) -> list[dict]:
    """Get open orders, optionally filtered by symbol."""
    client = get_client()
    orders = client.get_orders(status="open", symbol=symbol) if symbol else client.get_orders(status="open")
    return [
        {
            "id": str(o.id),
            "symbol": o.symbol,
            "qty": float(o.qty) if o.qty else 0,
            "side": str(o.side),
            "status": str(o.status),
        }
        for o in orders
    ]


if __name__ == "__main__":
    require_alpaca_credentials()
    eq = get_account_equity()
    print("Equity:", eq)
    positions = get_positions()
    print("Positions:", positions)
