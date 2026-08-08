"""Cycle search over the full pool graph.

Two stages, because exact quotes are expensive and mid prices are cheap:

  1. SCREEN  - read every pool's state in one batch, build a directed graph
               whose edge weights are post-fee marginal prices, and enumerate
               2- and 3-hop cycles whose product exceeds 1. This is free of
               price impact, so it over-reports: every hit is a candidate, not
               a trade.

  2. CONFIRM - re-quote surviving candidates through the real quoters at the
               real size, which prices impact exactly. Only these numbers are
               trustworthy.

The screen exists to make the confirm step affordable: it turns thousands of
possible cycles into a handful worth an RPC round trip.
"""

from decimal import Decimal
from collections import defaultdict

from pool_registry import TOKENS, PX

# Depth floor for a cycle. This is a WEAK filter and deliberately low: measured
# against live quotes, reserve depth turns out to be a poor predictor of what a
# hop actually costs. DAI/USDC.e on sushiV2 carries $15.2k of depth and loses
# 3.74% round-trip at $250; DAI/USDT on quickV3 has *less* depth ($11.0k) and
# costs 0.0063% — a 590x difference at comparable depth. 27 pools above $20k
# still lose >0.5%, so a depth threshold both admits traps and hides good pools.
#
# Depth is therefore used only to drop genuinely empty pools. The real filter is
# MAX_HOP_COST_PCT below, applied to a measured quote.
MIN_HOP_DEPTH_USD = 5000

# Per-hop cost ceiling, measured not assumed: a hop may not cost more than this
# fraction of the trade (fee + price impact at the intended size). Concentrated
# V3 liquidity clears this comfortably; drained V2 pools do not, regardless of
# how much sits in their reserves.
MAX_HOP_COST_PCT = 0.15

# Assets we are willing to borrow (Balancer vault must hold them).
BORROWABLE = {"USDC.e", "USDC", "USDT", "DAI", "WETH", "WPOL", "WBTC"}


def index_edges(edges, costly_pools=None):
    """
    Group edges by source token, dropping empty pools and — when a cost profile
    has been measured — pools whose real cost exceeds MAX_HOP_COST_PCT.
    """
    costly_pools = costly_pools or set()
    by_src = defaultdict(list)
    for e in edges:
        if e["depthUsd"] < MIN_HOP_DEPTH_USD:
            continue
        if e["pool"] in costly_pools:
            continue
        by_src[e["src"]].append(e)
    return by_src


def profile_hop_costs(w3, pools, size_usd=250, verbose=False):
    """
    Measure each pool's real round-trip cost and return the set of pool
    addresses too expensive to route through.

    Quotes out and back through the *same* pool so the USD price mark cancels;
    what remains is exactly 2x fee + 2x price impact. That makes the result
    independent of the static PX table, which is only accurate for stablecoins.
    Halved to approximate a single hop.
    """
    import research as R
    import pool_registry as PR

    costly = set()
    for p in pools:
        a, b = p["tokenA"], p["tokenB"]
        px = PR.PX.get(a, 0)
        if px <= 0:
            continue
        amt = Decimal(size_usd) / Decimal(str(px))
        try:
            mid = R.quote(w3, p["venue"], a, b, amt, fee=p["fee"], tokens=PR.TOKENS)
            back = R.quote(w3, p["venue"], b, a, mid, fee=p["fee"], tokens=PR.TOKENS) if mid else None
        except Exception:
            back = None
        if not back:
            costly.add(p["pool"])
            continue
        one_way = float((amt - back) / amt * 100) / 2
        if one_way > MAX_HOP_COST_PCT:
            costly.add(p["pool"])
            if verbose:
                print(f"  prune {a}/{b} {p['venue']}:{p['feeLabel']} "
                      f"cost {one_way:.4f}% (depth ${p['depthUsd']:,.0f})")
    return costly


def find_cycles(edges, max_hops=3, top_n=40, costly_pools=None):
    """
    Enumerate profitable-at-mid cycles starting and ending at a borrowable asset.

    Returns candidates sorted by gross edge, best first. `gross` is the product
    of post-fee edge multipliers minus 1 — the theoretical return ignoring
    price impact, which real depth will erode.
    """
    by_src = index_edges(edges, costly_pools)
    out = []

    for start in sorted(BORROWABLE):
        if start not in by_src:
            continue

        # 2-hop: start -> mid -> start, across two different venues
        for e1 in by_src[start]:
            mid = e1["dst"]
            for e2 in by_src.get(mid, []):
                if e2["dst"] != start or e2["pool"] == e1["pool"]:
                    continue
                gross = e1["mult"] * e2["mult"] - 1
                if gross > 0:
                    out.append({
                        "start": start, "hops": 2, "gross": gross,
                        "path": [start, mid, start],
                        "legs": [e1, e2],
                    })

        if max_hops < 3:
            continue

        # 3-hop triangular: start -> a -> b -> start
        for e1 in by_src[start]:
            a = e1["dst"]
            for e2 in by_src.get(a, []):
                b = e2["dst"]
                if b == start or e2["pool"] == e1["pool"]:
                    continue
                partial = e1["mult"] * e2["mult"]
                for e3 in by_src.get(b, []):
                    if e3["dst"] != start:
                        continue
                    if e3["pool"] in (e1["pool"], e2["pool"]):
                        continue
                    gross = partial * e3["mult"] - 1
                    if gross > 0:
                        out.append({
                            "start": start, "hops": 3, "gross": gross,
                            "path": [start, a, b, start],
                            "legs": [e1, e2, e3],
                        })

    out.sort(key=lambda c: -c["gross"])
    # dedupe by route signature, keeping the best variant
    seen, uniq = set(), []
    for c in out:
        sig = tuple(l["pool"] for l in c["legs"])
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(c)
    return uniq[:top_n]


def describe(c):
    route = " -> ".join(c["path"])
    venues = " | ".join(l["venue"] for l in c["legs"])
    cap = min(l["depthUsd"] for l in c["legs"])
    return (f"{route:<34} {float(c['gross'])*100:>+9.4f}%  "
            f"cap ${cap:>10,.0f}  {venues}")
