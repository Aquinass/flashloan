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

# Depth floor for a cycle: every hop must be able to absorb the trade. Using
# the smallest hop is the honest bound, since one thin leg caps the whole route.
MIN_HOP_DEPTH_USD = 20000

# Assets we are willing to borrow (Balancer vault must hold them).
BORROWABLE = {"USDC.e", "USDC", "USDT", "DAI", "WETH", "WPOL", "WBTC"}


def index_edges(edges):
    by_src = defaultdict(list)
    for e in edges:
        if e["depthUsd"] >= MIN_HOP_DEPTH_USD:
            by_src[e["src"]].append(e)
    return by_src


def find_cycles(edges, max_hops=3, top_n=40):
    """
    Enumerate profitable-at-mid cycles starting and ending at a borrowable asset.

    Returns candidates sorted by gross edge, best first. `gross` is the product
    of post-fee edge multipliers minus 1 — the theoretical return ignoring
    price impact, which real depth will erode.
    """
    by_src = index_edges(edges)
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
