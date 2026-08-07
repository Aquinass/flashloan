# Polygon Flash-Loan Arbitrage Scanner

Pool discovery, depth measurement, and cycle search across every major Polygon
DEX venue — plus a static dashboard published on GitHub Pages.

**Result so far: no profitable arbitrage.** 4,166 cycles across 99 pools, zero
confirmed. Nothing has ever been traded; realised P&L is $0.00. The interesting
output of this repo is the measurement, not a return.

## What it measures

| Question | Answer, measured on-chain |
|---|---|
| Where is the liquidity? | 829 pools exist; **99** hold ≥ $5k. Deepest is quickV2 USDC.e/WETH at **$994k** |
| What does a flash loan cost? | Aave V3 **0.05%**; Balancer V2 **0%** — verified by asserting `owed == principal` |
| What does a round trip cost? | **0.0041%** on 0.01%-tier stables; **0.60%** on V2's 0.30% pools |
| What does gas cost? | **$0.0067** per arb at 277 gwei — 0.0001% of a $5k trade |
| So what must a spread beat? | **0.0042%** (Balancer) vs **0.0542%** (Aave) |
| How big can you go? | ~$1k. At $25k cost is 0.08%; at $500k the route falls back to a drained V2 pool and loses **61%** |

The binding constraint is **pool depth**, not the flash loan and not gas.

## Why two stages

Mid prices are cheap (one batched read for every pool) but ignore price impact.
Exact quotes price impact correctly but cost an RPC round trip each.

So the scanner screens on mid prices, then **re-quotes every hit through the real
quoters before calling it profitable**. This matters: a live scan produced a
`WETH → LINK → WETH` cycle at **+0.0055%** at mid that came back **−1.47%** when
quoted exactly. Without the confirm stage the dashboard would have published
"profitable" as its headline.

## Layout

| File | Role |
|---|---|
| `pool_registry.py` | Discovers all pools via Multicall3, measures depth → `data/pools.json` |
| `pool_state.py` | Batched reserves/`slot0` reads → post-fee marginal prices |
| `cycle_finder.py` | Enumerates 2- and 3-hop cycles over the pool graph |
| `research.py` | Per-venue quoting; `quote()` is the single dispatcher |
| `spread_logger.py` | Samples the best round trip every 20s → `data/spreads.jsonl` |
| `export_site_data.py` | Screen + confirm, writes `docs/data/summary.json` |
| `src/ArbEngine.sol` | Balancer zero-fee flash loan, 4 venue adapters, N-hop routes |
| `src/FlashArb.sol` | Original Aave + UniswapV2 contract (superseded) |
| `docs/` | The static dashboard |

## Running it

```bash
python -m venv venv && venv/Scripts/activate   # or source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                            # add your RPC_URL

python pool_registry.py        # discover pools        (~12s)
python export_site_data.py     # scan + write snapshot (~6s)
python spread_logger.py --hours 24   # optional: frequency data
python -m http.server 8000 --directory docs
```

Contracts:

```bash
forge build
FORK_RPC_URL=<archive-rpc> forge test    # 17 fork tests against live state
```

## The dashboard

`docs/` is served by GitHub Pages. Pages runs no server code, so the site reads a
committed JSON snapshot; `.github/workflows/refresh.yml` regenerates it hourly.

To enable: **Settings → Pages → Source: `main` / `docs`**, then add an `RPC_URL`
repo secret under **Settings → Secrets and variables → Actions**. The workflow
fails loudly if that secret is missing rather than publishing stale data.

## Safety

`DRY_RUN=true` is the default and no transaction has ever been broadcast.
`.env` is gitignored and was never committed — verify with
`git log --all -- .env` (empty).

`ArbEngine.sol` is **unaudited**. It holds no funds between transactions and its
`minProfit` floor reverts unprofitable trades so a bad route costs only gas, but
it moves borrowed capital and should be reviewed before running live.

## Honest limits

- One chain, one snapshot per run. A saturated market can still dislocate during
  volatility; the 24h logger exists to measure that and hasn't finished.
- The SushiSwap V3 **router** is not deployed at either published address on
  Polygon, so that venue is quotable but not yet tradeable.
- Depth caps size near $1k, where a 3bps dislocation nets about **$0.14**.
  Whether that is worth trading is a separate question from whether it exists.
