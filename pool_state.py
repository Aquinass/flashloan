"""Batched mid-price reads for every pool in the registry.

Exact quoter calls cost one RPC round trip each and are far too slow to sweep
the whole pool graph every block. This reads raw pool state instead — reserves
for V2, slot0 for V3/Algebra — for all pools in a single Multicall3 batch, and
derives mid prices from it.

Mid prices ignore price impact, so they are a screen, not a quote: they say
"these two venues disagree enough to be worth an exact quote", and nothing more.
The exact quote still decides whether a trade is real.
"""

from decimal import Decimal, getcontext

from eth_abi import decode as abi_decode
from web3 import Web3

from pool_registry import TOKENS, multicall

getcontext().prec = 50

SEL_GET_RESERVES = Web3.keccak(text="getReserves()")[:4]
SEL_SLOT0 = Web3.keccak(text="slot0()")[:4]
SEL_GLOBAL_STATE = Web3.keccak(text="globalState()")[:4]
SEL_TOKEN0 = Web3.keccak(text="token0()")[:4]

Q96 = Decimal(2) ** 96

V2_VENUES = {"quickV2", "sushiV2"}
ALGEBRA_VENUES = {"quickV3"}

# Effective fee per swap, as a fraction. Algebra pools are dynamic-fee; 0.01%
# is its typical stable-pair floor and is refined by the exact quote anyway.
FEE_FRACTION = {
    100: Decimal("0.0001"),
    500: Decimal("0.0005"),
    3000: Decimal("0.003"),
    10000: Decimal("0.01"),
}
ALGEBRA_ASSUMED_FEE = Decimal("0.0001")


def state_calls(pools):
    """Build (target, calldata) pairs: token0 ordering + price state per pool."""
    calls = []
    for p in pools:
        calls.append((p["pool"], SEL_TOKEN0))
        if p["venue"] in V2_VENUES:
            calls.append((p["pool"], SEL_GET_RESERVES))
        elif p["venue"] in ALGEBRA_VENUES:
            calls.append((p["pool"], SEL_GLOBAL_STATE))
        else:
            calls.append((p["pool"], SEL_SLOT0))
    return calls


def _decode_first_uint(raw, kinds):
    """Algebra/Uniswap state structs differ; try candidate layouts."""
    for k in kinds:
        try:
            return abi_decode(k, raw)
        except Exception:
            continue
    return None


def read_states(w3, pools):
    """
    Return {pool_index: {"price": Decimal, "fee": Decimal}}.

    price is tokenB per tokenA in human units, derived from raw pool state.
    Pools whose state cannot be decoded are dropped rather than guessed at.
    """
    res = multicall(w3, state_calls(pools))
    out = {}
    for i, p in enumerate(pools):
        raw_t0, raw_state = res[2 * i], res[2 * i + 1]
        if not raw_t0 or not raw_state:
            continue
        try:
            token0 = "0x" + raw_t0[-20:].hex()
        except Exception:
            continue

        a_addr = TOKENS[p["tokenA"]][0].lower()
        b_addr = TOKENS[p["tokenB"]][0].lower()
        dec_a, dec_b = TOKENS[p["tokenA"]][1], TOKENS[p["tokenB"]][1]
        a_is_0 = token0.lower() == a_addr
        if not a_is_0 and token0.lower() != b_addr:
            continue

        fee = FEE_FRACTION.get(p["fee"], Decimal("0.003"))

        if p["venue"] in V2_VENUES:
            d = _decode_first_uint(raw_state, [["uint112", "uint112", "uint32"]])
            if not d or d[0] == 0 or d[1] == 0:
                continue
            r0, r1 = Decimal(d[0]), Decimal(d[1])
            # price = out per in, scaled to human decimals
            if a_is_0:
                price = (r1 / r0) * (Decimal(10) ** dec_a) / (Decimal(10) ** dec_b)
            else:
                price = (r0 / r1) * (Decimal(10) ** dec_a) / (Decimal(10) ** dec_b)
            fee = Decimal("0.003")
        else:
            if p["venue"] in ALGEBRA_VENUES:
                d = _decode_first_uint(raw_state, [
                    ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                    ["uint160", "int24", "uint16", "uint16", "uint16", "uint16", "bool"],
                    ["uint160", "int24", "uint16", "uint16", "uint8", "uint8", "bool"],
                ])
                fee = ALGEBRA_ASSUMED_FEE
            else:
                d = _decode_first_uint(raw_state, [
                    ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                ])
            if not d or not d[0]:
                continue
            sqrt_p = Decimal(d[0]) / Q96
            # (sqrtP)^2 = token1/token0 in raw units
            p10 = sqrt_p * sqrt_p
            if a_is_0:
                price = p10 * (Decimal(10) ** dec_a) / (Decimal(10) ** dec_b)
            else:
                if p10 == 0:
                    continue
                price = (1 / p10) * (Decimal(10) ** dec_a) / (Decimal(10) ** dec_b)

        if price <= 0:
            continue
        out[i] = {"price": price, "fee": fee}
    return out


def build_edges(pools, states):
    """
    Directed edge list for cycle search.

    Each edge carries the post-fee multiplier: how much tokenB you get per
    tokenA at the margin. A cycle is profitable iff the product of its edge
    multipliers exceeds 1.
    """
    edges = []
    for i, p in enumerate(pools):
        st = states.get(i)
        if not st:
            continue
        price, fee = st["price"], st["fee"]
        keep = Decimal(1) - fee
        label = f"{p['venue']}:{p['feeLabel']}"
        edges.append({
            "src": p["tokenA"], "dst": p["tokenB"],
            "mult": price * keep, "venue": label,
            "pool": p["pool"], "depthUsd": p["depthUsd"],
            "fee": fee,
        })
        edges.append({
            "src": p["tokenB"], "dst": p["tokenA"],
            "mult": (Decimal(1) / price) * keep, "venue": label,
            "pool": p["pool"], "depthUsd": p["depthUsd"],
            "fee": fee,
        })
    return edges
