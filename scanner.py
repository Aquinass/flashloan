"""
Flash-loan arbitrage scanner/executor.

Loop:
  1. For each configured pair, quote a round trip (asset -> intermediate on
     router A, intermediate -> asset on router B) on both router orderings.
  2. Estimate gas cost + Aave flash loan premium (0.05%).
  3. If net profit clears MIN_PROFIT_USD, call FlashArb.executeArb().

Run with DRY_RUN=True (default) first. It will print every opportunity it
finds and what it *would* have done, without sending any transaction.
"""

import os
import sys
import time
import logging
from decimal import Decimal

import requests
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

import config
import private_relay
from abis import ROUTER_ABI, ERC20_ABI, FLASH_ARB_ABI

def _configure_logging() -> logging.Logger:
    """
    INFO to stdout, level overridable via LOG_LEVEL.

    Forces UTF-8 on the stream: the default Windows console codepage is cp1252,
    which mangles non-ASCII in log messages into replacement characters.
    """
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # non-reconfigurable stream (pipe/pytest capture); not fatal

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.basicConfig(level=logging.DEBUG, handlers=[handler], force=True)

    # LOG_LEVEL controls this bot's own output. web3/urllib3 log every JSON-RPC
    # request at DEBUG, which buries the scan results, so pin them to WARNING.
    for noisy in ("web3", "urllib3", "requests", "web3.providers", "web3.manager"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    app_log = logging.getLogger("flash-arb")
    app_log.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
    return app_log


log = _configure_logging()

AAVE_FLASHLOAN_PREMIUM_BPS = 5  # 0.05% as of Aave V3 default

# Price cache, one entry per CoinGecko id. Keying by id rather than by symbol
# means USDC and USDC.e share a slot (same underlying peg, one request) while
# genuinely different assets never contaminate each other's price.
#   price        — last successfully fetched price, None until one succeeds
#   fetched_at   — monotonic time of that success (for staleness reporting)
#   last_attempt — monotonic time of the last attempt, success or not; gates
#                  both cache freshness and retry backoff
_price_cache: dict[str, dict] = {}


def _fetch_price_usd(cg_id: str, label: str, fallback: str | None) -> Decimal | None:
    """
    USD price for one CoinGecko id, cached for PRICE_CACHE_TTL_SEC.

    Never raises. On failure the last good price is reused; a stale price is a
    bad estimate, but a crashed scanner misses every opportunity. With nothing
    cached yet, `fallback` applies if given and None is returned otherwise —
    letting the caller skip an asset rather than trade on an invented number.
    """
    now = time.monotonic()
    entry = _price_cache.setdefault(
        cg_id, {"price": None, "fetched_at": 0.0, "last_attempt": 0.0}
    )
    cached = entry["price"]

    # Inside the TTL: serve the cache. This also throttles retries after a
    # failure, so an API outage costs one request per minute, not one per scan.
    if now - entry["last_attempt"] < config.PRICE_CACHE_TTL_SEC:
        if cached is not None:
            return cached
        return Decimal(fallback) if fallback is not None else None

    entry["last_attempt"] = now

    try:
        resp = requests.get(
            config.PRICE_FEED_URL,
            params={"ids": cg_id, "vs_currencies": "usd"},
            timeout=config.PRICE_FEED_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        raw = resp.json()[cg_id]["usd"]
        # Via str: float -> Decimal directly would bake in binary rounding error.
        price = Decimal(str(raw))
        if price <= 0:
            raise ValueError(f"feed returned non-positive price: {raw!r}")
    except Exception as e:
        if cached is not None:
            age = int(now - entry["fetched_at"])
            log.warning(
                f"{label} price fetch failed ({e}); "
                f"reusing cached ${cached} from {age}s ago"
            )
            return cached
        if fallback is not None:
            log.warning(
                f"{label} price fetch failed ({e}) and no cached price exists yet - "
                f"falling back to ${fallback}. Estimates are unreliable until the "
                f"feed recovers."
            )
            return Decimal(fallback)
        log.warning(
            f"{label} price fetch failed ({e}), no cached price and no fallback; "
            f"pairs based on {label} will be skipped this cycle."
        )
        return None

    previous = cached
    entry["price"] = price
    entry["fetched_at"] = now

    if previous is None:
        log.info(f"{label} price: ${price} (via CoinGecko)")
    elif price != previous:
        delta_pct = (price - previous) / previous * 100
        log.info(f"{label} price updated: ${previous} -> ${price} ({delta_pct:+.2f}%)")
    else:
        log.info(f"{label} price refreshed: ${price} (unchanged)")

    return price


def get_gas_token_price_usd() -> Decimal:
    """USD price of the gas token (POL on Polygon). Always returns a number."""
    return _fetch_price_usd(
        config.GAS_TOKEN_COINGECKO_ID,
        config.GAS_TOKEN_SYMBOL,
        config.GAS_TOKEN_PRICE_FALLBACK_USD,
    )


def get_token_price_usd(symbol: str) -> Decimal | None:
    """
    USD price for a token symbol, or None if it cannot be priced.

    None means "do not trade this asset" — either no CoinGecko id is mapped or
    the feed is down with no cached price and no safe fallback.
    """
    cg_id = config.TOKEN_COINGECKO_IDS.get(symbol)
    if cg_id is None:
        log.warning(f"No CoinGecko id mapped for {symbol}; cannot value it in USD")
        return None
    return _fetch_price_usd(
        cg_id, symbol, config.TOKEN_PRICE_FALLBACK_USD.get(symbol)
    )


def get_web3() -> Web3:
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)  # Polygon is PoA-style
    if not w3.is_connected():
        raise ConnectionError(f"Could not connect to RPC at {config.RPC_URL}")
    return w3


def get_quote(w3: Web3, router_addr: str, amount_in: int, path: list) -> int:
    router = w3.eth.contract(address=Web3.to_checksum_address(router_addr), abi=ROUTER_ABI)
    amounts = router.functions.getAmountsOut(amount_in, path).call()
    return amounts[-1]


def estimate_gas_cost_in_asset(
    w3: Web3, asset_decimals: int, asset_price_usd: Decimal
) -> Decimal | None:
    """
    Rough gas cost for the whole flashloan+2 swaps tx, in asset units.

    Returns None when the current gas price is above MAX_GAS_GWEI, signalling
    the caller to skip rather than quoting a cost it would not act on.
    """
    gas_price_wei = w3.eth.gas_price
    gas_price_gwei = Decimal(gas_price_wei) / Decimal(10**9)
    if gas_price_gwei > config.MAX_GAS_GWEI:
        log.debug(
            f"Gas {gas_price_gwei:.1f} gwei exceeds MAX_GAS_GWEI={config.MAX_GAS_GWEI}; "
            f"skipping scan. Raise the cap to scan through high-gas periods."
        )
        return None  # signal: too expensive right now, skip
    est_gas_units = Decimal(450_000)  # flashloan + 2 swaps, rough
    gas_token_price_usd = get_gas_token_price_usd()
    cost_gas_token = (gas_price_wei * est_gas_units) / Decimal(10**18)
    cost_usd = cost_gas_token * gas_token_price_usd
    cost_in_asset = cost_usd / asset_price_usd if asset_price_usd > 0 else Decimal(0)
    return cost_in_asset


def scan_pair(w3: Web3, base_symbol: str, quote_symbol: str):
    base = config.TOKENS[base_symbol]
    quote = config.TOKENS[quote_symbol]
    borrow_amount_units = config.BORROW_AMOUNT.get(base_symbol)
    if borrow_amount_units is None:
        return None
    amount_in = int(borrow_amount_units * (10 ** base["decimals"]))

    path_out = [Web3.to_checksum_address(base["address"]), Web3.to_checksum_address(quote["address"])]
    path_back = [Web3.to_checksum_address(quote["address"]), Web3.to_checksum_address(base["address"])]

    router_names = list(config.ROUTERS.keys())
    best = None

    # Try both directions: buy on router A / sell on router B, and vice versa
    for buy_name in router_names:
        for sell_name in router_names:
            if buy_name == sell_name:
                continue
            try:
                intermediate_out = get_quote(w3, config.ROUTERS[buy_name], amount_in, path_out)
                final_out = get_quote(w3, config.ROUTERS[sell_name], intermediate_out, path_back)
            except Exception as e:
                log.debug(f"Quote failed {buy_name}->{sell_name} for {base_symbol}/{quote_symbol}: {e}")
                continue

            gross_profit_units = final_out - amount_in

            # Log every round trip, winning or losing. A losing spread is the
            # normal case and is the only way to tell "scanned, no edge" apart
            # from "never scanned" in DRY_RUN output.
            scale = Decimal(10 ** base["decimals"])
            round_trip_pct = (Decimal(gross_profit_units) / Decimal(amount_in)) * 100
            log.debug(
                f"  {base_symbol}/{quote_symbol} buy@{buy_name} sell@{sell_name}: "
                f"{Decimal(amount_in)/scale:.2f} -> {Decimal(final_out)/scale:.2f} "
                f"{base_symbol} ({round_trip_pct:+.4f}%)"
            )

            if gross_profit_units <= 0:
                continue

            gross_profit = Decimal(gross_profit_units) / Decimal(10 ** base["decimals"])

            # Aave premium
            premium = Decimal(amount_in) * Decimal(AAVE_FLASHLOAN_PREMIUM_BPS) / Decimal(10000)
            premium_units = premium / Decimal(10 ** base["decimals"])

            # Gas is paid in the gas token but netted against profit denominated
            # in the borrowed asset, so the borrowed asset needs a live USD price
            # too. Unpriceable means unprofitable-until-proven: skip, don't guess.
            asset_price_usd = get_token_price_usd(base_symbol)
            if asset_price_usd is None:
                log.info(f"No USD price for {base_symbol}, skipping this pair")
                continue

            gas_cost = estimate_gas_cost_in_asset(w3, base["decimals"], asset_price_usd)
            if gas_cost is None:
                log.info("Gas price too high right now, skipping this cycle")
                continue

            net_profit = gross_profit - premium_units - gas_cost
            # net_profit is denominated in the borrowed asset; MIN_PROFIT_USD is
            # in dollars. Convert once here so every downstream comparison and
            # log line is unambiguously USD.
            net_profit_usd = net_profit * asset_price_usd

            candidate = {
                "buy_router": buy_name,
                "sell_router": sell_name,
                "base": base_symbol,
                "quote": quote_symbol,
                "amount_in": amount_in,
                "gross_profit": gross_profit,
                "net_profit": net_profit,          # in base-asset units
                "net_profit_usd": net_profit_usd,  # comparable across assets
                "asset_price_usd": asset_price_usd,
            }

            # Compare in USD: ranking raw asset units would rate 1 unit of WETH
            # the same as 1 USDC once more than one borrowable asset is enabled.
            if best is None or net_profit_usd > best["net_profit_usd"]:
                best = candidate

    return best


def execute_trade(w3: Web3, opportunity: dict):
    base = config.TOKENS[opportunity["base"]]
    quote = config.TOKENS[opportunity["quote"]]

    if config.DRY_RUN:
        log.info(
            f"[DRY RUN] Would execute: borrow {opportunity['amount_in']/10**base['decimals']} "
            f"{opportunity['base']}, buy on {opportunity['buy_router']}, sell on "
            f"{opportunity['sell_router']}, est. net profit ${opportunity['net_profit_usd']:.2f}"
        )
        return

    if not config.PRIVATE_KEY or not config.CONTRACT_ADDRESS:
        log.error("PRIVATE_KEY / CONTRACT_ADDRESS not set — cannot execute live trade. "
                   "Set them in your .env, or leave DRY_RUN=true.")
        return

    account = w3.eth.account.from_key(config.PRIVATE_KEY)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(config.CONTRACT_ADDRESS), abi=FLASH_ARB_ABI
    )

    # On-chain profit floor, in asset units. Derived from a USD figure via the
    # live price: hardcoding 1 unit means $1 for USDC but ~$3000 for WETH.
    asset_price_usd = opportunity.get("asset_price_usd") or get_token_price_usd(
        opportunity["base"]
    )
    if asset_price_usd is None or asset_price_usd <= 0:
        log.error(f"No USD price for {opportunity['base']}; refusing to set an "
                  f"unbounded on-chain minProfit. Trade abandoned.")
        return
    min_profit_units = int(
        (Decimal(str(config.ONCHAIN_MIN_PROFIT_USD)) / asset_price_usd)
        * (10 ** base["decimals"])
    )

    tx = contract.functions.executeArb(
        Web3.to_checksum_address(base["address"]),
        opportunity["amount_in"],
        Web3.to_checksum_address(config.ROUTERS[opportunity["buy_router"]]),
        Web3.to_checksum_address(config.ROUTERS[opportunity["sell_router"]]),
        Web3.to_checksum_address(quote["address"]),
        min_profit_units,
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 600_000,
        "gasPrice": min(w3.eth.gas_price, w3.to_wei(config.MAX_GAS_GWEI, "gwei")),
        "chainId": config.CHAIN_ID,
    })

    signed = account.sign_transaction(tx)

    try:
        submitted = private_relay.submit_transaction(w3, signed.raw_transaction)
    except private_relay.RelaySubmissionError as e:
        # Deliberately no public-mempool fallback: leaking the tx defeats the
        # reason for submitting privately.
        log.error(f"Private submission failed, trade abandoned: {e}")
        return

    log.info(f"Submitted arb via {config.SUBMISSION_MODE}: {submitted}")

    if config.SUBMISSION_MODE == "flashbots":
        # A bundle has no receipt to await — it either lands in the target block
        # window or expires unexecuted. Expiry is the correct outcome for a
        # missed arb, so this is not treated as an error.
        log.info("Bundle submitted; inclusion depends on builders. No receipt to await.")
        return

    receipt = w3.eth.wait_for_transaction_receipt(submitted, timeout=120)
    if receipt.status == 1:
        log.info(f"Trade succeeded. Tx: {submitted}")
    else:
        log.error(f"Trade reverted. Tx: {submitted}")


def main():
    config.validate_submission_mode()  # fail fast on a mode that can't work here
    w3 = get_web3()
    log.info(
        f"Connected to {config.RPC_URL} | chain_id={w3.eth.chain_id} | "
        f"DRY_RUN={config.DRY_RUN} | submission={config.SUBMISSION_MODE}"
    )

    if not config.DRY_RUN and config.SUBMISSION_MODE == "public":
        log.warning(
            "Submitting to the PUBLIC mempool. The first swap leg uses amountOutMin=0, "
            "which is sandwich-able once the tx is visible. Consider "
            "SUBMISSION_MODE=private_rpc."
        )

    if config.DRY_RUN:
        log.warning("Running in DRY_RUN mode — no real transactions will be sent. "
                     "Set DRY_RUN=false in .env once you've validated on a fork.")

    while True:
        try:
            for base_symbol, quote_symbol in config.PAIRS_TO_SCAN:
                opp = scan_pair(w3, base_symbol, quote_symbol)
                if opp and opp["net_profit_usd"] >= Decimal(str(config.MIN_PROFIT_USD)):
                    log.info(
                        f"Opportunity: {opp['base']}/{opp['quote']} "
                        f"buy@{opp['buy_router']} sell@{opp['sell_router']} "
                        f"net_profit=${opp['net_profit_usd']:.2f} "
                        f"({opp['net_profit']:.6f} {opp['base']})"
                    )
                    execute_trade(w3, opp)
                else:
                    net = f"${opp['net_profit_usd']:.4f}" if opp else None
                    log.debug(f"No profitable opp for {base_symbol}/{quote_symbol} (best net={net})")
        except Exception as e:
            log.error(f"Scan loop error: {e}")

        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
