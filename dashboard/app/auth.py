"""Local admin authentication for the skill dashboard.

Design notes:
- One local user ("admin"); the password is stored only as a PBKDF2-SHA256
  hash under ``config/admin.json`` (mode 600). There is no default password:
  the first visit offers a setup form.
- Sessions are stateless signed tokens (HMAC-SHA256) in an HttpOnly,
  SameSite=Strict cookie. Each token carries a CSRF secret that mutating
  requests must echo in ``X-CSRF-Token``.
- Login attempts are rate limited in memory. The service binds to 127.0.0.1
  only, so no TLS is involved; the cookie is deliberately not marked Secure.

This module never touches skills or Hermes configuration.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .audit_log import log_action

DASH_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("SKD_CONFIG_DIR", str(DASH_DIR / "config")))
ADMIN_FILE = CONFIG_DIR / "admin.json"
SECRET_FILE = CONFIG_DIR / "session.key"

COOKIE_NAME = "skd_session"
SESSION_TTL = 12 * 3600
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD = 10
MAX_PASSWORD = 200

MAX_FAILURES = 5
FAILURE_WINDOW = 300
LOCK_SECONDS = 300

_lock = threading.RLock()
_failures: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


class AuthError(ValueError):
    pass


class RateLimited(AuthError):
    def __init__(self, retry_after: int):
        super().__init__(f"尝试次数过多，请 {retry_after} 秒后再试")
        self.retry_after = retry_after


def _now() -> float:
    return time.time()


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    _ensure_config_dir()
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _secret() -> bytes:
    with _lock:
        if SECRET_FILE.is_file():
            key = SECRET_FILE.read_bytes()
            if len(key) != 32:
                raise AuthError("session.key 损坏（长度不是 32 字节）")
            return key
        _ensure_config_dir()
        key = secrets.token_bytes(32)
        _atomic_write(SECRET_FILE, key)
        log_action("auth", "生成会话签名密钥")
        return key


def admin_record() -> dict | None:
    if not ADMIN_FILE.is_file():
        return None
    try:
        data = json.loads(ADMIN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthError(f"admin.json 无法读取：{exc}") from exc
    if not isinstance(data, dict) or not data.get("hash") or not data.get("salt"):
        raise AuthError("admin.json 内容不完整")
    return data


def setup_required() -> bool:
    return not ADMIN_FILE.is_file()


def _record_ok(record: dict) -> str:
    if record.get("algorithm") != "pbkdf2_sha256":
        raise AuthError("不支持的密码哈希算法")
    try:
        iterations = int(record.get("iterations") or PBKDF2_ITERATIONS)
    except (TypeError, ValueError) as exc:
        raise AuthError("密码哈希参数无效") from exc
    if not (100_000 <= iterations <= 2_000_000):
        raise AuthError("密码哈希迭代次数超出允许范围")
    return str(record["salt"])


def _derive(password: str, salt_b64: str, iterations: int) -> bytes:
    try:
        salt = base64.b64decode(salt_b64.encode("ascii"), validate=True)
    except (ValueError, TypeError) as exc:
        raise AuthError("密码盐值损坏") from exc
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password: str) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return {
        "version": 1,
        "user": "admin",
        "algorithm": "pbkdf2_sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(digest).decode("ascii"),
        "created_at": _iso(),
    }


def _check_password(password: str, record: dict) -> bool:
    try:
        iterations = int(record.get("iterations") or PBKDF2_ITERATIONS)
        expected = base64.b64decode(str(record.get("hash") or "").encode("ascii"), validate=True)
        actual = _derive(password, _record_ok(record), iterations)
    except (AuthError, ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _validate_new_password(password: str, confirm: str) -> None:
    if not isinstance(password, str) or not isinstance(confirm, str):
        raise AuthError("密码格式无效")
    if password != confirm:
        raise AuthError("两次输入的密码不一致")
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"密码至少需要 {MIN_PASSWORD} 个字符")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"密码不能超过 {MAX_PASSWORD} 个字符")


def _purge(now: float) -> None:
    for ip, stamps in list(_failures.items()):
        kept = [t for t in stamps if now - t < FAILURE_WINDOW]
        if kept:
            _failures[ip] = kept
        else:
            _failures.pop(ip, None)
    for ip, until in list(_locked_until.items()):
        if until <= now:
            _locked_until.pop(ip, None)


def check_rate_limit(ip: str) -> None:
    with _lock:
        now = _now()
        _purge(now)
        until = _locked_until.get(ip)
        if until and until > now:
            raise RateLimited(max(1, int(until - now) + 1))


def record_failure(ip: str) -> None:
    with _lock:
        now = _now()
        _purge(now)
        stamps = _failures.setdefault(ip, [])
        stamps.append(now)
        if len(stamps) >= MAX_FAILURES:
            _locked_until[ip] = now + LOCK_SECONDS
            _failures.pop(ip, None)
            log_action("auth", f"登录失败次数达到上限，已锁定 {LOCK_SECONDS}s", ok=False)


def reset_failures(ip: str) -> None:
    with _lock:
        _failures.pop(ip, None)
        _locked_until.pop(ip, None)


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def create_session() -> str:
    now = int(_now())
    payload = {
        "v": 1,
        "sub": "admin",
        "iat": now,
        "exp": now + SESSION_TTL,
        "csrf": secrets.token_urlsafe(24),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64d(body).decode("utf-8"))
    except (AuthError, ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("sub") != "admin":
        return None
    try:
        exp = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return None
    if exp <= int(_now()):
        return None
    if not isinstance(payload.get("csrf"), str) or len(payload["csrf"]) < 16:
        return None
    return payload


def setup(password: str, confirm: str, ip: str) -> tuple[str, dict]:
    with _lock:
        if not setup_required():
            raise AuthError("管理密码已经设置，请直接登录")
        _validate_new_password(password, confirm)
        _secret()
        record = hash_password(password)
        _atomic_write(
            ADMIN_FILE,
            (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        reset_failures(ip)
        log_action("auth", "首次设置管理密码")
        token = create_session()
        return token, {"user": "admin", "csrf_token": json.loads(_b64d(token.split(".")[0]))["csrf"]}


def login(password: str, ip: str) -> str:
    if setup_required():
        raise AuthError("尚未设置管理密码")
    check_rate_limit(ip)
    record = admin_record()
    if record is None:
        raise AuthError("尚未设置管理密码")
    if not isinstance(password, str) or not _check_password(password, record):
        record_failure(ip)
        log_action("auth", f"登录失败 ip={ip}", ok=False)
        raise AuthError("密码错误")
    reset_failures(ip)
    log_action("auth", f"登录成功 ip={ip}")
    return create_session()


def cookie_max_age() -> int:
    return SESSION_TTL


def cookie_secure() -> bool:
    return False


def set_cookie(response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=cookie_secure(),
        path="/",
    )


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def csrf_from_token(token: str) -> str:
    payload = read_session(token)
    return str(payload.get("csrf")) if payload else ""
