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
        self.db_path = db_path
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
                    entry_size,
                    yes_size, no_size,
                    entry_yes_price, entry_no_price,
                    conviction_at_entry, conviction_score_at_entry,
                    ofi_at_entry,
                    regime_at_entry,
                    winner_exit_reason,
                    loser_exit_price, winner_exit_price,
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

    # ── Conviction-weighted entry simulation ──────────────────────────────────

    def simulate_weighting(
        self,
        min_scores: Optional[list[float]] = None,
        max_ratio: float = 2.0,
    ) -> list[dict]:
        """
        Simulate what historical P&L would have been with conviction-weighted entry sizes.

        For each closed trade:
        - If conviction_at_entry aligns with actual_winner → winner side was bigger → better P&L
        - If conviction was wrong → loser side was bigger → worse P&L

        Returns one row per min_score threshold, showing simulated vs. actual P&L.
        Uses actual fill prices (entry, winner exit, loser exit) from the DB.
        """
        if min_scores is None:
            min_scores = [0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70]

        rows = []
        for min_score in min_scores:
            sim_pnls, actual_pnls = [], []
            correct, wrong, neutral = 0, 0, 0

            for t in self.trades:
                actual_pnl = t.get("net_pnl") or 0.0
                actual_pnls.append(actual_pnl)

                conv_dir = t.get("conviction_at_entry")
                conv_score = float(t.get("conviction_score_at_entry") or 0.0)
                actual_winner = t.get("actual_winner")  # "YES" or "NO"

                base = float(t.get("entry_size") or 2.0)
                entry_yes = float(t.get("entry_yes_price") or 0.50)
                entry_no = float(t.get("entry_no_price") or 0.50)
                loser_price = float(t.get("loser_exit_price") or 0.17)
                winner_price = float(t.get("winner_exit_price") or 0.80)
                fees = float(t.get("fees_paid") or 0.0)

                if conv_dir and conv_score >= min_score and actual_winner:
                    _range = max(0.001, 1.0 - min_score)
                    weight = 1.0 + (conv_score - min_score) / _range * (max_ratio - 1.0)
                    if conv_dir == "UP":
                        sim_yes, sim_no = base * weight, base
                    else:
                        sim_yes, sim_no = base, base * weight

                    # Did conviction align with actual winner?
                    conv_winner = "YES" if conv_dir == "UP" else "NO"
                    if conv_winner == actual_winner:
                        correct += 1
                    else:
                        wrong += 1

                    sim_entry = entry_yes * sim_yes + entry_no * sim_no
                    if actual_winner == "YES":
                        sim_winner_sz, sim_loser_sz = sim_yes, sim_no
                    else:
                        sim_winner_sz, sim_loser_sz = sim_no, sim_yes

                    sim_gross = winner_price * sim_winner_sz + loser_price * sim_loser_sz - sim_entry
                    # Scale fees proportionally to total size change
                    actual_total = float(t.get("yes_size") or base) + float(t.get("no_size") or base)
                    sim_total = sim_yes + sim_no
                    fee_scale = sim_total / actual_total if actual_total > 0 else 1.0
                    sim_net = sim_gross - fees * fee_scale
                else:
                    neutral += 1
                    sim_net = actual_pnl  # unchanged

                sim_pnls.append(sim_net)

            n = len(sim_pnls)
            if n == 0:
                continue

            sim_total = sum(sim_pnls)
            actual_total_pnl = sum(actual_pnls)
            sim_wins = sum(1 for p in sim_pnls if p > 0)

            rows.append({
                "Min score": min_score,
                "Trades gewogen": correct + wrong,
                "Correct gewogen": correct,
                "Fout gewogen": wrong,
                "Ongewijzigd": neutral,
                "Winrate % (sim)": round(sim_wins / n * 100, 1),
                "Totaal P&L (sim)": round(sim_total, 4),
                "Totaal P&L (echt)": round(actual_total_pnl, 4),
                "Delta P&L": round(sim_total - actual_total_pnl, 4),
                "Max drawdown (sim)": round(self._max_drawdown(sim_pnls), 4),
            })

        return rows

    # ── Early loser sell simulation ───────────────────────────────────────────

    def simulate_early_loser_sell(
        self,
        thresholds: Optional[list[float]] = None,
    ) -> list[dict]:
        """Simulate selling the loser early when it first drops below each mid threshold.

        Uses 30-second snapshots recorded during monitoring to replay the loser
        price history. For each threshold, finds the first snapshot where
        loser_mid <= threshold and estimates the sell price at that moment.

        Winner exit is assumed unchanged (uses actual winner_exit_price).
        This isolates the P&L improvement from the loser side only.

        Returns one row per threshold for comparison vs. actual P&L.
        """
        if thresholds is None:
            thresholds = [0.48, 0.45, 0.42, 0.40, 0.38, 0.35, 0.30]

        if not self.trades:
            return []

        trade_ids = [t["trade_id"] for t in self.trades]
        snapshots = self._load_snapshots(trade_ids)

        snaps_by_trade: dict[str, list[dict]] = defaultdict(list)
        for s in snapshots:
            snaps_by_trade[s["trade_id"]].append(s)

        rows = []
        for threshold in thresholds:
            sim_pnls: list[float] = []
            actual_pnls: list[float] = []
            n_early = 0
            early_prices: list[float] = []
            actual_loser_prices: list[float] = []

            for t in self.trades:
                actual_pnl = float(t.get("net_pnl") or 0.0)
                actual_pnls.append(actual_pnl)

                winner = t.get("actual_winner") or t.get("winner_side")
                if not winner:
                    sim_pnls.append(actual_pnl)
                    continue

                loser_is_yes = (winner == "NO")
                base = float(t.get("entry_size") or 2.0)
                entry_yes = float(t.get("entry_yes_price") or 0.50)
                entry_no = float(t.get("entry_no_price") or 0.50)
                yes_size = float(t.get("yes_size") or base)
                no_size = float(t.get("no_size") or base)
                winner_size = yes_size if winner == "YES" else no_size
                loser_size = no_size if winner == "YES" else yes_size
                winner_price = float(t.get("winner_exit_price") or 0.80)
                fees = float(t.get("fees_paid") or 0.0)
                actual_loser_price = float(t.get("loser_exit_price") or 0.17)
                actual_loser_prices.append(actual_loser_price)

                # Find first snapshot where loser_mid <= threshold
                early_bid: Optional[float] = None
                for snap in snaps_by_trade.get(t["trade_id"], []):
                    loser_mid = snap.get("yes_mid" if loser_is_yes else "no_mid")
                    if loser_mid is None:
                        continue
                    loser_mid = float(loser_mid)
                    if loser_mid <= threshold:
                        # YES-loser: use actual best_bid from snapshot
                        # NO-loser: estimate from mid using spread model
                        if loser_is_yes and snap.get("best_bid"):
                            early_bid = float(snap["best_bid"])
                        else:
                            early_bid = self._estimate_loser_bid(loser_mid)
                        break

                if early_bid is not None:
                    entry_cost = entry_yes * yes_size + entry_no * no_size
                    sim_gross = winner_price * winner_size + early_bid * loser_size - entry_cost
                    sim_pnls.append(sim_gross - fees)
                    early_prices.append(early_bid)
                    n_early += 1
                else:
                    sim_pnls.append(actual_pnl)

            n = len(sim_pnls)
            if n == 0:
                continue

            sim_total = sum(sim_pnls)
            actual_total = sum(actual_pnls)
            sim_wins = sum(1 for p in sim_pnls if p > 0)
            rows.append({
                "Loser mid drempel": threshold,
                "Trades vroeg exit": n_early,
                "Gem. loser bid (vroeg)": round(sum(early_prices) / len(early_prices), 3) if early_prices else None,
                "Gem. loser bid (huidig)": round(sum(actual_loser_prices) / len(actual_loser_prices), 3),
                "Totaal P&L (sim)": round(sim_total, 4),
                "Totaal P&L (echt)": round(actual_total, 4),
                "Delta P&L": round(sim_total - actual_total, 4),
                "Winrate % (sim)": round(sim_wins / n * 100, 1),
                "Max drawdown (sim)": round(self._max_drawdown(sim_pnls), 4),
            })

        return rows

    def _load_snapshots(self, trade_ids: list[str]) -> list[dict]:
        """Load all monitoring snapshots for the given trades, ordered by id."""
        if not self.db_path.exists() or not trade_ids:
            return []
        import json as _json
        placeholders = ",".join("?" * len(trade_ids))
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT trade_id, best_bid, best_ask, snapshot
                FROM orderbook_snapshots
                WHERE trade_id IN ({placeholders})
                ORDER BY trade_id, id""",
            trade_ids,
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            snap_data: dict = {}
            try:
                snap_data = _json.loads(r["snapshot"] or "{}")
            except Exception:
                pass
            result.append({
                "trade_id": r["trade_id"],
                "best_bid": r["best_bid"],
                "best_ask": r["best_ask"],
                **snap_data,
            })
        return result

    def _estimate_loser_bid(self, loser_mid: float) -> float:
        """Estimate bid price from mid using an empirical spread model.

        Calibrated to observed Polymarket data: at mid=0.27 the bid is ~0.17
        (half-spread ≈ 0.10). Spread narrows as mid approaches 0.50 where
        market makers provide tighter quotes.

        Formula: half_spread = 0.04 + max(0, 0.50 - mid) * 0.26
        """
        half_spread = 0.04 + max(0.0, 0.50 - loser_mid) * 0.26
        return max(0.01, round(loser_mid - half_spread, 3))
