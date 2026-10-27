"""app/snapshot.py — patch_verisnap_share
Verisphere Snapshot: a claim's shareable snapshot. (patch_snapshot_naming: the working name "Verisnap" was retired —
an existing product of that name verifies snapshots; user-facing wording is "Verisphere Snapshot" / "Snapshot".)

  GET /api/claims/{post_id}/snapshot   JSON — text, Verity Score, band, stake split, settlement times, urls
  GET /api/claims/{post_id}/card.png   PNG 1200×630 (Open Graph size), rendered server-side, cached 60 s
  GET /c/{post_id}                     the share page: Open Graph / Twitter meta pointing at the card, the card
                                       itself, and a link into the app — what a pasted link unfurls into

Wording rule (design doc "Principles"): the card states facts about the game — score, stake, settlement —
never a verdict about the world and nothing about price or value. Bands are on the support/challenge axis.
The renderer is Pillow with a fixed template (DejaVu Sans in the image; falls back to Pillow's default font);
no browser. Reads come from the indexed tables the claim summary already uses (no RPC on the request path).
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

CARD_W, CARD_H = 1200, 630
CARD_TTL_SEC = int(os.getenv("SNAPSHOT_CARD_TTL_SEC", "60"))
_CARD_CACHE_MAX = 500
_card_cache: dict[int, tuple[float, bytes]] = {}
_card_lock = threading.Lock()

# palette (landing page / app)
INK, MUTED, SUBTLE, VIOLET_DEEP = (0x1C, 0x15, 0x33), (0x5D, 0x57, 0x76), (0x8A, 0x84, 0xA6), (0x37, 0x29, 0x9B)
GREEN, GREEN_TEXT = (0x16, 0xA3, 0x4A), (0x15, 0x80, 0x3D)
RED, RED_TEXT = (0xDC, 0x26, 0x26), (0xB9, 0x1C, 0x1C)
GRAY_FILL, PILL_BG, PILL_BORDER, CARD_BORDER = (0xD1, 0xD5, 0xDB), (0xF3, 0xF4, 0xF6), (0xE5, 0xE7, 0xEB), (0xE4, 0xE1, 0xF2)

_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", str(Path(__file__).resolve().parent / "assets" / "fonts")]
_LOGO = Path(__file__).resolve().parent / "assets" / "vsp-128.png"


# ── data ────────────────────────────────────────────────────────────────────────────────────────────

def band_for(vs: float) -> tuple[str, str]:
    """Band word on the support/challenge axis and its colour role ('support' | 'challenge' | 'neutral')."""
    if vs >= 60:
        return "Strongly supported", "support"
    if vs >= 20:
        return "Mostly supported", "support"
    if vs > -20:
        return "Contested", "neutral"
    if vs > -60:
        return "Mostly challenged", "challenge"
    return "Strongly challenged", "challenge"


def support_pct(support: float, challenge: float) -> int | None:
    total = (support or 0) + (challenge or 0)
    if total <= 0:
        return None
    return int(round(100 * (support or 0) / total))


def next_settlement(now: datetime | None = None) -> datetime:
    """Settlement runs shortly after 00:00 UTC every day."""
    now = now or datetime.now(timezone.utc)
    nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return nxt


def _public_base(request: Request) -> str:
    """The public origin for absolute urls: PUBLIC_APP_URL if set, else the forwarded host behind the edge."""
    base = os.getenv("PUBLIC_APP_URL", "").strip().rstrip("/")
    if base:
        return base
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def build_snapshot(db: Session, post_id: int, base: str) -> dict:
    from chain.chain_db import get_post_info, get_claim_text
    try:
        from claim_views import _moderate_text  # admin takedowns + content filter, as the claim summary applies them
    except Exception:  # pragma: no cover — claim_views needs a live RPC at import in some test setups
        def _moderate_text(text, post_id=None, db=None):
            return text
    info = get_post_info(db, post_id)
    if not info or info.get("content_type", 0) != 0:
        raise HTTPException(404, f"Claim {post_id} not found")
    text = _moderate_text(get_claim_text(db, post_id) or "", post_id, db)
    vs = float(info.get("effective_vs") or 0.0)
    support = float(info.get("support_total") or 0.0)
    challenge = float(info.get("challenge_total") or 0.0)
    band, role = band_for(vs)
    now = datetime.now(timezone.utc)
    pct = support_pct(support, challenge)
    stake_line = (f"{pct} % of stake backs this claim" if pct is not None and vs >= 0
                  else f"{100 - pct} % of stake challenges this claim" if pct is not None
                  else "No stake yet")
    return {
        "post_id": post_id,
        "text": text,
        "verity_score": round(vs, 1),
        "band": band,
        "band_role": role,
        "support_pct": pct,
        "stake_support": round(support, 4),
        "stake_challenge": round(challenge, 4),
        "total_stake": round(support + challenge, 4),
        "stake_line": stake_line,
        "is_active": bool(info.get("is_active", True)),
        "snapshot_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "next_settlement_at": next_settlement(now).isoformat().replace("+00:00", "Z"),
        "share_url": f"{base}/c/{post_id}",
        "card_url": f"{base}/api/claims/{post_id}/card.png",
        "app_url": f"{base}/claims?post={post_id}",
        "share_text": (f"“{text}” — Verisphere: {band}, VS {vs:+.0f}"
                       + (f" ({stake_line.replace('this claim', 'it')})" if pct is not None else "")
                       + f". Details and evidence: {base}/c/{post_id}"),
    }


# ── render ──────────────────────────────────────────────────────────────────────────────────────────

def _font(size: int, bold: bool = True):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in _FONT_DIRS:
        p = Path(d) / name
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if draw.textlength(cand, font=font) <= max_w:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"
    return lines


FIT_SIZES = (44, 40, 36, 32, 28, 26)


def fit_claim(draw, quote: str, max_w: int, avail_h: int):
    """44 px when the whole quote fits; else the largest smaller size at which it does (an ellipsis only at 26 px).
    Returns (font, lines, line_height)."""
    for size in FIT_SIZES:
        font = _font(size); lh = int(size * 1.22)
        max_lines = max(1, avail_h // lh)
        lines = _wrap(draw, quote, font, max_w, max_lines)
        if not lines[-1].endswith("\u2026") or size == FIT_SIZES[-1]:
            return font, lines, lh
    return font, lines, lh  # pragma: no cover


def render_card(snap: dict) -> bytes:
    """The 1200×630 Snapshot card as PNG bytes. Layout = the design mockup (Main.dc.html)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (CARD_W, CARD_H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, CARD_W - 1, CARD_H - 1], radius=24, outline=CARD_BORDER, width=2)
    pad_x = 56
    # header: logo + Verisphere; Snapshot · claim #N at right
    y = 40
    x = pad_x
    if _LOGO.exists():
        try:
            logo = Image.open(_LOGO).convert("RGBA").resize((56, 56), Image.LANCZOS)
            img.paste(logo, (x, y), logo)
            x += 56 + 16
        except Exception:
            pass
    d.text((x, y + 4), "Verisphere", font=_font(36), fill=INK)
    right = f"Snapshot · claim #{snap['post_id']}"
    f18 = _font(18, bold=False)
    d.text((CARD_W - pad_x - d.textlength(right, font=f18), y + 18), right, font=f18, fill=SUBTLE)
    # claim text: 44 px when it fits; otherwise the largest smaller size at which the WHOLE claim fits the space
    # above the pill (the pill and band move down with the text; the footer stays). Ellipsis only past 26 px.
    quote = f"\u201c{snap['text']}\u201d"
    text_top = 140
    avail_h = (CARD_H - 40 - 52) - 24 - 84 - 36 - text_top   # footer top − gap − pill − gap − text top
    f_claim, lines, lh = fit_claim(d, quote, CARD_W - 2 * pad_x, avail_h)
    y = text_top
    for ln in lines:
        d.text((pad_x, y), ln, font=f_claim, fill=INK)
        y += lh
    # pill + band — directly under the text (never above where a 3-line claim would put it)
    y = max(y + 36, 330)
    vs = float(snap["verity_score"])
    pw, ph = 260, 84
    px, py = pad_x, y
    d.rounded_rectangle([px, py, px + pw, py + ph], radius=8, fill=PILL_BG, outline=PILL_BORDER, width=1)
    fill_w = int(pw * max(abs(vs), 2) / 100)
    fill_col = GREEN if vs > 0 else RED if vs < 0 else GRAY_FILL
    d.rounded_rectangle([px, py, px + fill_w, py + ph], radius=8, fill=fill_col)
    if fill_w < pw - 8:  # square the inner edge of the fill
        d.rectangle([px + fill_w - 8, py, px + fill_w, py + ph], fill=fill_col)
    label = f"{vs:+.0f}"
    f_pill = _font(40)
    tw = d.textlength(label, font=f_pill)
    tx, ty = px + (pw - tw) / 2, py + (ph - f_pill.size) / 2 - 6
    # label drawn twice: dark over the empty part of the pill, white over the fill (each clipped to its region)
    from PIL import ImageChops
    for colour, box in (((0x11, 0x18, 0x27), [px + fill_w, py, px + pw, py + ph]), ((255, 255, 255), [px, py, px + fill_w, py + ph])):
        layer = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((tx, ty), label, font=f_pill, fill=colour + (255,))
        region = Image.new("L", (CARD_W, CARD_H), 0)
        ImageDraw.Draw(region).rectangle(box, fill=255)
        img.paste(layer, (0, 0), ImageChops.multiply(layer.split()[3], region))
    band_col = GREEN_TEXT if snap["band_role"] == "support" else RED_TEXT if snap["band_role"] == "challenge" else MUTED
    bx = px + pw + 32
    d.text((bx, py + 2), snap["band"], font=_font(34), fill=band_col)
    total = f"{snap['total_stake']:,.0f} VSP staked" if snap["total_stake"] >= 1 else f"{snap['total_stake']:.2f} VSP staked"
    d.text((bx, py + 50), f"{snap['stake_line']} · {total}", font=_font(22, bold=False), fill=MUTED)
    # footer
    f17 = _font(17, bold=False)
    snap_at = snap["snapshot_at"].replace("T", " ").replace("Z", " UTC")
    foot1 = "Verity Score, settled daily from stake and linked evidence. It changes as people stake."
    foot2 = f"Snapshot {snap_at} · next settlement 00:00 UTC"
    d.text((pad_x, CARD_H - 40 - 52), foot1, font=f17, fill=SUBTLE)
    d.text((pad_x, CARD_H - 40 - 26), foot2, font=f17, fill=SUBTLE)
    url = snap["share_url"].replace("https://", "").replace("http://", "")
    f20 = _font(20)
    d.text((CARD_W - pad_x - d.textlength(url, font=f20), CARD_H - 40 - 30), url, font=f20, fill=VIOLET_DEEP)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _cached_card(db: Session, post_id: int, base: str) -> bytes:
    now = time.time()
    with _card_lock:
        hit = _card_cache.get(post_id)
        if hit and now - hit[0] < CARD_TTL_SEC:
            return hit[1]
    snap = build_snapshot(db, post_id, base)
    png = render_card(snap)
    with _card_lock:
        if len(_card_cache) >= _CARD_CACHE_MAX:
            _card_cache.clear()
        _card_cache[post_id] = (now, png)
    return png


# ── routes ──────────────────────────────────────────────────────────────────────────────────────────

@router.get("/api/claims/{post_id}/snapshot")
def claim_snapshot(post_id: int, request: Request, db: Session = Depends(get_db)):
    return build_snapshot(db, post_id, _public_base(request))


@router.api_route("/api/claims/{post_id}/card.png", methods=["GET", "HEAD"])
def claim_card(post_id: int, request: Request, db: Session = Depends(get_db)):
    png = _cached_card(db, post_id, _public_base(request))
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": f"public, max-age={CARD_TTL_SEC}"})


@router.api_route("/c/{post_id}", methods=["GET", "HEAD"], response_class=HTMLResponse)
def share_page(post_id: int, request: Request, db: Session = Depends(get_db)):
    """What a pasted link unfurls into. No scripts (the edge CSP allows none inline); a plain link into the app."""
    snap = build_snapshot(db, post_id, _public_base(request))
    title = escape(snap["text"][:200])
    desc = escape(f"{snap['band']} — {snap['stake_line']} · Verity Score {snap['verity_score']:+.0f} · Verisphere")
    card = escape(snap["card_url"]); share = escape(snap["share_url"]); app_url = escape(snap["app_url"])
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claim #{post_id} · Verisphere</title>
<meta name="description" content="{desc}">
<meta property="og:type" content="website"><meta property="og:site_name" content="Verisphere">
<meta property="og:title" content="{title}"><meta property="og:description" content="{desc}">
<meta property="og:url" content="{share}"><meta property="og:image" content="{card}">
<meta property="og:image:width" content="{CARD_W}"><meta property="og:image:height" content="{CARD_H}">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{desc}"><meta name="twitter:image" content="{card}">
<link rel="canonical" href="{share}">
<style>body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#F7F5FE;color:#1C1533}}
main{{max-width:760px;margin:0 auto;padding:24px 16px 48px}}img{{width:100%;height:auto;border-radius:16px;border:1px solid #E4E1F2;background:#fff}}
a.btn{{display:inline-block;margin-top:20px;padding:14px 22px;border-radius:999px;background:#5B4BC4;color:#fff;text-decoration:none;font-weight:600;font-size:16px}}
p{{color:#5D5776;font-size:15px;line-height:1.5}}</style></head>
<body><main>
<img src="{card}" alt="Verisphere Snapshot of claim #{post_id}: {title}">
<a class="btn" href="{app_url}">Open claim #{post_id} on Verisphere</a>
<p>Verisphere is a truth-staking protocol: people stake VSP to support or challenge factual claims, and every claim settles once a day. The score above is the stake behind each side right now, not a verdict; it changes as people stake.</p>
</main></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": f"public, max-age={CARD_TTL_SEC}"})
