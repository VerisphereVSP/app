"""safe_fetch.py — outbound HTTP fetch that cannot be pointed at our own network.

patch_app_hardening_ssrf (2026-10-01, external review: article generation fetched any
user-supplied URL with requests.get, no host filter, redirects followed, no size cap).

Rules, in order:
  1. scheme must be http or https; a hostname must be present; no userinfo; no raw IP
     literals that fail the address check; the hostname "localhost" and the cloud
     metadata names are refused by name.
  2. every address the hostname resolves to (A and AAAA) must be globally routable:
     loopback, private (RFC1918 / fc00::/7), link-local (169.254/16, fe80::/10), the
     carrier-grade NAT range (100.64/10), multicast, reserved and unspecified are refused.
     One bad address refuses the whole name (DNS round-robin cannot smuggle one in).
  3. redirects are not followed by the HTTP client; each Location is re-validated with
     the same rules and followed manually, at most MAX_REDIRECTS times.
  4. the body is streamed and cut at MAX_BYTES; only text/* and *xml/*html/*json
     content types are read at all.
Residual: a hostname whose DNS answer changes between our resolution and the client's
connect (rebinding) is not defended here; the fetch runs from the app container, which
has no credentials of its own to leak, and the fetched text only feeds the LLM prompt.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse, urljoin

import requests

MAX_REDIRECTS = 3
MAX_BYTES = 1_000_000
TIMEOUT = 15
_BLOCKED_NAMES = {"localhost", "metadata", "metadata.google.internal", "instance-data"}
_READABLE_TYPES = ("text/", "application/xhtml", "application/xml", "application/json")


class UnsafeURL(ValueError):
    pass


def _check_ip(addr: str) -> None:
    ip = ipaddress.ip_address(addr)
    if (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
        or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))
        or (ip.version == 6 and ip.ipv4_mapped is not None and (
            ip.ipv4_mapped.is_private or ip.ipv4_mapped.is_loopback or ip.ipv4_mapped.is_link_local))
    ):
        raise UnsafeURL(f"address {addr} is not globally routable")


def validate_url(url: str) -> str:
    """Return the URL if it may be fetched; raise UnsafeURL otherwise."""
    p = urlparse((url or "").strip())
    if p.scheme not in ("http", "https"):
        raise UnsafeURL("scheme must be http or https")
    if not p.hostname:
        raise UnsafeURL("no host")
    if p.username or p.password:
        raise UnsafeURL("userinfo not allowed")
    host = p.hostname.lower().rstrip(".")
    if host in _BLOCKED_NAMES or host.endswith(".localhost") or host.endswith(".internal"):
        raise UnsafeURL(f"host {host} refused")
    try:
        _check_ip(host)  # literal IP
        return url
    except ValueError as e:
        if isinstance(e, UnsafeURL):
            raise
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURL(f"cannot resolve {host}: {e}")
    if not infos:
        raise UnsafeURL(f"{host} resolves to nothing")
    for info in infos:
        _check_ip(info[4][0])
    return url


def fetch_text(url: str, max_bytes: int = MAX_BYTES, timeout: int = TIMEOUT) -> tuple[str, bytes]:
    """GET a validated URL. Returns (final_url, body) with body cut at max_bytes.
    Raises UnsafeURL for any refused destination (including redirect targets) and
    requests exceptions for network errors."""
    current = validate_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        with requests.get(
            current, timeout=timeout, allow_redirects=False, stream=True,
            headers={"User-Agent": "Verisphere/1.0"},
        ) as resp:
            if resp.is_redirect or resp.is_permanent_redirect:
                loc = resp.headers.get("Location")
                if not loc:
                    raise UnsafeURL("redirect without Location")
                current = validate_url(urljoin(current, loc))
                continue
            resp.raise_for_status()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and not ctype.startswith(_READABLE_TYPES):
                raise UnsafeURL(f"content type {ctype} not readable")
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=65536):
                buf.extend(chunk)
                if len(buf) >= max_bytes:
                    break
            return current, bytes(buf[:max_bytes])
    raise UnsafeURL("too many redirects")
