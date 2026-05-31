"""
Vergelijking: onze initiele instap vs. is5minfixedyet

is5's methode: koop ALTIJD de markt-dominante kant (geprijsd > 0.50) bij window-start.
Onze methode:  koop de OFI/conviction-gestuurde kant, ook als die sub-0.50 staat.

Afleidingsregel voor is5's keuze:
  - Als our entry_price > 0.50  → wij en is5 kozen dezelfde kant (dominant)
  - Als our entry_price < 0.50  → wij kozen de underdog; is5 koos de ANDERE kant
  - Als our entry_price == 0.50 → volledig onzeker (50/50 call)

Data: 17 BGGDSB-windows uit backtest_is5_strategy.py
"""

from dataclasses import dataclass

ENTRY_AMBIG_THRESHOLD = 0.005   # binnen 0.005 van 0.50 = ambiguous


@dataclass
class Window:
    time: str
    our_side: str        # onze dominant keuze
    entry_price: float   # prijs van onze dominant kant (proxy uit CSV)
    winner: str          # werkelijke winnaar


WINDOWS = [
    Window("05:12", "YES", 0.71, "NO"),
    Window("05:15", "NO",  0.56, "YES"),
    Window("05:25", "NO",  0.47, "YES"),
    Window("05:30", "NO",  0.37, "NO"),
    Window("05:35", "YES", 0.51, "YES"),
    Window("05:40", "NO",  0.52, "NO"),
    Window("05:45", "YES", 0.55, "NO"),
    Window("05:50", "YES", 0.50, "NO"),
    Window("05:55", "YES", 0.56, "YES"),
    Window("06:00", "NO",  0.48, "NO"),
    Window("06:05", "YES", 0.55, "YES"),
    Window("06:10", "YES", 0.77, "YES"),
    Window("06:15", "NO",  0.49, "YES"),
    Window("06:20", "YES", 0.53, "YES"),
    Window("06:25", "YES", 0.55, "YES"),
    Window("06:30", "YES", 0.58, "YES"),
    Window("06:35", "YES", 0.53, "YES"),
]


def is5_choice(w: Window) -> str | None:
    """Afleiden van is5's keuze op basis van marktprijs."""
    diff = w.entry_price - 0.50
    if abs(diff) <= ENTRY_AMBIG_THRESHOLD:
        return None   # ambiguous
    # is5 koopt de markt-favoriet (prijs > 0.50)
    if w.entry_price > 0.50:
        return w.our_side           # zelfde kant als wij
    else:
        return "NO" if w.our_side == "YES" else "YES"   # tegenovergestelde kant


def our_correct(w: Window) -> bool:
    return w.our_side == w.winner


def is5_correct(w: Window) -> bool | None:
    choice = is5_choice(w)
    if choice is None:
        return None
    return choice == w.winner


# ─── Tabel ────────────────────────────────────────────────────────────────────

print("=" * 80)
print("VERGELIJKING: ONZE INSTAP vs. IS5MINFIXEDYET")
print(f"{'Tijd':<7} {'Onze':<5} {'Prijs':<7} {'is5':<5} {'Zelfde?':<9} {'Wij OK?':<9} {'is5 OK?':<9} {'Winner'}")
print("-" * 80)

same_count   = 0
diff_count   = 0
ambig_count  = 0
our_ok       = 0
is5_ok       = 0
is5_valid_n  = 0

# subcategories: same choice + different choice
same_our_ok  = 0
same_is5_ok  = 0
same_n       = 0
diff_our_ok  = 0
diff_is5_ok  = 0
diff_n       = 0

rows = []
for w in WINDOWS:
    i5 = is5_choice(w)
    same = None
    if i5 is None:
        same_str  = "ambig"
        is5ok_str = "ambig"
        ambig_count += 1
    elif i5 == w.our_side:
        same = True
        same_str  = "✓ JA"
        same_count += 1
    else:
        same = False
        same_str  = "✗ NEE"
        diff_count += 1

    ourok = our_correct(w)
    i5ok  = is5_correct(w)
    our_ok += int(ourok)
    if i5ok is not None:
        is5_ok      += int(i5ok)
        is5_valid_n += 1
        is5ok_str    = "✓ JA" if i5ok else "✗ NEE"
        if same is True:
            same_n    += 1
            same_our_ok += int(ourok)
            same_is5_ok += int(i5ok)
        elif same is False:
            diff_n    += 1
            diff_our_ok += int(ourok)
            diff_is5_ok += int(i5ok)
    else:
        is5ok_str = "ambig"

    ourok_str = "✓ JA" if ourok else "✗ NEE"
    i5_disp   = i5 if i5 else "?"
    rows.append((w.time, w.our_side, w.entry_price, i5_disp, same_str, ourok_str, is5ok_str, w.winner))

for r in rows:
    print(f"{r[0]:<7} {r[1]:<5} {r[2]:<7.2f} {r[3]:<5} {r[4]:<9} {r[5]:<9} {r[6]:<9} {r[7]}")

n = len(WINDOWS)
print("-" * 80)
print(f"\n{'SAMENVATTING':}")
print(f"  Totaal windows analyseerd  : {n}")
print(f"  Zelfde keuze               : {same_count}/{n - ambig_count} = {same_count/(n-ambig_count)*100:.1f}%")
print(f"  Andere keuze               : {diff_count}/{n - ambig_count} = {diff_count/(n-ambig_count)*100:.1f}%")
print(f"  Ambiguous (exact 0.50)     : {ambig_count}")
print()
print(f"  Onze richting correct      : {our_ok}/{n} = {our_ok/n*100:.1f}%")
print(f"  is5 richting correct       : {is5_ok}/{is5_valid_n} = {is5_ok/is5_valid_n*100:.1f}%  (excl. ambig)")
print()

# ─── Breakdown: zelfde vs. verschillende keuze ────────────────────────────────
print("─" * 80)
print("  BREAKDOWN: wanneer dezelfde vs. andere keuze")
print("─" * 80)
if same_n:
    print(f"  Zelfde keuze (n={same_n})")
    print(f"    → Wij correct   : {same_our_ok}/{same_n} = {same_our_ok/same_n*100:.1f}%")
    print(f"    → is5 correct   : {same_is5_ok}/{same_n} = {same_is5_ok/same_n*100:.1f}%  (zelfde, dus identiek)")
if diff_n:
    print(f"  Andere keuze (n={diff_n}): WIJ kozen de underdog (<0.50), is5 koos de favoriet (>0.50)")
    print(f"    → Wij correct   : {diff_our_ok}/{diff_n} = {diff_our_ok/diff_n*100:.1f}%")
    print(f"    → is5 correct   : {diff_is5_ok}/{diff_n} = {diff_is5_ok/diff_n*100:.1f}%")

# ─── Prijsklassen ─────────────────────────────────────────────────────────────
print()
print("─" * 80)
print("  INSTAPPRIJS ANALYSE: effect op richting-accuracy")
print("─" * 80)
buckets = [
    ("0.37–0.49 (underdog)",    0.37, 0.499),
    ("0.50–0.54 (rand-50)",     0.50, 0.549),
    ("0.55–0.64 (licht dom.)",  0.55, 0.649),
    ("0.65+ (sterk dom.)",      0.65, 1.00),
]
for label, lo, hi in buckets:
    ws = [w for w in WINDOWS if lo <= w.entry_price <= hi]
    if not ws:
        continue
    ok = sum(1 for w in ws if our_correct(w))
    print(f"  {label:<28} n={len(ws):>2}  wij correct={ok}/{len(ws)}={ok/len(ws)*100:.0f}%")

# ─── Cruciale insight ─────────────────────────────────────────────────────────
print()
print("─" * 80)
print("  CONCLUSIE")
print("─" * 80)
underdog_wins = [w for w in WINDOWS if w.entry_price < 0.50 and abs(w.entry_price - 0.50) > ENTRY_AMBIG_THRESHOLD]
print(f"  Wij handelden {len(underdog_wins)}x TEGEN de markt (entry_price < 0.50) — conviction override.")
print(f"  In die windows: wij correct {sum(1 for w in underdog_wins if our_correct(w))}/{len(underdog_wins)}, "
      f"is5 correct {sum(1 for w in underdog_wins if is5_correct(w))}/{len(underdog_wins)}")
print()
fav_wins = [w for w in WINDOWS if w.entry_price > 0.50 + ENTRY_AMBIG_THRESHOLD]
print(f"  Wij handelden {len(fav_wins)}x MET de markt (entry_price > 0.50) — markt-favoriet.")
print(f"  In die windows: wij correct {sum(1 for w in fav_wins if our_correct(w))}/{len(fav_wins)} = "
      f"{sum(1 for w in fav_wins if our_correct(w))/len(fav_wins)*100:.0f}%")

if __name__ == "__main__":
    pass  # script is top-level, output al gedaan
