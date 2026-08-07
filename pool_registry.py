"""Pool discovery and depth measurement across every Polygon venue.

research.py hardcoded five pairs. This discovers the full pool graph instead:
every token pair, every venue, every fee tier, with measured on-chain depth.

The whole thing is built on Multicall3 batching. Naive discovery over N tokens
costs N(N-1)/2 pairs x (2 V3 factories x 4 fee tiers + Algebra + 2 V2) calls,
which is tens of thousands of RPC round trips and hours of wall clock. Batched
several hundred per call, it is a couple of minutes.

Output is a pool registry cached to data/pools.json so downstream scanning does
not re-discover on every run.
"""

import json
import time
from decimal import Decimal
from itertools import combinations
from pathlib import Path

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from web3 import Web3

import config

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
REGISTRY_PATH = Path(__file__).parent / "data" / "pools.json"

# Token universe. Deliberately wider than the pairs we expect to trade: a pool
# only earns a place in the scan by measuring deep, not by being on this list.
TOKENS = {
    "USDC":    ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
    "USDC.e":  ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 6),
    "USDT":    ("0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6),
    "DAI":     ("0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063", 18),
    "FRAX":    ("0x45c32fA6DF82ead1e2EF74d17b76547EDdFaFF89", 18),
    "MAI":     ("0xa3Fa99A148fA48D14Ed51d610c367C61876997F1", 18),
    "WETH":    ("0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", 18),
    "WBTC":    ("0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6", 8),
    "WPOL":    ("0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", 18),
    "stMATIC": ("0x3A58a54C066FdC0f2D55FC9C89F0415C92eBf3C4", 18),
    "MaticX":  ("0xfa68FB4628DFF1028CFEc22b4162FCcd0d45efb6", 18),
    "wstETH":  ("0x03b54A6e9a984069379fae1a4fC4dBAE93B3bCCD", 18),
    "LINK":    ("0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39", 18),
    "AAVE":    ("0xD6DF932A45C0f255f85145f286eA0b292B21C90B", 18),
    "UNI":     ("0xb33EaAd8d922B1083446DC23f610c2567fB5180f", 18),
    "CRV":     ("0x172370d5Cd63279eFa6d502DAB29171933a610AF", 18),
    "BAL":     ("0x9a71012B13CA4d3D0Cdc72A177DF3ef03b0E76A3", 18),
    "SUSHI":   ("0x0b3F868E0BE5597D5DB7fEB59E1CADBb0fdDa50a", 18),
    "GHST":    ("0x385Eeac5cB85A38A9a07A70c73e0a3271CfB54A7", 18),
    "SAND":    ("0xBbba073C31bF03b8ACf7c28EF0738DeCF3695683", 18),
}

# Rough USD marks, used only to convert raw balances into a depth ranking.
# Live prices come from the scanner's CoinGecko feed at trade time.
PX = {
    "USDC": 1, "USDC.e": 1, "USDT": 1, "DAI": 1, "FRAX": 1, "MAI": 1,
    "WETH": 1913, "WBTC": 63000, "WPOL": 0.075, "stMATIC": 0.082,
    "MaticX": 0.085, "wstETH": 2310, "LINK": 11, "AAVE": 140, "UNI": 4.4,
    "CRV": 0.28, "BAL": 0.95, "SUSHI": 0.35, "GHST": 0.35, "SAND": 0.16,
}

V3_FACTORIES = {
    "uniV3":   "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    "sushiV3": "0x917933899c6a5F8E37F31E19f92CdBFF7e8FF0e2",
}
ALGEBRA_FACTORY = "0x411b0fAcC3489691f28ad58c47006AF5E3Ab3A28"
V2_FACTORIES = {
    "quickV2": "0x5757371414417b8C6CAad45bAeF941aBc7d3Ab32",
    "sushiV2": "0xc35DADB65012eC5796536bD9864eD8773aBc74C4",
}
FEE_TIERS = [100, 500, 3000, 10000]

SEL_GET_POOL = Web3.keccak(text="getPool(address,address,uint24)")[:4]
SEL_POOL_BY_PAIR = Web3.keccak(text="poolByPair(address,address)")[:4]
SEL_GET_PAIR = Web3.keccak(text="getPair(address,address)")[:4]
SEL_BALANCE_OF = Web3.keccak(text="balanceOf(address)")[:4]

ABI_MULTICALL3 = [{
    "name": "aggregate3", "type": "function", "stateMutability": "payable",
    "inputs": [{"components": [
        {"name": "target", "type": "address"},
        {"name": "allowFailure", "type": "bool"},
        {"name": "callData", "type": "bytes"}], "name": "calls", "type": "tuple[]"}],
    "outputs": [{"components": [
        {"name": "success", "type": "bool"},
        {"name": "returnData", "type": "bytes"}], "name": "returnData", "type": "tuple[]"}],
}]


def ck(a):
    return Web3.to_checksum_address(a)


def multicall(w3, calls, batch_size=400, retries=3):
    """
    Run (target, calldata) pairs through Multicall3.

    Returns a list of raw return bytes (b"" where the sub-call failed).
    allowFailure is set per call: a nonexistent pool reverting must not take
    the whole batch down with it.
    """
    mc = w3.eth.contract(address=ck(MULTICALL3), abi=ABI_MULTICALL3)
    out = []
    for i in range(0, len(calls), batch_size):
        chunk = [(ck(t), True, d) for t, d in calls[i:i + batch_size]]
        for attempt in range(retries):
            try:
                res = mc.functions.aggregate3(chunk).call()
                out.extend(r[1] if r[0] else b"" for r in res)
                break
            except Exception:
                if attempt == retries - 1:
                    out.extend(b"" for _ in chunk)
                else:
                    time.sleep(1.5 * (attempt + 1))
    return out


def _addr_from(raw):
    if not raw or len(raw) < 32:
        return None
    a = "0x" + raw[-20:].hex()
    return None if int(a, 16) == 0 else ck(a)


def discover_pools(w3, symbols=None, verbose=True):
    """Enumerate every pool across all venues for all token pairs."""
    syms = sorted(symbols or TOKENS.keys())
    pairs = list(combinations(syms, 2))

    calls, meta = [], []
    for a, b in pairs:
        addr_a, addr_b = TOKENS[a][0], TOKENS[b][0]
        for vname, fac in V3_FACTORIES.items():
            for fee in FEE_TIERS:
                calls.append((fac, SEL_GET_POOL + abi_encode(
                    ["address", "address", "uint24"], [ck(addr_a), ck(addr_b), fee])))
                meta.append((a, b, vname, f"{fee/10000:.2f}%", fee))
        calls.append((ALGEBRA_FACTORY, SEL_POOL_BY_PAIR + abi_encode(
            ["address", "address"], [ck(addr_a), ck(addr_b)])))
        meta.append((a, b, "quickV3", "dyn", 0))
        for vname, fac in V2_FACTORIES.items():
            calls.append((fac, SEL_GET_PAIR + abi_encode(
                ["address", "address"], [ck(addr_a), ck(addr_b)])))
            meta.append((a, b, vname, "0.30%", 3000))

    if verbose:
        print(f"discovering: {len(pairs)} pairs -> {len(calls):,} factory calls", flush=True)
    results = multicall(w3, calls)

    found = []
    for (a, b, venue, label, fee), raw in zip(meta, results):
        addr = _addr_from(raw)
        if addr:
            found.append({"tokenA": a, "tokenB": b, "venue": venue,
                          "feeLabel": label, "fee": fee, "pool": addr})
    if verbose:
        print(f"  {len(found):,} pools exist", flush=True)
    return found


def measure_depth(w3, pools, verbose=True):
    """Attach measured token balances and a USD depth figure to each pool."""
    calls = []
    for p in pools:
        pa = TOKENS[p["tokenA"]][0]
        pb = TOKENS[p["tokenB"]][0]
        arg = abi_encode(["address"], [ck(p["pool"])])
        calls.append((pa, SEL_BALANCE_OF + arg))
        calls.append((pb, SEL_BALANCE_OF + arg))

    if verbose:
        print(f"measuring depth: {len(calls):,} balance calls", flush=True)
    res = multicall(w3, calls)

    out = []
    for i, p in enumerate(pools):
        ra, rb = res[2 * i], res[2 * i + 1]
        if not ra or not rb:
            continue
        try:
            bal_a = abi_decode(["uint256"], ra)[0]
            bal_b = abi_decode(["uint256"], rb)[0]
        except Exception:
            continue
        da, db = TOKENS[p["tokenA"]][1], TOKENS[p["tokenB"]][1]
        amt_a = Decimal(bal_a) / Decimal(10) ** da
        amt_b = Decimal(bal_b) / Decimal(10) ** db
        usd_a = amt_a * Decimal(str(PX.get(p["tokenA"], 0)))
        usd_b = amt_b * Decimal(str(PX.get(p["tokenB"], 0)))
        q = dict(p)
        q["amtA"] = float(amt_a)
        q["amtB"] = float(amt_b)
        # Depth is the smaller side: that is what caps a round trip through it.
        q["depthUsd"] = float(min(usd_a, usd_b))
        out.append(q)
    return out


def build_registry(w3, min_depth_usd=5000, verbose=True):
    pools = discover_pools(w3, verbose=verbose)
    pools = measure_depth(w3, pools, verbose=verbose)
    live = [p for p in pools if p["depthUsd"] >= min_depth_usd]
    live.sort(key=lambda p: -p["depthUsd"])
    if verbose:
        print(f"  {len(live):,} pools with >= ${min_depth_usd:,} depth", flush=True)
    reg = {
        "block": w3.eth.block_number,
        "minDepthUsd": min_depth_usd,
        "pools": live,
    }
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(reg, indent=1))
    return reg


def load_registry():
    if not REGISTRY_PATH.exists():
        return None
    return json.loads(REGISTRY_PATH.read_text())


def w3conn():
    from web3.middleware import ExtraDataToPOAMiddleware
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 60}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


if __name__ == "__main__":
    w3 = w3conn()
    t0 = time.time()
    reg = build_registry(w3)
    print(f"\nregistry written to {REGISTRY_PATH} in {time.time()-t0:.1f}s")
    print(f"\n{'pool':<34} {'venue':<9} {'fee':<6} {'depth':>14}")
    for p in reg["pools"][:30]:
        pair = f"{p['tokenA']}/{p['tokenB']}"
        print(f"{pair:<34} {p['venue']:<9} {p['feeLabel']:<6} ${p['depthUsd']:>13,.0f}")
