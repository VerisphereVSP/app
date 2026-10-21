# app/tests/test_fee_collector_contract.py — patch_fee_collector
"""
The FeeCollector contract as the relay's fee spender: the route decision (contract / legacy), the ref
bytes, what /relay/config and the permit allow-list advertise with and without FEE_COLLECTOR_ADDRESS,
and that the worker builds a `collect` call (not a transferFrom) when the user's allowance names the
contract. Pure unit: config via monkeypatch, web3 contract calls stubbed, no chain.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fee_collector as fcmod  # noqa: E402

FC = "0x00000000000000000000000000000000000000fc"
RELAY = "0x000000000000000000000000000000000000ae1a"
USER = "0x000000000000000000000000000000000000ca11"
TREASURY = "0x00000000000000000000000000000000000000c0"
TOKEN = "0x0000000000000000000000000000000000000070"
ONE = 10**18


class TestRoute:
    def test_no_contract_means_legacy(self):
        assert fcmod.collect_route("", 0, 0, ONE) == "legacy"
        assert fcmod.collect_route("", 10 * ONE, 10 * ONE, ONE) == "legacy"

    def test_contract_when_its_allowance_covers(self):
        assert fcmod.collect_route(FC, ONE, 0, ONE) == "contract"
        assert fcmod.collect_route(FC, 100 * ONE, 100 * ONE, ONE) == "contract"  # both present: contract wins

    def test_legacy_only_for_not_yet_repermitted_users(self):
        assert fcmod.collect_route(FC, 0, 100 * ONE, ONE) == "legacy"
        assert fcmod.collect_route(FC, ONE - 1, 100 * ONE, ONE) == "legacy"

    def test_neither_covers_attempts_the_contract_so_the_revert_is_recorded(self):
        assert fcmod.collect_route(FC, 0, 0, ONE) == "contract"

    def test_ref_is_the_action_hash_as_bytes32(self):
        h = "0x" + "ab" * 32
        assert fcmod._ref_bytes32(h) == bytes.fromhex("ab" * 32)
        assert len(fcmod._ref_bytes32("0x12")) == 32 and fcmod._ref_bytes32("0x12")[-1] == 0x12
        assert fcmod._ref_bytes32("") == b"\0" * 32
        assert fcmod._ref_bytes32("not-hex") == b"\0" * 32


class _Fn:
    def __init__(self, name, args, log):
        self.name, self.args, self.log = name, args, log

    def call(self):
        return self.log["allowance"][self.args[1].lower()]

    def build_transaction(self, params):
        self.log["built"].append((self.name, self.args, params))
        return {"fn": self.name, "args": self.args, **params}


class _Functions:
    def __init__(self, log):
        self.log = log

    def __getattr__(self, name):
        return lambda *args: _Fn(name, args, self.log)


class _Contract:
    def __init__(self, address, abi, log):
        self.address, self.abi, self.functions = address, abi, _Functions(log)


class _Eth:
    def __init__(self, log):
        self.log = log

    def contract(self, address, abi):
        self.log["contracts"].append(address)
        return _Contract(address, abi, self.log)


class _W3:
    def __init__(self, log):
        self.eth = _Eth(log)


class _Account:
    address = RELAY


def _wallet(log):
    sent = []
    return (_W3(log), _Account(), lambda tx: sent.append(tx) or "0xfee", RuntimeError), sent


@pytest.fixture
def cfg(monkeypatch):
    import config
    monkeypatch.setattr(config, "RELAY_FEE_COLLECT_GAS", 60000)
    monkeypatch.setattr(config, "RELAY_FEE_COLLECT_GAS_CONTRACT", 90000)
    return config


class TestSend:
    def test_contract_path_builds_collect_with_ref(self, cfg, monkeypatch):
        monkeypatch.setattr(cfg, "FEE_COLLECTOR_ADDRESS", FC)
        log = {"allowance": {FC: 100 * ONE, RELAY: 0}, "built": [], "contracts": []}
        wallet, sent = _wallet(log)
        h = fcmod._send_collect(USER, ONE // 10, TREASURY, TOKEN, wallet, tx_hash="0x" + "cd" * 32)
        assert h == "0xfee"
        name, args, params = log["built"][-1]
        assert name == "collect"
        assert args[0].lower() == USER and args[1] == ONE // 10 and args[2] == bytes.fromhex("cd" * 32)
        assert params["gas"] == 90000 and params["from"] == RELAY
        assert sent and sent[0]["fn"] == "collect"

    def test_legacy_user_still_collected_by_transfer_from(self, cfg, monkeypatch):
        monkeypatch.setattr(cfg, "FEE_COLLECTOR_ADDRESS", FC)
        log = {"allowance": {FC: 0, RELAY: 100 * ONE}, "built": [], "contracts": []}
        wallet, sent = _wallet(log)
        fcmod._send_collect(USER, ONE // 10, TREASURY, TOKEN, wallet, tx_hash="0x01")
        name, args, params = log["built"][-1]
        assert name == "transferFrom" and args[1].lower() == TREASURY and params["gas"] == 60000

    def test_without_contract_no_allowance_reads_and_transfer_from(self, cfg, monkeypatch):
        monkeypatch.setattr(cfg, "FEE_COLLECTOR_ADDRESS", "")
        log = {"allowance": {}, "built": [], "contracts": []}
        wallet, sent = _wallet(log)
        fcmod._send_collect(USER, ONE, TREASURY, TOKEN, wallet)
        assert log["built"][-1][0] == "transferFrom" and len(log["contracts"]) == 1


class TestAdvertised:
    """relay.py needs a live RPC at import (relay_wallet), so — as test_relay_gas_fee does — the relay is checked by
    its source; relay_gateway's helper is importable and checked live."""

    def test_relay_config_names_the_contract_when_set(self, cfg, monkeypatch):
        monkeypatch.setattr(cfg, "FEE_COLLECTOR_ADDRESS", FC)
        import relay_gateway
        assert relay_gateway._fee_collector() == FC

    def test_relay_config_falls_back_to_the_relay_wallet(self, cfg, monkeypatch):
        monkeypatch.setattr(cfg, "FEE_COLLECTOR_ADDRESS", "")
        import relay_gateway
        monkeypatch.setitem(sys.modules, "relay_wallet", type("M", (), {"account": _Account()}))
        assert relay_gateway._fee_collector() == RELAY

    def test_relay_source_routes_the_collector_through_the_contract(self):
        src = (ROOT / "relay.py").read_text()
        # the permit allow-list and the fee policy both go through _collector_address(), which prefers the contract
        assert "collector = _collector_address().lower()" in src
        assert "FEE_COLLECTOR_ADDRESS if FEE_COLLECTOR_ADDRESS else _relay_account.address" in src
        # a stale client's permit to the relay wallet stays accepted during the transition
        assert "fwd_addr, collector, legacy" in src

    def test_relay_collector_logic_in_isolation(self, cfg, monkeypatch):
        import ast
        src = (ROOT / "relay.py").read_text()
        tree = ast.parse(src)
        fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_collector_address"]
        ns = {"_relay_account": _Account()}
        exec(compile(ast.Module(body=fn, type_ignores=[]), "relay-slice", "exec"), ns)
        monkeypatch.setattr(cfg, "FEE_COLLECTOR_ADDRESS", FC)
        assert ns["_collector_address"]() == FC
        monkeypatch.setattr(cfg, "FEE_COLLECTOR_ADDRESS", "")
        assert ns["_collector_address"]() == RELAY


class TestPreflightAllowance:
    """patch_fee_collector_2: the relay's pre-flight check must read the allowance that will pay the fee."""

    def _alw(self, table):
        return lambda u, s: table.get((u.lower(), s.lower()), 0)

    def test_contract_allowance_covers(self):
        a = self._alw({(USER, FC): 100 * ONE})
        assert fcmod.fee_allowance_wei(a, USER, FC, RELAY, ONE // 10) == 100 * ONE

    def test_legacy_allowance_covers_a_not_yet_repermitted_user(self):
        a = self._alw({(USER, RELAY): 100 * ONE})
        assert fcmod.fee_allowance_wei(a, USER, FC, RELAY, ONE // 10) == 100 * ONE

    def test_neither_covers(self):
        a = self._alw({(USER, FC): ONE // 100, (USER, RELAY): ONE // 50})
        assert fcmod.fee_allowance_wei(a, USER, FC, RELAY, ONE // 10) == ONE // 50  # the larger, still short

    def test_legacy_mode_reads_the_relay_once(self):
        calls = []
        def a(u, s):
            calls.append(s); return 5 * ONE
        assert fcmod.fee_allowance_wei(a, USER, RELAY, RELAY, ONE) == 5 * ONE
        assert calls == [RELAY]

    def test_relay_source_uses_the_helper_not_the_relay_wallet_alone(self):
        src = (ROOT / "relay.py").read_text()
        assert "fee_allowance_wei(" in src
        assert "vsp_c.functions.allowance(user_addr, _relay_account.address).call()" not in src
