"""비밀번호 해시(PBKDF2-SHA256, 표준 라이브러리), 비밀번호 정책, 로그인 잠금."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict

from .config import get_settings

ALGO = "pbkdf2_sha256"


def hash_password(password: str, iterations: int | None = None) -> str:
    iterations = iterations or get_settings().password_iterations
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{ALGO}${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < 10:
        problems.append("10자 이상")
    if not re.search(r"[A-Za-z]", password):
        problems.append("영문 포함")
    if not re.search(r"\d", password):
        problems.append("숫자 포함")
    if not re.search(r"[^A-Za-z0-9]", password):
        problems.append("특수문자 포함")
    return problems


class LoginThrottle:
    """연속 로그인 실패 시 계정 잠금(메모리 기반, 단일 인스턴스용)."""

    def __init__(self, max_failures: int = 5, lock_seconds: int = 600):
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self._failures: dict[str, list[float]] = defaultdict(list)

    def is_locked(self, username: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures[username] if now - t < self.lock_seconds]
        self._failures[username] = recent
        return len(recent) >= self.max_failures

    def fail(self, username: str) -> None:
        self._failures[username].append(time.time())

    def reset(self, username: str) -> None:
        self._failures.pop(username, None)


throttle = LoginThrottle()
