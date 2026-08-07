"""Time-series logger for cross-venue round-trip spreads on Polygon.

The one-block census in research.py showed every pair negative, best case
-0.0038%. That is the fee floor, and a single block cannot distinguish "always
arbed out" from "efficient except during dislocations". This samples the same
round trip once per new block and appends every observation to JSONL so the
frequency question can be answered from data:

  - how often does the best round trip clear the 0.0042% hurdle?
  - when it clears, how many consecutive blocks does it persist?

Persistence is the part that decides whether a public-RPC bot can trade this.
A one-block spread is lost to faster searchers; a multi-block spread is not.

Usage:
    python spread_logger.py                # run until interrupted
    python spread_logger.py --hours 24     # stop after 24h
    python spread_logger.py --report       # summarize existing log, no polling
"""

import argparse
import json
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import research as R

LOG_PATH = Path(__file__).parent / "data" / "spreads.jsonl"

# Hurdle from the measured cost stack: Balancer zero-fee loan + 2x 0.01% swap
# + gas at ~$0.0067. See ArbEngine.sol notes.
HURDLE_PCT = Decimal("0.0042")

PX = {"USDC.e": 1, "USDC": 1, "USDT": 1, "DAI": 1,
      "WETH": 1913, "WPOL": 0.0749, "WBTC": 63000}

# Ranked by measured tightness. The stable pairs are the only ones whose round
# trip lands near the hurdle; WETH/WBTC is included as a volatility control
# since dislocations should show up there first.
PAIRS = [
    ("USDC.e", "DAI"),
    ("USDC.e", "USDT"),
    ("USDC", "USDT"),
    ("USDC", "USDC.e"),
    ("WETH", "WBTC"),
]

# Depth caps trading size well below the configured $5k; sample where the edge
# actually survives.
SIZES_USD = (1000,)

# A full sample costs ~(3N-2) quotes per pair, several seconds of RPC. Polygon
# blocks land every ~2s, so per-block polling is unreachable and pointless —
# sample on a wall-clock cadence and record which block it landed on.
SAMPLE_INTERVAL_SEC = 20

# build_venues does pool discovery + depth filtering, which is slow and changes
# slowly. Refresh periodically rather than per block.
VENUE_TTL_SEC = 900
MIN_DEPTH_USD = 8000

_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True
    print("\nstopping after current block...", flush=True)


def utcnow():
    return datetime.now(timezone.utc)


def load_rows(path=LOG_PATH):
    if not path.exists():
        return []
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def report(path=LOG_PATH):
    rows = load_rows(path)
    if not rows:
        print(f"no observations in {path}")
        return
    blocks = {r["block"] for r in rows}
    t0, t1 = min(r["ts"] for r in rows), max(r["ts"] for r in rows)
    print(f"{len(rows):,} observations over {len(blocks):,} blocks")
    print(f"window: {t0} -> {t1}")
    print(f"hurdle: {HURDLE_PCT}%\n")

    print(f"{'pair':<16} {'size':>7} {'n':>6} {'best':>10} {'median':>10} "
          f"{'>hurdle':>8} {'rate':>7}")
    groups = defaultdict(list)
    for r in rows:
        groups[(r["pair"], r["size_usd"])].append(r)

    clears = defaultdict(list)
    for (pair, size), rs in sorted(groups.items()):
        pcts = sorted(Decimal(str(r["pct"])) for r in rs)
        best = pcts[-1]
        median = pcts[len(pcts) // 2]
        over = [r for r in rs if Decimal(str(r["pct"])) > HURDLE_PCT]
        rate = len(over) / len(rs) * 100
        print(f"{pair:<16} ${size:>6,} {len(rs):>6} {best:>+9.4f}% "
              f"{median:>+9.4f}% {len(over):>8} {rate:>6.2f}%")
        for r in over:
            clears[(pair, size)].append(r["block"])

    if not clears:
        # Guard the conclusion on sample size. A short window that sees no
        # dislocation is the expected result even for a pair that dislocates
        # several times a day, so calling it "arbed out" early is unjustified.
        span_h = 0.0
        try:
            t0 = datetime.fromisoformat(min(r["ts"] for r in rows))
            t1 = datetime.fromisoformat(max(r["ts"] for r in rows))
            span_h = (t1 - t0).total_seconds() / 3600
        except Exception:
            pass
        if span_h < 6:
            print(f"\nNo observation cleared the hurdle yet, but this window is "
                  f"only {span_h:.2f}h. That is far too short to distinguish "
                  f"'arbed out' from 'dislocates occasionally' — dislocations "
                  f"cluster around volatility, and a quiet hour proves nothing. "
                  f"Let it reach ~24h before drawing any conclusion.")
        else:
            print(f"\nNo observation cleared the hurdle across {span_h:.1f}h. "
                  f"On this evidence the pairs are arbed out at this size and "
                  f"further engineering will not change that.")
        return

    print("\nhurdle-clearing runs (consecutive blocks):")
    for (pair, size), bs in sorted(clears.items()):
        bs = sorted(set(bs))
        runs, cur = [], [bs[0]]
        for b in bs[1:]:
            if b == cur[-1] + 1:
                cur.append(b)
            else:
                runs.append(cur)
                cur = [b]
        runs.append(cur)
        lens = sorted((len(r) for r in runs), reverse=True)
        multi = sum(1 for L in lens if L > 1)
        print(f"  {pair:<16} ${size:>6,}  {len(runs)} run(s), "
              f"longest {lens[0]} block(s), {multi} lasting >1 block")
    print("\nRuns lasting >1 block are the tradeable ones: a single-block "
          "spread is lost to faster searchers before a public-RPC bot lands.")


def best_round_trip(venues, sym_a, sym_b, amt_a):
    """
    Best (pct, buy, sell) over ordered venue pairs, in 3N-2 quotes instead of
    research.round_trip's N + N(N-1).

    Exact, not sampled. A venue's return-leg output is monotonically increasing
    in its input, so for any fixed sell venue the best buy venue is simply the
    one with the largest forward output. That collapses the N^2 search to
    "rank forward legs, then price the return leg for the top few".

    Top TWO forward legs are evaluated, not one: a sell venue cannot also be the
    buy venue, so when the best return venue *is* the best forward venue, the
    runner-up forward leg can win overall.
    """
    fwd = {}
    for label, fn in venues:
        try:
            out = fn(sym_a, sym_b, amt_a)
            if out > 0:
                fwd[label] = out
        except Exception:
            pass
    if len(fwd) < 2:
        return None

    ranked = sorted(fwd.items(), key=lambda kv: kv[1], reverse=True)[:2]
    fns = dict(venues)
    best = None
    for buy_label, mid in ranked:
        for sell_label, sell_fn in venues:
            if sell_label == buy_label:
                continue
            try:
                back = sell_fn(sym_b, sym_a, mid)
            except Exception:
                continue
            pct = (back - amt_a) / amt_a * 100
            if best is None or pct > best[0]:
                best = (pct, buy_label, sell_label)
    return best


def sample(w3, venues, block, fh):
    """Quote every pair/size at this block and append observations."""
    ts = utcnow().isoformat()
    n = 0
    for pair in PAIRS:
        a, b = pair
        vs = venues.get(pair) or []
        if len(vs) < 2:
            continue
        for size in SIZES_USD:
            amt = Decimal(size) / Decimal(str(PX[a]))
            try:
                res = best_round_trip(vs, a, b, amt)
            except Exception as exc:
                print(f"  {a}/{b} ${size}: {str(exc)[:70]}", flush=True)
                continue
            if not res:
                continue
            pct, buy, sell = res
            fh.write(json.dumps({
                "ts": ts,
                "block": block,
                "pair": f"{a}/{b}",
                "size_usd": size,
                "pct": float(pct),
                "buy": buy,
                "sell": sell,
                "clears": bool(Decimal(str(pct)) > HURDLE_PCT),
            }) + "\n")
            n += 1
            if Decimal(str(pct)) > HURDLE_PCT:
                print(f"  ** block {block} {a}/{b} ${size:,}: {pct:+.4f}% "
                      f"{buy}->{sell} CLEARS HURDLE", flush=True)
    fh.flush()
    return n


def refresh_venues(w3):
    out = {}
    for pair in PAIRS:
        a, b = pair
        try:
            out[pair] = R.build_venues(w3, a, b,
                                       min_depth_usd=MIN_DEPTH_USD, px=PX)
        except Exception as exc:
            print(f"venue build {a}/{b}: {str(exc)[:70]}", flush=True)
            out[pair] = []
    live = {f"{a}/{b}": len(v) for (a, b), v in out.items()}
    print(f"venues: {live}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None,
                    help="stop after this many hours (default: run until Ctrl-C)")
    ap.add_argument("--report", action="store_true",
                    help="summarize the existing log and exit")
    args = ap.parse_args()

    if args.report:
        report()
        return

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    w3 = R.w3conn()
    print(f"logging to {LOG_PATH}")
    print(f"hurdle {HURDLE_PCT}%  pairs={len(PAIRS)}  sizes={SIZES_USD}",
          flush=True)

    venues = refresh_venues(w3)
    venues_at = time.monotonic()
    deadline = time.monotonic() + args.hours * 3600 if args.hours else None

    last_block = 0
    obs = 0
    errors = 0
    with LOG_PATH.open("a") as fh:
        while not _stop:
            if deadline and time.monotonic() > deadline:
                print("reached --hours limit", flush=True)
                break
            cycle_start = time.monotonic()
            try:
                block = w3.eth.block_number
            except Exception as exc:
                errors += 1
                print(f"rpc: {str(exc)[:70]}", flush=True)
                time.sleep(5)
                continue

            last_block = block

            if time.monotonic() - venues_at > VENUE_TTL_SEC:
                venues = refresh_venues(w3)
                venues_at = time.monotonic()

            try:
                obs += sample(w3, venues, block, fh)
            except Exception as exc:
                errors += 1
                print(f"sample block {block}: {str(exc)[:70]}", flush=True)

            if obs and obs % 200 < len(PAIRS) * len(SIZES_USD):
                print(f"[{utcnow():%H:%M:%S}] block {block} "
                      f"obs={obs:,} errors={errors}", flush=True)

            # pace to the configured cadence; a slow sample just means the next
            # one starts immediately rather than queueing up backlog
            slept = time.monotonic() - cycle_start
            if slept < SAMPLE_INTERVAL_SEC:
                time.sleep(SAMPLE_INTERVAL_SEC - slept)

    print(f"\ndone: {obs:,} observations, {errors} errors", flush=True)
    report()


if __name__ == "__main__":
    main()
