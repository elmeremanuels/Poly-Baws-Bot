"""Position manager for the Stoplicht Scalper."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Position:
    direction: str        # "UP" or "DOWN"
    entry_price: float
    size_eur: float
    peak_price: float
    trailing_active: bool = False

    @property
    def token_direction(self) -> str:
        return "YES" if self.direction == "UP" else "NO"


@dataclass
class WindowPositions:
    window_id: str
    main: Optional[Position] = None
    hedge: Optional[Position] = None
    _closed: list = field(default_factory=list)

    def can_open_main(self) -> bool:
        return self.main is None

    def can_open_hedge(self) -> bool:
        return self.hedge is None

    def open_main(self, direction: str, entry_price: float, size_eur: float) -> None:
        self.main = Position(direction=direction, entry_price=entry_price,
                             size_eur=size_eur, peak_price=entry_price)

    def open_hedge(self, direction: str, entry_price: float, size_eur: float) -> None:
        self.hedge = Position(direction=direction, entry_price=entry_price,
                              size_eur=size_eur, peak_price=entry_price)

    def close_main(self, exit_price: float, reason: str) -> float:
        if not self.main:
            return 0.0
        pnl = _calc_pnl(self.main, exit_price)
        self._closed.append({"slot": "main", "direction": self.main.direction,
                              "entry": self.main.entry_price, "exit": exit_price,
                              "size_eur": self.main.size_eur, "pnl": pnl, "reason": reason})
        self.main = None
        return pnl

    def close_hedge(self, exit_price: float, reason: str) -> float:
        if not self.hedge:
            return 0.0
        pnl = _calc_pnl(self.hedge, exit_price)
        self._closed.append({"slot": "hedge", "direction": self.hedge.direction,
                              "entry": self.hedge.entry_price, "exit": exit_price,
                              "size_eur": self.hedge.size_eur, "pnl": pnl, "reason": reason})
        self.hedge = None
        return pnl

    @property
    def total_pnl(self) -> float:
        return round(sum(t["pnl"] for t in self._closed), 4)

    @property
    def trades_count(self) -> int:
        return len(self._closed)


def _calc_pnl(pos: Position, exit_price: float) -> float:
    if pos.entry_price <= 0:
        return 0.0
    shares = pos.size_eur / pos.entry_price
    gross = (exit_price - pos.entry_price) * shares
    fee_entry = 0.018 * min(pos.entry_price, 1 - pos.entry_price) / 0.5 * pos.size_eur
    fee_exit = (0.018 * min(exit_price, 1 - exit_price) / 0.5 * (shares * exit_price)
                if exit_price < 1.0 else 0.0)
    return round(gross - fee_entry - fee_exit, 4)


def get_position_sizes() -> dict:
    """Portfolio-based sizing: €5 main + €1 hedge per €100 portfolio."""
    try:
        from .db_sync import get_state
        portfolio_eur = float(get_state("portfolio_usdc") or 100.0)
    except Exception:
        portfolio_eur = 100.0
    from .config_loader import CONFIG
    sizing = CONFIG.get("stoplicht_scalper", {}).get("sizing", {})
    brackets = max(1, int(portfolio_eur // 100))
    return {
        "main_eur": brackets * float(sizing.get("main_pct_per_100", 5.0)),
        "hedge_eur": brackets * float(sizing.get("hedge_pct_per_100", 1.0)),
    }
