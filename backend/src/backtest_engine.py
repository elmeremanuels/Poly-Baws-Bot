"""
Scenario replay / backtest engine.

Replays closed trades from trades.db under different strategy parameters to find
what combination of conviction threshold, regime filter, and OFI filter would
have produced the best risk-adjusted returns.

No hypothetical price data needed — outcomes (net_pnl) are real. The engine
filters which trades would have been taken and recomputes portfolio metrics.

Importable by Streamlit (analytics_tab.py) and usable as a standalone CLI
(scripts/backtest.py).
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_DEFAULT_DB = Path(__file__).parent.parent / "data" / "trades.db"

REGIME_ALL = ["TRENDING", "BREAKOUT", "RANGING", "CHOPPY", "UNKNOWN"]
REGIME_NO_CHOPPY = ["TRENDING", "BREAKOUT", "RANGING", "UNKNOWN"]
REGIME_TRENDING_BREAKOUT = ["TRENDING", "BREAKOUT"]
REGIME_RANGING = ["RANGING"]
REGIME_CHOPPY = ["CHOPPY"]


@dataclass
class Strategy:
    name: str = "baseline"
    conviction_min: float = 0.0
    regimes: Optional[list[str]] = None          # None = all regimes
    ofi_min: Optional[float] = None              # None = no OFI filter
    ofi_max: Optional[float] = None
    exit_reasons: Optional[list[str]] = None     # None = all exit reasons


@dataclass
class BacktestResult:
    name: str
    n_trades: int
    n_skipped: int
    win_rate: float        # 0–100
    total_pnl: float
    avg_pnl: float
    sharpe: Optional[float]   # daily Sharpe; None if < 5 trading days
    max_drawdown: float        # max peak-to-trough € drop
    peg_cross_rate: float      # % of trades exiting via peg_cross
    cumulative_pnl: list[float] = field(default_factory=list)  # per-trade running total

    def as_dict(self) -> dict:
        return {
            "Strategie": self.name,
            "Trades": self.n_trades,
            "Overgeslagen": self.n_skipped,
            "Winrate %": self.win_rate,
            "Totaal P&L": self.total_pnl,
            "Gem. P&L": self.avg_pnl,
            "Sharpe": self.sharpe,
            "Max drawdown": self.max_drawdown,
            "Peg-cross %": self.peg_cross_rate,
        }


class BacktestEngine:
    """
    Load trades once, run many strategies cheaply.

    Usage:
        engine = BacktestEngine()
        results = engine.sweep_conviction()
        grid = engine.grid_search()
    """

    def __init__(self, db_path: Path = _DEFAULT_DB,
                 coin: Optional[str] = None,
                 days: Optional[int] = None):
        self.trades = self._load(db_path, coin, days)

    # ── Data loading ──────────────────────────────────────────────────────────

    def _load(self, db_path: Path, coin: Optional[str], days: Optional[int]) -> list[dict]:
        if not db_path.exists():
            return []
        conditions = [
            "status IN ('closed','resolved')",
            "trigger_hit = 1",
            "net_pnl IS NOT NULL",
        ]
        params: list = []
        if coin:
            conditions.append("coin = ?")
            params.append(coin)
        if days:
            conditions.append("created_at >= datetime('now', ?)")
            params.append(f"-{days} days")
        where = " AND ".join(conditions)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT
                    trade_id, coin, created_at,
                    conviction_score_at_entry,
                    ofi_at_entry,
                    regime_at_entry,
                    winner_exit_reason,
                    net_pnl, fees_paid,
                    actual_winner, winner_side
                FROM trades
                WHERE {where}
                ORDER BY created_at ASC""",
            params,
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── Core run ──────────────────────────────────────────────────────────────

    def run(self, strategy: Strategy) -> BacktestResult:
        taken, skipped = [], []
        for t in self.trades:
            (taken if self._passes(t, strategy) else skipped).append(t)

        if not taken:
            return BacktestResult(
                name=strategy.name, n_trades=0, n_skipped=len(skipped),
                win_rate=0.0, total_pnl=0.0, avg_pnl=0.0,
                sharpe=None, max_drawdown=0.0, peg_cross_rate=0.0,
            )

        pnls = [t["net_pnl"] for t in taken]
        wins = sum(1 for p in pnls if p > 0)
        peg_crosses = sum(1 for t in taken if (t.get("winner_exit_reason") or "") == "peg_cross")
        total_pnl = sum(pnls)

        cumulative = []
        running = 0.0
        for p in pnls:
            running += p
            cumulative.append(round(running, 4))

        return BacktestResult(
            name=strategy.name,
            n_trades=len(taken),
            n_skipped=len(skipped),
            win_rate=round(wins / len(taken) * 100, 1),
            total_pnl=round(total_pnl, 4),
            avg_pnl=round(total_pnl / len(taken), 4),
            sharpe=self._sharpe(taken),
            max_drawdown=round(self._max_drawdown(pnls), 4),
            peg_cross_rate=round(peg_crosses / len(taken) * 100, 1),
            cumulative_pnl=cumulative,
        )

    # ── Filters ───────────────────────────────────────────────────────────────

    def _passes(self, trade: dict, s: Strategy) -> bool:
        score = trade.get("conviction_score_at_entry") or 0.0
        if score < s.conviction_min:
            return False
        if s.regimes is not None:
            regime = trade.get("regime_at_entry") or "UNKNOWN"
            if regime not in s.regimes:
                return False
        ofi = trade.get("ofi_at_entry")
        if s.ofi_min is not None and (ofi is None or ofi < s.ofi_min):
            return False
        if s.ofi_max is not None and (ofi is None or ofi > s.ofi_max):
            return False
        if s.exit_reasons is not None:
            if (trade.get("winner_exit_reason") or "") not in s.exit_reasons:
                return False
        return True

    # ── Metrics ───────────────────────────────────────────────────────────────

    def _sharpe(self, trades: list[dict]) -> Optional[float]:
        """Daily Sharpe ratio. Requires >= 5 distinct trading days."""
        daily: dict[str, float] = defaultdict(float)
        for t in trades:
            day = (t.get("created_at") or "")[:10]
            daily[day] += t["net_pnl"]
        if len(daily) < 5:
            return None
        vals = list(daily.values())
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = math.sqrt(var)
        return round(mean / std, 3) if std > 0 else None

    def _max_drawdown(self, pnls: list[float]) -> float:
        peak = max_dd = cumulative = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        return max_dd

    # ── Sweeps ────────────────────────────────────────────────────────────────

    def sweep_conviction(self) -> list[BacktestResult]:
        """Conviction threshold sweep from 0.0 to 0.7."""
        thresholds = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.55, 0.6, 0.7]
        return [
            self.run(Strategy(name=f"≥{t:.2f}", conviction_min=t))
            for t in thresholds
        ]

    def sweep_regime(self) -> list[BacktestResult]:
        """Compare P&L across regime filter combinations."""
        scenarios = [
            ("Alle regimes",         None),
            ("Geen CHOPPY",          REGIME_NO_CHOPPY),
            ("Alleen TRENDING+BREAKOUT", REGIME_TRENDING_BREAKOUT),
            ("Alleen RANGING",       REGIME_RANGING),
            ("Alleen CHOPPY",        REGIME_CHOPPY),
        ]
        return [self.run(Strategy(name=name, regimes=regimes)) for name, regimes in scenarios]

    def sweep_exit_reason(self) -> list[BacktestResult]:
        """Compare limit_filled vs peg_cross exit performance."""
        scenarios = [
            ("Alle exits",      None),
            ("limit_filled",    ["limit_filled"]),
            ("peg_cross",       ["peg_cross"]),
            ("held_resolution", ["held_for_resolution"]),
            ("force_exit",      ["force_exit_window_end"]),
        ]
        return [self.run(Strategy(name=name, exit_reasons=reasons)) for name, reasons in scenarios]

    def grid_search(self) -> list[BacktestResult]:
        """2D sweep: conviction threshold × regime filter. Sorted by total P&L."""
        thresholds = [0.0, 0.2, 0.4, 0.55]
        regime_sets = [
            ("all",              None),
            ("no_choppy",        REGIME_NO_CHOPPY),
            ("trend+break",      REGIME_TRENDING_BREAKOUT),
        ]
        results = []
        for thresh in thresholds:
            for regime_name, regimes in regime_sets:
                name = f"c≥{thresh}|{regime_name}"
                results.append(self.run(Strategy(
                    name=name, conviction_min=thresh, regimes=regimes
                )))
        return sorted(results, key=lambda r: r.total_pnl, reverse=True)
