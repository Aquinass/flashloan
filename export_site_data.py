"""Export scan results to JSON for the static dashboard.

GitHub Pages serves files; it cannot run Python or hold an RPC connection. So
the site reads a committed snapshot that this script regenerates — locally, or
from the scheduled GitHub Action.

Writes docs/data/summary.json. No secrets go in: RPC_URL is read from the
environment for the live scan and never serialised.
"""

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

OUT = Path(__file__).parent / "docs" / "data" / "summary.json"
SPREADS = Path(__file__).parent / "data" / "spreads.jsonl"
HURDLE = 0.0042
LOGGER_TARGET_HOURS = 24.0
# Size at which a screen hit must still clear the hurdle to count as real.
CONFIRM_SIZE_USD = 1000


def _f(x):
    return float(x) if x is not None else None


def exact_route_return(w3, pools, path, venues, size_usd):
    """Walk a cycle through the real quoters and return its % return, or None.

    Reuses research.quote so the confirm stage and the scanner price through one
    code path; a second implementation here would be free to drift.
    """
    import pool_registry as PR
    import research as R

    px = PR.PX.get(path[0])
    if not px:
        return None
    start_amt = Decimal(size_usd) / Decimal(str(px))

    amt = start_amt
    for i, venue_label in enumerate(venues):
        venue, _, fee_label = venue_label.partition(":")
        sym_in, sym_out = path[i], path[i + 1]
        fee = next((p["fee"] for p in pools
                    if p["venue"] == venue
                    and {p["tokenA"], p["tokenB"]} == {sym_in, sym_out}
                    and (not fee_label or p["feeLabel"] == fee_label)), None)
        if fee is None:
            return None
        try:
            amt = R.quote(w3, venue, sym_in, sym_out, amt, fee=fee, tokens=PR.TOKENS)
        except Exception:
            return None
        if not amt:
            return None

    return float((amt - start_amt) / start_amt * 100)


def collect_pools(reg):
    pools = reg.get("pools", [])
    by_venue = defaultdict(lambda: {"count": 0, "depth": 0.0})
    for p in pools:
        v = by_venue[p["venue"]]
        v["count"] += 1
        v["depth"] += p["depthUsd"]
    return {
        "block": reg.get("block"),
        "minDepthUsd": reg.get("minDepthUsd"),
        "total": len(pools),
        "totalDepthUsd": sum(p["depthUsd"] for p in pools),
        "byVenue": [
            {"venue": k, "count": v["count"], "depthUsd": v["depth"]}
            for k, v in sorted(by_venue.items(), key=lambda kv: -kv[1]["depth"])
        ],
        "top": [
            {
                "pair": f"{p['tokenA']}/{p['tokenB']}",
                "venue": p["venue"],
                "fee": p["feeLabel"],
                "depthUsd": p["depthUsd"],
                "pool": p["pool"],
            }
            for p in pools[:25]
        ],
    }


def collect_cycles(w3, pools):
    """Run the live cycle search and return the ranked best routes."""
    import cycle_finder as CF
    import pool_state as PS

    states = PS.read_states(w3, pools)
    edges = PS.build_edges(pools, states)

    # Prune hops by measured cost, not by reserve depth. Depth is a poor proxy:
    # a $15.2k sushiV2 pool loses 3.74% round-trip at $250 while a $11.0k
    # quickV3 pool costs 0.0063%. Filtering on depth alone both admits traps
    # and hides good routes.
    costly = CF.profile_hop_costs(w3, pools, size_usd=CONFIRM_SIZE_USD)

    by = defaultdict(list)
    for e in edges:
        if e["depthUsd"] >= CF.MIN_HOP_DEPTH_USD and e["pool"] not in costly:
            by[e["src"]].append(e)

    found, n2, n3 = [], 0, 0
    for start in sorted(CF.BORROWABLE):
        for e1 in by.get(start, []):
            mid = e1["dst"]
            for e2 in by.get(mid, []):
                if e2["dst"] != start or e2["pool"] == e1["pool"]:
                    continue
                n2 += 1
                found.append((e1["mult"] * e2["mult"] - 1, 2,
                              [start, mid, start], [e1, e2]))
        for e1 in by.get(start, []):
            a = e1["dst"]
            for e2 in by.get(a, []):
                b = e2["dst"]
                if b == start or e2["pool"] == e1["pool"]:
                    continue
                part = e1["mult"] * e2["mult"]
                for e3 in by.get(b, []):
                    if e3["dst"] != start:
                        continue
                    if e3["pool"] in (e1["pool"], e2["pool"]):
                        continue
                    n3 += 1
                    found.append((part * e3["mult"] - 1, 3,
                                  [start, a, b, start], [e1, e2, e3]))

    found.sort(key=lambda x: -x[0])
    seen, uniq = set(), []
    for g, h, path, legs in found:
        sig = tuple(l["pool"] for l in legs)
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append({
            "route": " → ".join(path),
            "hops": h,
            "grossPct": _f(g * 100),
            "capUsd": min(l["depthUsd"] for l in legs),
            "venues": [l["venue"] for l in legs],
            "path": path,
        })

    # Mid prices ignore price impact, so a positive screen hit is a candidate,
    # not a trade. Re-quote every one through the real quoters at real size —
    # thin-leg routes routinely swing from +0.012% at mid to -0.38% exact.
    # Only a route that survives this may be reported as profitable.
    confirmed = 0
    for c in uniq:
        if c["grossPct"] <= HURDLE:
            continue
        net = exact_route_return(w3, pools, c["path"], c["venues"], CONFIRM_SIZE_USD)
        c["exactPct"] = _f(net)
        c["confirmed"] = bool(net is not None and net > HURDLE)
        if c["confirmed"]:
            confirmed += 1

    for c in uniq:
        c.pop("path", None)

    return {
        "enumerated": n2 + n3,
        "twoHop": n2,
        "threeHop": n3,
        "profitableAtMid": sum(1 for c in uniq if c["grossPct"] > HURDLE),
        "profitable": confirmed,
        "confirmSizeUsd": CONFIRM_SIZE_USD,
        "statesDecoded": len(states),
        "poolsConsidered": len(pools),
        "poolsPruned": len(costly),
        "maxHopCostPct": CF.MAX_HOP_COST_PCT,
        "deepEdges": sum(len(v) for v in by.values()),
        "best": uniq[:20],
    }


def collect_logger():
    if not SPREADS.exists():
        return None
    rows = []
    for line in SPREADS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    if not rows:
        return None

    ts = [r["ts"] for r in rows if r.get("ts")]
    t0 = datetime.fromisoformat(min(ts))
    t1 = datetime.fromisoformat(max(ts))
    hours = (t1 - t0).total_seconds() / 3600

    by_pair = defaultdict(list)
    for r in rows:
        if r.get("pair") and r.get("pct") is not None:
            by_pair[r["pair"]].append(float(r["pct"]))

    pairs = []
    for pair, vals in sorted(by_pair.items()):
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        pairs.append({
            "pair": pair,
            "n": n,
            "best": max(vals_sorted),
            "median": vals_sorted[n // 2],
            "worst": min(vals_sorted),
            "cleared": sum(1 for v in vals if v > HURDLE),
        })
    pairs.sort(key=lambda p: -p["best"])

    return {
        "observations": len(rows),
        "hours": hours,
        "targetHours": LOGGER_TARGET_HOURS,
        "progressPct": min(100.0, hours / LOGGER_TARGET_HOURS * 100),
        "since": min(ts),
        "until": max(ts),
        "clearedTotal": sum(p["cleared"] for p in pairs),
        "pairs": pairs,
    }


def main(live=True):
    import pool_registry as PR

    reg = PR.load_registry()
    if reg is None and live:
        w3 = PR.w3conn()
        reg = PR.build_registry(w3, verbose=True)
    if reg is None:
        raise SystemExit("no registry; run pool_registry.py first")

    cycles = None
    if live:
        try:
            w3 = PR.w3conn()
            cycles = collect_cycles(w3, reg["pools"])
        except Exception as e:
            print(f"  cycle scan skipped: {type(e).__name__}: {e}")

    data = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "chain": "Polygon",
        "hurdlePct": HURDLE,
        "pools": collect_pools(reg),
        "cycles": cycles,
        "logger": collect_logger(),
        "verdict": {
            "tradesExecuted": 0,
            "dryRun": True,
            "realisedPnlUsd": 0.0,
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1))
    print(f"wrote {OUT}")
    print(f"  pools: {data['pools']['total']}")
    if cycles:
        print(f"  cycles: {cycles['enumerated']:,} enumerated, "
              f"{cycles['profitable']} profitable")
    if data["logger"]:
        print(f"  logger: {data['logger']['observations']} obs, "
              f"{data['logger']['hours']:.2f}h")


if __name__ == "__main__":
    import sys
    t0 = time.time()
    main(live="--offline" not in sys.argv)
    print(f"done in {time.time()-t0:.1f}s")
