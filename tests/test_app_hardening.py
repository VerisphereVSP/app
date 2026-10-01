"""patch_app_hardening (2026-10-01): three findings from the external app review.
  SSRF      — article generation fetched any URL; now safe_fetch refuses non-public destinations
  detect-topic — caller-chosen text could be attached to another claim's post_id
  endpoint limit — per-endpoint rate limit ran after the handler; now a pre-handler dependency
"""
import types
import pytest


# ───────────────────────── SSRF ─────────────────────────
from safe_fetch import validate_url, fetch_text, UnsafeURL


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://10.0.0.1/x", "http://192.168.1.1/", "http://172.16.5.5/",
    "http://169.254.169.254/latest/meta-data/", "http://100.64.0.1/", "http://0.0.0.0/",
    "http://[::1]/", "http://[fd00::1]/", "http://[fe80::1]/", "http://[::ffff:127.0.0.1]/",
    "http://localhost/", "http://LOCALHOST:8080/", "http://foo.localhost/",
    "http://metadata.google.internal/computeMetadata/v1/", "http://metadata/",
    "ftp://example.com/", "file:///etc/passwd", "http://user:pw@1.1.1.1/", "http:///nohost",
])
def test_validate_url_refuses_internal_and_odd(url):
    with pytest.raises(UnsafeURL):
        validate_url(url)


def test_validate_url_accepts_public_literal():
    assert validate_url("https://1.1.1.1/page") == "https://1.1.1.1/page"


def test_validate_url_resolves_names_and_refuses_any_private_answer(monkeypatch):
    import safe_fetch
    def fake_gai(host, port, proto=0):
        if host == "good.example":
            return [(2, 1, 6, "", ("93.184.216.34", port))]
        if host == "mixed.example":  # round-robin with one private answer
            return [(2, 1, 6, "", ("93.184.216.34", port)), (2, 1, 6, "", ("10.1.2.3", port))]
        raise safe_fetch.socket.gaierror("nx")
    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", fake_gai)
    assert validate_url("https://good.example/a") == "https://good.example/a"
    with pytest.raises(UnsafeURL):
        validate_url("https://mixed.example/a")
    with pytest.raises(UnsafeURL):
        validate_url("https://nx.example/a")


class _Resp:
    def __init__(self, status, headers=None, body=b""):
        self.status_code = status; self.headers = headers or {}; self._body = body
        self.is_redirect = status in (301, 302, 303, 307, 308); self.is_permanent_redirect = status in (301, 308)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)
    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]


def test_fetch_text_revalidates_redirects(monkeypatch):
    import safe_fetch
    seen = []
    def fake_get(url, **kw):
        seen.append(url); assert kw["allow_redirects"] is False
        return _Resp(302, {"Location": "http://169.254.169.254/latest/meta-data/"})
    monkeypatch.setattr(safe_fetch.requests, "get", fake_get)
    with pytest.raises(UnsafeURL):
        fetch_text("https://1.1.1.1/start")
    assert seen == ["https://1.1.1.1/start"]  # the metadata hop was never requested


def test_fetch_text_caps_body_and_type(monkeypatch):
    import safe_fetch
    monkeypatch.setattr(safe_fetch.requests, "get",
                        lambda url, **kw: _Resp(200, {"Content-Type": "text/html"}, b"x" * 5000))
    _, body = fetch_text("https://1.1.1.1/", max_bytes=1000)
    assert len(body) == 1000
    monkeypatch.setattr(safe_fetch.requests, "get",
                        lambda url, **kw: _Resp(200, {"Content-Type": "application/octet-stream"}, b"\x00" * 10))
    with pytest.raises(UnsafeURL):
        fetch_text("https://1.1.1.1/bin")


def test_article_gen_uses_safe_fetch(monkeypatch):
    import articles.article_gen as ag
    import safe_fetch
    calls = []
    monkeypatch.setattr(safe_fetch, "fetch_text", lambda url: calls.append(url) or (url, b"<title>T</title><p>hello</p>"))
    title, text = ag._fetch_url_text("https://1.1.1.1/p")
    assert calls == ["https://1.1.1.1/p"] and title == "T" and "hello" in text
    assert not hasattr(ag, "requests"), "article_gen must not call requests directly any more"


# ───────────────────────── detect-topic ─────────────────────────
from fastapi import HTTPException
import articles.article_routes as ar


class _DB:
    def __init__(self, chain_text):
        self._t = chain_text
    def execute(self, *a, **k):
        t = self._t
        class R:
            def fetchone(self_inner):
                return (t,) if t is not None else None
        return R()


def test_detect_topic_requires_text_to_match_post(monkeypatch):
    # chain says post 7 is "The Earth is round"
    db = _DB("The Earth is round")
    # same text, different whitespace/case: on-chain normalization makes them equal
    assert ar._require_text_matches_post(db, "  the earth   is ROUND ", 7) == "The Earth is round"
    with pytest.raises(HTTPException) as e:
        ar._require_text_matches_post(db, "Buy my token", 7)
    assert e.value.status_code == 422
    # not indexed and registry read fails -> 409, never a silent accept
    monkeypatch.setitem(__import__("sys").modules, "chain.registry_reader",
                        types.SimpleNamespace(get_claim_text=lambda pid: (_ for _ in ()).throw(RuntimeError("rpc"))))
    with pytest.raises(HTTPException) as e:
        ar._require_text_matches_post(_DB(None), "anything", 7)
    assert e.value.status_code == 409


def test_detect_topic_endpoint_rejects_mismatch(monkeypatch):
    called = []
    import articles.topic_detect as td
    monkeypatch.setattr(td, "snap_topic", lambda db, pid: called.append("snap") or None)
    monkeypatch.setattr(td, "detect_topic", lambda t: called.append("detect") or "x")
    monkeypatch.setattr(td, "ensure_article_for_claim", lambda *a, **k: called.append("ensure"))
    with pytest.raises(HTTPException) as e:
        ar.detect_topic_endpoint(ar.DetectTopicRequest(claim_text="Buy my token", post_id=7), db=_DB("The Earth is round"))
    assert e.value.status_code == 422 and called == []  # nothing ran, nothing stored


# ───────────────────────── endpoint rate limit ─────────────────────────
def test_endpoint_limit_blocks_before_handler(monkeypatch):
    from fastapi import FastAPI, APIRouter, Depends
    from fastapi.testclient import TestClient
    import rate_limit as rl
    monkeypatch.setattr(rl, "ENDPOINT_RATE_LIMITS", {"/api/article/{topic}/generate": 1})
    monkeypatch.setattr(rl, "ENDPOINT_RATE_DEFAULT", 1000)
    rl._limiter.cleanup(max_age=0)
    app = FastAPI(dependencies=[Depends(rl.enforce_endpoint_limit)])
    r = APIRouter(prefix="/api")
    ran = []
    @r.post("/article/{topic}/generate")
    def gen(topic: str):
        ran.append(topic); return {"ok": True}
    @r.get("/other")
    def other():
        ran.append("other"); return {"ok": True}
    app.include_router(r)
    c = TestClient(app)
    assert c.post("/api/article/a/generate").status_code == 200
    r2 = c.post("/api/article/b/generate")
    assert r2.status_code == 429 and "Retry-After" in r2.headers
    assert ran == ["a"], "the limited request must not reach the handler"
    assert c.get("/api/other").status_code == 200  # other templates unaffected


def test_middleware_no_longer_meters_per_endpoint():
    import inspect, rate_limit as rl
    src = inspect.getsource(rl.RateLimitMiddleware.dispatch)
    assert "ENDPOINT_RATE_LIMITS" not in src
    assert "dependencies=[_Depends(_enforce_endpoint_limit)]" in open(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", "main.py")).read()
