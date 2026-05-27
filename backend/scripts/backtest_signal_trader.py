#!/usr/bin/env python3
"""
Backtest signal_trader strategie op bestaande DB-data.

Databronnen:
  - Signal Lab (straddle) trades: altijd ingestapt, conviction_score_at_trigger gestempeld
    → ideaal voor threshold-sweep (elke trade zit erin, ongeacht score)
  - Signal_trader trades: alleen ingestapt bij score >= drempel
    → selectiebias, maar toont echte fill-prijzen

Gebruik:
  python3 backend/scripts/backtest_signal_trader.py
  python3 backend/scripts/backtest_signal_trader.py --db /opt/poly-baws-bot/backend/data/trades.db
"""
import argparse
import sqlite3
from pathlib import Path

# ── Argument parsing ──────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--db", default="backend/data/trades.db",
                    help="Pad naar trades.db")
args = parser.parse_args()

db_path = Path(args.db)
if not db_path.exists():
    print(f"❌  Database niet gevonden: {db_path}")
    raise SystemExit(1)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# ── Helper ────────────────────────────────────────────────────────────────────

def pct(n, d):
    return f"{n/d*100:.1f}%" if d else "—"

def eur(x):
    return f"{'+'if x>=0 else ''}{x:.2f}€"

SEP = "─" * 72

# ══════════════════════════════════════════════════════════════════════════════
# 0. Dataset overzicht
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{'═'*72}")
print("  SIGNAL TRADER BACKTEST")
print(f"{'═'*72}\n")

rows = conn.execute("""
    SELECT mode, status, COUNT(*) n
    FROM trades
    GROUP BY mode, status
    ORDER BY mode, n DESC
""").fetchall()
print("📦  Dataset overzicht:")
for r in rows:
    print(f"    {r['mode']:<20} {r['status']:<20} {r['n']:>5} trades")
print()

# ══════════════════════════════════════════════════════════════════════════════
# 1. Straddle-data als backtest-basis
#    Straddle stapt ALTIJD in → geen selectiebias → ideaal voor threshold-sweep
# ══════════════════════════════════════════════════════════════════════════════

straddle = conn.execute("""
    SELECT
        conviction_score_at_trigger  AS score,
        conviction_at_trigger        AS direction,
        actual_winner                AS winner,
        winner_side                  AS bot_side,
        winner_exit_price            AS exit_price_winner,
        loser_exit_price             AS exit_price_loser,
        net_pnl,
        entry_yes_price,
        entry_no_price
    FROM trades
    WHERE mode != 'signal_trader'
      AND status IN ('closed', 'resolved')
      AND actual_winner IS NOT NULL
      AND conviction_at_trigger IS NOT NULL
      AND conviction_score_at_trigger IS NOT NULL
""").fetchall()

total_straddle = len(straddle)
print(f"📊  Straddle trades bruikbaar voor backtest: {total_straddle}")

if total_straddle == 0:
    print("    ⚠️  Geen bruikbare straddle-data. Controleer of de bot heeft gerund.")
else:
    # Conviction-richting correct?
    # UP → verwachten YES wint; DOWN → verwachten NO wint
    def signal_correct(row):
        d = row["direction"]
        w = row["winner"]
        return (d == "UP" and w == "YES") or (d == "DOWN" and w == "NO")

    # Simuleer signal_trader P&L bij een goed/fout signaal
    # Signal_trader koopt ALLEEN de conviction-kant (niet beide)
    TRADE_EUR = 10.0  # inleg per trade

    def sim_pnl(row, correct: bool) -> float:
        """Schat P&L als we ALLEEN de conviction-kant hadden gekocht.
        Gebruik echte entry-prijs van die kant als beschikbaar,
        anders neem 0.50 als default.
        """
        d = row["direction"]
        if d == "UP":
            entry = row["entry_yes_price"] or 0.50
        else:
            entry = row["entry_no_price"] or 0.50
        size = TRADE_EUR / max(entry, 0.01)
        if correct:
            # Winst: payout $1/share − entry
            return round((1.0 - entry) * size, 4)
        else:
            # Verlies: alles kwijt
            return round(-entry * size, 4)

    print(f"\n{SEP}")
    print("  THRESHOLD SWEEP — Straddle data (geen selectiebias)")
    print(f"  Simulatie: €{TRADE_EUR:.0f}/trade, winnaar payout $1, verliezer $0")
    print(SEP)
    header = f"{'Drempel':>8}  {'# trades':>8}  {'% totaal':>8}  {'Win%':>6}  {'Gem P&L':>9}  {'Totaal P&L':>11}"
    print(header)
    print(SEP)

    THRESHOLDS = [0.0, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50, 0.65]

    baseline_pnl = None
    for thr in THRESHOLDS:
        subset = [r for r in straddle
                  if r["score"] is not None and r["score"] >= thr]
        n = len(subset)
        if n == 0:
            print(f"  {thr:>7.2f}  {'—':>8}  {'—':>8}  {'—':>6}  {'—':>9}  {'—':>11}")
            continue
        wins = sum(1 for r in subset if signal_correct(r))
        win_pct = wins / n * 100
        pnls = [sim_pnl(r, signal_correct(r)) for r in subset]
        avg_pnl = sum(pnls) / n
        total_pnl = sum(pnls)
        pct_of_total = n / total_straddle * 100

        if baseline_pnl is None:
            baseline_pnl = total_pnl

        marker = " ◄ nieuw" if abs(thr - 0.35) < 0.001 else (
                 " ◄ oud"  if abs(thr - 0.65) < 0.001 else "")
        print(f"  {thr:>7.2f}  {n:>8}  {pct_of_total:>7.1f}%  "
              f"{win_pct:>5.1f}%  {eur(avg_pnl):>9}  {eur(total_pnl):>11}{marker}")

    print(SEP)

# ══════════════════════════════════════════════════════════════════════════════
# 2. Signal_trader trades zelf (echte live/paper)
# ══════════════════════════════════════════════════════════════════════════════

st_trades = conn.execute("""
    SELECT
        conviction_score_at_trigger AS score,
        conviction_at_trigger       AS direction,
        actual_winner               AS winner,
        winner_side                 AS side,
        net_pnl,
        winner_exit_reason          AS reason,
        triggered_by
    FROM trades
    WHERE mode = 'signal_trader'
      AND status IN ('resolved', 'closed')
      AND actual_winner IS NOT NULL
""").fetchall()

print(f"\n{SEP}")
print("  ECHTE SIGNAL_TRADER TRADES (live/paper resultaten)")
print(SEP)

if not st_trades:
    print("  Geen afgeronde signal_trader trades gevonden.")
else:
    wins = sum(1 for r in st_trades
               if (r["direction"] == "UP" and r["winner"] == "YES") or
                  (r["direction"] == "DOWN" and r["winner"] == "NO"))
    n = len(st_trades)
    total_pnl = sum(r["net_pnl"] or 0 for r in st_trades)
    avg_pnl = total_pnl / n

    print(f"  Trades: {n}  |  Win%: {pct(wins, n)}  |  "
          f"Gem P&L: {eur(avg_pnl)}  |  Totaal: {eur(total_pnl)}")
    print()

    # Per triggered_by
    for label in ("signal_paper", "signal_live"):
        sub = [r for r in st_trades if r["triggered_by"] == label]
        if not sub:
            continue
        sw = sum(1 for r in sub
                 if (r["direction"] == "UP" and r["winner"] == "YES") or
                    (r["direction"] == "DOWN" and r["winner"] == "NO"))
        sp = sum(r["net_pnl"] or 0 for r in sub)
        icon = "📄" if "paper" in label else "💸"
        print(f"  {icon} {label:<15} n={len(sub):>4}  win%={pct(sw,len(sub)):>6}  "
              f"totaal={eur(sp)}")

    print()
    # Score verdeling van echte trades
    scores = [r["score"] for r in st_trades if r["score"] is not None]
    if scores:
        print(f"  Conviction score verdeling (echte trades):")
        buckets = [(0.0,0.2,"0.00–0.20"), (0.2,0.35,"0.20–0.35"),
                   (0.35,0.5,"0.35–0.50"), (0.5,0.65,"0.50–0.65"),
                   (0.65,1.01,"0.65–1.00")]
        for lo, hi, label in buckets:
            sub = [r for r in st_trades if r["score"] is not None and lo <= r["score"] < hi]
            if not sub:
                continue
            sw = sum(1 for r in sub
                     if (r["direction"] == "UP" and r["winner"] == "YES") or
                        (r["direction"] == "DOWN" and r["winner"] == "NO"))
            sp = sum(r["net_pnl"] or 0 for r in sub)
            print(f"    {label}  n={len(sub):>4}  win%={pct(sw,len(sub)):>6}  "
                  f"totaal={eur(sp)}")

# ══════════════════════════════════════════════════════════════════════════════
# 3. Score distributiediagram
# ══════════════════════════════════════════════════════════════════════════════

all_scores = conn.execute("""
    SELECT conviction_score_at_trigger AS score
    FROM trades
    WHERE conviction_score_at_trigger IS NOT NULL
      AND conviction_score_at_trigger > 0
      AND status IN ('closed', 'resolved')
""").fetchall()

if all_scores:
    print(f"\n{SEP}")
    print("  CONVICTION SCORE VERDELING (alle afgeronde trades)")
    print(SEP)
    scores = [r["score"] for r in all_scores]
    bins = [0.0, 0.10, 0.20, 0.30, 0.35, 0.40, 0.50, 0.60, 0.65, 0.80, 1.01]
    labels = ["0.00–0.10","0.10–0.20","0.20–0.30","0.30–0.35",
              "0.35–0.40","0.40–0.50","0.50–0.60","0.60–0.65","0.65–0.80","0.80–1.00"]
    max_bar = 40
    total = len(scores)
    for i, label in enumerate(labels):
        lo, hi = bins[i], bins[i+1]
        cnt = sum(1 for s in scores if lo <= s < hi)
        bar = "█" * int(cnt / total * max_bar) if total else ""
        marker = " ◄ 0.35 grens" if label == "0.30–0.35" else (
                 " ◄ 0.65 grens" if label == "0.60–0.65" else "")
        print(f"  {label}  {bar:<{max_bar}}  {cnt:>5} ({pct(cnt,total):>5}){marker}")
    print(f"  Totaal: {total}")

conn.close()
print(f"\n{'═'*72}\n")
