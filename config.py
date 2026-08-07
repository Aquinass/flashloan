"""
Configuration for the flash-arb scanner/executor.
Fill in RPC_URL, PRIVATE_KEY, and CONTRACT_ADDRESS via environment variables
(see .env.example) — never hardcode secrets here.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Network ---
RPC_URL = os.getenv("RPC_URL", "https://polygon-rpc.com")
CHAIN_ID = 137  # Polygon mainnet

# --- Wallet / contract ---
PRIVATE_KEY = os.getenv("PRIVATE_KEY")  # controls the owner EOA of FlashArb.sol
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS")  # deployed FlashArb address

# --- Safety ---
# When True, the bot only logs opportunities and NEVER sends a transaction.
# Flip to False only after you've tested on a fork (see README).
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"

# Max gas price (in gwei) the bot will submit at. Prevents firing during spikes.
MAX_GAS_GWEI = float(os.getenv("MAX_GAS_GWEI", "200"))

# --- Submission mode ---
# "public"      — normal mempool. Visible to everyone, including sandwichers.
# "flashbots"   — signed eth_sendBundle to the Flashbots relay. ETHEREUM ONLY;
#                 there is no Flashbots relay on Polygon.
# "private_rpc" — eth_sendRawTransaction to a private endpoint. This is the
#                 Polygon-compatible way to skip the public mempool
#                 (Fastlane, bloXroute, Merkle).
SUBMISSION_MODE = os.getenv("SUBMISSION_MODE", "public").lower()

# Flashbots reputation/auth key. NOT a funding key — holds no balance and only
# ever signs relay request bodies. Generate a throwaway and keep it separate.
FLASHBOTS_SIGNING_KEY = os.getenv("FLASHBOTS_SIGNING_KEY")

# Private RPC endpoint for SUBMISSION_MODE=private_rpc.
PRIVATE_RPC_URL = os.getenv("PRIVATE_RPC_URL")


def validate_submission_mode() -> None:
    """Fail fast on a submission mode that cannot work on the configured chain."""
    valid = {"public", "flashbots", "private_rpc"}
    if SUBMISSION_MODE not in valid:
        raise ValueError(f"SUBMISSION_MODE must be one of {sorted(valid)}, got {SUBMISSION_MODE!r}")

    if SUBMISSION_MODE == "flashbots":
        if CHAIN_ID not in (1, 11155111, 17000):
            raise ValueError(
                f"SUBMISSION_MODE=flashbots is not available on chain_id={CHAIN_ID}. "
                "Flashbots serves Ethereum mainnet/testnets only. On Polygon use "
                "SUBMISSION_MODE=private_rpc with a Fastlane/bloXroute/Merkle endpoint."
            )
        if not FLASHBOTS_SIGNING_KEY:
            raise ValueError("SUBMISSION_MODE=flashbots requires FLASHBOTS_SIGNING_KEY")

    if SUBMISSION_MODE == "private_rpc" and not PRIVATE_RPC_URL:
        raise ValueError("SUBMISSION_MODE=private_rpc requires PRIVATE_RPC_URL")

# Minimum net profit (in USD-equivalent of the borrowed asset) to trigger a trade
MIN_PROFIT_USD = 5.0

# On-chain minProfit floor, in USD, converted to asset units at the live price
# before submission. This is the contract-enforced backstop: if the trade lands
# in a worse state than the scanner predicted, it reverts rather than paying gas
# for a loss. Lower than MIN_PROFIT_USD on purpose — the scanner decides whether
# a trade is worth attempting, this only rejects outright bad outcomes.
ONCHAIN_MIN_PROFIT_USD = float(os.getenv("ONCHAIN_MIN_PROFIT_USD", "1.0"))

# How much to borrow per attempt, in token units (will be scaled by decimals)
BORROW_AMOUNT = {
    "USDC": 5000,   # $5k per attempt — start small
}

# Poll interval in seconds
POLL_INTERVAL_SEC = 3

# --- Gas token price feed ---
# Polygon PoS gas is paid in POL, which replaced MATIC in Sept 2024. The legacy
# "matic-network" id still resolves on CoinGecko but tracks a different (higher)
# price, so using it would overstate gas costs.
GAS_TOKEN_COINGECKO_ID = os.getenv("GAS_TOKEN_COINGECKO_ID", "polygon-ecosystem-token")
GAS_TOKEN_SYMBOL = os.getenv("GAS_TOKEN_SYMBOL", "POL")
PRICE_FEED_URL = "https://api.coingecko.com/api/v3/simple/price"
PRICE_FEED_TIMEOUT_SEC = 8

# How long a fetched price stays good. Doubles as the retry throttle, so a
# failing API is polled once per TTL instead of once per scan loop.
PRICE_CACHE_TTL_SEC = 60

# Bootstrap only — used if the very first fetch fails, before any live price has
# been cached. Not a substitute for the feed; the bot logs loudly when it applies.
GAS_TOKEN_PRICE_FALLBACK_USD = "0.075"

# --- Tokens (Polygon mainnet addresses) ---
TOKENS = {
    "USDC": {"address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": 6},
    "WMATIC": {"address": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "decimals": 18},
    "WETH": {"address": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", "decimals": 18},
}

# CoinGecko id per token, for valuing gas and profit in USD.
# The cache is keyed by id, so two symbols naming the same underlying asset
# (USDC/USDC.e) correctly share a slot while distinct assets never do.
TOKEN_COINGECKO_IDS = {
    "USDC": "usd-coin",
    "USDC.e": "usd-coin",  # bridged variant, same peg
    "WMATIC": "polygon-ecosystem-token",
    "WETH": "ethereum",
}

# Bootstrap-only fallbacks, applied ONLY when the very first fetch for a token
# fails and nothing is cached yet.
#
# Deliberately limited to pegged assets. A wrong stablecoin guess is off by
# fractions of a percent; a wrong WETH guess can be off by 2x and would silently
# corrupt every profit calculation. Volatile tokens have no entry here, so an
# unpriceable pair gets skipped instead of traded on an invented number.
TOKEN_PRICE_FALLBACK_USD = {
    "USDC": "1.00",
    "USDC.e": "1.00",
}

# --- DEX routers (Polygon mainnet) ---
ROUTERS = {
    "quickswap": "0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff",
    "sushiswap": "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
}

# Pairs to scan: (base, quote) — bot checks base->quote->base round trip
PAIRS_TO_SCAN = [
    ("USDC", "WMATIC"),
    ("USDC", "WETH"),
]

# Aave V3 Pool Addresses Provider (Polygon mainnet)
AAVE_ADDRESSES_PROVIDER = "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"
