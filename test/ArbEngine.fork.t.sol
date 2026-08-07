// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import {Test, console} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ArbEngine} from "../src/ArbEngine.sol";

interface IQuoterV2 {
    struct QuoteExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint256 amountIn;
        uint24 fee;
        uint160 sqrtPriceLimitX96;
    }
    function quoteExactInputSingle(QuoteExactInputSingleParams memory params)
        external returns (uint256 amountOut, uint160, uint32, uint256);
}

interface IAlgebraQuoter {
    function quoteExactInputSingle(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint160 limitSqrtPrice
    ) external returns (uint256 amountOut, uint16 fee);
}

/// @notice Fork tests for ArbEngine against live Polygon state.
///
/// These assert the mechanics that cannot be checked off-chain: that a Balancer
/// zero-fee loan repays correctly, that each venue adapter encodes its router's
/// ABI correctly, and that the profit floor actually reverts. Whether a given
/// spread exists at a given block is a market question, not a code question, so
/// profitability is asserted only where the fork block guarantees it.
contract ArbEngineForkTest is Test {
    address constant VAULT = 0xBA12222222228d8Ba445958a75a0704d566BF2C8;

    address constant USDC_E = 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174;
    address constant USDT   = 0xc2132D05D31c914a87C6611C10748AEb04B58e8F;
    address constant DAI    = 0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063;
    address constant WETH   = 0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619;

    address constant UNI_ROUTER02  = 0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45;
    address constant SUSHI_V3_ROUTER = 0x0389879e0156033202C44BF784ac18fC02edeE4f;
    address constant QUICK_V3_ROUTER = 0xf5b509bB0909a69B1c207E495f687a596C168E12;
    address constant QUICK_V2_ROUTER = 0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff;

    address constant UNI_QUOTER    = 0x61fFE014bA17989E743c5F6cB21bF9697530B21e;
    address constant ALGEBRA_QUOTER = 0xa15F0D7377B2A0C0c10db057f641beD21028FC89;

    ArbEngine engine;
    address owner = address(0xA11CE);

    function setUp() public {
        vm.createSelectFork(vm.envString("FORK_RPC_URL"));
        vm.prank(owner);
        engine = new ArbEngine(VAULT);
    }

    function test_VaultHasBorrowableDepth() public view {
        uint256 bal = IERC20(USDC_E).balanceOf(VAULT);
        assertGt(bal, 100_000e6, "Balancer vault should hold >100k USDC.e");
    }

    /// @dev The core economic claim: Balancer charges no premium, so a route only
    ///      has to beat swap cost, not swap cost + 5bps.
    function test_BalancerFlashLoanIsFeeFree() public {
        uint256 borrow = 50_000e6;
        // Route that loses money: minProfit 0 still reverts on shortfall, and the
        // shortfall equals swap cost alone if the loan itself is free.
        ArbEngine.Hop[] memory hops = _twoHop(
            USDC_E, USDT,
            ArbEngine.Venue.UniV3_02, UNI_ROUTER02, 100,
            ArbEngine.Venue.UniV3_02, UNI_ROUTER02, 100
        );
        vm.prank(owner);
        try engine.executeArb(USDC_E, borrow, hops, 0) {
            // profitable at this block; fine
        } catch (bytes memory err) {
            bytes4 sel = bytes4(err);
            assertEq(sel, ArbEngine.RepaymentShortfall.selector, "expected shortfall, not fee failure");
            (uint256 have, uint256 owed) = abi.decode(_strip(err), (uint256, uint256));
            // owed == principal exactly proves feeAmounts[0] == 0
            assertEq(owed, borrow, "Balancer charged a flash loan fee");
            uint256 lossBps = ((borrow - have) * 10_000) / borrow;
            console.log("round-trip cost, same-tier 0.01%% (bps):", lossBps);
        }
    }

    function test_RevertsWhenNotOwner() public {
        ArbEngine.Hop[] memory hops = _twoHop(
            USDC_E, USDT,
            ArbEngine.Venue.UniV3_02, UNI_ROUTER02, 100,
            ArbEngine.Venue.Algebra, QUICK_V3_ROUTER, 0
        );
        vm.expectRevert();
        engine.executeArb(USDC_E, 1000e6, hops, 0);
    }

    function test_RevertsOnNonCircularRoute() public {
        ArbEngine.Hop[] memory hops = new ArbEngine.Hop[](1);
        hops[0] = ArbEngine.Hop(ArbEngine.Venue.UniV3_02, UNI_ROUTER02, USDC_E, USDT, 100);
        vm.prank(owner);
        vm.expectRevert(ArbEngine.RouteNotCircular.selector);
        engine.executeArb(USDC_E, 1000e6, hops, 0);
    }

    function test_RevertsOnBrokenHopChain() public {
        ArbEngine.Hop[] memory hops = new ArbEngine.Hop[](2);
        hops[0] = ArbEngine.Hop(ArbEngine.Venue.UniV3_02, UNI_ROUTER02, USDC_E, USDT, 100);
        // hop 1 starts at DAI, not USDT
        hops[1] = ArbEngine.Hop(ArbEngine.Venue.UniV3_02, UNI_ROUTER02, DAI, USDC_E, 100);
        vm.prank(owner);
        vm.expectRevert(ArbEngine.RouteNotCircular.selector);
        engine.executeArb(USDC_E, 1000e6, hops, 0);
    }

    function test_RevertsOnEmptyRoute() public {
        ArbEngine.Hop[] memory hops = new ArbEngine.Hop[](0);
        vm.prank(owner);
        vm.expectRevert(ArbEngine.EmptyRoute.selector);
        engine.executeArb(USDC_E, 1000e6, hops, 0);
    }

    /// @dev Anyone can ask the Vault to flash-loan to an arbitrary recipient.
    ///      Without the _inFlight latch, a third party could drive our callback
    ///      with a route of their choosing.
    function test_CallbackRejectsUninitiatedLoan() public {
        address[] memory tokens = new address[](1);
        tokens[0] = USDC_E;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = 1000e6;
        uint256[] memory fees = new uint256[](1);
        fees[0] = 0;
        ArbEngine.Hop[] memory hops = _twoHop(
            USDC_E, USDT,
            ArbEngine.Venue.UniV3_02, UNI_ROUTER02, 100,
            ArbEngine.Venue.Algebra, QUICK_V3_ROUTER, 0
        );
        vm.prank(VAULT);
        vm.expectRevert(ArbEngine.NotInFlight.selector);
        engine.receiveFlashLoan(tokens, amounts, fees, abi.encode(hops, uint256(0)));
    }

    function test_CallbackRejectsNonVaultCaller() public {
        address[] memory tokens = new address[](1);
        tokens[0] = USDC_E;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = 1000e6;
        uint256[] memory fees = new uint256[](1);
        fees[0] = 0;
        vm.expectRevert(ArbEngine.NotVault.selector);
        engine.receiveFlashLoan(tokens, amounts, fees, abi.encode(new ArbEngine.Hop[](0), uint256(0)));
    }

    function test_ProfitFloorReverts() public {
        ArbEngine.Hop[] memory hops = _twoHop(
            USDC_E, USDT,
            ArbEngine.Venue.Algebra, QUICK_V3_ROUTER, 0,
            ArbEngine.Venue.UniV3_02, UNI_ROUTER02, 100
        );
        vm.prank(owner);
        // an absurd floor must revert regardless of market state
        vm.expectRevert();
        engine.executeArb(USDC_E, 5000e6, hops, 1_000_000e6);
    }

    function test_RescueTokensReturnsStuckFunds() public {
        deal(USDC_E, address(engine), 123e6);
        uint256 before = IERC20(USDC_E).balanceOf(owner);
        vm.prank(owner);
        engine.rescueTokens(USDC_E, 123e6);
        assertEq(IERC20(USDC_E).balanceOf(owner) - before, 123e6);
    }

    /// @dev Each adapter must encode its router's ABI correctly. A wrong struct
    ///      layout reverts, so a successful nonzero swap is the proof.
    function test_AllVenueAdaptersExecute() public {
        _assertVenueSwaps(ArbEngine.Venue.UniV3_02, UNI_ROUTER02, 100, "uniV3 0.01%");
        _assertVenueSwaps(ArbEngine.Venue.Algebra, QUICK_V3_ROUTER, 0, "quickV3 algebra");
        _assertVenueSwaps(ArbEngine.Venue.UniV3_01, SUSHI_V3_ROUTER, 100, "sushiV3 0.01%");
        _assertVenueSwaps(ArbEngine.Venue.UniV2, QUICK_V2_ROUTER, 0, "quickV2");
    }

    /// @dev Round trip out-and-back on one venue: proves the adapter works in
    ///      both directions. Loss is expected (2x fee); we assert it lands in a
    ///      sane band rather than reverting or returning zero.
    function _assertVenueSwaps(
        ArbEngine.Venue venue,
        address router,
        uint24 fee,
        string memory label
    ) private {
        uint256 borrow = 1000e6;
        ArbEngine.Hop[] memory hops = _twoHop(
            USDC_E, USDT, venue, router, fee, venue, router, fee
        );
        vm.prank(owner);
        try engine.executeArb(USDC_E, borrow, hops, 0) {
            console.log(label, "profitable round trip");
        } catch (bytes memory err) {
            assertEq(bytes4(err), ArbEngine.RepaymentShortfall.selector, label);
            (uint256 have,) = abi.decode(_strip(err), (uint256, uint256));
            assertGt(have, borrow * 90 / 100, string.concat(label, ": swap returned too little"));
            assertLt(have, borrow, string.concat(label, ": impossible gain"));
            console.log(label, "cost bps:", ((borrow - have) * 10_000) / borrow);
        }
    }

    function _twoHop(
        address a,
        address b,
        ArbEngine.Venue v1,
        address r1,
        uint24 f1,
        ArbEngine.Venue v2,
        address r2,
        uint24 f2
    ) private pure returns (ArbEngine.Hop[] memory hops) {
        hops = new ArbEngine.Hop[](2);
        hops[0] = ArbEngine.Hop(v1, r1, a, b, f1);
        hops[1] = ArbEngine.Hop(v2, r2, b, a, f2);
    }

    function _strip(bytes memory err) private pure returns (bytes memory out) {
        out = new bytes(err.length - 4);
        for (uint256 i = 4; i < err.length; ++i) out[i - 4] = err[i];
    }
}
