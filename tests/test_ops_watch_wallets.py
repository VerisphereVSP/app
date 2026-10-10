"""patch_ops_watch_wallets — the sampler knows every system wallet, the real services, and
the settlement pass leaves a record. Pure-unit: no RPC, no database."""
import os

import pytest

import balance_sampler as bs
import settle_keeper as sk

A1 = "0x19f6f9053c1085BCd4c4872845636153c90e4Ebf"
A2 = "0x6Bd8443E21310DCef4e8D7d4a70a1Cb20Ffe9bE6"
A3 = "0xF489bed8C00Aa34B9ceEc8691FEC08c0a22c3AD0"


def test_parse_watch_wallets_accepts_good_and_skips_bad():
    got = bs._parse_watch_wallets(f"deployer:{A1}, signer_lavey:{A2},nocolon,bad_label-!:{A3},short:0x1234,ok2:{A3}")
    assert got == {"deployer": A1, "signer_lavey": A2, "ok2": A3}
    assert bs._parse_watch_wallets("") == {} and bs._parse_watch_wallets(None) == {}


def test_components_fixed_roles_plus_watch_list(monkeypatch):
    for env in ("RELAY_ADDRESS", "KEEPER_ADDRESS", "TREASURY_ADDRESS", "HOT_SAFE_ADDRESS",
                "GUARDIAN_SAFE_ADDRESS", "VSP_COLD_RESERVE_ADDRESS", "VSP_WATCH_WALLETS"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(bs, "MM_ADDRESS", "")
    monkeypatch.setattr(bs, "WORKER_ADDRESS", "")
    monkeypatch.setenv("RELAY_ADDRESS", A1)
    monkeypatch.setenv("KEEPER_ADDRESS", A2)
    monkeypatch.setenv("TREASURY_ADDRESS", A3)
    monkeypatch.setenv("HOT_SAFE_ADDRESS", "not-an-address")           # skipped, not fatal
    monkeypatch.setenv("VSP_WATCH_WALLETS", f"deployer:{A1},relay:{A2}")  # 'relay' must not override the fixed role
    got = bs._components()
    assert got == {"relay": A1, "keeper": A2, "cold_safe": A3, "deployer": A1}


def test_components_empty_when_nothing_configured(monkeypatch):
    for env in ("RELAY_ADDRESS", "KEEPER_ADDRESS", "TREASURY_ADDRESS", "HOT_SAFE_ADDRESS",
                "GUARDIAN_SAFE_ADDRESS", "VSP_COLD_RESERVE_ADDRESS", "VSP_WATCH_WALLETS"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(bs, "MM_ADDRESS", "")
    monkeypatch.setattr(bs, "WORKER_ADDRESS", "")
    assert bs._components() == {}


def test_http_probes_default_is_the_prod_edge_set(monkeypatch):
    monkeypatch.delenv("SVC_HTTP_PROBES", raising=False)
    names = [n for n, _ in bs._http_probes()]
    assert names == ["app", "caddy", "grafana"]
    assert "frontend" not in names and "treasury_worker" not in names


def test_http_probes_override(monkeypatch):
    monkeypatch.setenv("SVC_HTTP_PROBES", "app=http://app:8070/healthz, grafana=http://grafana:3000/api/health,,junk")
    assert bs._http_probes() == [("app", "http://app:8070/healthz"), ("grafana", "http://grafana:3000/api/health")]


def test_token_components_default_and_override(monkeypatch):
    monkeypatch.delenv("VSP_WATCH_TOKEN_COMPONENTS", raising=False)
    assert bs._token_components() == {"cold_safe", "hot_safe", "guardian_safe"}
    monkeypatch.setenv("VSP_WATCH_TOKEN_COMPONENTS", "cold_safe, deployer")
    assert bs._token_components() == {"cold_safe", "deployer"}


class _FakeDB:
    def __init__(self):
        self.rows, self.committed, self.closed = [], False, False

    def execute(self, stmt, params=None):
        self.rows.append(params)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_record_pass_writes_three_metrics_with_epoch_label():
    db = _FakeDB()
    sk._record_pass(lambda: db, 20735, 4, 0, 0)
    metrics = {r["m"]: (r["v"], r["l"]) for r in db.rows}
    assert set(metrics) == {"settle_pass_settled", "settle_pass_failed", "settle_pass_pending"}
    assert metrics["settle_pass_settled"][0] == 4.0 and metrics["settle_pass_failed"][0] == 0.0
    assert '"epoch": 20735' in metrics["settle_pass_settled"][1]
    assert db.committed and db.closed


def test_record_pass_never_raises():
    def boom():
        raise RuntimeError("db down")
    sk._record_pass(boom, 1, 0, 0, 0)  # must swallow
