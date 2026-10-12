# tests/test_relay_pause.py — patch_relay_pause
"""The operator's relay pause: env knob or flag file -> /api/relay/async refuses with 503 and the message; nothing
else changes. Pure unit: config via monkeypatch, the flag file in tmp_path, the endpoint called as a coroutine with
the chain never touched (the pause check runs first)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import relay_pause  # noqa: E402


@pytest.fixture(autouse=True)
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RELAY_PAUSED", False)
    monkeypatch.setattr(config, "RELAY_PAUSE_FILE", str(tmp_path / "relay.paused"))
    monkeypatch.setattr(config, "RELAY_PAUSE_MESSAGE", "paused for the test")
    relay_pause._announced = None
    yield


def test_not_paused_by_default():
    assert relay_pause.paused_reason() is None


def test_env_knob_pauses(monkeypatch):
    monkeypatch.setattr(config, "RELAY_PAUSED", True)
    assert relay_pause.paused_reason() == "paused for the test"


def test_flag_file_pauses_and_resumes(tmp_path):
    flag = Path(config.RELAY_PAUSE_FILE)
    assert relay_pause.paused_reason() is None
    flag.write_text("")
    assert relay_pause.paused_reason() == "paused for the test"
    flag.unlink()
    assert relay_pause.paused_reason() is None


def test_refuse_raises_503_with_message():
    from fastapi import HTTPException

    relay_pause.refuse_if_paused()  # not paused: no raise
    Path(config.RELAY_PAUSE_FILE).write_text("")
    with pytest.raises(HTTPException) as ei:
        relay_pause.refuse_if_paused()
    assert ei.value.status_code == 503 and ei.value.detail == "paused for the test"


def test_relay_async_checks_the_pause_first():
    """relay.py imports a live RPC at module load, so the wiring is checked in the source: the first statement of
    relay_async is the pause check, ahead of target validation and the chain."""
    src = (Path(__file__).resolve().parents[1] / "relay.py").read_text()
    assert "from relay_pause import refuse_if_paused as _refuse_if_paused" in src
    body = src.split("async def relay_async(")[1]
    first_stmt = next(l.strip() for l in body.splitlines()[1:] if l.strip() and not l.strip().startswith(("#", '"""')))
    assert first_stmt.startswith("_refuse_if_paused()"), first_stmt


def test_config_reports_paused(monkeypatch):
    import relay_gateway

    monkeypatch.setattr(relay_gateway, "_relay_config", lambda: {"chainId": 1})
    endpoint = relay_gateway.relay_config.__wrapped__  # under public_endpoint's limiter
    assert endpoint(request=None) == {"chainId": 1, "paused": False}
    monkeypatch.setattr(config, "RELAY_PAUSED", True)
    out = endpoint(request=None)
    assert out["paused"] is True and out["pausedMessage"] == "paused for the test" and out["chainId"] == 1
