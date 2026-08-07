"""
Venue and pair research for Polygon flash-loan arbitrage.

Read-only. Enumerates real on-chain pools across V2 and V3 venues, measures
depth, and quotes round trips at multiple sizes so pair selection is driven by
measurement rather than by which addresses happened to be in config.py.

Run:  python research.py
"""

import json
import time
from decimal import Decimal, getcontext

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

import config

getcontext().prec = 40

# --- Tokens (Polygon mainnet) ---
T = {
    "USDC.e":  ("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", 6),
    "USDC":    ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
    "USDT":    ("0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6),
    "DAI":     ("0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063", 18),
    "WETH":    ("0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", 18),
    "WPOL":    ("0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", 18),
    "WBTC":    ("0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6", 8),
    "LINK":    ("0x53E0bca35eC356BD5ddDFebbD1Fc0fD03FaBad39", 18),
    "AAVE":    ("0xD6DF932A45C0f255f85145f286eA0b292B21C90B", 18),
    "stMATIC": ("0x3A58a54C066FdC0f2D55FC9C89F0415C92eBf3C4", 18),
    "MaticX":  ("0xfa68FB4628DFF1028CFEc22b4162FCcd0d45efb6", 18),
}

# --- Venues ---
V3_FACTORIES = {
    "uniV3":   "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    "sushiV3": "0x917933899c6a5F8E37F31E19f92CdBFF7e8FF0e2",
}
UNI_QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"

# QuickSwap V3 is Algebra-based: single pool per pair, dynamic fee, no fee tier.
ALGEBRA_FACTORY = "0x411b0fAcC3489691f28ad58c47006AF5E3Ab3A28"
ALGEBRA_QUOTER = "0xa15F0D7377B2A0C0c10db057f641beD21028FC89"

V2_ROUTERS = {
    "quickV2": "0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff",
    "sushiV2": "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
}

FEE_TIERS = [100, 500, 3000, 10000]  # 0.01% / 0.05% / 0.30% / 1.00%

# --- Minimal ABIs ---
ABI_V3_FACTORY = [{"name": "getPool", "type": "function", "stateMutability": "view",
    "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}],
    "outputs": [{"type": "address"}]}]
ABI_ALGEBRA_FACTORY = [{"name": "poolByPair", "type": "function", "stateMutability": "view",
    "inputs": [{"type": "address"}, {"type": "address"}],
    "outputs": [{"type": "address"}]}]
ABI_ERC20 = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
    "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]}]
ABI_QUOTER_V2 = [{"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
    "inputs": [{"components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}], "name": "params", "type": "tuple"}],
    "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "sqrtPriceX96After", "type": "uint160"},
        {"name": "initializedTicksCrossed", "type": "uint32"}, {"name": "gasEstimate", "type": "uint256"}]}]
ABI_ALGEBRA_QUOTER = [{"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
    "inputs": [{"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "limitSqrtPrice", "type": "uint160"}],
    "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "fee", "type": "uint16"}]}]
ABI_V2_ROUTER = [{"name": "getAmountsOut", "type": "function", "stateMutability": "view",
    "inputs": [{"type": "uint256"}, {"type": "address[]"}],
    "outputs": [{"type": "uint256[]"}]}]


def w3conn():
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 30}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def ck(a):
    return Web3.to_checksum_address(a)


def find_v3_pools(w3, sym_a, sym_b):
    """Return [(venue, fee_bps_label, pool_addr, tvl_usd_side_a, tvl_side_b)]."""
    a, da = T[sym_a]
    b, db = T[sym_b]
    found = []
    for vname, faddr in V3_FACTORIES.items():
        f = w3.eth.contract(address=ck(faddr), abi=ABI_V3_FACTORY)
        for fee in FEE_TIERS:
            try:
                pool = f.functions.getPool(ck(a), ck(b), fee).call()
            except Exception:
                continue
            if int(pool, 16) == 0:
                continue
            bal_a = w3.eth.contract(address=ck(a), abi=ABI_ERC20).functions.balanceOf(pool).call()
            bal_b = w3.eth.contract(address=ck(b), abi=ABI_ERC20).functions.balanceOf(pool).call()
            found.append((vname, f"{fee/10000:.2f}%", pool,
                          Decimal(bal_a) / 10**da, Decimal(bal_b) / 10**db))
    # Algebra (QuickSwap V3)
    try:
        af = w3.eth.contract(address=ck(ALGEBRA_FACTORY), abi=ABI_ALGEBRA_FACTORY)
        pool = af.functions.poolByPair(ck(a), ck(b)).call()
        if int(pool, 16) != 0:
            bal_a = w3.eth.contract(address=ck(a), abi=ABI_ERC20).functions.balanceOf(pool).call()
            bal_b = w3.eth.contract(address=ck(b), abi=ABI_ERC20).functions.balanceOf(pool).call()
            found.append(("quickV3", "dyn", pool,
                          Decimal(bal_a) / 10**da, Decimal(bal_b) / 10**db))
    except Exception:
        pass
    return found


def quote_uni_v3(w3, sym_in, sym_out, amt_in, fee):
    a, da = T[sym_in]
    b, db = T[sym_out]
    q = w3.eth.contract(address=ck(UNI_QUOTER_V2), abi=ABI_QUOTER_V2)
    res = q.functions.quoteExactInputSingle(
        (ck(a), ck(b), int(amt_in * 10**da), fee, 0)).call()
    return Decimal(res[0]) / 10**db


def quote_algebra(w3, sym_in, sym_out, amt_in):
    a, da = T[sym_in]
    b, db = T[sym_out]
    q = w3.eth.contract(address=ck(ALGEBRA_QUOTER), abi=ABI_ALGEBRA_QUOTER)
    res = q.functions.quoteExactInputSingle(ck(a), ck(b), int(amt_in * 10**da), 0).call()
    return Decimal(res[0]) / 10**db


def quote_v2(w3, router, sym_in, sym_out, amt_in):
    a, da = T[sym_in]
    b, db = T[sym_out]
    r = w3.eth.contract(address=ck(router), abi=ABI_V2_ROUTER)
    out = r.functions.getAmountsOut(int(amt_in * 10**da), [ck(a), ck(b)]).call()
    return Decimal(out[-1]) / 10**db


SUSHI_QUOTER_V3 = "0xb1E835Dc2785b52265711e17fCCb0fd018226a6e"


# Router/quoter dispatch by venue name, so callers (pool_registry-driven scans,
# the logger, ad-hoc checks) all price through one code path instead of each
# re-deriving which quoter a venue needs.
def quote(w3, venue, sym_in, sym_out, amt_in, fee=None, tokens=None):
    """
    Exact quote for one hop. Returns Decimal output, or None if the venue
    cannot fill it.

    `tokens` overrides the module-level T map so the wider pool_registry
    universe can be priced without duplicating the token table.
    """
    tok = tokens if tokens is not None else T
    prev = None
    if tokens is not None:
        prev, globals()["T"] = T, tokens
    try:
        if venue == "uniV3":
            return quote_uni_v3(w3, sym_in, sym_out, amt_in, int(fee))
        if venue == "sushiV3":
            return quote_sushi_v3(w3, sym_in, sym_out, amt_in, int(fee))
        if venue == "quickV3":
            return quote_algebra(w3, sym_in, sym_out, amt_in)
        if venue in V2_ROUTERS:
            return quote_v2(w3, V2_ROUTERS[venue], sym_in, sym_out, amt_in)
        return None
    except Exception:
        return None
    finally:
        if prev is not None:
            globals()["T"] = prev


def quote_sushi_v3(w3, sym_in, sym_out, amt_in, fee):
    a, da = T[sym_in]
    b, db = T[sym_out]
    q = w3.eth.contract(address=ck(SUSHI_QUOTER_V3), abi=ABI_QUOTER_V2)
    res = q.functions.quoteExactInputSingle(
        (ck(a), ck(b), int(amt_in * 10**da), fee, 0)).call()
    return Decimal(res[0]) / 10**db


def build_venues(w3, sym_a, sym_b, min_depth_usd=10000, px=None):
    """
    Discover every venue that can trade sym_a/sym_b with real depth, and return
    a list of (label, quote_fn) where quote_fn(sym_in, sym_out, amt) -> Decimal.

    Venues are validated by a live probe quote, so a registered-but-dust pool
    (e.g. sushiV3 0.05% on USDC.e/WETH) is dropped rather than trusted.
    """
    px = px or {}
    venues = []

    for venue, fee_label, pool, ba, bb in find_v3_pools(w3, sym_a, sym_b):
        usd_a = ba * Decimal(str(px.get(sym_a, 1)))
        usd_b = bb * Decimal(str(px.get(sym_b, 1)))
        if min(usd_a, usd_b) < min_depth_usd:
            continue
        if venue == "quickV3":
            venues.append((f"quickV3", lambda i, o, m: quote_algebra(w3, i, o, m)))
        elif venue == "uniV3":
            fee = int(round(float(fee_label.rstrip("%")) * 10000))
            venues.append((f"uniV3:{fee_label}",
                           lambda i, o, m, f=fee: quote_uni_v3(w3, i, o, m, f)))
        elif venue == "sushiV3":
            fee = int(round(float(fee_label.rstrip("%")) * 10000))
            venues.append((f"sushiV3:{fee_label}",
                           lambda i, o, m, f=fee: quote_sushi_v3(w3, i, o, m, f)))

    for vn, ra in V2_ROUTERS.items():
        venues.append((vn, lambda i, o, m, r=ra: quote_v2(w3, r, i, o, m)))

    # probe: drop venues that can't actually fill a small clip
    live = []
    probe = Decimal("100") / Decimal(str(px.get(sym_a, 1)))
    for label, fn in venues:
        try:
            out = fn(sym_a, sym_b, probe)
            if out > 0:
                live.append((label, fn))
        except Exception:
            pass
    return live


def round_trip(venues, sym_a, sym_b, amt_a):
    """Best (pct, buy_label, sell_label) over all ordered venue pairs."""
    best = None
    mids = {}
    for lb, fb in venues:
        try:
            mids[lb] = fb(sym_a, sym_b, amt_a)
        except Exception:
            pass
    for lb, mid in mids.items():
        if mid <= 0:
            continue
        for ls, fs in venues:
            if ls == lb:
                continue
            try:
                back = fs(sym_b, sym_a, mid)
            except Exception:
                continue
            pct = (back - amt_a) / amt_a * 100
            if best is None or pct > best[0]:
                best = (pct, lb, ls)
    return best
