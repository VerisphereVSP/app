// SPDX-License-Identifier: BUSL-1.1
// Copyright (c) 2025 Verisphere Ltd. All rights reserved.
//
// This contract is a COMMERCIAL SERVICE COMPONENT operated by Verisphere Ltd.
// It is NOT part of the VeriSphere open-source protocol (verisphere/core).
//
// License: Business Source License 1.1 (BUSL-1.1)
// Change Date: 2028-01-01
// Change License: MIT

pragma solidity ^0.8.20;

import "@openzeppelin/contracts/metatx/ERC2771Forwarder.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/proxy/utils/UUPSUpgradeable.sol";
import "@openzeppelin/contracts/proxy/utils/Initializable.sol";
import "@openzeppelin/contracts/utils/math/SignedMath.sol";

/// @title VerisphereForwarder (Upgradeable)
/// @notice Trusted forwarder for gasless meta-transactions (ERC-2771) with
///         a percentage-based VSP relay fee. Deployed behind an ERC1967 proxy.
///
///         The protocol does not require this forwarder. Users may call
///         protocol contracts directly with their own wallet and gas.
///
///         The forwarder is PROTOCOL-ONLY. Only the four whitelisted
///         protocol entry points (createClaim, createLink, stake, withdraw)
///         can be relayed. Unknown selectors revert.
///
///         Owner is a separate Verisphere Ltd Ops Safe (not protocol
///         governance). Two-step ownership transfer via proposeOwner /
///         acceptOwner.
contract VerisphereForwarder is Initializable, ERC2771Forwarder, UUPSUpgradeable {
    using SafeERC20 for IERC20;

    // ── State (stored in proxy) ──────────────────────────────
    //
    // Slot layout. VERIFIED with `forge inspect VerisphereForwarder
    // storage` after compile. DO NOT INSERT OR REORDER.
    //
    // Inherited slots (from EIP712 + Nonces, via ERC2771Forwarder):
    //   slot 0: _nameFallback     (string, EIP712)
    //   slot 1: _versionFallback  (string, EIP712)
    //   slot 2: _nonces           (mapping, Nonces)
    //
    // This contract's own state, identical to v1 for slots 3..8:
    //   slot  3: vspToken         (existing v1)
    //   slot  4: treasury         (existing v1)
    //   slot  5: owner            (existing v1)
    //   slot  6: feeBps           (existing v1)
    //   slot  7: minFeeWei        (existing v1)
    //   slot  8: feeEnabled (bool, offset 0) | _initialized (bool, offset 1)
    //            — packed in slot 8; existing v1 layout. New variables
    //              below intentionally start fresh slots, leaving bytes
    //              [2..31] of slot 8 as the zeros they were post-v1-init.
    //   slot  9: _entered         — NEW v2. uint256 (matches OZ
    //            ReentrancyGuard convention) so it gets its own slot
    //            instead of packing into slot 8. Cost: 1 extra slot;
    //            benefit: layout is unambiguous and easy to reason about.
    //   slot 10: pendingOwner     — NEW v2. Address; own slot for the
    //            same reason.
    //   slot 11..60: __gap[50]    — NEW v2. Reserved for future state
    //            additions without colliding with anything inherited
    //            from ERC2771Forwarder (in case OZ adds state to that
    //            base in a future version).
    //
    // Note: Initializable (added to the base list in v2) uses ERC-7201
    // namespaced storage (a hashed slot, NOT in the linear layout).
    // Adding it as a base did not shift any linear slot indices.

    IERC20 public vspToken;
    address public treasury;
    address public owner;
    uint256 public feeBps;
    uint256 public minFeeWei;
    bool public feeEnabled;
    bool private _initialized;
    uint256 private _entered; // v2: nonReentrant guard (1 = entered, 0 = not)
    address public pendingOwner; // v2: Ownable2Step
    /// v5 (F-1, private disclosure 2026-09): the relay pays `request.value`
    /// from ITS OWN wallet and OZ requires msg.value == request.value, so an
    /// attacker-signed request with a non-zero value and an attacker-chosen
    /// `to` drained the relayer EOA. The protocol takes no native value from
    /// users and has exactly three mutating targets. Both invariants now live
    /// in the contract, where they survive backend rewrites.
    mapping(address => bool) public allowedTarget; // v5, storage appended (slot 11)
    uint256[50] private __gap; // v2: storage gap

    // ── Constants ────────────────────────────────────────────

    /// @dev Hard ceiling on feeBps. Governance can set feeBps within
    ///      [0, MAX_FEE_BPS] via setFeeConfig. Raising the ceiling
    ///      itself requires a contract upgrade (deliberate ceremony).
    uint256 public constant MAX_FEE_BPS = 1000; // 10%

    /// @dev Hard ceiling on minFeeWei. Prevents DOS via
    ///      setFeeConfig(_, huge_minFeeWei, _).
    uint256 public constant MAX_MIN_FEE_WEI = 10 * 1e18; // 10 VSP

    // ── Whitelisted selectors ────────────────────────────────
    //
    // Hardcoded. Adding/removing a selector requires a contract
    // upgrade. Unknown selectors revert in execute() / executeBatch().

    bytes4 private constant SEL_CREATE_CLAIM = bytes4(keccak256("createClaim(string)"));
    bytes4 private constant SEL_CREATE_LINK = bytes4(keccak256("createLink(uint256,uint256,bool)"));
    bytes4 private constant SEL_STAKE = bytes4(keccak256("stake(uint256,uint8,uint256)"));
    bytes4 private constant SEL_WITHDRAW = bytes4(keccak256("withdraw(uint256,uint8,uint256,bool)"));
    bytes4 private constant SEL_SET_STAKE = bytes4(keccak256("setStake(uint256,int256)"));

    // ── Events ───────────────────────────────────────────────

    event FeeCollected(address indexed user, uint256 fee, uint256 txValue);
    event FeeConfigUpdated(uint256 feeBps, uint256 minFeeWei, bool enabled);
    event TreasuryUpdated(address indexed oldTreasury, address indexed newTreasury);
    event VspTokenUpdated(address indexed oldToken, address indexed newToken); // v3
    event OwnerProposed(address indexed currentOwner, address indexed pendingOwner);
    event OwnerAccepted(address indexed oldOwner, address indexed newOwner);
    event ERC20Rescued(address indexed token, address indexed to, uint256 amount);
    event ETHRescued(address indexed to, uint256 amount);

    // ── Errors ───────────────────────────────────────────────

    error NotOwner();
    error NotPendingOwner();
    error ZeroAddress();
    error AlreadyInitialized();
    error FeeBpsTooHigh(uint256 got, uint256 max);
    error MinFeeWeiTooHigh(uint256 got, uint256 max);
    error TreasuryIsForwarder();
    error NotAContract(address token); // v3
    error BatchMustBeAtomic(); // v4 (C1)
    error ValueNotAllowed(uint256 value); // v5 (F-1)
    error TargetNotAllowed(address to); // v5 (F-1)
    event AllowedTargetSet(address indexed target, bool allowed); // v5
    error UnknownSelector(bytes4 sel);
    error Reentrant();

    // ── Constructor (implementation only) ────────────────────

    /// @dev Constructor sets up EIP-712 domain for the implementation
    ///      and locks the implementation against direct initialization.
    ///
    ///      EIP-712 note: when called via proxy, EIP712._domainSeparatorV4()
    ///      detects the address mismatch and recomputes using the proxy
    ///      address. The "VerisphereForwarder" name MUST stay constant
    ///      across upgrades or signed forward requests will become
    ///      unverifiable (the EIP-712 domain hash would change).
    ///
    ///      The implementation is locked against direct initialization
    ///      via TWO mechanisms:
    ///
    ///        (a) _disableInitializers() locks any function using OZ's
    ///            `initializer` or `reinitializer` modifier. We don't
    ///            currently have any, but this future-proofs the impl
    ///            against later versions that might add OZ-style
    ///            initializers.
    ///
    ///        (b) Setting `_initialized = true` at construction locks
    ///            the bespoke `initialize` function below, which is
    ///            gated on this flag rather than on OZ's namespaced
    ///            storage. Without this, an attacker could call
    ///            `impl.initialize(...)` directly on a fresh impl and
    ///            become "owner" of the impl — harmless (the impl has
    ///            no users) but untidy.
    ///
    ///      The proxy is unaffected: constructors run on the
    ///      implementation contract; the proxy's storage is set by
    ///      the delegatecall to initialize() at proxy deployment time.
    constructor() ERC2771Forwarder("VerisphereForwarder") {
        _disableInitializers();
        _initialized = true;
    }

    // ── Initializer (called once on proxy at v1 deploy; never again) ──

    /// @notice Initialize the proxy with forwarder configuration.
    ///         Called once during proxy deployment via ERC1967Proxy
    ///         constructor. Subsequent calls revert.
    ///
    ///         This is the BESPOKE v1 initializer. It does not use OZ's
    ///         `initializer` modifier because that would require a
    ///         storage migration to set OZ's Initializable slot — which
    ///         is unnecessary since the bespoke _initialized flag
    ///         already gates re-entry.
    function initialize(address vspToken_, address treasury_, address owner_, uint256 feeBps_, uint256 minFeeWei_)
        external
    {
        if (_initialized) revert AlreadyInitialized();
        if (vspToken_ == address(0)) revert ZeroAddress();
        if (treasury_ == address(0)) revert ZeroAddress();
        if (owner_ == address(0)) revert ZeroAddress();
        if (feeBps_ > MAX_FEE_BPS) revert FeeBpsTooHigh(feeBps_, MAX_FEE_BPS);
        if (minFeeWei_ > MAX_MIN_FEE_WEI) revert MinFeeWeiTooHigh(minFeeWei_, MAX_MIN_FEE_WEI);
        _initialized = true;
        vspToken = IERC20(vspToken_);
        treasury = treasury_;
        owner = owner_;
        feeBps = feeBps_;
        minFeeWei = minFeeWei_;
        feeEnabled = true;
    }

    // ── UUPS authorization ───────────────────────────────────

    function _authorizeUpgrade(address) internal view override {
        if (msg.sender != owner) revert NotOwner();
    }

    // ── Modifiers ────────────────────────────────────────────

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    /// @dev Bespoke nonReentrant. _entered uses uint256 (matches OZ's
    ///      ReentrancyGuard convention) and lives in its own slot —
    ///      see storage-layout comment above for rationale.
    modifier nonReentrant() {
        if (_entered == 1) revert Reentrant();
        _entered = 1;
        _;
        _entered = 0;
    }

    // ── Fee extraction ───────────────────────────────────────

    /// @dev Returns the on-protocol "transaction value" used to compute
    ///      the percentage fee, OR reverts if the selector is unknown.
    ///      Soft protocol-only enforcement: any non-whitelisted call
    ///      cannot be relayed through this forwarder.
    function _extractTxValue(bytes calldata data) internal pure returns (uint256) {
        if (data.length < 4) revert UnknownSelector(bytes4(0));
        bytes4 sel = bytes4(data[:4]);

        if (sel == SEL_CREATE_CLAIM || sel == SEL_CREATE_LINK) {
            return 1e18;
        }
        if (sel == SEL_STAKE || sel == SEL_WITHDRAW) {
            // amount is the third arg of stake(uint256,uint8,uint256)
            // and the third arg of withdraw(uint256,uint8,uint256,bool).
            // Calldata layout: 4 bytes selector + 3*32 bytes args before
            // the amount. Amount lives at bytes [68, 100).
            if (data.length < 100) return 0;
            return uint256(bytes32(data[68:100]));
        }
        if (sel == SEL_SET_STAKE) {
            // setStake(uint256 postId, int256 target).
            // Calldata layout: 4 bytes selector + uint256 postId (32)
            // + int256 target (32). target is at bytes [36, 68).
            // Fee is charged on abs(target) — overcharges small
            // adjustments, undercharges no one. SignedMath.abs handles
            // int256.min cleanly; for our scale that case is impossible
            // anyway (target is VSP wei, far below 2^200).
            if (data.length < 68) return 0;
            int256 target = int256(uint256(bytes32(data[36:68])));
            return SignedMath.abs(target);
        }
        revert UnknownSelector(sel);
    }

    function _collectFee(address user, bytes calldata innerData) internal {
        if (!feeEnabled || feeBps == 0) {
            // Even when fees are disabled, the selector check still
            // applies — _extractTxValue reverts on unknown selectors.
            // That keeps the protocol-only gate active regardless of
            // fee configuration.
            _extractTxValue(innerData);
            return;
        }

        uint256 txValue = _extractTxValue(innerData);
        uint256 fee = (txValue * feeBps) / 10_000;
        if (fee < minFeeWei) fee = minFeeWei;

        // Pull fee from user. SafeERC20 reverts on failure / non-standard
        // tokens; cleaner than the previous bool-return pattern.
        vspToken.safeTransferFrom(user, treasury, fee);
        emit FeeCollected(user, fee, txValue);
    }

    /// @notice View helper for relayers / clients estimating gas costs.
    ///         Reverts on unknown selectors — a relayer can use this to
    ///         pre-validate that a request will be accepted by the
    ///         forwarder before paying gas to submit it.
    function estimateFee(bytes calldata innerData) external view returns (uint256) {
        if (!feeEnabled || feeBps == 0) {
            // Still gate via selector check. _extractTxValue reverts
            // on unknown selectors regardless of fee state.
            _extractTxValue(innerData);
            return 0;
        }
        uint256 txValue = _extractTxValue(innerData);
        uint256 fee = (txValue * feeBps) / 10_000;
        if (fee < minFeeWei) fee = minFeeWei;
        return fee;
    }

    // ── Execute overrides ────────────────────────────────────

    function execute(ForwardRequestData calldata request) public payable override nonReentrant {
        _checkTarget(request); // v5 (F-1)
        _collectFee(request.from, request.data);
        super.execute(request);
    }

    /// @dev v5 (F-1): no native value ever, and only allow-listed protocol targets.
    function _checkTarget(ForwardRequestData calldata request) internal view {
        if (request.value != 0) revert ValueNotAllowed(request.value);
        if (!allowedTarget[request.to]) revert TargetNotAllowed(request.to);
    }

    /// @notice v5: owner-managed target allowlist (the deployed protocol contracts).
    function setAllowedTarget(address target, bool allowed) external onlyOwner {
        if (target == address(0)) revert ZeroAddress();
        allowedTarget[target] = allowed;
        emit AllowedTargetSet(target, allowed);
    }

    /// @notice Override of executeBatch to enforce per-request fee
    ///         collection. Without this override, a relayer could
    ///         submit a batch and pay zero fees — a fee-evasion hole
    ///         in the v1 contract.
    function executeBatch(ForwardRequestData[] calldata requests, address payable refundReceiver)
        public
        payable
        override
        nonReentrant
    {
        // v4 — C1 (security review 2026-09): the previous version pulled every
        // request's fee BEFORE OZ validated signatures, and OZ's executeBatch
        // with a non-zero refundReceiver SKIPS invalid-signature requests
        // instead of reverting — so a forged request's fee transferFrom stuck.
        // Because the fee is derived from attacker-controlled calldata, that
        // was an unauthenticated drain of any user's allowance (min(balance,
        // permit) per victim per batch). Two independent guards now:
        // The fix: batches are forced ATOMIC (refundReceiver == 0). In atomic
        // mode OZ validates each request immediately before executing it and
        // REVERTS THE WHOLE TRANSACTION on any invalid one — so every fee
        // pull below is unwound together with the forged request, and the
        // calldata a surviving fee was computed from is, by construction,
        // signed calldata. (Pre-validating all requests up front is NOT an
        // option: a legitimate batch of two requests from one signer carries
        // nonces N and N+1, and N+1 cannot validate until N has executed.)
        if (refundReceiver != address(0)) revert BatchMustBeAtomic();
        for (uint256 i; i < requests.length; ++i) {
            _checkTarget(requests[i]); // v5 (F-1)
            _collectFee(requests[i].from, requests[i].data);
        }
        super.executeBatch(requests, refundReceiver);
    }

    // ── Admin ────────────────────────────────────────────────

    function setFeeConfig(uint256 feeBps_, uint256 minFeeWei_, bool enabled_) external onlyOwner {
        if (feeBps_ > MAX_FEE_BPS) revert FeeBpsTooHigh(feeBps_, MAX_FEE_BPS);
        if (minFeeWei_ > MAX_MIN_FEE_WEI) revert MinFeeWeiTooHigh(minFeeWei_, MAX_MIN_FEE_WEI);
        feeBps = feeBps_;
        minFeeWei = minFeeWei_;
        feeEnabled = enabled_;
        emit FeeConfigUpdated(feeBps_, minFeeWei_, enabled_);
    }

    /// @notice v3 (patch_fw_token): re-point the fee asset after a token
    ///         genesis. The 2026-09 Fuji genesis replaced VSPToken while this
    ///         forwarder — deployed separately, initialized once — kept
    ///         pulling fees from the retired token, so every meta-tx reverted
    ///         ERC20InsufficientAllowance on a token users no longer approve.
    ///         Storage layout untouched (function + event only). Intended to
    ///         be invoked atomically via upgradeToAndCall.
    function setVspToken(address newToken) external onlyOwner {
        if (newToken == address(0)) revert ZeroAddress();
        if (newToken.code.length == 0) revert NotAContract(newToken);
        address old = address(vspToken);
        vspToken = IERC20(newToken);
        emit VspTokenUpdated(old, newToken);
    }

    function setTreasury(address treasury_) external onlyOwner {
        if (treasury_ == address(0)) revert ZeroAddress();
        if (treasury_ == address(this)) revert TreasuryIsForwarder();
        address old = treasury;
        treasury = treasury_;
        emit TreasuryUpdated(old, treasury_);
    }

    // ── Ownable2Step ─────────────────────────────────────────

    /// @notice Begin owner transfer. The proposed new owner must call
    ///         acceptOwner() to complete the transfer. Setting
    ///         pendingOwner = address(0) cancels a pending proposal.
    function proposeOwner(address newOwner) external onlyOwner {
        // address(0) is allowed here — used to cancel a pending proposal.
        pendingOwner = newOwner;
        emit OwnerProposed(owner, newOwner);
    }

    /// @notice Complete owner transfer. Caller must be the proposed
    ///         pendingOwner. Cleared after acceptance.
    function acceptOwner() external {
        if (msg.sender != pendingOwner) revert NotPendingOwner();
        address old = owner;
        owner = pendingOwner;
        pendingOwner = address(0);
        emit OwnerAccepted(old, msg.sender);
    }

    // ── Rescue ───────────────────────────────────────────────

    /// @notice Emergency withdrawal of ERC-20 tokens accidentally sent
    ///         to this contract. The forwarder never holds tokens as
    ///         part of normal operation — fees flow user -> treasury
    ///         directly, never transit the forwarder.
    function rescueERC20(IERC20 token, address to, uint256 amount) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        token.safeTransfer(to, amount);
        emit ERC20Rescued(address(token), to, amount);
    }

    /// @notice Emergency withdrawal of ETH/AVAX accidentally sent
    ///         (or stuck in a refund path edge case) to this contract.
    function rescueETH(address payable to, uint256 amount) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        (bool ok,) = to.call{value: amount}("");
        require(ok, "rescueETH: send failed");
        emit ETHRescued(to, amount);
    }
}
