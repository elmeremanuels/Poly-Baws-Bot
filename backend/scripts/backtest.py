"""
Standalone CLI backtest script.

Usage:
    cd /opt/poly-baws-bot/backend
    /opt/poly-baws-bot/venv/bin/python scripts/backtest.py
    /opt/poly-baws-bot/venv/bin/python scripts/backtest.py --days 7
    /opt/poly-baws-bot/venv/bin/python scripts/backtest.py --coin BTC --days 30
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest_engine import BacktestEngine, BacktestResult


def _bar(val: float, min_val: float, max_val: float, width: int = 20) -> str:
    if max_val == min_val:
        return " " * width
    filled = int((val - min_val) / (max_val - min_val) * width)
    return "█" * filled + "░" * (width - filled)


def _print_table(results: list[BacktestResult], title: str) -> None:
    if not results:
        print(f"\n{title}: geen data\n")
        return

    print(f"\n{'─' * 80}")
    print(f"  {title}")
    print(f"{'─' * 80}")
    print(f"  {'Strategie':<28} {'N':>5} {'Win%':>6} {'P&L €':>8} {'Gem €':>7} {'Sharpe':>7} {'DD €':>7} {'PegX%':>6}")
    print(f"  {'─'*28} {'─'*5} {'─'*6} {'─'*8} {'─'*7} {'─'*7} {'─'*7} {'─'*6}")

    pnls = [r.total_pnl for r in results]
    max_pnl, min_pnl = max(pnls), min(pnls)

    for r in results:
        sharpe_str = f"{r.sharpe:>7.3f}" if r.sharpe is not None else "   n/a "
        bar = _bar(r.total_pnl, min_pnl, max_pnl, width=10)
        print(
            f"  {r.name:<28} {r.n_trades:>5} {r.win_rate:>6.1f} "
            f"{r.total_pnl:>8.4f} {r.avg_pnl:>7.4f} {sharpe_str} "
            f"{r.max_drawdown:>7.4f} {r.peg_cross_rate:>6.1f}  {bar}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Poly-Baws backtest / scenario replay")
    parser.add_argument("--days", type=int, default=None, help="Limit to last N days")
    parser.add_argument("--coin", type=str, default=None, help="Filter by coin (e.g. BTC)")
    args = parser.parse_args()

    print(f"\n{'=' * 80}")
    print(f"  Poly-Baws Backtest Engine")
    if args.coin:
        print(f"  Coin: {args.coin}")
    if args.days:
        print(f"  Periode: afgelopen {args.days} dagen")
    print(f"{'=' * 80}")

    engine = BacktestEngine(coin=args.coin, days=args.days)
    n_total = len(engine.trades)
    print(f"\n  Geladen: {n_total} gesloten trades met signaaldata")

    if n_total == 0:
        print("\n  Geen trades gevonden. Controleer of de bot al trades heeft gemaakt.")
        sys.exit(0)

    # 1. Conviction sweep
    _print_table(engine.sweep_conviction(), "Conviction drempel sweep")

    # 2. Regime sweep
    _print_table(engine.sweep_regime(), "Regime filter sweep")

    # 3. Exit reason breakdown
    _print_table(engine.sweep_exit_reason(), "Exit-reden analyse")

    # 4. Grid search
    grid = engine.grid_search()
    _print_table(grid, "Grid search: conviction × regime (gesorteerd op P&L)")

    # 5. Beste strategie samenvatting
    best = grid[0] if grid else None
    baseline = next((r for r in engine.sweep_conviction() if r.name == "≥0.00"), None)
    if best and baseline:
        print(f"{'─' * 80}")
        print(f"  Beste strategie:  {best.name}")
        print(f"  Baseline (alles): {baseline.name}")
        delta_pnl = best.total_pnl - baseline.total_pnl
        delta_wr  = best.win_rate  - baseline.win_rate
        print(f"  P&L verschil:    {delta_pnl:+.4f} €")
        print(f"  Winrate verschil: {delta_wr:+.1f}%")
        print(f"  Trades overgeslagen door filter: {best.n_skipped}")
        print(f"{'─' * 80}\n")


if __name__ == "__main__":
    main()
