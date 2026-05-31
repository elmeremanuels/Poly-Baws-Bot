"""
BGGDSB Backtest: Oude flip-strategie vs. Nieuwe hold-strategie (is5 aanpak).

Data: 12 gesloten BGGDSB trades uit de server export + log-data.
Nieuwe strategie: koop dominant kant bij window-start, hold to expiry.
P&L = winner_shares × €1.00 - total_cost.
"""

import json
from dataclasses import dataclass, field

BUDGET = 20.0        # nieuw window_budget_eur
HEDGE_PCT = 0.10     # 10% van budget op hedge
HEDGE_PRICE = 0.11   # hedge trigger prijs


@dataclass
class Trade:
    time: str
    dominant: str       # "YES" or "NO"
    entry_price: float  # dom_ask bij window-start (benadering uit CSV)
    actual_winner: str  # werkelijke winnaar
    old_pnl: float      # gerapporteerde P&L uit bot (formule-bug inbegrepen)
    old_exit: str       # exit reason
    # Of de verliezende kant op enig moment ≤ 0.11 was:
    loser_hit_011: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# Trade data: gecombineerd uit export CSV + server logs.
#
# dominant:     de kant die de bot koos (degene met shares in het snapshot)
# entry_price:  YES prijs of NO prijs uit CSV (proxy voor werkelijke dom_ask)
#               Kanttekening: dit is de prijs bij export (net na window), niet
#               bij window-start. Werkelijke instapprijs is iets lager → onze
#               schatting is conservatief.
# loser_hit_011: True als we weten (uit is5 data) dat de verliezende kant
#               op enig moment tot ≤ 0.11 daalde.
# ──────────────────────────────────────────────────────────────────────────────
TRADES = [
    # Vroegste windows (05:xx UTC)
    Trade("05:12", "YES", 0.71, "NO",  -6.09,  "held_for_resolution", loser_hit_011=True),
    Trade("05:15", "NO",  0.56, "YES", -16.78, "peg_cross",            loser_hit_011=False),
    Trade("05:25", "NO",  0.47, "YES", -20.00, "expiry",               loser_hit_011=False),
    Trade("05:30", "NO",  0.37, "NO",  -13.20, "peg_cross",            loser_hit_011=True),
    Trade("05:35", "YES", 0.51, "YES",  -1.22, "peg_cross",            loser_hit_011=False),
    Trade("05:40", "NO",  0.52, "NO",   +6.25, "limit_filled",         loser_hit_011=False),
    Trade("05:45", "YES", 0.55, "NO",  -14.65, "peg_cross",            loser_hit_011=False),
    Trade("05:50", "YES", 0.50, "NO",   -8.17, "peg_cross",            loser_hit_011=False),
    Trade("05:55", "YES", 0.56, "YES",  -0.67, "peg_cross",            loser_hit_011=False),
    Trade("06:00", "NO",  0.48, "NO",   +8.49, "limit_filled",         loser_hit_011=True),
    Trade("06:05", "YES", 0.55, "YES", -30.00, "expiry",               loser_hit_011=False),
    Trade("06:10", "YES", 0.77, "YES",  +4.19, "held_for_resolution",  loser_hit_011=True),

    # Server logs (06:15-06:39 UTC) — alleen via logs zichtbaar, niet in CSV
    # yes_spend / no_spend aanwezig; dom_side van initial_spend afgeleid
    # entry_price: budget / initial_dom_shares (benaderd uit DOM=YES→yes_spend=20→price≈0.55)
    Trade("06:15", "NO",  0.49, "YES", -20.00, "expiry",               loser_hit_011=False),
    Trade("06:20", "YES", 0.53, "YES",   0.00, "expiry",               loser_hit_011=False),
    Trade("06:25", "YES", 0.55, "YES", -20.00, "expiry",               loser_hit_011=False),
    Trade("06:30", "YES", 0.58, "YES", -20.00, "expiry",               loser_hit_011=False),
    Trade("06:35", "YES", 0.53, "YES", -23.50, "expiry",               loser_hit_011=False),
]


def sim_new_strategy(t: Trade, budget: float = BUDGET) -> tuple[float, str]:
    """
    Simuleer nieuwe hold-strategie voor één trade.

    Returns (net_pnl, note).
    """
    dom_shares = budget / t.entry_price    # shares gekocht bij dom_ask
    dom_cost = budget                       # volledig budget op dominant kant

    hedge_cost = 0.0
    hedge_shares = 0.0
    if t.loser_hit_011:
        hedge_cost   = round(budget * HEDGE_PCT, 2)
        hedge_shares = round(hedge_cost / HEDGE_PRICE, 2)

    total_cost = dom_cost + hedge_cost

    if t.dominant == t.actual_winner:
        # Dominant kant wint → REDEEM dom_shares × €1
        payout = dom_shares
        note = f"{t.dominant} dom ({dom_shares:.1f} shares × €1)"
        if hedge_cost > 0:
            note += f" + hedge verloren (€{hedge_cost:.2f})"
    else:
        # Dominant kant verliest
        if hedge_cost > 0:
            payout = hedge_shares  # hedge wint → REDEEM
            note = f"{t.dominant} dom verloor, hedge ({hedge_shares:.1f} shares × €1)"
        else:
            payout = 0.0
            note = f"{t.dominant} dom verloor, geen hedge"

    net_pnl = round(payout - total_cost, 2)
    return net_pnl, note


# ── Resultaten ─────────────────────────────────────────────────────────────────

print("=" * 80)
print("BGGDSB Backtest: Flip-strategie vs. Hold-strategie (is5 aanpak)")
print(f"Budget per window: €{BUDGET:.0f}  |  Hedge: {HEDGE_PCT*100:.0f}% @ €{HEDGE_PRICE}")
print("=" * 80)

header = f"{'Tijd':<8} {'DOM':<5} {'Prijs':<7} {'Winner':<7} {'Oud P&L':>9} {'Nieuw P&L':>10} {'Delta':>8}  Noot"
print(header)
print("-" * 80)

old_total  = 0.0
new_total  = 0.0
old_wins   = 0
new_wins   = 0
old_losses = 0
new_losses = 0

for t in TRADES:
    new_pnl, note = sim_new_strategy(t)
    delta = new_pnl - t.old_pnl
    old_total += t.old_pnl
    new_total += new_pnl
    if t.old_pnl > 0: old_wins += 1
    else: old_losses += 1
    if new_pnl > 0: new_wins += 1
    else: new_losses += 1

    old_str = f"€{t.old_pnl:+.2f}"
    new_str = f"€{new_pnl:+.2f}"
    delta_str = f"€{delta:+.2f}"
    print(f"{t.time:<8} {t.dominant:<5} {t.entry_price:<7.2f} {t.actual_winner:<7} {old_str:>9} {new_str:>10} {delta_str:>8}  {note}")

print("-" * 80)
print(f"{'TOTAAL':<8} {'':<5} {'':<7} {'':<7} {'€'+f'{old_total:+.2f}':>9} {'€'+f'{new_total:+.2f}':>10} {'€'+f'{new_total-old_total:+.2f}':>8}")
print()
print(f"Winst-trades:  Oud={old_wins}/{len(TRADES)} ({old_wins/len(TRADES)*100:.0f}%)  Nieuw={new_wins}/{len(TRADES)} ({new_wins/len(TRADES)*100:.0f}%)")
print(f"Verlies-trades: Oud={old_losses}  Nieuw={new_losses}")
print()

# ── Per-categorie analyse ───────────────────────────────────────────────────────
correct_dir   = [t for t in TRADES if t.dominant == t.actual_winner]
incorrect_dir = [t for t in TRADES if t.dominant != t.actual_winner]

print(f"Richting correct: {len(correct_dir)}/{len(TRADES)} ({len(correct_dir)/len(TRADES)*100:.0f}%)")
if correct_dir:
    new_pnls_correct = [sim_new_strategy(t)[0] for t in correct_dir]
    print(f"  → Nieuw P&L bij correcte richting: "
          f"gem €{sum(new_pnls_correct)/len(new_pnls_correct):+.2f}  "
          f"totaal €{sum(new_pnls_correct):+.2f}")
    print(f"  → Gemiddelde entry prijs: {sum(t.entry_price for t in correct_dir)/len(correct_dir):.3f}")
    print(f"  → Gemiddelde shares bij €{BUDGET}: {sum(BUDGET/t.entry_price for t in correct_dir)/len(correct_dir):.1f}")

print(f"Richting fout:   {len(incorrect_dir)}/{len(TRADES)} ({len(incorrect_dir)/len(TRADES)*100:.0f}%)")
if incorrect_dir:
    new_pnls_wrong = [sim_new_strategy(t)[0] for t in incorrect_dir]
    print(f"  → Nieuw P&L bij foute richting: "
          f"gem €{sum(new_pnls_wrong)/len(new_pnls_wrong):+.2f}  "
          f"totaal €{sum(new_pnls_wrong):+.2f}")
    hedged = [t for t in incorrect_dir if t.loser_hit_011]
    print(f"  → Verlies met hedge: {len(hedged)} trades "
          f"(gem P&L: €{sum(sim_new_strategy(t)[0] for t in hedged)/len(hedged):+.2f})" if hedged else "  → Geen hedges geplaatst")

print()
print("── Break-even analyse ─────────────────────────────────────────────────")
# Bij welke win-rate is de nieuwe strategie winstgevend?
# Win: gem win P&L = BUDGET × (1/avg_win_price - 1)
# Loss: -BUDGET per verlies
avg_win_price = sum(t.entry_price for t in correct_dir) / len(correct_dir) if correct_dir else 0.50
avg_win_pnl   = BUDGET / avg_win_price - BUDGET
avg_loss_pnl  = -BUDGET

be_winrate = BUDGET / (avg_win_pnl + BUDGET)  # break-even win%
print(f"Gemiddelde instapprijs bij winst: €{avg_win_price:.3f}")
print(f"Gem P&L per win:  €{avg_win_pnl:+.2f}")
print(f"Gem P&L per loss: €{avg_loss_pnl:+.2f}")
print(f"Break-even win%:  {be_winrate*100:.1f}%  (huidige: {len(correct_dir)/len(TRADES)*100:.1f}%)")
margin = len(correct_dir)/len(TRADES) - be_winrate
print(f"Marge boven break-even: {margin*100:+.1f}%")

print()
print("── Is5 vergelijking ───────────────────────────────────────────────────")
print(f"Is5 gem entry prijs: ~€0.38 → {BUDGET/0.38:.1f} shares → +€{BUDGET/0.38 - BUDGET:.2f} per win")
print(f"Onze gem entry prijs: €{sum(t.entry_price for t in TRADES)/len(TRADES):.3f} → "
      f"{BUDGET/0.545:.1f} shares → +€{BUDGET/0.545 - BUDGET:.2f} per win")
print(f"→ Is5 verdient {(BUDGET/0.38 - BUDGET)/(BUDGET/0.545 - BUDGET):.1f}× meer per gewonnen window")
print()
print("Kanttekening: entry_price in CSV is prijs bij export (≈ vlak na window-start),")
print("niet de exacte dom_ask. Werkelijke instapprijs bij window-start = iets lager")
print("→ echte P&L met nieuwe strategie is waarschijnlijk iets beter dan hier getoond.")
