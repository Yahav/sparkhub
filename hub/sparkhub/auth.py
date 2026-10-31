"""Dashboard login: username + password (argon2) + TOTP, recovery codes, sessions, lockout.

Sessions are random tokens in an HttpOnly, Secure, SameSite=Strict cookie; the database keeps only their hash.
A session starts at stage "password" (TOTP or enrolment still due) and becomes "full" after the second factor.
Five failed attempts (password or code) lock the account for 15 minutes, doubling on each further lock.
Unknown usernames are counted the same way, so the responses don't reveal which names exist.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass

import pyotp
import segno
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request, Response

from . import metrics
from .db import DB
from .keys import SCOPES, Principal

PH = PasswordHasher()
MAX_FAILS = 5
LOCK_BASE_S = 15 * 60
PRE_SESSION_S = 10 * 60          # time to finish the second factor
IDLE_S = 12 * 3600               # session idle timeout
ABSOLUTE_S = 7 * 86400           # session absolute lifetime
REMEMBER_S = 30 * 86400          # "remember this device": no idle timeout, password + TOTP again after 30 days
ISSUER = "SparkHub"
_DUMMY_HASH = PH.hash("not-a-real-password")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def create_user(db: DB, username: str, password: str, role: str = "admin", must_change: bool = False,
                actor: str = "cli") -> int:
    """role: admin, switch or infer. must_change: a temporary password, replaced at the first login."""
    if role not in SCOPES:
        raise ValueError(f"role must be one of {', '.join(SCOPES)}")
    username = username.strip()
    if not username or len(username) > 32 or not username.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ValueError("username: 1-32 letters, digits, . _ -")
    check_password_policy(password)
    if db.one("SELECT 1 FROM users WHERE username=?", (username,)):
        raise ValueError("user exists")
    cur = db.execute("INSERT INTO users(username, pw_hash, is_admin, role, must_change_pw, created_at)"
                     " VALUES(?, ?, ?, ?, ?, ?)",
                     (username, PH.hash(password), int(role == "admin"), role, int(must_change), time.time()))
    db.event("user_created", actor, username=username, role=role)
    return cur.lastrowid


def check_password_policy(pw: str) -> None:
    if len(pw) < 12:
        raise ValueError("password: at least 12 characters")
    if len(pw) > 256:
        raise ValueError("password: at most 256 characters")


def set_password(db: DB, username: str, password: str, actor: str, must_change: bool = False) -> bool:
    """Set a password and end the user's sessions. must_change: a temporary one (an admin's reset)."""
    check_password_policy(password)
    n = db.execute("UPDATE users SET pw_hash=?, failed_count=0, locked_until=NULL, must_change_pw=? WHERE username=?",
                   (PH.hash(password), int(must_change), username)).rowcount
    if n:
        end_sessions(db, username)
        db.event("password_reset", actor, username=username, temporary=must_change)
    return bool(n)


def replace_temporary_password(db: DB, user_id: int, password: str, session_id: str) -> None:
    """The user's own new password in place of a temporary one, during login (keeps this session)."""
    check_password_policy(password)
    row = db.one("SELECT username, pw_hash FROM users WHERE id=?", (user_id,))
    try:
        PH.verify(row["pw_hash"], password[:256])
        raise ValueError("choose a password different from the temporary one")
    except (VerificationError, InvalidHashError):
        pass
    db.execute("UPDATE users SET pw_hash=?, must_change_pw=0 WHERE id=?", (PH.hash(password), user_id))
    db.execute("DELETE FROM sessions WHERE user_id=? AND id!=?", (user_id, session_id))
    db.event("password_changed", f"user:{row['username']}", temporary_replaced=True)


def reset_totp(db: DB, username: str, actor: str) -> bool:
    n = db.execute("UPDATE users SET totp_secret=NULL, totp_enabled=0, totp_last_step=NULL, recovery=NULL"
                   " WHERE username=?", (username,)).rowcount
    if n:
        end_sessions(db, username)
        db.event("totp_reset", actor, username=username)
    return bool(n)


def end_sessions(db: DB, username: str) -> None:
    db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (username,))


def new_recovery_codes(db: DB, user_id: int) -> list[str]:
    codes = ["-".join(secrets.token_hex(3) for _ in range(2)) for _ in range(10)]
    db.execute("UPDATE users SET recovery=? WHERE id=?", (json.dumps([_sha(c) for c in codes]), user_id))
    return codes


def qr_data_uri(uri: str) -> str:
    # a standalone SVG (with xmlns): svg_inline() omits it, and browsers then render a data: image as nothing
    svg = segno.make(uri, error="m").svg_inline(scale=5, dark="#111", light="#fff")
    if "xmlns=" not in svg:
        svg = svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


@dataclass
class Session:
    id: str
    user_id: int
    username: str
    role: str                 # admin | switch | infer (the key scopes)
    stage: str
    csrf: str
    totp_enabled: bool
    remember: bool = False
    must_change_pw: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can(self, role: str) -> bool:
        return SCOPES.index(self.role) >= SCOPES.index(role)


class Auth:
    def __init__(self, db: DB, cookie_secure: bool):
        self.db = db
        self.cookie_secure = cookie_secure
        self.cookie = "__Host-sparkhub" if cookie_secure else "sparkhub_session"
        self.unknown_fails: dict[str, tuple[int, float]] = {}   # unknown username -> (fails, locked_until)
        self.ip_fails: dict[str, tuple[int, float]] = {}        # ip -> (fails, last)

    # -------------------------------------------------------------- sessions
    def _set_cookie(self, response: Response, token: str, max_age: int | None) -> None:
        response.set_cookie(self.cookie, token, httponly=True, secure=self.cookie_secure, samesite="strict",
                            path="/", max_age=max_age)

    def _new_session(self, response: Response, user_id: int, stage: str, request: Request,
                     remember: bool = False) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = now + (PRE_SESSION_S if stage != "full" else ABSOLUTE_S)
        self.db.execute("INSERT INTO sessions(id, user_id, stage, csrf, created_at, last_seen, expires_at, ip, ua,"
                        " remember) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (_sha(token), user_id, stage, secrets.token_urlsafe(24), now, now, expires,
                         client_ip(request), (request.headers.get("user-agent") or "")[:200], int(remember)))
        self._set_cookie(response, token, int(expires - now))
        return token

    def finish_login(self, request: Request, response: Response) -> None:
        """After the second factor: replace the short pre-login cookie with the session's real lifetime.
        Remembered devices keep a 30-day cookie; otherwise it is a browser-session cookie (gone when the
        browser closes), with the server-side limits of 12 h idle and 7 days in any case."""
        token = request.cookies.get(self.cookie)
        s = self.session(request)
        if not token or s is None:
            return
        self._set_cookie(response, token, REMEMBER_S if s.remember else None)

    def session(self, request: Request) -> Session | None:
        token = request.cookies.get(self.cookie)
        if not token or len(token) > 100:
            return None
        sid = _sha(token)
        row = self.db.one("SELECT s.*, u.username, u.role, u.totp_enabled, u.disabled, u.must_change_pw FROM sessions s"
                          " JOIN users u ON u.id=s.user_id WHERE s.id=?", (sid,))
        now = time.time()
        if row is None or row["disabled"]:
            return None
        idle_limit = REMEMBER_S if row["remember"] else IDLE_S
        if row["expires_at"] < now or (row["stage"] == "full" and now - row["last_seen"] > idle_limit):
            self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            return None
        if now - row["last_seen"] > 60:
            self.db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now, sid))
        return Session(sid, row["user_id"], row["username"], row["role"], row["stage"], row["csrf"],
                       bool(row["totp_enabled"]), bool(row["remember"]), bool(row["must_change_pw"]))

    def session_principal(self, request: Request) -> Principal | None:
        s = self.session(request)
        if s is None or s.stage != "full":
            return None
        return Principal("user", s.user_id, s.username, s.role, session_id=s.id, csrf=s.csrf, owner_id=s.user_id,
                         owner_name=s.username)

    def logout(self, request: Request, response: Response) -> None:
        s = self.session(request)
        if s:
            self.db.execute("DELETE FROM sessions WHERE id=?", (s.id,))
            self.db.event("logout", f"user:{s.username}")
        response.delete_cookie(self.cookie, path="/", secure=self.cookie_secure, httponly=True, samesite="strict")

    def upgrade(self, s: Session) -> None:
        life = REMEMBER_S if s.remember else ABSOLUTE_S
        self.db.execute("UPDATE sessions SET stage='full', expires_at=?, csrf=?, last_seen=? WHERE id=?",
                        (time.time() + life, secrets.token_urlsafe(24), time.time(), s.id))

    # -------------------------------------------------------------- lockout
    def ip_delay(self, ip: str | None) -> float:
        """Backoff per client IP: 0.5 s per recent failure, at most 5 s."""
        fails, last = self.ip_fails.get(ip or "", (0, 0.0))
        if time.time() - last > 900:
            return 0.0
        return min(5.0, 0.5 * fails)

    def _ip_fail(self, ip: str | None) -> None:
        fails, last = self.ip_fails.get(ip or "", (0, 0.0))
        if time.time() - last > 900:
            fails = 0
        self.ip_fails[ip or ""] = (fails + 1, time.time())
        if len(self.ip_fails) > 10000:
            self.ip_fails.clear()

    def locked_until(self, username: str) -> float | None:
        row = self.db.one("SELECT locked_until FROM users WHERE username=?", (username,))
        if row is None:
            fails, until = self.unknown_fails.get(username, (0, 0.0))
            return until if until > time.time() else None
        lu = row["locked_until"]
        return lu if lu and lu > time.time() else None

    def _fail(self, username: str, ip: str | None, reason: str) -> None:
        metrics.LOGIN_FAILURES.labels(reason).inc()
        self._ip_fail(ip)
        row = self.db.one("SELECT id, failed_count FROM users WHERE username=?", (username,))
        self.db.event("login_failed", f"user:{username}" if row else "unknown", reason=reason, ip=ip)
        if row is None:
            fails, _ = self.unknown_fails.get(username, (0, 0.0))
            fails += 1
            until = time.time() + LOCK_BASE_S if fails >= MAX_FAILS else 0.0
            if len(self.unknown_fails) > 10000:
                self.unknown_fails.clear()
            self.unknown_fails[username[:64]] = (fails if fails < MAX_FAILS else 0, until)
            return
        fails = row["failed_count"] + 1
        if fails >= MAX_FAILS:
            locks = self.db.one("SELECT COUNT(*) n FROM events WHERE kind='account_locked' AND actor=? AND ts>?",
                                (f"user:{username}", time.time() - 86400))["n"]
            until = time.time() + LOCK_BASE_S * (2 ** min(locks, 6))
            self.db.execute("UPDATE users SET failed_count=0, locked_until=? WHERE id=?", (until, row["id"]))
            self.db.event("account_locked", f"user:{username}", until=until, ip=ip)
            metrics.LOCKOUTS.inc()
        else:
            self.db.execute("UPDATE users SET failed_count=? WHERE id=?", (fails, row["id"]))

    # -------------------------------------------------------------- steps
    def check_password(self, username: str, password: str, ip: str | None) -> tuple[int | None, str | None]:
        """(user_id, None) on success, (None, message) otherwise."""
        username = username.strip()[:64]
        lu = self.locked_until(username)
        if lu:
            return None, f"Too many failed attempts. Try again after {time.strftime('%H:%M', time.localtime(lu))}."
        row = self.db.one("SELECT id, pw_hash, disabled FROM users WHERE username=?", (username,))
        try:
            PH.verify(row["pw_hash"] if row else _DUMMY_HASH, password[:256])
            ok = row is not None and not row["disabled"]
        except (VerificationError, InvalidHashError):
            ok = False
        if not ok:
            self._fail(username, ip, "password")
            lu = self.locked_until(username)
            if lu:
                return None, f"Too many failed attempts. Try again after {time.strftime('%H:%M', time.localtime(lu))}."
            return None, "Wrong username or password."
        if PH.check_needs_rehash(row["pw_hash"]):
            self.db.execute("UPDATE users SET pw_hash=? WHERE id=?", (PH.hash(password), row["id"]))
        return row["id"], None

    def start_session(self, response: Response, user_id: int, request: Request, remember: bool = False) -> None:
        self._new_session(response, user_id, "password", request, remember)

    def verify_second_factor(self, s: Session, code: str, ip: str | None) -> str | None:
        """None on success, else an error message. Accepts a TOTP code or a recovery code (used once)."""
        lu = self.locked_until(s.username)
        if lu:
            return f"Too many failed attempts. Try again after {time.strftime('%H:%M', time.localtime(lu))}."
        row = self.db.one("SELECT * FROM users WHERE id=?", (s.user_id,))
        code = code.strip().replace(" ", "").lower()
        ok = False
        how = "totp"
        if row["totp_secret"] and code.isdigit() and len(code) == 6:
            totp = pyotp.TOTP(row["totp_secret"])
            now = time.time()
            for drift in (0, -1, 1):
                step = int(now // 30) + drift
                if secrets.compare_digest(totp.at(step * 30), code):
                    if row["totp_last_step"] is not None and step <= row["totp_last_step"]:
                        break          # replay of a used code
                    self.db.execute("UPDATE users SET totp_last_step=? WHERE id=?", (step, s.user_id))
                    ok = True
                    break
        elif row["recovery"] and len(code) == 13 and code[6] == "-":
            hashes = json.loads(row["recovery"])
            h = _sha(code)
            if h in hashes:
                hashes.remove(h)
                self.db.execute("UPDATE users SET recovery=? WHERE id=?", (json.dumps(hashes), s.user_id))
                ok = True
                how = "recovery_code"
        if not ok:
            self._fail(s.username, ip, "totp")
            if self.locked_until(s.username):
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (s.user_id,))
                return "Too many failed attempts; the account is locked."
            return "Wrong code."
        self.db.execute("UPDATE users SET failed_count=0, last_login_at=? WHERE id=?", (time.time(), s.user_id))
        self.db.event("login", f"user:{s.username}", ip=ip, second_factor=how)
        self.upgrade(s)
        return None

    def confirm_totp(self, s: Session, code: str, ip: str | None, purpose: str) -> str | None:
        """A fresh authenticator code for a sensitive action (approving a Custom recipe): None if valid.
        Only TOTP (no recovery codes), not a code already used, and failures count toward the lockout."""
        lu = self.locked_until(s.username)
        if lu:
            return f"Too many failed attempts. Try again after {time.strftime('%H:%M', time.localtime(lu))}."
        row = self.db.one("SELECT totp_secret, totp_enabled, totp_last_step FROM users WHERE id=?", (s.user_id,))
        if not row or not row["totp_enabled"] or not row["totp_secret"]:
            return "Set up two-factor authentication first."
        code = code.strip().replace(" ", "")
        if code.isdigit() and len(code) == 6:
            totp = pyotp.TOTP(row["totp_secret"])
            for drift in (0, -1, 1):
                step = int(time.time() // 30) + drift
                if secrets.compare_digest(totp.at(step * 30), code):
                    if row["totp_last_step"] is not None and step <= row["totp_last_step"]:
                        return "That code was already used; wait for the next one."
                    self.db.execute("UPDATE users SET totp_last_step=?, failed_count=0 WHERE id=?", (step, s.user_id))
                    return None
        self._fail(s.username, ip, "totp")
        self.db.event("totp_confirm_failed", f"user:{s.username}", ip=ip, purpose=purpose)
        if self.locked_until(s.username):
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (s.user_id,))
            return "Too many failed attempts; the account is locked."
        return "Wrong code."

    def begin_enrolment(self, s: Session) -> tuple[str, str]:
        """New TOTP secret (not active until confirmed). Returns (secret, otpauth uri)."""
        row = self.db.one("SELECT totp_secret, totp_enabled FROM users WHERE id=?", (s.user_id,))
        if row["totp_enabled"]:
            raise ValueError("TOTP already enrolled")
        secret = row["totp_secret"] or pyotp.random_base32()
        if not row["totp_secret"]:
            self.db.execute("UPDATE users SET totp_secret=? WHERE id=?", (secret, s.user_id))
        uri = pyotp.TOTP(secret).provisioning_uri(name=s.username, issuer_name=ISSUER)
        return secret, uri

    def confirm_enrolment(self, s: Session, code: str, ip: str | None) -> tuple[list[str] | None, str | None]:
        row = self.db.one("SELECT totp_secret, totp_enabled FROM users WHERE id=?", (s.user_id,))
        if row["totp_enabled"] or not row["totp_secret"]:
            return None, "nothing to confirm"
        code = code.strip().replace(" ", "")
        if not pyotp.TOTP(row["totp_secret"]).verify(code, valid_window=1):
            self._fail(s.username, ip, "totp_enrol")
            return None, "Wrong code. Check the phone's clock and try again."
        self.db.execute("UPDATE users SET totp_enabled=1, totp_last_step=?, failed_count=0, last_login_at=?"
                        " WHERE id=?", (int(time.time() // 30), time.time(), s.user_id))
        codes = new_recovery_codes(self.db, s.user_id)
        self.db.event("totp_enrolled", f"user:{s.username}", ip=ip)
        self.db.event("login", f"user:{s.username}", ip=ip, second_factor="enrolment")
        self.upgrade(s)
        return codes, None


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None
