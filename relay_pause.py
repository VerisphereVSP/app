# app/relay_pause.py — patch_relay_pause
"""
Manual relay pause. The spend breaker in rate_limit.py is automatic (budget); this is the operator's switch.

    paused_reason() -> str | None   None when the relay accepts submissions; the user-facing message otherwise.
    refuse_if_paused()              raises HTTPException(503, message) when paused; relay.py calls it first thing.

Two independent knobs, either pauses:
  * config.RELAY_PAUSED (env RELAY_PAUSED=1; an app restart is needed to flip it), and
  * the flag file config.RELAY_PAUSE_FILE (default /tmp/relay.paused inside the container):
        docker exec verisphere-app-1 touch /tmp/relay.paused    # pause, takes effect on the next request
        docker exec verisphere-app-1 rm -f /tmp/relay.paused    # resume
The file is checked on every submission (one stat(); cheap). Only /api/relay/async is refused — quotes, config,
nonce and every read keep answering, so the UI still renders and tells the user why the action did not go through.
"""
from __future__ import annotations

import logging
import os

from fastapi import HTTPException

import config

logger = logging.getLogger(__name__)
_announced: bool | None = None


def paused_reason() -> str | None:
    global _announced
    paused = bool(getattr(config, "RELAY_PAUSED", False)) or os.path.exists(getattr(config, "RELAY_PAUSE_FILE", "/tmp/relay.paused"))
    if paused != _announced:  # log each transition once, not every refused request
        logger.warning("relay %s (%s)", "PAUSED" if paused else "resumed",
                       "env RELAY_PAUSED" if getattr(config, "RELAY_PAUSED", False) else f"flag file {getattr(config, 'RELAY_PAUSE_FILE', '')}")
        _announced = paused
    return getattr(config, "RELAY_PAUSE_MESSAGE", "Relay paused.") if paused else None


def refuse_if_paused() -> None:
    why = paused_reason()
    if why is not None:
        raise HTTPException(503, why)
