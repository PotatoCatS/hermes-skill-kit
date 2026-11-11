"""Hermes Skill Dashboard - local FastAPI app.

Bind address is fixed in run.sh: 127.0.0.1 only. Mirrored WSL networking lets the
Windows host reach it via http://localhost:8000 without exposing it to the LAN.

Phase 3 adds a local admin session and CSRF protection. Every /api route except
health and the three auth entry points requires a valid signed session cookie;
every mutating /api request must also echo the session-bound CSRF token and,
when the browser sends Origin, match the request host.
"""
import hmac
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from .api import router

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

PUBLIC_API = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)

app = FastAPI(
    title="Hermes Skill Dashboard",
    description="本地 Skill 管理控制台：盘点、审计、报告与审批中心。",
    version="0.3.0",
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)


def _deny(status: int, message: str) -> JSONResponse:
    response = JSONResponse({"detail": message}, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            if urlparse(origin).netloc != host:
                return _deny(403, "请求来源与 Host 不一致")

    if path.startswith("/api/") and path not in PUBLIC_API:
        session = auth.read_session(request.cookies.get(auth.COOKIE_NAME))
        if not session:
            return _deny(401, "未登录或会话已过期")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            token = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(token, str(session.get("csrf", ""))):
                return _deny(403, "CSRF 校验失败")

    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif path == "/" or path.startswith("/static/"):
        # Always revalidate the shell and its assets so a deploy shows up on a
        # normal refresh instead of being served from the browser's heuristic cache.
        response.headers.setdefault("Cache-Control", "no-cache")
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/html"):
        response.headers.setdefault("Content-Security-Policy", CSP)
    return response


app.include_router(router, prefix="/api")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
