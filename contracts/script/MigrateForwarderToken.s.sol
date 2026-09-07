// SPDX-License-Identifier: BUSL-1.1
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../VerisphereForwarder.sol";

/// @notice patch_fw_token: upgrade the forwarder proxy to v3 and re-point its
///         fee asset at the post-genesis VSPToken in ONE transaction
///         (upgradeToAndCall + setVspToken). Optionally re-points treasury.
///
/// Env:
///   DEPLOYER_PRIVATE_KEY  must be the proxy's owner()
///   FORWARDER_ADDRESS     the proxy
///   VSP_TOKEN_ADDRESS     the current VSPToken (code required)
///   TREASURY_ADDRESS      optional; if set and different, setTreasury() too
///   MAINNET_UPGRADE_CONFIRM  must be 1 on chainid 43114
contract MigrateForwarderToken is Script {
    function run() external {
        if (block.chainid == 43114) {
            require(
                vm.envOr("MAINNET_UPGRADE_CONFIRM", uint256(0)) == 1,
                "MigrateForwarderToken: mainnet requires MAINNET_UPGRADE_CONFIRM=1"
            );
        }
        uint256 pk = vm.envUint("DEPLOYER_PRIVATE_KEY");
        address sender = vm.addr(pk);
        address proxy = vm.envAddress("FORWARDER_ADDRESS");
        address newToken = vm.envAddress("VSP_TOKEN_ADDRESS");
        address newTreasury = vm.envOr("TREASURY_ADDRESS", address(0));
        require(newToken.code.length > 0, "MigrateForwarderToken: VSP_TOKEN_ADDRESS has no code");

        VerisphereForwarder fw = VerisphereForwarder(proxy);
        require(fw.owner() == sender, "MigrateForwarderToken: broadcaster is not the forwarder owner");
        address oldToken = address(fw.vspToken());
        console.log("forwarder proxy:", proxy);
        console.log("fee token before:", oldToken);
        console.log("treasury before:", fw.treasury());

        vm.startBroadcast(pk);
        if (oldToken == newToken) {
            console.log("fee token already current - SKIP upgrade");
        } else {
            VerisphereForwarder impl = new VerisphereForwarder();
            fw.upgradeToAndCall(address(impl), abi.encodeCall(VerisphereForwarder.setVspToken, (newToken)));
            console.log("upgraded to v3 impl:", address(impl));
        }
        if (newTreasury != address(0) && fw.treasury() != newTreasury) {
            fw.setTreasury(newTreasury);
            console.log("treasury re-pointed:", newTreasury);
        }
        vm.stopBroadcast();

        require(address(fw.vspToken()) == newToken, "MigrateForwarderToken: post-check failed");
        console.log("fee token after:", address(fw.vspToken()));
        console.log("treasury after:", fw.treasury());
        console.log("MIGRATION COMPLETE");
    }
}
