"""Analyseer is5minfixedyet CSV — reconstrueer elk window chronologisch."""
import csv
from collections import defaultdict
from dataclasses import dataclass, field

CSV_PATH = "/root/.claude/uploads/b5e2290e-1135-4bf4-a81f-837845a63a2d/acf66e41-is5minfixedyet_activity_4.csv"

@dataclass
class WindowSummary:
    question: str
    market_id: str
    trades: list = field(default_factory=list)   # (ts, side, price, usdc, shares, type)
    redeem_shares: float = 0.0
    redeem_usdc: float = 0.0

def load(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows

def analyze(rows):
    # Group by market_id (= window)
    windows: dict[str, WindowSummary] = {}
    for r in rows:
        mid = r["market_id"]
        if mid not in windows:
            windows[mid] = WindowSummary(question=r["question"], market_id=mid)
        w = windows[mid]
        ts    = int(r["event_ts"]) if r["event_ts"] else 0
        side  = r["outcome_side"]
        ttype = r["trade_type"]
        price = float(r["price"]) if r["price"] else 0.0
        usdc  = float(r["usdc_size"]) if r["usdc_size"] else 0.0
        shares= float(r["size"]) if r["size"] else 0.0
        if ttype == "REDEEM":
            w.redeem_shares += shares
            w.redeem_usdc   += usdc
        else:
            w.trades.append((ts, side, price, usdc, shares))

    # Sort trades within each window chronologically (ascending ts)
    for w in windows.values():
        w.trades.sort(key=lambda x: x[0])

    return windows

def summarize_window(w: WindowSummary):
    if not w.trades:
        return None

    # Determine dominant side (first trade = initial direction)
    dom_side = w.trades[0][1]

    # Group trades by side
    dom_trades   = [(ts,p,u,s) for ts,side,p,u,s in w.trades if side == dom_side]
    other_side   = "Down" if dom_side == "Up" else "Up"
    hedge_trades = [(ts,p,u,s) for ts,side,p,u,s in w.trades if side == other_side]

    dom_usdc   = sum(u for _,_,u,_ in dom_trades)
    dom_shares = sum(s for _,_,_,s in dom_trades)
    hedge_usdc = sum(u for _,_,u,_ in hedge_trades)
    hedge_shares = sum(s for _,_,_,s in hedge_trades)

    total_usdc = dom_usdc + hedge_usdc

    entry_price = dom_trades[0][1] if dom_trades else 0
    min_dom_price = min(p for _,p,_,_ in dom_trades) if dom_trades else 0
    max_dom_price = max(p for _,p,_,_ in dom_trades) if dom_trades else 0
    avg_dom_price = dom_usdc / dom_shares if dom_shares else 0

    # Hedge prices (is it always ~0.11?)
    hedge_prices = sorted(set(round(p, 2) for _,p,_,_ in hedge_trades))

    # Did he flip? = did he buy the other side at price > 0.20?
    flipped = any(p > 0.20 for _,p,_,_ in hedge_trades)

    # Was this a win? (redeemed the dom side → dom won)
    won = w.redeem_shares > 0 and w.redeem_shares >= dom_shares * 0.5

    # Price at redeem = inferred winner
    winner = dom_side if won else (other_side if w.redeem_shares > 0 else "?")

    # P&L
    payout = w.redeem_usdc
    pnl    = round(payout - total_usdc, 2)

    return {
        "question":       w.question[-45:],
        "dom_side":       dom_side,
        "entry_price":    round(entry_price, 3),
        "min_dom_price":  round(min_dom_price, 3),
        "avg_dom_price":  round(avg_dom_price, 3),
        "dom_usdc":       round(dom_usdc, 2),
        "dom_shares":     round(dom_shares, 1),
        "dom_n_trades":   len(dom_trades),
        "hedge_usdc":     round(hedge_usdc, 2),
        "hedge_prices":   hedge_prices,
        "total_usdc":     round(total_usdc, 2),
        "redeem_usdc":    round(w.redeem_usdc, 2),
        "pnl":            pnl,
        "winner":         winner,
        "correct":        dom_side == winner,
        "flipped":        flipped,
        "n_trades":       len(w.trades),
        "first_ts":       w.trades[0][0] if w.trades else 0,
    }

def main():
    rows  = load(CSV_PATH)
    windows = analyze(rows)
    summaries = [s for s in (summarize_window(w) for w in windows.values()) if s]
    summaries.sort(key=lambda x: x["first_ts"])

    print("=" * 110)
    print(f"IS5MINFIXEDYET — {len(summaries)} windows geanalyseerd")
    print("=" * 110)
    hdr = f"{'Window':<46} {'Dom':>4} {'Entry':>6} {'Min':>5} {'Avg':>5} {'€Dom':>7} {'NTr':>3} {'€Hed':>6} {'HdgPx':<16} {'€Tot':>7} {'Payout':>7} {'P&L':>8} {'Win?':>5} {'Flip?':>5}"
    print(hdr)
    print("-" * 110)

    wins = losses = 0
    total_pnl = 0.0
    total_invested = 0.0
    flips = 0
    hedge_prices_all = []

    for s in summaries:
        won_str  = "✓" if s["correct"] else "✗"
        flip_str = "FLIP!" if s["flipped"] else ""
        pnl_str  = f"€{s['pnl']:+.2f}"
        hp       = ",".join(f"{p:.2f}" for p in s["hedge_prices"][:3])
        print(f"{s['question']:<46} {s['dom_side']:>4} {s['entry_price']:>6.3f} {s['min_dom_price']:>5.3f} {s['avg_dom_price']:>5.3f} "
              f"€{s['dom_usdc']:>6.1f} {s['dom_n_trades']:>3} €{s['hedge_usdc']:>5.2f} {hp:<16} €{s['total_usdc']:>6.1f} "
              f"€{s['redeem_usdc']:>6.1f} {pnl_str:>8} {won_str:>5} {flip_str}")
        if s["correct"]:
            wins += 1
        else:
            losses += 1
        total_pnl      += s["pnl"]
        total_invested += s["total_usdc"]
        if s["flipped"]:
            flips += 1
        hedge_prices_all.extend(s["hedge_prices"])

    n = wins + losses
    print("=" * 110)
    print(f"\nRESULTAAT ({n} windows met duidelijke uitkomst):")
    print(f"  Win rate          : {wins}/{n} = {wins/n*100:.1f}%")
    print(f"  Totaal geïnvesteerd: €{total_invested:.2f}")
    print(f"  Totaal payout     : €{total_invested+total_pnl:.2f}")
    print(f"  Netto P&L         : €{total_pnl:+.2f}")
    print(f"  ROI               : {total_pnl/total_invested*100:+.1f}%")
    print(f"  Gem. P&L/window   : €{total_pnl/n:+.2f}")
    print(f"  Flipt hij?        : {flips}x van de {n} windows ({flips/n*100:.1f}%)")

    # Entry price distribution
    all_entries = [s["entry_price"] for s in summaries]
    print(f"\nINSTAPPRIJS DOMINANTE KANT:")
    for lo, hi in [(0.0,0.30),(0.30,0.40),(0.40,0.50),(0.50,0.60),(0.60,0.70),(0.70,1.01)]:
        bucket = [s for s in summaries if lo <= s["entry_price"] < hi]
        if bucket:
            wr = sum(1 for s in bucket if s["correct"]) / len(bucket) * 100
            avg_pnl = sum(s["pnl"] for s in bucket) / len(bucket)
            print(f"  {lo:.2f}–{hi:.2f}: n={len(bucket):>3}  WR={wr:>5.1f}%  gem P&L=€{avg_pnl:+.2f}")

    # Hedge price analysis
    from collections import Counter
    hp_counts = Counter(round(p,2) for p in hedge_prices_all)
    print(f"\nHEDGE PRIJZEN (alle windows):")
    for price, cnt in sorted(hp_counts.items()):
        print(f"  {price:.2f}: {cnt}x")

    # Averaging down analysis
    print(f"\nAVERAGING DOWN ANALYSE:")
    avg_down = [s for s in summaries if s["min_dom_price"] < s["entry_price"] - 0.10]
    print(f"  Windows met price drop > 10ct: {len(avg_down)}/{n} ({len(avg_down)/n*100:.0f}%)")
    if avg_down:
        wr_ad = sum(1 for s in avg_down if s["correct"]) / len(avg_down) * 100
        print(f"  Winrate bij grote price drop  : {wr_ad:.1f}%")
        drops = [s for s in avg_down if not s["correct"]]
        print(f"  Verliezen bij grote drop      : {len(drops)}")

if __name__ == "__main__":
    main()
