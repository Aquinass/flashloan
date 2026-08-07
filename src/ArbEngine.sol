// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @dev Balancer V2 Vault. Flash loan fee is governance-set and has always been
///      zero; we never assume that — repayment uses the feeAmounts the Vault
///      hands us, so a fee switch flip cannot strand a trade.
interface IBalancerVault {
    function flashLoan(
        address recipient,
        address[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external;
}

interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

/// @dev Uniswap SwapRouter02: no deadline in params.
interface ISwapRouter02 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params)
        external payable returns (uint256 amountOut);
}

/// @dev Uniswap V3 SwapRouter (v1) / SushiSwap V3: deadline in params.
interface ISwapRouter01 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params)
        external payable returns (uint256 amountOut);
}

/// @dev Algebra (QuickSwap V3): dynamic fee, so no fee field; has deadline.
interface IAlgebraRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 limitSqrtPrice;
    }
    function exactInputSingle(ExactInputSingleParams calldata params)
        external payable returns (uint256 amountOut);
}

/// @title ArbEngine
/// @notice Zero-fee (Balancer V2) flash-loan arbitrage across V2, Uniswap V3,
///         SushiSwap V3, and Algebra/QuickSwap V3 venues.
///
/// @dev Why this exists alongside FlashArb.sol: FlashArb borrows from Aave at a
///      0.05% premium and can only trade UniswapV2-style routers. On Polygon the
///      V2 pools for the configured pairs are effectively drained (a $5k USDC
///      round trip returns ~$24), while the 0.01% V3 tiers round-trip at
///      ~0.004%. Measured on mainnet, the Aave premium alone is ~12x the entire
///      swap cost of a stable-pair round trip. Both changes are required for the
///      hurdle rate to be reachable at all.
///
/// @dev Route model: an arb is an ordered list of hops. Hop i's output token is
///      hop i+1's input token; the last hop must return to the borrowed asset.
///      This keeps triangular and multi-venue routes expressible without any
///      arbitrary-calldata escape hatch.
contract ArbEngine is Ownable {
    using SafeERC20 for IERC20;

    enum Venue {
        UniV2,      // 0: swapExactTokensForTokens
        UniV3_02,   // 1: SwapRouter02, fee, no deadline
        UniV3_01,   // 2: SwapRouter/SushiV3, fee + deadline
        Algebra     // 3: QuickSwap V3, dynamic fee + deadline
    }

    struct Hop {
        Venue venue;
        address router;
        address tokenIn;
        address tokenOut;
        uint24 fee; // ignored for UniV2 and Algebra
    }

    IBalancerVault public immutable VAULT;

    /// @dev Guards receiveFlashLoan against being driven by anyone other than
    ///      our own executeArb call. The Vault calls back unconditionally, so
    ///      msg.sender == VAULT alone would let a third party initiate a loan
    ///      naming this contract as recipient and hand it a hostile route.
    bool private _inFlight;

    event ArbExecuted(address indexed asset, uint256 amountBorrowed, uint256 profit);

    error NotVault();
    error NotInFlight();
    error EmptyRoute();
    error RouteNotCircular();
    error RepaymentShortfall(uint256 have, uint256 owed);
    error ProfitBelowFloor(uint256 profit, uint256 floor);

    constructor(address vault) Ownable(msg.sender) {
        VAULT = IBalancerVault(vault);
    }

    /// @notice Execute an arbitrage funded by a zero-fee Balancer flash loan.
    /// @param asset Token to borrow and repay.
    /// @param amount Amount to borrow.
    /// @param hops Ordered route; must start and end at `asset`.
    /// @param minProfit Minimum profit in `asset` units, else the tx reverts.
    function executeArb(
        address asset,
        uint256 amount,
        Hop[] calldata hops,
        uint256 minProfit
    ) external onlyOwner {
        if (hops.length == 0) revert EmptyRoute();
        if (hops[0].tokenIn != asset || hops[hops.length - 1].tokenOut != asset) {
            revert RouteNotCircular();
        }
        for (uint256 i = 1; i < hops.length; ++i) {
            if (hops[i].tokenIn != hops[i - 1].tokenOut) revert RouteNotCircular();
        }

        address[] memory tokens = new address[](1);
        tokens[0] = asset;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = amount;

        _inFlight = true;
        VAULT.flashLoan(address(this), tokens, amounts, abi.encode(hops, minProfit));
        _inFlight = false;
    }

    /// @dev Balancer callback. Balancer requires repayment by *transferring*
    ///      principal + fee back to the Vault, not by approval.
    function receiveFlashLoan(
        address[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes memory userData
    ) external {
        if (msg.sender != address(VAULT)) revert NotVault();
        if (!_inFlight) revert NotInFlight();

        (Hop[] memory hops, uint256 minProfit) = abi.decode(userData, (Hop[], uint256));

        address asset = tokens[0];
        uint256 owed = amounts[0] + feeAmounts[0];

        uint256 amountIn = amounts[0];
        for (uint256 i = 0; i < hops.length; ++i) {
            amountIn = _swap(hops[i], amountIn);
        }

        uint256 finalBalance = IERC20(asset).balanceOf(address(this));
        if (finalBalance < owed) revert RepaymentShortfall(finalBalance, owed);

        uint256 profit = finalBalance - owed;
        if (profit < minProfit) revert ProfitBelowFloor(profit, minProfit);

        IERC20(asset).safeTransfer(address(VAULT), owed);
        if (profit > 0) IERC20(asset).safeTransfer(owner(), profit);

        emit ArbExecuted(asset, amounts[0], profit);
    }

    /// @dev amountOutMinimum is 0 on every hop by design: the only check that
    ///      matters is the final repayment + minProfit assertion, which makes
    ///      the whole route atomic. A per-hop minimum would add gas and could
    ///      revert a route that is still profitable end to end.
    function _swap(Hop memory h, uint256 amountIn) private returns (uint256) {
        IERC20(h.tokenIn).forceApprove(h.router, amountIn);

        if (h.venue == Venue.UniV2) {
            address[] memory path = new address[](2);
            path[0] = h.tokenIn;
            path[1] = h.tokenOut;
            uint256[] memory out = IUniswapV2Router(h.router).swapExactTokensForTokens(
                amountIn, 0, path, address(this), block.timestamp
            );
            return out[out.length - 1];
        }

        if (h.venue == Venue.UniV3_02) {
            return ISwapRouter02(h.router).exactInputSingle(
                ISwapRouter02.ExactInputSingleParams({
                    tokenIn: h.tokenIn,
                    tokenOut: h.tokenOut,
                    fee: h.fee,
                    recipient: address(this),
                    amountIn: amountIn,
                    amountOutMinimum: 0,
                    sqrtPriceLimitX96: 0
                })
            );
        }

        if (h.venue == Venue.UniV3_01) {
            return ISwapRouter01(h.router).exactInputSingle(
                ISwapRouter01.ExactInputSingleParams({
                    tokenIn: h.tokenIn,
                    tokenOut: h.tokenOut,
                    fee: h.fee,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: amountIn,
                    amountOutMinimum: 0,
                    sqrtPriceLimitX96: 0
                })
            );
        }

        return IAlgebraRouter(h.router).exactInputSingle(
            IAlgebraRouter.ExactInputSingleParams({
                tokenIn: h.tokenIn,
                tokenOut: h.tokenOut,
                recipient: address(this),
                deadline: block.timestamp,
                amountIn: amountIn,
                amountOutMinimum: 0,
                limitSqrtPrice: 0
            })
        );
    }

    /// @notice Recover tokens sent here by mistake. Profit is forwarded to the
    ///         owner inside the same transaction, so a nonzero balance here is
    ///         always either dust or an accident.
    function rescueTokens(address token, uint256 amount) external onlyOwner {
        IERC20(token).safeTransfer(owner(), amount);
    }
}
