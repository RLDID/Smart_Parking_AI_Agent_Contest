from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
import secrets
import time


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


@dataclass
class Session:
    username: str
    role: str
    csrf: str
    expires: float


class Auth:
    """Public synthetic demo accounts. Not an identity provider for deployment."""

    def __init__(self, store):
        self.store = store
        self.sessions = {}
        self.attempts = {}
        # Unknown accounts still perform the same password work.
        self.dummy_salt = secrets.token_bytes(16)
        self.dummy_hash = self.hash_password(secrets.token_hex(32), self.dummy_salt)

    @staticmethod
    def hash_password(password, salt):
        return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)

    def login(self, username, password, client):
        now = time.monotonic()
        history = self.attempts.setdefault(client, deque())
        while history and history[0] <= now - 60:
            history.popleft()
        if len(history) >= 10:
            raise ApiError(429, "RATE_LIMITED", "잠시 후 다시 로그인하세요.")
        history.append(now)
        account = self.store.db.execute("SELECT password_salt,password_hash FROM users WHERE username=? AND disabled_at IS NULL",
                                        (username,)).fetchone()
        salt, expected = account if account else (self.dummy_salt, self.dummy_hash)
        valid = hmac.compare_digest(self.hash_password(password, salt), expected)
        role = self.store.registry.role(username)
        if not account or not valid or not role:
            raise ApiError(401, "UNAUTHENTICATED", "가상 계정 또는 비밀번호를 확인하세요.")
        token = secrets.token_urlsafe(32)
        self.sessions[token] = Session(username, role, secrets.token_urlsafe(32), now + 3600)
        return token, self.sessions[token]

    def lookup(self, token):
        session = self.sessions.get(token)
        if session and session.expires > time.monotonic():
            role = self.store.registry.role(session.username)
            # Do not silently elevate or retain a session after a grant change.
            if role == session.role:
                return session
        self.sessions.pop(token, None)
        return None

    def require(self, token, roles=None):
        session = self.lookup(token)
        if not session:
            raise ApiError(401, "UNAUTHENTICATED", "로그인이 필요합니다.")
        if roles and session.role not in roles:
            raise ApiError(403, "FORBIDDEN", "이 작업의 권한이 없습니다.")
        return session
