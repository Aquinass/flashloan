// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import {IPoolAddressesProvider} from "@aave/core-v3/contracts/interfaces/IPoolAddressesProvider.sol";
import {IPool} from "@aave/core-v3/contracts/interfaces/IPool.sol";
import {IFlashLoanSimpleReceiver} from "@aave/core-v3/contracts/flashloan/interfaces/IFlashLoanSimpleReceiver.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external returns (uint[] memory amounts);

    function getAmountsOut(uint amountIn, address[] calldata path)
        external view returns (uint[] memory amounts);
}

/// @title FlashArb
/// @notice Borrows a single asset via Aave V3 flash loan, swaps it across two
///         DEX routers (buy low on one, sell high on the other), repays the
///         loan + premium, and keeps the difference. Reverts if unprofitable
///         (Aave itself enforces repayment, so a bad trade just reverts the
///         whole tx and you only lose gas, not principal).
contract FlashArb is IFlashLoanSimpleReceiver, Ownable {
    using SafeERC20 for IERC20;

    IPoolAddressesProvider public immutable ADDRESSES_PROVIDER;
    IPool public immutable POOL;

    event ArbExecuted(
        address indexed asset,
        uint256 amountBorrowed,
        uint256 profit
    );

    constructor(address _addressesProvider) Ownable(msg.sender) {
        ADDRESSES_PROVIDER = IPoolAddressesProvider(_addressesProvider);
        POOL = IPool(ADDRESSES_PROVIDER.getPool());
    }

    /// @notice Kick off the arb. Only owner can trigger.
    /// @param asset Token to borrow (e.g. USDC)
    /// @param amount Amount to borrow
    /// @param routerBuy Router to buy the intermediate token on (cheap)
    /// @param routerSell Router to sell the intermediate token on (expensive)
    /// @param intermediate The other token in the pair (e.g. WMATIC)
    /// @param minProfit Minimum profit required, in `asset` units, or the tx reverts
    function executeArb(
        address asset,
        uint256 amount,
        address routerBuy,
        address routerSell,
        address intermediate,
        uint256 minProfit
    ) external onlyOwner {
        bytes memory params = abi.encode(
            routerBuy,
            routerSell,
            intermediate,
            minProfit,
            msg.sender
        );

        POOL.flashLoanSimple(address(this), asset, amount, params, 0);
    }

    /// @dev Called by Aave Pool mid-flashloan. This is where the actual
    ///      arbitrage logic runs.
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == address(POOL), "caller must be Aave Pool");
        require(initiator == address(this), "initiator must be this contract");

        (
            address routerBuy,
            address routerSell,
            address intermediate,
            uint256 minProfit,
            // originalCaller — encoded by executeArb, unused here
        ) = abi.decode(params, (address, address, address, uint256, address));

        uint256 amountOwed = amount + premium;

        // Leg 1: asset -> intermediate on the cheap router
        address[] memory pathBuy = new address[](2);
        pathBuy[0] = asset;
        pathBuy[1] = intermediate;

        IERC20(asset).forceApprove(routerBuy, amount);
        uint256[] memory out1 = IUniswapV2Router(routerBuy).swapExactTokensForTokens(
            amount,
            0, // slippage check happens on final profit check below
            pathBuy,
            address(this),
            block.timestamp
        );
        uint256 intermediateReceived = out1[out1.length - 1];

        // Leg 2: intermediate -> asset on the expensive router
        address[] memory pathSell = new address[](2);
        pathSell[0] = intermediate;
        pathSell[1] = asset;

        IERC20(intermediate).forceApprove(routerSell, intermediateReceived);
        uint256[] memory out2 = IUniswapV2Router(routerSell).swapExactTokensForTokens(
            intermediateReceived,
            amountOwed, // must at least cover what we owe, or this leg reverts
            pathSell,
            address(this),
            block.timestamp
        );
        uint256 assetReceived = out2[out2.length - 1];

        require(assetReceived >= amountOwed, "trade did not cover flash loan repayment");
        uint256 profit = assetReceived - amountOwed;
        require(profit >= minProfit, "profit below minProfit threshold");

        // Repay Aave (pool pulls amountOwed via allowance)
        IERC20(asset).forceApprove(address(POOL), amountOwed);

        // Send remaining profit to owner
        if (profit > 0) {
            IERC20(asset).safeTransfer(owner(), profit);
        }

        emit ArbExecuted(asset, amount, profit);
        return true;
    }

    /// @notice Rescue any tokens accidentally stuck in the contract
    function rescueTokens(address token, uint256 amount) external onlyOwner {
        IERC20(token).safeTransfer(owner(), amount);
    }

    function ADDRESSES_PROVIDER_() external view returns (IPoolAddressesProvider) {
        return ADDRESSES_PROVIDER;
    }

    function POOL_() external view returns (IPool) {
        return POOL;
    }
}
