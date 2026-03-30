"""
Position sizing: fixed risk %, ATR-based stop sizing, fractional Kelly.
All functions return the number of shares (or dollar amount) to trade.
"""

from typing import Optional

import pandas as pd


def fixed_risk_shares(
    equity: float,
    price: float,
    risk_pct: float = 0.01,
    stop_loss_pct: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
) -> int:
    """
    Size position so that if stopped out, loss = risk_pct of equity.
    Provide either stop_loss_pct (e.g. 0.02 for 2%) or stop_loss_price.

    Returns
    -------
    int
        Number of shares (floor).
    """
    if price <= 0 or equity <= 0 or risk_pct <= 0:
        return 0
    risk_amount = equity * risk_pct
    if stop_loss_price is not None:
        if stop_loss_price >= price:
            return 0
        loss_per_share = price - stop_loss_price
    elif stop_loss_pct is not None and stop_loss_pct > 0:
        loss_per_share = price * stop_loss_pct
    else:
        raise ValueError("Provide either stop_loss_pct or stop_loss_price.")
    if loss_per_share <= 0:
        return 0
    shares = risk_amount / loss_per_share
    return int(shares)


def atr_sized_shares(
    equity: float,
    price: float,
    atr: float,
    atr_mult: float = 2.0,
    risk_pct: float = 0.01,
    max_position_pct: float = 0.25,
) -> int:
    """
    Size using ATR-based stop: stop = entry - atr_mult * ATR (long).
    Risk amount = risk_pct * equity. Cap position at max_position_pct of equity.

    Parameters
    ----------
    equity, price, atr : float
        Current account equity, entry price, ATR value.
    atr_mult : float
        Stop distance in ATRs (e.g. 2.0 = 2 ATRs below entry).
    risk_pct, max_position_pct : float
        Risk per trade and max fraction of equity in this position.

    Returns
    -------
    int
        Number of shares.
    """
    if price <= 0 or equity <= 0 or atr <= 0:
        return 0
    stop_distance = atr_mult * atr
    stop_price = price - stop_distance
    if stop_price <= 0:
        stop_price = price * 0.01
        stop_distance = price - stop_price
    risk_amount = equity * risk_pct
    loss_per_share = stop_distance
    shares_by_risk = risk_amount / loss_per_share
    max_notional = equity * max_position_pct
    shares_by_cap = max_notional / price
    shares = min(shares_by_risk, shares_by_cap)
    return int(shares)


def kelly_fraction(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    fraction: float = 0.25,
) -> float:
    """
    Fractional Kelly: f = fraction * (p*b - q)/b where b = avg_win/|avg_loss|,
    p = win_rate, q = 1-p. avg_win_pct and avg_loss_pct in decimal (e.g. 0.05 for 5%).

    Returns
    -------
    float
        Fraction of equity to risk (0..1). Use with fixed_risk or position sizing.
    """
    if avg_loss_pct >= 0:
        return 0.0
    p = win_rate
    q = 1.0 - p
    b = abs(avg_win_pct / avg_loss_pct)
    kelly = (p * b - q) / b
    kelly = max(0.0, min(1.0, kelly))
    return fraction * kelly


def kelly_sized_shares(
    equity: float,
    price: float,
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    kelly_fraction_param: float = 0.25,
    stop_loss_pct: float = 0.02,
) -> int:
    """
    Size position using fractional Kelly. Risk per trade = kelly_fraction(win_rate, avg_win, avg_loss)
    of equity; stop at stop_loss_pct. avg_win_pct and avg_loss_pct in decimal.

    Returns
    -------
    int
        Number of shares.
    """
    frac = kelly_fraction(
        win_rate,
        avg_win_pct,
        avg_loss_pct,
        fraction=kelly_fraction_param,
    )
    risk_pct = frac
    return fixed_risk_shares(
        equity=equity,
        price=price,
        risk_pct=risk_pct,
        stop_loss_pct=stop_loss_pct,
    )
