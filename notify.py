#!/usr/bin/env python3
"""app/notify.py — patch_bundle08_alert_sink

Fan-out alert sink. send_alert(kind, message, **fields) delivers a single alert
to EVERY configured channel independently:

  Webhooks : ALERT_WEBHOOK_URLS         comma-separated URLs (Slack/Discord/PagerDuty/custom)
  Telegram : ALERT_TELEGRAM_BOT_TOKEN + ALERT_TELEGRAM_CHAT_IDS (comma-separated chat ids)
  Email    : ALERT_EMAIL_TO (comma-separated recipients, or one list address) over SMTP:
             ALERT_SMTP_HOST [, ALERT_SMTP_PORT=587, ALERT_SMTP_USER, ALERT_SMTP_PASSWORD,
             ALERT_EMAIL_FROM, ALERT_SMTP_TLS=1]

Design contract:
  * Every channel is best-effort and ISOLATED — one channel raising never blocks
    the others, and send_alert NEVER raises into the caller (alerting must not
    crash a monitor loop).
  * Every channel is env-gated and NO-OP when unconfigured, so this module is
    safe to import and call before any channel is set up (ships dormant).
  * Optional sink-level cooldown as a flood backstop on top of per-site throttles:
    ALERT_SINK_MIN_INTERVAL_SEC (default 0 = off) — min seconds between deliveries
    of the same `kind`.
  * `requests` is imported lazily so importing this module pulls no heavy deps.
"""
import os
import time
import socket
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage

logger = logging.getLogger("notify")

_HOST = socket.gethostname()
_last_sent: dict = {}


def _csv(name: str) -> list:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def _http_timeout() -> float:
    try:
        return float(os.getenv("ALERT_HTTP_TIMEOUT", "5"))
    except ValueError:
        return 5.0


def _format_text(kind: str, message: str, fields: dict) -> str:
    text = f"[{kind}] {message}"
    if fields:
        text += " | " + " ".join(f"{k}={v}" for k, v in fields.items())
    return text


def _deliver_webhooks(payload: dict) -> None:
    urls = _csv("ALERT_WEBHOOK_URLS")
    if not urls:
        return
    import requests
    for url in urls:
        try:
            requests.post(url, json=payload, timeout=_http_timeout())
        except Exception as e:
            logger.warning("notify: webhook delivery to %s... failed: %s", url[:40], e)


def _deliver_telegram(text: str) -> None:
    token = os.getenv("ALERT_TELEGRAM_BOT_TOKEN", "").strip()
    chats = _csv("ALERT_TELEGRAM_CHAT_IDS")
    if not token or not chats:
        return
    import requests
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat in chats:
        try:
            requests.post(api, json={"chat_id": chat, "text": text}, timeout=_http_timeout())
        except Exception as e:
            logger.warning("notify: telegram delivery to chat %s failed: %s", chat, e)


def _deliver_email(kind: str, text: str) -> None:
    recipients = _csv("ALERT_EMAIL_TO")
    host = os.getenv("ALERT_SMTP_HOST", "").strip()
    if not recipients or not host:
        return
    port = int(os.getenv("ALERT_SMTP_PORT", "587"))
    user = os.getenv("ALERT_SMTP_USER", "").strip()
    password = os.getenv("ALERT_SMTP_PASSWORD", "")
    sender = os.getenv("ALERT_EMAIL_FROM", user or "alerts@verisphere.local").strip()
    use_tls = os.getenv("ALERT_SMTP_TLS", "1") not in ("0", "false", "False", "")
    try:
        timeout = float(os.getenv("ALERT_SMTP_TIMEOUT", "10"))
    except ValueError:
        timeout = 10.0

    msg = EmailMessage()
    msg["Subject"] = f"[Verisphere ALERT] {kind}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    try:
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            if use_tls:
                server.starttls(context=ssl.create_default_context())
            if user:
                server.login(user, password)
            server.send_message(msg)
    except Exception as e:
        logger.warning("notify: email delivery to %s failed: %s", recipients, e)


def _network() -> str:
    """The running network's name ("mainnet", "fuji", "chain-<id>"), from config; "unknown" if config is unavailable."""
    try:
        from config import NETWORK
        return str(NETWORK)
    except Exception:
        return "unknown"


def _allowed_networks() -> set:
    """ALERT_NETWORKS: comma-separated names allowed to deliver; default mainnet only. "*" allows every network."""
    raw = os.getenv("ALERT_NETWORKS", "mainnet")
    names = {x.strip() for x in raw.split(",") if x.strip()}
    return {"*"} | names if "*" in names else names


def send_alert(kind: str, message: str, **fields) -> None:
    """Fan out one alert to all configured channels. NEVER raises."""
    try:
        try:
            cooldown = int(os.getenv("ALERT_SINK_MIN_INTERVAL_SEC", "0"))
        except ValueError:
            cooldown = 0
        if cooldown > 0:
            now = time.time()
            if now - _last_sent.get(kind, 0.0) < cooldown:
                return
            _last_sent[kind] = now

        # patch_alert_network_guard: every alert names its network, and only the networks in
        # ALERT_NETWORKS (default "mainnet") leave the host — a dev/Fuji worker with a token configured
        # by mistake logs the alert instead of posting it to the ops channel (2026-10-27: dev's keeper alerts
        # reached Telegram through secrets shared by both networks).
        network = _network()
        message = f"[{network}] {message}"
        allowed = _allowed_networks()
        if "*" not in allowed and network not in allowed:
            logger.warning("notify: alert suppressed off-network (%s not in ALERT_NETWORKS): %s %s", network, kind, message)
            return
        text = _format_text(kind, message, fields)
        payload = {
            "kind": kind,
            "message": message,
            "fields": fields,
            "host": _HOST,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        _deliver_webhooks(payload)
        _deliver_telegram(text)
        _deliver_email(kind, text)
    except Exception as e:
        logger.warning("notify: send_alert failed: %s", e)


def configured_channels() -> list:
    """Diagnostics: which channels are currently active (for startup logging)."""
    ch = []
    n_wh = len(_csv("ALERT_WEBHOOK_URLS"))
    if n_wh:
        ch.append(f"webhook(x{n_wh})")
    if os.getenv("ALERT_TELEGRAM_BOT_TOKEN", "").strip() and _csv("ALERT_TELEGRAM_CHAT_IDS"):
        ch.append(f"telegram(x{len(_csv('ALERT_TELEGRAM_CHAT_IDS'))})")
    if _csv("ALERT_EMAIL_TO") and os.getenv("ALERT_SMTP_HOST", "").strip():
        ch.append(f"email(x{len(_csv('ALERT_EMAIL_TO'))})")
    return ch
