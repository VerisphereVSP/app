# app/article_gen.py
"""
AI article generation: creates a Wikipedia-style article decomposed into
sections of atomic sentences, each sentence a stakeable claim.
"""
import json
from urllib.parse import urlparse
import re
import logging
from typing import Dict, Any, List

from llm_provider import complete
from moderation import check_content_fast
from lang_detect import detect_language, lang_instruction

logger = logging.getLogger(__name__)


def _sanitize_topic(topic: str) -> str:
    """APP-06: Sanitize user topic input before passing to LLM.
    Strips control characters, XML/HTML tags, and enforces length limit.
    Wraps in XML delimiters to prevent prompt injection."""
    if not topic:
        return ""
    # Strip control characters (keep basic whitespace)
    cleaned = "".join(ch for ch in topic if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) < 127) or ord(ch) > 127)
    # Strip XML/HTML-like tags
    import re as _re
    cleaned = _re.sub(r"<[^>]+>", "", cleaned)
    # Strip common prompt injection prefixes
    cleaned = _re.sub(r"^(ignore|forget|disregard|override|system:)\s+.*?[.!]\s*", "", cleaned, flags=_re.IGNORECASE)
    # Enforce length
    if len(cleaned) > 200:
        cleaned = cleaned[:200]
    return cleaned.strip()

ARTICLE_SYSTEM = """You are an encyclopedia article writer. Given a topic, write a comprehensive factual article.

Return ONLY valid JSON in this exact format:
{
  "title": "Topic Title",
  "sections": [
    {
      "heading": "Section Name",
      "sentences": [
        "First atomic factual sentence.",
        "Second atomic factual sentence.",
        "Third atomic factual sentence."
      ]
    }
  ]
}

RULES:
- Write 4-6 sections covering the topic comprehensively.
- Each section has a heading and 3-6 sentences.
- Each sentence must be ONE complete, standalone factual assertion.
- Each sentence must make sense on its own without context from other sentences.
- Sentences should be clear, concise, and specific — like encyclopedia text.
- Include numbers, dates, measurements where relevant.
- Do NOT combine multiple facts in one sentence.
- Total: 18-30 sentences across all sections.
- CRITICAL: Your entire response must be valid, complete JSON.
- If given a language instruction, write ALL content (title, headings, sentences) in that language."""

CLEANUP_SYSTEM = """You are a copy editor cleaning up grammar and spelling.
Each sentence must stand alone and be understandable without surrounding context.

CRITICAL: You are NOT a fact-checker. Do NOT evaluate whether claims are true, false, proven, or unproven. Do NOT refuse, rewrite, or editorialize based on your opinion of the claim. Your ONLY job is grammar, spelling, and pronoun resolution. Controversial, disputed, and minority-viewpoint claims are explicitly allowed and must be preserved exactly as the user intends them.

Rules:
1. Fix grammar, spelling, and punctuation.
2. Replace ALL pronouns (it, its, they, their, this, these, he, she, etc.) with the specific noun they refer to. The topic/subject is provided.
3. Preserve the meaning and assertion exactly as written. Do not soften, hedge, or qualify.
4. Return ONLY the cleaned sentence, nothing else. No commentary, no disclaimers.
5. PRESERVE the original language — if the sentence is in Hebrew, return Hebrew. If in Arabic, return Arabic. Never translate.

Example -- Topic: Earth
Input: It orbits the Sun every 365.25 days.
Output: Earth orbits the Sun every 365.25 days."""




def _fetch_url_text(url: str, max_chars: int = 12000) -> str:
    """Fetch a URL and extract plain text content.

    patch_app_hardening_ssrf: the fetch goes through safe_fetch (scheme/host/address
    checks on the URL and on every redirect, 1 MB cap) so a topic that is a URL can
    never make this server read loopback, private, link-local or metadata addresses.
    """
    import re as _re
    from safe_fetch import fetch_text
    _, body = fetch_text(url)
    html = body.decode("utf-8", errors="replace")
    # Remove scripts, styles, comments
    html = _re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", html, flags=_re.DOTALL | _re.IGNORECASE)
    html = _re.sub(r"<!--.*?-->", "", html, flags=_re.DOTALL)
    # Extract title
    title_m = _re.search(r"<title[^>]*>(.*?)</title>", html, _re.DOTALL | _re.IGNORECASE)
    title = _re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
    # Strip tags
    text = _re.sub(r"<[^>]+>", " ", html)
    text = _re.sub(r"\s+", " ", text).strip()
    # Truncate
    if len(text) > max_chars:
        text = text[:max_chars]
    return title, text


def _is_url(topic: str) -> bool:
    """Check if a topic string is a URL."""
    try:
        p = urlparse(topic.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _build_prompt(topic: str) -> str:
    """Build the appropriate prompt based on whether input is a URL or topic."""
    if _is_url(topic):
        try:
            title, text = _fetch_url_text(topic)
            from lang_detect import detect_language, lang_instruction
            lang_extra = lang_instruction(detect_language(text[:500]))
            safe_title = _sanitize_topic(title)
            # Truncate page text to prevent prompt stuffing
            safe_text = text[:8000] if len(text) > 8000 else text
            return (
                f"Structure the following web page content into an encyclopedia-style article. "
                f"The page title is: <title>{safe_title}</title>\n\n"
                f"<page_content>{safe_text}</page_content>"
                f"{lang_extra}"
            )
        except Exception as e:
            logger.warning(f"URL fetch failed, treating as topic: {e}")
    from lang_detect import detect_language, lang_instruction
    safe = _sanitize_topic(topic)
    return f"Write an encyclopedia article about the following topic:\n<topic>{safe}</topic>" + lang_instruction(detect_language(safe))

def generate_article(topic: str) -> Dict[str, Any]:
    """Generate a full article for a topic. Returns {title, sections}."""
    safe_topic = _sanitize_topic(topic)
    raw = complete(
        prompt=_build_prompt(safe_topic) + lang_instruction(detect_language(safe_topic)),
        system=ARTICLE_SYSTEM,
        max_tokens=6144,
        temperature=0.4,
    )

    # Clean markdown fences
    content = re.sub(r'^```json\s*|\s*```$', '', raw).strip()
    content = re.sub(r',\s*([}\]])', r'\1', content)

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        logger.warning("Article JSON parse error: %s", e)
        parsed = _try_repair(content)
        if not parsed:
            raise ValueError(f"Failed to parse article JSON: {e}")

    title = parsed.get("title", topic.title())
    sections = parsed.get("sections", [])

    # Validate and clean sections
    clean_sections = []
    for sec in sections:
        heading = sec.get("heading", "")
        sentences = sec.get("sentences", [])
        clean_sents = [str(s).strip() for s in sentences if str(s).strip()]
        if clean_sents:
            clean_sections.append({"heading": heading, "sentences": clean_sents})

    if not clean_sections:
        raise ValueError("Article generation produced no sections")

    return {"title": title, "sections": clean_sections}


def cleanup_sentence(text: str, topic: str = "") -> str:
    """Use LLM to clean up grammar/spelling and replace pronouns with nouns."""
    prompt = f"Topic: {topic}\nInput: {text}" if topic else text
    result = complete(
        prompt=prompt,
        system=CLEANUP_SYSTEM,
        max_tokens=256,
        temperature=0.1,
    )
    cleaned = result.strip().strip('"').strip("'").strip()
    return cleaned if cleaned else text


def split_into_sentences(text: str) -> List[str]:
    """Split user input into individual sentences.
    Avoids splitting on abbreviations like Dr., Mr., U.S., etc."""
    # Common abbreviations that shouldn't trigger a split
    _ABBREVS = r'(?:Dr|Mr|Mrs|Ms|Prof|Rev|Sr|Jr|St|Gen|Gov|Sgt|Cpl|Pvt|Mt|Ft|Lt|Capt|Col|Maj|Inc|Corp|Ltd|Co|vs|etc|al|approx|dept|est|vol|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec|U\.S|U\.K|U\.N|E\.U)'
    # Split on sentence-ending punctuation followed by space + uppercase letter,
    # but NOT after known abbreviations
    raw = re.split(r'(?<!' + _ABBREVS + r')(?<=[.!?])\s+(?=[A-Z"“(])', text.strip())
    sentences = []
    for s in raw:
        s = s.strip()
        if s:
            if not s[-1] in '.!?':
                s += '.'
            sentences.append(s)
    return sentences if sentences else [text.strip()]


def _try_repair(content: str):
    repairs = [
        content + ']}',
        content + '"]}',
        content + '"]}]}',
    ]
    last_bracket = content.rfind(']')
    if last_bracket > 0:
        repairs.append(content[:last_bracket + 1] + '}]}')
        repairs.append(content[:last_bracket + 1] + ']}')

    for attempt in repairs:
        try:
            parsed = json.loads(attempt)
            if "sections" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue
    return None