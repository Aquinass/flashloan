"""
Private transaction submission — Flashbots bundles and generic private RPCs.

Why this exists: a signed arb tx sitting in the public mempool is a free option
for anyone watching. Searchers can copy the trade, or sandwich the first leg
(which FlashArb submits with `amountOutMin=0`). Private submission skips the
public mempool, so the tx is only visible to the relay/builder.

Two backends behind one call:

  * FLASHBOTS — real Flashbots bundles via signed `eth_sendBundle`. Ethereum
    mainnet and its testnets only; there is no Flashbots relay on Polygon.
  * PRIVATE_RPC — plain `eth_sendRawTransaction` to a private endpoint. This is
    the Polygon-compatible path (Fastlane, bloXroute, Merkle). No bundle
    semantics, but the tx still bypasses the public mempool.
  * PUBLIC — normal mempool submission. Default, unchanged behavior.

Flashbots auth uses a signature over the request body from a keypair that is
NOT your funding key. It only builds searcher reputation, holds no funds, and
must be a distinct key.
"""

import json
import logging
from typing import Optional

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

import config

log = logging.getLogger("flash-arb.relay")

# Chains where a Flashbots relay actually exists.
FLASHBOTS_RELAYS = {
    1: "https://relay.flashbots.net",
    11155111: "https://relay-sepolia.flashbots.net",
    17000: "https://relay-holesky.flashbots.net",
}

# How many blocks ahead a bundle stays valid.
BUNDLE_BLOCK_WINDOW = 3


class RelaySubmissionError(Exception):
    """Raised when a private submission is rejected or misconfigured."""


def _hex0x(value) -> str:
    """
    Normalize to 0x-prefixed hex. hexbytes >=1.0 dropped the 0x prefix from
    .hex(), and the relay rejects unprefixed values, so never call .hex() bare.
    """
    raw = value.hex() if hasattr(value, "hex") else str(value)
    return raw if raw.startswith("0x") else "0x" + raw


def _flashbots_signature(body: str, signing_key: str) -> str:
    """Build the X-Flashbots-Signature header value for a request body."""
    signer = Account.from_key(signing_key)
    body_hash = _hex0x(Web3.keccak(text=body))
    signed = Account.sign_message(encode_defunct(text=body_hash), private_key=signing_key)
    return f"{signer.address}:{_hex0x(signed.signature)}"


def _post_relay(relay_url: str, body: dict, signing_key: Optional[str]) -> dict:
    payload = json.dumps(body, separators=(",", ":"))
    headers = {"Content-Type": "application/json"}
    if signing_key:
        headers["X-Flashbots-Signature"] = _flashbots_signature(payload, signing_key)

    resp = requests.post(relay_url, data=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if "error" in result:
        raise RelaySubmissionError(f"relay rejected request: {result['error']}")
    return result


def submit_flashbots_bundle(w3: Web3, signed_raw_tx: bytes) -> str:
    """
    Send a single-tx bundle to the Flashbots relay.

    Returns the bundle hash. A bundle hash is NOT an inclusion guarantee — if no
    builder lands it within the target window the bundle simply expires and
    nothing happened on chain. That is the desired failure mode: an arb that
    missed its block should not execute late.
    """
    chain_id = config.CHAIN_ID
    relay_url = FLASHBOTS_RELAYS.get(chain_id)
    if relay_url is None:
        raise RelaySubmissionError(
            f"no Flashbots relay for chain_id={chain_id}. Flashbots serves Ethereum "
            f"mainnet/testnets only — use SUBMISSION_MODE=private_rpc on Polygon."
        )
    if not config.FLASHBOTS_SIGNING_KEY:
        raise RelaySubmissionError(
            "FLASHBOTS_SIGNING_KEY not set. Use a throwaway key, never your funding key."
        )

    target_block = w3.eth.block_number + 1
    raw_hex = _hex0x(signed_raw_tx)

    bundle_hash = None
    for block in range(target_block, target_block + BUNDLE_BLOCK_WINDOW):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_sendBundle",
            "params": [{"txs": [raw_hex], "blockNumber": hex(block)}],
        }
        result = _post_relay(relay_url, body, config.FLASHBOTS_SIGNING_KEY)
        bundle_hash = result.get("result", {}).get("bundleHash", bundle_hash)
        log.info(f"Bundle submitted for block {block}")

    log.info(f"Flashbots bundle hash: {bundle_hash}")
    return bundle_hash or ""


def submit_private_rpc(signed_raw_tx: bytes) -> str:
    """
    Forward a signed tx to a private RPC endpoint (Polygon-compatible path).

    Bypasses the public mempool without bundle semantics — no bundle hash, no
    atomic multi-tx guarantee, just reduced mempool exposure.
    """
    if not config.PRIVATE_RPC_URL:
        raise RelaySubmissionError(
            "PRIVATE_RPC_URL not set. Point it at a Fastlane/bloXroute/Merkle endpoint."
        )

    raw_hex = _hex0x(signed_raw_tx)

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_sendRawTransaction",
        "params": [raw_hex],
    }
    result = _post_relay(config.PRIVATE_RPC_URL, body, None)
    tx_hash = result.get("result", "")
    log.info(f"Submitted via private RPC: {tx_hash}")
    return tx_hash


def submit_transaction(w3: Web3, signed_raw_tx: bytes) -> str:
    """
    Submit a signed tx using the configured mode. Returns a tx hash, or a bundle
    hash in flashbots mode.

    Never silently downgrades to the public mempool: if a private mode is
    configured and fails, that raises. Falling back to public would leak exactly
    the tx the mode exists to protect.
    """
    mode = config.SUBMISSION_MODE

    if mode == "flashbots":
        return submit_flashbots_bundle(w3, signed_raw_tx)
    if mode == "private_rpc":
        return submit_private_rpc(signed_raw_tx)
    if mode == "public":
        tx_hash = w3.eth.send_raw_transaction(signed_raw_tx)
        log.info(f"Submitted via public mempool: {_hex0x(tx_hash)}")
        return _hex0x(tx_hash)

    raise RelaySubmissionError(
        f"unknown SUBMISSION_MODE={mode!r} (expected: public, flashbots, private_rpc)"
    )
