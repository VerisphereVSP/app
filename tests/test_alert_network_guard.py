# app/tests/test_alert_network_guard.py — patch_alert_network_guard
"""
Every alert is prefixed with its network, and only networks listed in ALERT_NETWORKS (default: mainnet)
deliver; a Fuji worker with a Telegram token configured by mistake logs instead of posting.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import notify  # noqa: E402


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "_deliver_webhooks", lambda payload: sent.append(("wh", payload)))
    monkeypatch.setattr(notify, "_deliver_telegram", lambda text: sent.append(("tg", text)))
    monkeypatch.setattr(notify, "_deliver_email", lambda kind, text: sent.append(("em", text)))
    monkeypatch.delenv("ALERT_SINK_MIN_INTERVAL_SEC", raising=False)
    return sent


def test_fuji_is_suppressed_by_default(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(notify, "_network", lambda: "fuji")
    monkeypatch.delenv("ALERT_NETWORKS", raising=False)
    notify.send_alert("keeper_low_balance", "sMax keeper gas balance 0.5000 AVAX below threshold")
    assert sent == []


def test_mainnet_delivers_with_prefix(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(notify, "_network", lambda: "mainnet")
    monkeypatch.delenv("ALERT_NETWORKS", raising=False)
    notify.send_alert("keeper_low_balance", "sMax keeper gas balance 0.5000 AVAX below threshold", address="0xabc")
    kinds = [k for k, _ in sent]
    assert kinds == ["wh", "tg", "em"]
    assert sent[0][1]["message"].startswith("[mainnet] ")
    assert "[mainnet] sMax keeper" in sent[1][1]


def test_allow_list_and_wildcard(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(notify, "_network", lambda: "fuji")
    monkeypatch.setenv("ALERT_NETWORKS", "mainnet,fuji")
    notify.send_alert("x", "m")
    assert len(sent) == 3 and "[fuji] m" in sent[1][1]
    sent.clear()
    monkeypatch.setenv("ALERT_NETWORKS", "*")
    notify.send_alert("x", "m")
    assert len(sent) == 3


def test_network_from_config_name():
    assert notify._network() in ("fuji", "mainnet") or notify._network().startswith("chain-")
