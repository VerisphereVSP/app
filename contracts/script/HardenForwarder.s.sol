// SPDX-License-Identifier: BUSL-1.1
pragma solidity ^0.8.20;

import "forge-std/Script.sol";
import "../VerisphereForwarder.sol";

/// @notice F-1 (private disclosure 2026-09): upgrade the forwarder to v5 and
///         allow-list the three protocol targets. Env: PRIVATE_KEY (owner),
///         FORWARDER_ADDRESS, POST_REGISTRY_ADDRESS, STAKE_ENGINE_ADDRESS,
///         LINK_GRAPH_ADDRESS, MAINNET_UPGRADE_CONFIRM on 43114.
contract HardenForwarder is Script {
    function run() external {
        if (block.chainid == 43114) {
            require(
                vm.envOr("MAINNET_UPGRADE_CONFIRM", uint256(0)) == 1,
                "HardenForwarder: MAINNET_UPGRADE_CONFIRM=1 required"
            );
        }
        uint256 pk = vm.envUint("PRIVATE_KEY");
        VerisphereForwarder fw = VerisphereForwarder(vm.envAddress("FORWARDER_ADDRESS"));
        require(fw.owner() == vm.addr(pk), "HardenForwarder: broadcaster is not the owner");
        address[3] memory targets = [
            vm.envAddress("POST_REGISTRY_ADDRESS"),
            vm.envAddress("STAKE_ENGINE_ADDRESS"),
            vm.envAddress("LINK_GRAPH_ADDRESS")
        ];
        for (uint256 i = 0; i < 3; i++) {
            require(targets[i].code.length > 0, "HardenForwarder: target has no code");
        }

        vm.startBroadcast(pk);
        VerisphereForwarder impl = new VerisphereForwarder();
        fw.upgradeToAndCall(address(impl), "");
        for (uint256 i = 0; i < 3; i++) {
            if (!fw.allowedTarget(targets[i])) fw.setAllowedTarget(targets[i], true);
        }
        vm.stopBroadcast();

        for (uint256 i = 0; i < 3; i++) {
            require(fw.allowedTarget(targets[i]), "HardenForwarder: allowlist not set");
        }
        console.log("v5 impl:", address(impl));
        console.log("HARDEN COMPLETE: value==0 enforced, 3 targets allow-listed");
    }
}
