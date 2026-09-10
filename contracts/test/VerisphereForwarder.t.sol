// SPDX-License-Identifier: BUSL-1.1
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/metatx/ERC2771Context.sol";
import "../VerisphereForwarder.sol";

// ── Mocks ────────────────────────────────────────────────────

contract MockVSP is ERC20 {
    constructor() ERC20("MockVSP", "mVSP") {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

/// @dev A target that trusts the forwarder. Used to exercise execute()
///      end-to-end. Implements the same selectors the forwarder
///      whitelists so that fee extraction can match arguments.
contract MockTarget is ERC2771Context {
    address public lastSender;
    uint256 public lastAmount;

    constructor(address forwarder) ERC2771Context(forwarder) {}

    // signature matches SEL_STAKE: stake(uint256,uint8,uint256)
    function stake(
        uint256,
        /*postId*/
        uint8,
        /*side*/
        uint256 amount
    )
        external
    {
        lastSender = _msgSender();
        lastAmount = amount;
    }

    // signature matches SEL_CREATE_CLAIM: createClaim(string)
    function createClaim(
        string calldata /*content*/
    )
        external
        returns (uint256)
    {
        lastSender = _msgSender();
        return 1;
    }
}

/// @dev A target that does NOT trust the forwarder. Used to test that
///      the forwarder's _isTrustedByTarget check still rejects.
contract UntrustingTarget {
    function stake(uint256, uint8, uint256) external {}
}

// ── Tests ────────────────────────────────────────────────────

contract VerisphereForwarderTest is Test {
    VerisphereForwarder fwd;
    MockVSP vsp;
    MockTarget target;

    address deployer = address(0xDEEDED);
    address treasury = address(0xBEEF);
    address user;
    uint256 userPk;
    address attacker = address(0xBAD);

    uint256 constant INITIAL_FEE_BPS = 50; // 0.5%
    uint256 constant INITIAL_MIN_FEE = 1e17; // 0.1 VSP
    uint256 constant USER_VSP_BALANCE = 1000e18; // 1000 VSP

    function setUp() public {
        (user, userPk) = makeAddrAndKey("user");

        vsp = new MockVSP();
        vsp.mint(user, USER_VSP_BALANCE);

        // Deploy implementation + proxy
        VerisphereForwarder impl = new VerisphereForwarder();
        bytes memory initData = abi.encodeCall(
            VerisphereForwarder.initialize, (address(vsp), treasury, deployer, INITIAL_FEE_BPS, INITIAL_MIN_FEE)
        );
        ERC1967Proxy proxy = new ERC1967Proxy(address(impl), initData);
        fwd = VerisphereForwarder(payable(address(proxy)));

        target = new MockTarget(address(fwd));
        vm.prank(deployer);
        fwd.setAllowedTarget(address(target), true); // v5 (F-1): only allow-listed targets

        // User pre-approves forwarder for fees
        vm.prank(user);
        vsp.approve(address(fwd), type(uint256).max);
    }

    // ── Helper: build & sign a forward request for `data` ────

    function _buildRequest(address to, bytes memory data, uint256 nonce)
        internal
        view
        returns (VerisphereForwarder.ForwardRequestData memory)
    {
        VerisphereForwarder.ForwardRequestData memory r;
        r.from = user;
        r.to = to;
        r.value = 0;
        r.gas = 1_000_000;
        r.deadline = uint48(block.timestamp + 3600);
        r.data = data;
        r.signature = _sign(r, nonce);
        return r;
    }

    function _sign(VerisphereForwarder.ForwardRequestData memory r, uint256 nonce)
        internal
        view
        returns (bytes memory)
    {
        bytes32 typehash = keccak256(
            "ForwardRequest(address from,address to,uint256 value,uint256 gas,uint256 nonce,uint48 deadline,bytes data)"
        );
        bytes32 structHash =
            keccak256(abi.encode(typehash, r.from, r.to, r.value, r.gas, nonce, r.deadline, keccak256(r.data)));
        bytes32 domain = _domainSeparator();
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", domain, structHash));
        (uint8 v, bytes32 sigR, bytes32 sigS) = vm.sign(userPk, digest);
        return abi.encodePacked(sigR, sigS, v);
    }

    function _domainSeparator() internal view returns (bytes32) {
        // Use fwd's helper to read its EIP-712 domain. ERC2771Forwarder
        // inherits EIP712 which exposes eip712Domain().
        (
            bytes1 fields,
            string memory name,
            string memory version,
            uint256 chainId,
            address verifyingContract,
            bytes32 salt,
            uint256[] memory extensions
        ) = fwd.eip712Domain();
        fields;
        salt;
        extensions;
        return keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256(bytes(name)),
                keccak256(bytes(version)),
                chainId,
                verifyingContract
            )
        );
    }

    // ── 1. _disableInitializers ──────────────────────────────

    function test_implementationCannotBeInitialized() public {
        // The constructor sets _initialized = true on the impl, so the
        // bespoke initialize check fires immediately. (See patch16.1.)
        VerisphereForwarder impl = new VerisphereForwarder();
        vm.expectRevert(VerisphereForwarder.AlreadyInitialized.selector);
        impl.initialize(address(vsp), treasury, deployer, 50, 1e17);
    }

    function test_proxyCannotBeReinitialized() public {
        vm.expectRevert(VerisphereForwarder.AlreadyInitialized.selector);
        fwd.initialize(address(vsp), treasury, deployer, 50, 1e17);
    }

    // ── 2. Ownable2Step ──────────────────────────────────────

    function test_proposeAndAcceptOwner() public {
        address newOwner = address(0xCAFE);

        vm.prank(deployer);
        fwd.proposeOwner(newOwner);
        assertEq(fwd.pendingOwner(), newOwner);
        assertEq(fwd.owner(), deployer); // unchanged until accept

        vm.prank(newOwner);
        fwd.acceptOwner();
        assertEq(fwd.owner(), newOwner);
        assertEq(fwd.pendingOwner(), address(0));
    }

    function test_onlyPendingOwnerCanAccept() public {
        vm.prank(deployer);
        fwd.proposeOwner(address(0xCAFE));

        vm.prank(attacker);
        vm.expectRevert(VerisphereForwarder.NotPendingOwner.selector);
        fwd.acceptOwner();
    }

    function test_proposeOwner_canCancel() public {
        vm.startPrank(deployer);
        fwd.proposeOwner(address(0xCAFE));
        fwd.proposeOwner(address(0)); // cancel
        vm.stopPrank();
        assertEq(fwd.pendingOwner(), address(0));
    }

    function test_setOwnerRemoved() public {
        // setOwner no longer exists on the v2 contract. Calling it via
        // raw call should revert (no selector match).
        (bool ok,) = address(fwd).call(abi.encodeWithSignature("setOwner(address)", address(0xCAFE)));
        assertFalse(ok, "setOwner should not exist in v2");
    }

    // ── 3. MAX_FEE_BPS cap ───────────────────────────────────

    function test_maxFeeBpsConstant() public {
        assertEq(fwd.MAX_FEE_BPS(), 1000);
    }

    function test_setFeeConfig_atCap_succeeds() public {
        vm.prank(deployer);
        fwd.setFeeConfig(1000, 1e17, true);
        assertEq(fwd.feeBps(), 1000);
    }

    function test_setFeeConfig_aboveCap_reverts() public {
        vm.prank(deployer);
        vm.expectRevert(abi.encodeWithSelector(VerisphereForwarder.FeeBpsTooHigh.selector, 1001, 1000));
        fwd.setFeeConfig(1001, 1e17, true);
    }

    function test_initialize_aboveCap_reverts() public {
        VerisphereForwarder impl = new VerisphereForwarder();
        bytes memory initData =
            abi.encodeCall(VerisphereForwarder.initialize, (address(vsp), treasury, deployer, 1001, 1e17));
        vm.expectRevert(abi.encodeWithSelector(VerisphereForwarder.FeeBpsTooHigh.selector, 1001, 1000));
        new ERC1967Proxy(address(impl), initData);
    }

    // ── 4. executeBatch fee collection ───────────────────────

    function test_executeBatch_chargesFeePerRequest() public {
        bytes memory stakeData =
            abi.encodeWithSelector(MockTarget.stake.selector, uint256(1), uint8(0), uint256(100e18));

        // Build two requests with consecutive nonces
        VerisphereForwarder.ForwardRequestData[] memory reqs = new VerisphereForwarder.ForwardRequestData[](2);
        reqs[0] = _buildRequest(address(target), stakeData, 0);
        reqs[1] = _buildRequest(address(target), stakeData, 1);

        uint256 treasuryBefore = vsp.balanceOf(treasury);

        // Anyone can submit a batch; relayer is `attacker` to verify
        // the fee is collected from `user` (the request.from), not the
        // submitter.
        vm.prank(attacker);
        fwd.executeBatch(reqs, payable(address(0)));

        uint256 treasuryAfter = vsp.balanceOf(treasury);

        // Each stake of 100e18 at 50bps = 0.5e18 fee, but min is 1e17,
        // so each charges 0.5e18 (above min). Two requests = 1e18 total.
        uint256 expectedFee = ((100e18 * INITIAL_FEE_BPS) / 10_000) * 2;
        assertEq(treasuryAfter - treasuryBefore, expectedFee);
    }

    // ── 5. MAX_MIN_FEE_WEI cap ───────────────────────────────

    function test_setFeeConfig_minFeeAboveCap_reverts() public {
        uint256 cap = fwd.MAX_MIN_FEE_WEI();
        vm.prank(deployer);
        vm.expectRevert(abi.encodeWithSelector(VerisphereForwarder.MinFeeWeiTooHigh.selector, cap + 1, cap));
        fwd.setFeeConfig(50, cap + 1, true);
    }

    // ── 6. setTreasury rejects forwarder address ─────────────

    function test_setTreasury_rejectsForwarder() public {
        vm.prank(deployer);
        vm.expectRevert(VerisphereForwarder.TreasuryIsForwarder.selector);
        fwd.setTreasury(address(fwd));
    }

    function test_setTreasury_rejectsZero() public {
        vm.prank(deployer);
        vm.expectRevert(VerisphereForwarder.ZeroAddress.selector);
        fwd.setTreasury(address(0));
    }

    // ── 7. Rescue functions ──────────────────────────────────

    function test_rescueERC20_onlyOwner() public {
        vsp.mint(address(fwd), 100e18);
        vm.prank(attacker);
        vm.expectRevert(VerisphereForwarder.NotOwner.selector);
        fwd.rescueERC20(IERC20(address(vsp)), attacker, 100e18);
    }

    function test_rescueERC20_succeeds() public {
        vsp.mint(address(fwd), 100e18);
        uint256 deployerBefore = vsp.balanceOf(deployer);
        vm.prank(deployer);
        fwd.rescueERC20(IERC20(address(vsp)), deployer, 100e18);
        assertEq(vsp.balanceOf(deployer) - deployerBefore, 100e18);
    }

    function test_rescueETH_succeeds() public {
        vm.deal(address(fwd), 1 ether);
        uint256 deployerBefore = deployer.balance;
        vm.prank(deployer);
        fwd.rescueETH(payable(deployer), 1 ether);
        assertEq(deployer.balance - deployerBefore, 1 ether);
    }

    function test_rescueETH_zeroAddress() public {
        vm.deal(address(fwd), 1 ether);
        vm.prank(deployer);
        vm.expectRevert(VerisphereForwarder.ZeroAddress.selector);
        fwd.rescueETH(payable(address(0)), 1 ether);
    }

    // ── 8. Storage gap presence ──────────────────────────────
    //   Implicit — if the contract compiles with __gap[50] declared,
    //   it occupies slots 7..56. Verified by `forge inspect storage`
    //   in CI (see patch script).

    // ── 10. nonReentrant ─────────────────────────────────────
    //   Indirect — execute() and executeBatch() both have the modifier.
    //   A direct re-entrancy test would require a target contract that
    //   calls back into fwd.execute() during its handler. Since the
    //   nonce is consumed by OZ before the call (line 282 of
    //   ERC2771Forwarder.sol v5.1.0), the modifier is defense-in-depth;
    //   a behavior test is omitted in favor of the simpler invariant
    //   that the modifier exists (covered by compile + storage layout).

    // ── 11. Selector whitelist (hard reject) ─────────────────

    function test_unknownSelector_executeReverts() public {
        // Use a selector not in the whitelist
        bytes memory badData = abi.encodeWithSignature("transfer(address,uint256)", attacker, 1);
        VerisphereForwarder.ForwardRequestData memory r = _buildRequest(address(target), badData, 0);

        vm.prank(attacker);
        vm.expectRevert(); // UnknownSelector(0xa9059cbb) — transfer
        fwd.execute(r);
    }

    function test_emptyData_reverts() public {
        bytes memory empty = "";
        VerisphereForwarder.ForwardRequestData memory r = _buildRequest(address(target), empty, 0);

        vm.prank(attacker);
        vm.expectRevert(); // UnknownSelector(0x00000000)
        fwd.execute(r);
    }

    function test_estimateFee_unknownSelector_reverts() public {
        bytes memory badData = abi.encodeWithSignature("transfer(address,uint256)", attacker, 1);
        vm.expectRevert();
        fwd.estimateFee(badData);
    }

    function test_whitelistedSelector_succeeds() public {
        bytes memory stakeData =
            abi.encodeWithSelector(MockTarget.stake.selector, uint256(1), uint8(0), uint256(100e18));
        VerisphereForwarder.ForwardRequestData memory r = _buildRequest(address(target), stakeData, 0);

        vm.prank(attacker);
        fwd.execute(r);

        assertEq(target.lastSender(), user);
        assertEq(target.lastAmount(), 100e18);
    }

    // ── Disabled-fee path still gates by selector ────────────

    function test_feeDisabled_unknownSelectorStillReverts() public {
        vm.prank(deployer);
        fwd.setFeeConfig(0, 0, false); // fees off

        bytes memory badData = abi.encodeWithSignature("transfer(address,uint256)", attacker, 1);
        VerisphereForwarder.ForwardRequestData memory r = _buildRequest(address(target), badData, 0);

        vm.prank(attacker);
        vm.expectRevert();
        fwd.execute(r);
    }

    // ── setStake whitelist (patch16.2) ──────────────────────

    function test_setStake_isWhitelisted() public {
        // setStake(postId=1, target=100e18) should be relayable.
        // Fee should be 100e18 * 50bps / 10000 = 0.5e18 (above the
        // 0.1e18 minimum). MockTarget doesn't implement setStake so
        // the inner call would revert at execution, but the FORWARDER
        // should accept the request and collect the fee before
        // delegating. We use estimateFee instead to test the selector
        // recognition + fee math without needing a target that
        // actually implements setStake.
        bytes memory data = abi.encodeWithSignature("setStake(uint256,int256)", uint256(1), int256(100e18));
        uint256 fee = fwd.estimateFee(data);
        assertEq(fee, (100e18 * INITIAL_FEE_BPS) / 10_000);
    }

    function test_setStake_negativeTarget_chargesAbsValue() public {
        // setStake(postId=1, target=-50e18) — the user wants to be in
        // the challenge side at 50 VSP. Fee on abs(-50e18) = 50e18.
        bytes memory data = abi.encodeWithSignature("setStake(uint256,int256)", uint256(1), int256(-50e18));
        uint256 fee = fwd.estimateFee(data);
        assertEq(fee, (50e18 * INITIAL_FEE_BPS) / 10_000);
    }

    function test_setStake_zeroTarget_floorsToMinFee() public {
        // setStake(postId=1, target=0) — withdraw all. abs(0) = 0,
        // so percentage fee is zero. minFeeWei takes over.
        bytes memory data = abi.encodeWithSignature("setStake(uint256,int256)", uint256(1), int256(0));
        uint256 fee = fwd.estimateFee(data);
        assertEq(fee, INITIAL_MIN_FEE);
    }

    function test_setStake_smallAdjustment_floorsToMinFee() public {
        // setStake(postId=1, target=1e18) — small position. fee on
        // abs(1e18) = 1e18 * 50bps / 10000 = 5e15, which is below
        // the 1e17 minimum, so charged 1e17.
        bytes memory data = abi.encodeWithSignature("setStake(uint256,int256)", uint256(1), int256(1e18));
        uint256 fee = fwd.estimateFee(data);
        assertEq(fee, INITIAL_MIN_FEE);
    }

    function test_setStake_truncatedCalldata_returnsZero() public {
        // Calldata too short to contain the int256 target. estimateFee
        // returns 0 from _extractTxValue, then minFee floors it.
        bytes memory data = abi.encodePacked(
            bytes4(keccak256("setStake(uint256,int256)")),
            uint256(1) // only postId, missing target
        );
        uint256 fee = fwd.estimateFee(data);
        assertEq(fee, INITIAL_MIN_FEE);
    }

    // ── F-1 (private disclosure 2026-09): relayer-paid native value drain ──
    // The relay pays request.value from its OWN wallet and OZ requires
    // msg.value == request.value, so an attacker-signed request with a non-zero
    // value and an attacker-chosen `to` drained the relayer EOA. v5 rejects
    // both at the contract layer. PASS == fixed.

    function test_F1_relayerNeverPaysNativeValue() public {
        bytes memory stakeData = abi.encodeWithSelector(MockTarget.stake.selector, 1, 0, 10e18);
        VerisphereForwarder.ForwardRequestData memory r = _buildRequest(address(target), stakeData, 0);
        r.value = 0.5 ether; // attacker-chosen value
        r.signature = _sign(r, 0);
        address relayer = address(0x5E1A);
        vm.deal(relayer, 1 ether);
        uint256 before = relayer.balance;
        vm.prank(relayer);
        vm.expectRevert(abi.encodeWithSelector(VerisphereForwarder.ValueNotAllowed.selector, 0.5 ether));
        fwd.execute{value: 0.5 ether}(r);
        assertEq(relayer.balance, before, "relayer balance untouched");
    }

    function test_F1_unknownTargetRefused() public {
        MockTarget evil = new MockTarget(address(fwd)); // answers isTrustedForwarder, NOT allow-listed
        bytes memory stakeData = abi.encodeWithSelector(MockTarget.stake.selector, 1, 0, 10e18);
        VerisphereForwarder.ForwardRequestData memory r = _buildRequest(address(evil), stakeData, 0);
        vm.prank(attacker);
        vm.expectRevert(abi.encodeWithSelector(VerisphereForwarder.TargetNotAllowed.selector, address(evil)));
        fwd.execute(r);
    }

    function test_F1_allowlistIsOwnerOnly() public {
        vm.prank(attacker);
        vm.expectRevert(VerisphereForwarder.NotOwner.selector);
        fwd.setAllowedTarget(attacker, true);
    }
}
