"""BGGDSB Backtest — simuleer is5minfixedyet strategie op bestaande trades.

Gebruik:
  cd /opt/poly-baws-bot/backend
  python bggdsb_backtest.py

Strategie regels:
  1. Dominant kant moet 0.40–0.65 zijn bij entry
  2. Budget: €30 per window (configureerbaar)
  3. Split: 87.5% dominant / 12.5% hedge
  4. Richting: bias_direction_at_entry (UP=YES dominant, DOWN=NO dominant)
     Fallback: de kant die in de prijs-gate valt is dominant
  5. Exit: hold to expiry (winner = €1.00/share, loser = €0.00/share)
  6. Fee aanname: 2% van entry waarde (Polymarket taker fee)
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "trades.db"

# ── Configuratie ──────────────────────────────────────────────────────────────
BUDGET_EUR       = 30.0
DOMINANT_RATIO   = 0.875
ENTRY_MIN        = 0.40
ENTRY_MAX        = 0.65
FEE_RATE         = 0.02   # 2% taker fee op entry

# ── Query ─────────────────────────────────────────────────────────────────────
SQL = """
SELECT
    trade_id,
    coin,
    created_at,
    entry_yes_price,
    entry_no_price,
    actual_winner,
    winner_side,
    bias_direction_at_entry,
    conviction_at_entry,
    conviction_score_at_entry,
    regime_at_entry,
    net_pnl          AS actual_net_pnl,
    yes_size          AS actual_yes_size,
    no_size           AS actual_no_size,
    router_bucket
FROM trades
WHERE status IN ('closed', 'resolved')
  AND trigger_hit = 1
  AND entry_yes_price IS NOT NULL
  AND entry_no_price  IS NOT NULL
  AND actual_winner   IS NOT NULL
ORDER BY created_at
"""


def _dominant_side(row: dict) -> str | None:
    """Bepaal dominant kant op basis van conviction of prijs-gate."""
    bias = row.get("bias_direction_at_entry") or row.get("conviction_at_entry")
    if bias in ("UP", "YES"):
        return "YES"
    if bias in ("DOWN", "NO"):
        return "NO"
    # Fallback: welke kant zit in de prijs-gate?
    yp = row["entry_yes_price"]
    np_ = row["entry_no_price"]
    if ENTRY_MIN <= yp <= ENTRY_MAX:
        return "YES"
    if ENTRY_MIN <= np_ <= ENTRY_MAX:
        return "NO"
    return None


def simulate_trade(row: dict) -> dict | None:
    """Simuleer één BGGDSB trade. Geeft None als deze trade geskipt zou worden."""
    yp = row["entry_yes_price"]
    np_ = row["entry_no_price"]
    winner = row["actual_winner"]  # "YES" of "NO"

    # Prijs-gate: minstens één kant moet in range zitten
    yes_in_range = ENTRY_MIN <= yp <= ENTRY_MAX
    no_in_range  = ENTRY_MIN <= np_ <= ENTRY_MAX
    if not yes_in_range and not no_in_range:
        return None

    dom = _dominant_side(row)
    if dom is None:
        # Geen conviction + beide in range → pak de laagste (meest onzeker)
        dom = "YES" if yp <= np_ else "NO"

    # Sizing
    dom_eur   = round(BUDGET_EUR * DOMINANT_RATIO, 4)
    hedge_eur = round(BUDGET_EUR * (1.0 - DOMINANT_RATIO), 4)

    if dom == "YES":
        dom_price   = yp
        hedge_price = np_
    else:
        dom_price   = np_
        hedge_price = yp

    if dom_price <= 0 or hedge_price <= 0:
        return None

    dom_shares   = dom_eur   / dom_price
    hedge_shares = hedge_eur / hedge_price

    yes_shares = dom_shares   if dom == "YES" else hedge_shares
    no_shares  = hedge_shares if dom == "YES" else dom_shares

    # Fees op entry
    entry_cost = dom_eur + hedge_eur  # = BUDGET_EUR
    fees = entry_cost * FEE_RATE

    # Payout bij expiry (winner = €1.00/share)
    if winner == "YES":
        payout = yes_shares * 1.0
    else:
        payout = no_shares * 1.0

    gross_pnl = payout - entry_cost
    net_pnl   = gross_pnl - fees
    won       = net_pnl > 0

    return {
        "trade_id":   row["trade_id"],
        "coin":       row["coin"],
        "created_at": row["created_at"],
        "dominant":   dom,
        "winner":     winner,
        "dom_correct": dom == winner,
        "yes_shares": round(yes_shares, 4),
        "no_shares":  round(no_shares, 4),
        "payout":     round(payout, 4),
        "gross_pnl":  round(gross_pnl, 4),
        "fees":       round(fees, 4),
        "net_pnl":    round(net_pnl, 4),
        "won":        won,
        "regime":     row.get("regime_at_entry", ""),
        "actual_net_pnl": row.get("actual_net_pnl"),
    }


def main():
    if not DB_PATH.exists():
        print(f"Database niet gevonden: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(SQL).fetchall()
    conn.close()

    total_trades  = len(rows)
    simulated     = []
    skipped_gate  = 0
    skipped_no_winner = 0

    for row in rows:
        r = dict(row)
        result = simulate_trade(r)
        if result is None:
            skipped_gate += 1
        else:
            simulated.append(result)

    n = len(simulated)
    if n == 0:
        print("Geen trades voldoen aan de BGGDSB prijs-gate (0.40–0.65).")
        return

    wins        = sum(1 for t in simulated if t["won"])
    dom_correct = sum(1 for t in simulated if t["dom_correct"])
    total_pnl   = sum(t["net_pnl"] for t in simulated)
    total_fees  = sum(t["fees"] for t in simulated)
    avg_pnl     = total_pnl / n

    # ── Vergelijking met werkelijk resultaat ──────────────────────────────────
    actual_pnl_same_trades = sum(
        t["actual_net_pnl"] for t in simulated
        if t["actual_net_pnl"] is not None
    )

    # ── Per coin ──────────────────────────────────────────────────────────────
    from collections import defaultdict
    coin_stats: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in simulated:
        cs = coin_stats[t["coin"]]
        cs["n"]    += 1
        cs["wins"] += int(t["won"])
        cs["pnl"]  += t["net_pnl"]

    # ── Per regime ────────────────────────────────────────────────────────────
    regime_stats: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in simulated:
        rs = regime_stats[t["regime"] or "UNKNOWN"]
        rs["n"]    += 1
        rs["wins"] += int(t["won"])
        rs["pnl"]  += t["net_pnl"]

    # ── Cumulatief P&L ────────────────────────────────────────────────────────
    cum = 0.0
    cum_series = []
    for t in simulated:
        cum += t["net_pnl"]
        cum_series.append(cum)
    peak = max(cum_series) if cum_series else 0
    trough = min(cum_series) if cum_series else 0

    # ── Output ────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("  BGGDSB BACKTEST — is5minfixedyet strategie 1:1")
    print("=" * 60)
    print(f"  Budget per window : €{BUDGET_EUR:.2f}")
    print(f"  Split             : {DOMINANT_RATIO*100:.0f}% dominant / {(1-DOMINANT_RATIO)*100:.0f}% hedge")
    print(f"  Prijs-gate        : {ENTRY_MIN:.2f} – {ENTRY_MAX:.2f}")
    print(f"  Fee aanname       : {FEE_RATE*100:.0f}% op entry")
    print()
    print(f"  Totaal trades in DB       : {total_trades}")
    print(f"  Geskipt (prijs-gate)      : {skipped_gate} ({skipped_gate/total_trades*100:.1f}%)")
    print(f"  Gesimuleerd               : {n}")
    print()
    print("─" * 60)
    print("  RESULTAAT")
    print("─" * 60)
    pnl_sign = "+" if total_pnl >= 0 else ""
    print(f"  Win rate          : {wins}/{n} = {wins/n*100:.1f}%")
    print(f"  Dominant correct  : {dom_correct}/{n} = {dom_correct/n*100:.1f}%")
    print(f"  Totaal P&L        : €{pnl_sign}{total_pnl:.2f}")
    print(f"  Gem. P&L/trade    : €{avg_pnl:+.4f}")
    print(f"  Totaal fees       : €{total_fees:.2f}")
    print(f"  Peak equity       : €{peak:+.2f}")
    print(f"  Worst drawdown    : €{trough:+.2f}")
    print()
    actual_sign = "+" if actual_pnl_same_trades >= 0 else ""
    diff = total_pnl - actual_pnl_same_trades
    diff_sign = "+" if diff >= 0 else ""
    print(f"  Werkelijk P&L (zelfde trades) : €{actual_sign}{actual_pnl_same_trades:.2f}")
    print(f"  BGGDSB verschil               : €{diff_sign}{diff:.2f}")
    print()
    print("─" * 60)
    print("  PER COIN")
    print("─" * 60)
    for coin, cs in sorted(coin_stats.items(), key=lambda x: -abs(x[1]["pnl"])):
        wr = cs["wins"] / cs["n"] * 100
        pnl_s = "+" if cs["pnl"] >= 0 else ""
        print(f"  {coin:<6} n={cs['n']:>4}  WR={wr:>5.1f}%  P&L=€{pnl_s}{cs['pnl']:.2f}")
    print()
    print("─" * 60)
    print("  PER REGIME")
    print("─" * 60)
    for regime, rs in sorted(regime_stats.items(), key=lambda x: -x[1]["n"]):
        wr = rs["wins"] / rs["n"] * 100
        pnl_s = "+" if rs["pnl"] >= 0 else ""
        print(f"  {regime:<10} n={rs['n']:>4}  WR={wr:>5.1f}%  P&L=€{pnl_s}{rs['pnl']:.2f}")
    print()

    # ── Top 10 best + worst trades ────────────────────────────────────────────
    by_pnl = sorted(simulated, key=lambda t: t["net_pnl"])
    print("─" * 60)
    print("  TOP 10 SLECHTSTE TRADES")
    print("─" * 60)
    for t in by_pnl[:10]:
        print(f"  {t['created_at'][:16]}  {t['coin']:<6}  dom={t['dominant']}  winner={t['winner']}  P&L=€{t['net_pnl']:+.4f}  {t['regime']}")
    print()
    print("─" * 60)
    print("  TOP 10 BESTE TRADES")
    print("─" * 60)
    for t in by_pnl[-10:]:
        print(f"  {t['created_at'][:16]}  {t['coin']:<6}  dom={t['dominant']}  winner={t['winner']}  P&L=€{t['net_pnl']:+.4f}  {t['regime']}")
    print()
    print("=" * 60)

    # ── Sensitivity analyse: budget varianten ────────────────────────────────
    print("  SENSITIVITY — budget varianten (zelfde trades)")
    print("─" * 60)
    print(f"  {'Budget':>8}  {'P&L':>10}  {'P&L/trade':>10}")
    for b in [20, 25, 30, 35, 40, 45, 50]:
        scale = b / BUDGET_EUR
        sp = sum(t["net_pnl"] * scale for t in simulated)
        sign = "+" if sp >= 0 else ""
        print(f"  €{b:>6}   €{sign}{sp:>8.2f}   €{sp/n:>+8.4f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
