// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {FlashArb, IUniswapV2Router} from "../src/FlashArb.sol";

/// @notice Fork tests for FlashArb against live Polygon state.
///
/// Run with:
///   export FORK_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/<key>
///   forge test --match-path test/FlashArb.fork.t.sol -vv
///
/// These never touch mainnet: every tx lands on a local fork of Polygon state.
/// A real arb opportunity almost never exists at an arbitrary block, so
/// `test_ProfitableArb_PaysOwner` manufactures one by skewing a pool first —
/// that is the only way to exercise the profit path deterministically.
contract FlashArbForkTest is Test {
    // --- Polygon mainnet addresses ---
    address constant AAVE_PROVIDER = 0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb;
    address constant QUICKSWAP = 0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff;
    address constant SUSHISWAP = 0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506;

    // Bridged USDC.e — this is where the V2 liquidity actually is.
    address constant USDC_E = 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174;
    // Native USDC — what config.py currently points at. Thin V2 pools.
    address constant USDC_NATIVE = 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359;
    address constant WMATIC = 0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270;

    uint256 constant USDC_UNIT = 1e6;
    uint256 constant BORROW = 5_000 * USDC_UNIT; // matches config.BORROW_AMOUNT

    /// @dev Recent-ish Polygon block. Override with FORK_BLOCK, or set
    ///      FORK_BLOCK=0 to fork from latest.
    uint256 constant DEFAULT_FORK_BLOCK = 91_000_000;

    FlashArb arb;
    address owner = makeAddr("owner");

    function setUp() public {
        // Pinning a block keeps results reproducible, but needs an archive RPC.
        // Set FORK_BLOCK=0 to fork from latest instead (works on full nodes).
        uint256 forkBlock = vm.envOr("FORK_BLOCK", uint256(DEFAULT_FORK_BLOCK));
        if (forkBlock == 0) {
            vm.createSelectFork(vm.envString("FORK_RPC_URL"));
        } else {
            vm.createSelectFork(vm.envString("FORK_RPC_URL"), forkBlock);
        }

        vm.prank(owner);
        arb = new FlashArb(AAVE_PROVIDER);

        vm.label(address(arb), "FlashArb");
        vm.label(USDC_E, "USDC.e");
        vm.label(USDC_NATIVE, "USDC-native");
        vm.label(WMATIC, "WMATIC");
        vm.label(QUICKSWAP, "QuickSwap");
        vm.label(SUSHISWAP, "SushiSwap");
    }

    // ---------------------------------------------------------------
    // Wiring sanity
    // ---------------------------------------------------------------

    function test_PoolResolvedFromProvider() public view {
        assertTrue(address(arb.POOL()) != address(0), "Aave pool not resolved");
        assertEq(arb.owner(), owner, "owner should be deployer");
    }

    /// @dev Guards the config.py token choice. Native USDC has far thinner
    ///      V2 depth than USDC.e; quoting the round trip on it is what makes
    ///      the scanner return junk. Asserted so the difference is visible.
    function test_UsdcEHasDeeperV2LiquidityThanNative() public view {
        uint256 outE = _quote(QUICKSWAP, BORROW, USDC_E, WMATIC);
        uint256 outNative = _quoteOrZero(QUICKSWAP, BORROW, USDC_NATIVE, WMATIC);

        console.log("USDC.e  -> WMATIC out:", outE);
        console.log("native  -> WMATIC out:", outNative);

        assertGt(outE, 0, "USDC.e/WMATIC should quote");
        assertGt(outE, outNative, "USDC.e expected to be the deeper route");
    }

    // ---------------------------------------------------------------
    // Failure paths
    // ---------------------------------------------------------------

    function test_RevertsWhenNotOwner() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert();
        arb.executeArb(USDC_E, BORROW, SUSHISWAP, QUICKSWAP, WMATIC, 0);
    }

    /// @dev The whole safety premise: an unprofitable attempt costs gas only.
    ///      At an unskewed block the round trip cannot cover loan + premium,
    ///      so the tx must revert and leave no balance behind.
    function test_RevertsWhenUnprofitable() public {
        uint256 before = IERC20(USDC_E).balanceOf(owner);

        vm.prank(owner);
        vm.expectRevert();
        arb.executeArb(USDC_E, BORROW, QUICKSWAP, SUSHISWAP, WMATIC, 1 * USDC_UNIT);

        assertEq(IERC20(USDC_E).balanceOf(owner), before, "owner balance must not change");
        assertEq(IERC20(USDC_E).balanceOf(address(arb)), 0, "no dust should remain");
    }

    function test_RevertsWhenMinProfitUnreachable() public {
        _skewQuickswapWmaticUp(400_000 * USDC_UNIT);

        // Demand an absurd profit floor — must revert even though a real
        // (smaller) profit is available.
        vm.prank(owner);
        vm.expectRevert();
        arb.executeArb(USDC_E, BORROW, SUSHISWAP, QUICKSWAP, WMATIC, 100_000 * USDC_UNIT);
    }

    // ---------------------------------------------------------------
    // Success path
    // ---------------------------------------------------------------

    function test_ProfitableArb_PaysOwner() public {
        // Push WMATIC's price up on QuickSwap so buying on SushiSwap and
        // selling on QuickSwap clears the loan + premium.
        _skewQuickswapWmaticUp(400_000 * USDC_UNIT);

        uint256 expected = _expectedRoundTrip(SUSHISWAP, QUICKSWAP, BORROW);
        uint256 owed = BORROW + (BORROW * 5) / 10_000; // Aave premium, 0.05%
        assertGt(expected, owed, "fork not skewed enough to be profitable");

        uint256 before = IERC20(USDC_E).balanceOf(owner);

        vm.prank(owner);
        arb.executeArb(USDC_E, BORROW, SUSHISWAP, QUICKSWAP, WMATIC, 1 * USDC_UNIT);

        uint256 profit = IERC20(USDC_E).balanceOf(owner) - before;
        console.log("realized profit (USDC.e 6dp):", profit);

        assertGt(profit, 0, "owner should receive profit");
        assertEq(IERC20(USDC_E).balanceOf(address(arb)), 0, "contract should retain nothing");
        assertEq(IERC20(WMATIC).balanceOf(address(arb)), 0, "no intermediate left over");
    }

    function test_RescueTokensReturnsStuckFunds() public {
        deal(USDC_E, address(arb), 250 * USDC_UNIT);
        uint256 before = IERC20(USDC_E).balanceOf(owner);

        vm.prank(owner);
        arb.rescueTokens(USDC_E, 250 * USDC_UNIT);

        assertEq(IERC20(USDC_E).balanceOf(owner) - before, 250 * USDC_UNIT);
        assertEq(IERC20(USDC_E).balanceOf(address(arb)), 0);
    }

    // ---------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------

    function _path(address a, address b) internal pure returns (address[] memory p) {
        p = new address[](2);
        p[0] = a;
        p[1] = b;
    }

    function _quote(address router, uint256 amountIn, address a, address b)
        internal
        view
        returns (uint256)
    {
        uint256[] memory out = IUniswapV2Router(router).getAmountsOut(amountIn, _path(a, b));
        return out[out.length - 1];
    }

    function _quoteOrZero(address router, uint256 amountIn, address a, address b)
        internal
        view
        returns (uint256)
    {
        try IUniswapV2Router(router).getAmountsOut(amountIn, _path(a, b)) returns (
            uint256[] memory out
        ) {
            return out[out.length - 1];
        } catch {
            return 0;
        }
    }

    function _expectedRoundTrip(address routerBuy, address routerSell, uint256 amountIn)
        internal
        view
        returns (uint256)
    {
        uint256 mid = _quote(routerBuy, amountIn, USDC_E, WMATIC);
        return _quote(routerSell, mid, WMATIC, USDC_E);
    }

    /// @dev Buy WMATIC on QuickSwap with a large USDC.e order, draining WMATIC
    ///      from the pool and lifting its price there relative to SushiSwap.
    function _skewQuickswapWmaticUp(uint256 usdcIn) internal {
        address whale = makeAddr("whale");
        deal(USDC_E, whale, usdcIn);

        vm.startPrank(whale);
        IERC20(USDC_E).approve(QUICKSWAP, usdcIn);
        IUniswapV2Router(QUICKSWAP).swapExactTokensForTokens(
            usdcIn, 0, _path(USDC_E, WMATIC), whale, block.timestamp
        );
        vm.stopPrank();
    }
}
