// SPDX-License-Identifier: BUSL-1.1
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/metatx/ERC2771Context.sol";
import "../VerisphereForwarder.sol";

contract MockVSP2 is ERC20 {
    constructor() ERC20("MockVSP", "mVSP") {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

contract Target2 is ERC2771Context {
    constructor(address f) ERC2771Context(f) {}
    function stake(uint256, uint8, uint256) external {}
}

/// Review PoC (2026-09-07): fee is pulled from `request.from` BEFORE the
/// signature is verified. execute() is atomic so a bad signature reverts
/// the fee too — but executeBatch(..., refundReceiver != 0) SKIPS invalid
/// requests without reverting, so the fee sticks. Anyone can drain any
/// user's VSP allowance to the treasury with a forged request.
contract ForwarderDrainRegression is Test {
    VerisphereForwarder fwd;
    MockVSP2 vsp;
    Target2 target;
    address treasury = address(0xBEEF);
    address victim = address(0x7157);
    address attacker = address(0xBAD);

    function setUp() public {
        vsp = new MockVSP2();
        vsp.mint(victim, 1_000e18);
        VerisphereForwarder impl = new VerisphereForwarder();
        fwd = VerisphereForwarder(
            payable(address(
                    new ERC1967Proxy(
                        address(impl),
                        abi.encodeCall(
                            VerisphereForwarder.initialize, (address(vsp), treasury, address(this), 50, 1e17)
                        )
                    )
                ))
        );
        target = new Target2(address(fwd));
        // victim did what the dapp asks: approve the forwarder for fees
        vm.prank(victim);
        vsp.approve(address(fwd), type(uint256).max);
    }

    function test_C1_forgedBatchCannotDrain_validateBeforeFee() public {
        // forged request "from" victim, garbage signature, huge amount so fee == whole balance
        // fee = amount * 50 / 10000  ->  amount = 200_000e18 gives fee = 1_000e18
        VerisphereForwarder.ForwardRequestData[] memory reqs = new VerisphereForwarder.ForwardRequestData[](1);
        reqs[0].from = victim;
        reqs[0].to = address(target);
        reqs[0].value = 0;
        reqs[0].gas = 100_000;
        reqs[0].deadline = uint48(block.timestamp + 3600);
        reqs[0].data = abi.encodeWithSignature("stake(uint256,uint8,uint256)", 1, 0, 200_000e18);
        reqs[0].signature = hex"deadbeef";

        uint256 before = vsp.balanceOf(victim);
        // C1 regression (v4): a non-atomic batch is refused outright ...
        vm.prank(attacker);
        vm.expectRevert(VerisphereForwarder.BatchMustBeAtomic.selector);
        fwd.executeBatch(reqs, payable(attacker));
        // ... and an atomic batch with a forged signature reverts as a whole,
        // unwinding the fee pull with it (OZ ERC2771ForwarderInvalidSigner).
        vm.prank(attacker);
        vm.expectRevert();
        fwd.executeBatch(reqs, payable(address(0)));

        emit log_named_uint("victim balance before", before / 1e18);
        emit log_named_uint("victim balance after", vsp.balanceOf(victim) / 1e18);
        emit log_named_uint("treasury received", vsp.balanceOf(treasury) / 1e18);
        assertEq(vsp.balanceOf(victim), before, "victim balance untouched: no signature, no fee");
        assertEq(vsp.balanceOf(treasury), 0, "treasury received nothing from a forged batch");
    }

    function test_execute_isAtomic_soSingleForgeReverts() public {
        VerisphereForwarder.ForwardRequestData memory r;
        r.from = victim;
        r.to = address(target);
        r.gas = 100_000;
        r.deadline = uint48(block.timestamp + 3600);
        r.data = abi.encodeWithSignature("stake(uint256,uint8,uint256)", 1, 0, 200_000e18);
        r.signature = hex"deadbeef";
        vm.prank(attacker);
        vm.expectRevert();
        fwd.execute(r);
        assertEq(vsp.balanceOf(victim), 1_000e18);
    }
}
