#!/usr/bin/env python3
"""Trade analysis script — run against trades.db to get P&L and statistics."""
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

try:
    import pandas as pd
except ImportError:
    print("pandas required: pip install pandas")
    sys.exit(1)

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"


def load_trades(db_path: Path) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT * FROM trades WHERE status IN ('closed', 'resolved')", conn)
    conn.close()
    if df.empty:
        return df
    df["created_at"] = pd.to_datetime(df["created_at"])
    df["window_start_ts"] = pd.to_datetime(df["window_start_ts"])
    return df


def print_section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def global_stats(df: pd.DataFrame) -> None:
    print_section("GLOBAL STATISTICS")
    total = len(df)
    winners = df[df["net_pnl"] > 0]
    losers = df[df["net_pnl"] < 0]
    win_rate = len(winners) / total * 100 if total else 0

    print(f"Total trades:      {total}")
    print(f"Winners:           {len(winners)} ({win_rate:.1f}%)")
    print(f"Losers:            {len(losers)}")
    print(f"Avg win:           €{winners['net_pnl'].mean():.4f}" if len(winners) else "Avg win: N/A")
    print(f"Avg loss:          €{losers['net_pnl'].mean():.4f}" if len(losers) else "Avg loss: N/A")
    print(f"Total net P&L:     €{df['net_pnl'].sum():.4f}")
    print(f"Max drawdown:      €{_max_drawdown(df):.4f}")
    print(f"Max consec. losses:{_max_consecutive_losses(df)}")

    print_section("EXIT REASON DISTRIBUTION")
    if "winner_exit_reason" in df.columns:
        print(df["winner_exit_reason"].value_counts().to_string())


def per_coin_stats(df: pd.DataFrame) -> None:
    print_section("PER COIN STATISTICS")
    for coin in sorted(df["coin"].unique()):
        sub = df[df["coin"] == coin]
        total = len(sub)
        win_rate = (sub["net_pnl"] > 0).sum() / total * 100 if total else 0
        print(f"\n{coin}:")
        print(f"  Trades: {total}  Win rate: {win_rate:.1f}%  Net P&L: €{sub['net_pnl'].sum():.4f}")


def daily_pnl(df: pd.DataFrame) -> None:
    print_section("DAILY NET P&L")
    if df.empty:
        print("No data.")
        return
    df["date"] = df["created_at"].dt.date
    daily = df.groupby("date")["net_pnl"].sum()
    print(daily.to_string())


def mode_comparison(df: pd.DataFrame) -> None:
    if "mode" not in df.columns:
        return
    modes = df["mode"].unique()
    has_hybrid = any("hybrid" in m for m in modes)
    has_auto = any("auto" in m for m in modes)
    if not (has_hybrid and has_auto):
        return

    print_section("HYBRID vs AUTO COMPARISON")
    for mode_type in ["hybrid", "auto"]:
        sub = df[df["mode"].str.contains(mode_type)]
        total = len(sub)
        if total == 0:
            continue
        win_rate = (sub["net_pnl"] > 0).sum() / total * 100
        print(f"\n{mode_type.upper()} ({total} trades):")
        print(f"  Win rate: {win_rate:.1f}%  Net P&L: €{sub['net_pnl'].sum():.4f}")


def _max_drawdown(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    cumulative = df["net_pnl"].cumsum()
    peak = cumulative.cummax()
    drawdown = (cumulative - peak).min()
    return abs(drawdown)


def _max_consecutive_losses(df: pd.DataFrame) -> int:
    max_streak = 0
    streak = 0
    for pnl in df["net_pnl"]:
        if pnl < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    df = load_trades(db_path)
    if df.empty:
        print("No closed trades found in database.")
        sys.exit(0)

    global_stats(df)
    per_coin_stats(df)
    daily_pnl(df)
    mode_comparison(df)


if __name__ == "__main__":
    main()
